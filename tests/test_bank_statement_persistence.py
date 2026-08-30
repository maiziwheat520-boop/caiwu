from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from test_r1_database_migration import (
    _append_audit_event,
    _assert_db_rejection,
    _legacy_r1_database,
    _temporarily_runtime_membership,
    _upgrade_config,
)

from alembic import command
from ledgerbridge.bank_statement_persistence import (
    BankStatementImportContext,
    BankStatementImportService,
    BankStatementPersistenceError,
    BankStatementReadService,
    _build_request,
)
from ledgerbridge.internal_read_contract import (
    AuthorizationDenied,
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.mybank_statement import MyBankStatement, MyBankTransaction
from ledgerbridge.reconciliation import AccountOwnerKind
from scripts.backup_restore import (
    BANK_STATEMENT_SECURITY_SQL,
    BackupError,
    _validate_bank_statement_security,
)

STATEMENT_REF = UUID("82000000-0000-4000-8000-000000000001")
ACCOUNT_REF = UUID("82000000-0000-4000-8000-000000000002")
EVIDENCE_REF = UUID("82000000-0000-4000-8000-000000000003")
ENTITY_REF = UUID("82000000-0000-4000-8000-000000000004")
OTHER_ENTITY_REF = UUID("82000000-0000-4000-8000-000000000005")


def _principal(
    *,
    entity_ref: UUID = ENTITY_REF,
    capabilities: frozenset[Capability] = frozenset({Capability.LEDGER_READ}),
) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:statement-reader",
        san_uri="spiffe://ledgerbridge.test/statement-reader",
        policy_generation=1,
        capabilities=capabilities,
        grants=(EntityGrant(entity_ref=entity_ref, allow_unassigned_candidates=True),),
    )


def _statement() -> MyBankStatement:
    return MyBankStatement(
        statement_ref=STATEMENT_REF,
        source_sha256="a" * 64,
        source_size=4_096,
        declared_media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        currency="CNY",
        institution_code="mybank",
        account_suffix="7968",
        worksheet_index=1,
        header_row_number=8,
        transactions=(
            MyBankTransaction(
                source_event_ref=UUID("82000000-0000-4000-8000-000000000011"),
                source_row_number=9,
                source_row_sha256="1" * 64,
                occurred_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
                amount_minor=12_534,
                balance_minor=512_534,
                counterparty_name="Synthetic merchant A",
                counterparty_account="0000000000005678",
                counterparty_institution="Synthetic bank",
                transaction_serial="SYNTHETIC-0001",
                transaction_name="transfer",
            ),
            MyBankTransaction(
                source_event_ref=UUID("82000000-0000-4000-8000-000000000012"),
                source_row_number=10,
                source_row_sha256="2" * 64,
                occurred_at=datetime(2026, 1, 3, 6, 7, 8, tzinfo=UTC),
                amount_minor=-2_000,
                balance_minor=510_534,
                counterparty_name="Synthetic merchant B",
                counterparty_account="0000000000009012",
                counterparty_institution="Synthetic bank",
                transaction_serial="SYNTHETIC-0002",
                transaction_name="purchase",
            ),
        ),
    )


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> dict[str, object]:
        return cast(dict[str, object], self._value)

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, object]:
        return cast(dict[str, object], self._value)

    def all(self) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self._value)


class _StatementDatabase:
    def __init__(self) -> None:
        self.imported: set[str] = set()
        self.requests: list[dict[str, Any]] = []
        self.commits = 0

    def __enter__(self) -> _StatementDatabase:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, Any]) -> _Result:
        sql = str(statement)
        if "internal_import.import_bank_statement" in sql:
            request = json.loads(params["request"])
            self.requests.append(request)
            statement_ref = request["statement_ref"]
            created = statement_ref not in self.imported
            self.imported.add(statement_ref)
            return _Result(
                {
                    "statement_ref": statement_ref,
                    "managed_account_ref": request["managed_account_ref"],
                    "created": created,
                    "transaction_count": len(request["transactions"]),
                    "review_status": "PENDING",
                    "statement_review_count": 1,
                    "accounting_candidate_count": 0,
                }
            )
        if "internal_read.get_bank_statement_summary" in sql:
            statement_ref = str(params["statement_ref"])
            return _Result(
                {
                    "statement_ref": statement_ref,
                    "managed_account_ref": str(ACCOUNT_REF),
                    "evidence_ref": str(EVIDENCE_REF),
                    "period_start": datetime(2026, 1, 2, tzinfo=UTC).date(),
                    "period_end": datetime(2026, 1, 3, tzinfo=UTC).date(),
                    "transaction_count": 2,
                    "review_status": "PENDING",
                    "review_revision": 1,
                }
            )
        if "internal_read.list_bank_statement_transactions" in sql:
            rows = [
                {
                    "source_row_number": 9,
                    "occurred_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
                    "amount_minor": 12_534,
                    "balance_minor": 512_534,
                    "currency": "CNY",
                    "counterparty_ref": "cp_synthetic_a",
                    "counterparty_name": "Synthetic merchant A",
                    "counterparty_account_masked": "************5678",
                    "counterparty_institution": "Synthetic bank",
                    "transaction_serial": "SYNTHETIC-0001",
                    "transaction_name": "transfer",
                },
                {
                    "source_row_number": 10,
                    "occurred_at": datetime(2026, 1, 3, 6, 7, 8, tzinfo=UTC),
                    "amount_minor": -2_000,
                    "balance_minor": 510_534,
                    "currency": "CNY",
                    "counterparty_ref": "cp_synthetic_b",
                    "counterparty_name": "Synthetic merchant B",
                    "counterparty_account_masked": "************9012",
                    "counterparty_institution": "Synthetic bank",
                    "transaction_serial": "SYNTHETIC-0002",
                    "transaction_name": "purchase",
                },
            ]
            after_row = cast(int, params["after_row"])
            limit = cast(int, params["limit"])
            return _Result(
                [row for row in rows if cast(int, row["source_row_number"]) > after_row][:limit]
            )
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self) -> None:
        self.commits += 1


def _context() -> BankStatementImportContext:
    return BankStatementImportContext(
        entity_ref=ENTITY_REF,
        managed_account_ref=ACCOUNT_REF,
        account_key="mybank:personal:7968",
        owner_ref="owner:synthetic",
        owner_kind=AccountOwnerKind.PERSONAL,
        account_kind="BANK_CHECKING",
        evidence_ref=EVIDENCE_REF,
        actor="worker:controlled-import",
        reason="synthetic statement import test",
    )


def _overlapping_statement() -> MyBankStatement:
    original = _statement()
    return replace(
        original,
        statement_ref=UUID("82000000-0000-4000-8000-000000000101"),
        source_sha256="b" * 64,
        source_size=4_097,
        transactions=(
            replace(
                original.transactions[0],
                source_event_ref=UUID("82000000-0000-4000-8000-000000000111"),
                source_row_number=11,
                source_row_sha256="3" * 64,
            ),
            MyBankTransaction(
                source_event_ref=UUID("82000000-0000-4000-8000-000000000112"),
                source_row_number=12,
                source_row_sha256="4" * 64,
                occurred_at=datetime(2026, 1, 4, 9, 10, 11, tzinfo=UTC),
                amount_minor=-3_000,
                balance_minor=507_534,
                counterparty_name="Synthetic merchant C",
                counterparty_account="0000000000003456",
                counterparty_institution="Synthetic bank",
                transaction_serial="SYNTHETIC-0003",
                transaction_name="purchase",
            ),
        ),
    )


def _seed_statement_evidence(
    connection: Any,
    *,
    entity_ref: UUID,
    business_unit_ref: UUID,
    evidence_ref: UUID,
    statement: MyBankStatement,
) -> None:
    connection.execute(
        text(
            "INSERT INTO public.entity (id, entity_type, name) "
            "VALUES (:entity, 'COMPANY', 'Statement integration entity') "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"entity": entity_ref},
    )
    connection.execute(
        text(
            "INSERT INTO public.business_unit (id, entity_id, ref, label) "
            "VALUES (:unit, :entity, 'statement-unit', 'Statement Unit') "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {"unit": business_unit_ref, "entity": entity_ref},
    )
    audit = _append_audit_event(
        connection,
        "evidence.object.create",
        {
            "evidence_ref": str(evidence_ref),
            "entity_id": str(entity_ref),
            "business_unit_id": str(business_unit_ref),
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.evidence_object "
            "(evidence_ref, entity_id, business_unit_id, media_type, display_name, "
            "plaintext_sha256, plaintext_size, audit_event_id) VALUES "
            "(:evidence, :entity, :unit, :media_type, :display_name, "
            ":digest, :size, :audit)"
        ),
        {
            "evidence": evidence_ref,
            "entity": entity_ref,
            "unit": business_unit_ref,
            "media_type": statement.declared_media_type,
            "display_name": f"statement-{statement.statement_ref}.xlsx",
            "digest": bytes.fromhex(statement.source_sha256),
            "size": statement.source_size,
            "audit": audit,
        },
    )


def test_import_statement_is_idempotent_and_requires_one_statement_review() -> None:
    database = _StatementDatabase()
    service = BankStatementImportService(lambda: cast(Session, database))
    first = service.import_statement(_statement(), context=_context())
    replay = service.import_statement(_statement(), context=_context())
    assert first.created is True
    assert replay.created is False
    assert first.statement_ref == replay.statement_ref == STATEMENT_REF
    assert first.transaction_count == replay.transaction_count == 2
    assert first.review_status == replay.review_status == "PENDING"
    assert first.statement_review_count == replay.statement_review_count == 1
    assert first.accounting_candidate_count == replay.accounting_candidate_count == 0
    assert database.commits == 2
    request = database.requests[0]
    assert request["period_start"] == "2026-01-02"
    assert request["period_end"] == "2026-01-03"
    assert len(request["transactions"]) == 2
    assert request["transaction_set_sha256"] == (
        "2750b195eb9b8c950453887d6006751fe370aae2e9ce1564ae70507db2d430d7"
    )
    assert "candidates" not in request
    assert all(item["counterparty_ref"].startswith("cp_") for item in request["transactions"])


def test_statement_review_is_not_exposed_without_verified_user_assertion_route() -> None:
    service = BankStatementImportService(lambda: cast(Session, _StatementDatabase()))
    assert not hasattr(service, "review_statement")


def test_transaction_set_digest_is_canonical_by_source_row_number() -> None:
    database = _StatementDatabase()
    service = BankStatementImportService(lambda: cast(Session, database))
    original = _statement()
    reversed_rows = replace(original, transactions=tuple(reversed(original.transactions)))
    service.import_statement(original, context=_context())
    service.import_statement(reversed_rows, context=_context())
    assert (
        database.requests[0]["transaction_set_sha256"]
        == database.requests[1]["transaction_set_sha256"]
    )
    assert [item["source_row_number"] for item in database.requests[1]["transactions"]] == [
        9,
        10,
    ]


def test_statement_period_uses_china_local_dates_across_utc_midnight() -> None:
    database = _StatementDatabase()
    service = BankStatementImportService(lambda: cast(Session, database))
    original = _statement()
    crossing_midnight = replace(
        original,
        transactions=(
            replace(
                original.transactions[0],
                occurred_at=datetime(2026, 1, 1, 15, 30, tzinfo=UTC),
            ),
            replace(
                original.transactions[1],
                occurred_at=datetime(2026, 1, 1, 16, 30, tzinfo=UTC),
            ),
        ),
    )
    service.import_statement(crossing_midnight, context=_context())
    request = database.requests[0]
    assert request["period_start"] == "2026-01-01"
    assert request["period_end"] == "2026-01-02"


def test_statement_summary_exposes_one_review_without_transaction_details() -> None:
    database = _StatementDatabase()
    BankStatementImportService(lambda: cast(Session, database)).import_statement(
        _statement(), context=_context()
    )
    summary = BankStatementReadService(lambda: cast(Session, database)).get_statement_summary(
        STATEMENT_REF,
        principal=_principal(),
        entity_ref=ENTITY_REF,
        audit_horizon_sequence=42,
        audit_horizon_hash=b"h" * 32,
    )
    assert summary.statement_ref == STATEMENT_REF
    assert summary.transaction_count == 2
    assert summary.review_status == "PENDING"
    assert summary.review_revision == 1
    assert not hasattr(summary, "transactions")
    assert not hasattr(summary, "amount_minor")


def test_statement_transactions_are_entity_scoped_paginated_and_account_masked() -> None:
    database = _StatementDatabase()
    rows = BankStatementReadService(lambda: cast(Session, database)).list_statement_transactions(
        STATEMENT_REF,
        principal=_principal(),
        entity_ref=ENTITY_REF,
        audit_horizon_sequence=42,
        audit_horizon_hash=b"h" * 32,
        after_row=9,
        limit=1,
    )
    assert len(rows) == 1
    assert rows[0].source_row_number == 10
    assert rows[0].counterparty_account_masked == "************9012"
    assert "0000000000009012" not in repr(rows[0])


def test_statement_read_requires_capability_and_matching_entity_grant() -> None:
    service = BankStatementReadService(lambda: cast(Session, _StatementDatabase()))
    with pytest.raises(AuthorizationDenied):
        service.get_statement_summary(
            STATEMENT_REF,
            principal=_principal(capabilities=frozenset()),
            entity_ref=ENTITY_REF,
            audit_horizon_sequence=42,
            audit_horizon_hash=b"h" * 32,
        )
    with pytest.raises(ResourceNotVisible):
        service.list_statement_transactions(
            STATEMENT_REF,
            principal=_principal(entity_ref=OTHER_ENTITY_REF),
            entity_ref=ENTITY_REF,
            audit_horizon_sequence=42,
            audit_horizon_hash=b"h" * 32,
            after_row=0,
            limit=10,
        )


def test_0021_postgresql_replay_overlap_conflict_scope_acl_and_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEDGERBRIDGE_ENV", raising=False)
    with _legacy_r1_database(reader=True) as database_url:
        config = _upgrade_config(database_url)
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        unit_ref = uuid4()
        second_evidence = uuid4()
        conflict_evidence = uuid4()
        first = _statement()
        overlap = _overlapping_statement()
        conflict = replace(
            overlap,
            statement_ref=uuid4(),
            source_sha256="c" * 64,
            source_size=4_098,
            transactions=(
                replace(
                    overlap.transactions[0],
                    source_event_ref=uuid4(),
                    source_row_number=13,
                    source_row_sha256="5" * 64,
                    amount_minor=overlap.transactions[0].amount_minor + 1,
                ),
            ),
        )
        with engine.begin() as connection:
            _seed_statement_evidence(
                connection,
                entity_ref=ENTITY_REF,
                business_unit_ref=unit_ref,
                evidence_ref=EVIDENCE_REF,
                statement=first,
            )
            _seed_statement_evidence(
                connection,
                entity_ref=ENTITY_REF,
                business_unit_ref=unit_ref,
                evidence_ref=second_evidence,
                statement=overlap,
            )
            _seed_statement_evidence(
                connection,
                entity_ref=ENTITY_REF,
                business_unit_ref=unit_ref,
                evidence_ref=conflict_evidence,
                statement=conflict,
            )

        def worker_session() -> Session:
            session = Session(engine)
            session.execute(text("SET LOCAL ROLE ledgerbridge_worker"))
            return session

        with _temporarily_runtime_membership(database_url, "ledgerbridge_worker"):
            service = BankStatementImportService(worker_session)
            first_result = service.import_statement(first, context=_context())
            replay_result = service.import_statement(first, context=_context())
            overlap_result = service.import_statement(
                overlap,
                context=replace(_context(), evidence_ref=second_evidence),
            )
            with pytest.raises(BankStatementPersistenceError, match="import failed") as raised:
                service.import_statement(
                    conflict,
                    context=replace(_context(), evidence_ref=conflict_evidence),
                )
        assert first_result.created is True
        assert replay_result.created is False
        assert overlap_result.created is True
        assert isinstance(raised.value.__cause__, SQLAlchemyError)
        database_error = raised.value.__cause__
        assert "overlapping bank statement transaction conflicts with fact" in str(
            getattr(database_error, "orig", database_error)
        )

        with engine.begin() as connection:
            counts = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM public.bank_statement) AS statements, "
                    "(SELECT count(*) FROM public.bank_statement_transaction) AS facts, "
                    "(SELECT count(*) FROM public.bank_statement_observation) AS observations, "
                    "(SELECT count(*) FROM public.bank_statement_review) AS reviews, "
                    "(SELECT count(*) FROM public.candidate) AS candidates"
                )
            ).one()
            assert tuple(counts) == (2, 3, 4, 2, 0)
            horizon = connection.execute(
                text("SELECT sequence, hash FROM public.audit_event ORDER BY sequence DESC LIMIT 1")
            ).one()
            assert connection.execute(
                text(
                    "SELECT has_function_privilege("
                    "'ledgerbridge_reader', "
                    "'internal_read.list_bank_statement_transactions("
                    "uuid,uuid,bigint,bytea,integer,integer)', 'EXECUTE')"
                )
            ).scalar_one()
            assert not connection.execute(
                text(
                    "SELECT has_table_privilege("
                    "'ledgerbridge_reader', 'public.bank_statement_observation', 'SELECT')"
                )
            ).scalar_one()
            assert connection.execute(
                text(
                    "SELECT has_function_privilege("
                    "'ledgerbridge_worker', "
                    "'internal_import.import_bank_statement(jsonb)', 'EXECUTE')"
                )
            ).scalar_one()
            for table_name in (
                "managed_account",
                "managed_account_lifecycle",
                "bank_statement",
                "bank_statement_transaction",
                "bank_statement_observation",
                "bank_statement_review",
            ):
                assert not connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "'ledgerbridge_worker', :table_name, "
                        "'SELECT,INSERT,UPDATE,DELETE')"
                    ),
                    {"table_name": f"public.{table_name}"},
                ).scalar_one()
            assert not connection.execute(
                text(
                    "SELECT has_function_privilege("
                    "'ledgerbridge_api', "
                    "'internal_command.review_bank_statement("
                    "uuid,uuid,uuid,text,text,integer,text,text)', 'EXECUTE')"
                )
            ).scalar_one()

            extra_source_event = uuid4()
            extra_row_sha256 = b"x" * 32
            extra_transaction_ref = connection.execute(
                text(
                    "SELECT transaction_ref FROM public.bank_statement_transaction "
                    "WHERE managed_account_ref = :account "
                    "AND transaction_serial = 'SYNTHETIC-0003'"
                ),
                {"account": ACCOUNT_REF},
            ).scalar_one()
            extra_payload = {
                "source_event_ref": str(extra_source_event),
                "statement_ref": str(STATEMENT_REF),
                "managed_account_ref": str(ACCOUNT_REF),
                "transaction_ref": str(extra_transaction_ref),
                "source_row_number": 99,
                "source_row_sha256": extra_row_sha256.hex(),
            }
            extra_audit = connection.execute(
                text(
                    "SELECT public.append_audit_event("
                    "'r1-test', 'bank_statement.observation.import', "
                    "'extra observation rejection', "
                    "'ledgerbridge.bank-statement.v1', CAST(:payload AS jsonb))"
                ),
                {"payload": json.dumps(extra_payload, separators=(",", ":"))},
            ).scalar_one()
            extra_audit_time = connection.execute(
                text("SELECT occurred_at FROM public.audit_event WHERE id = :audit"),
                {"audit": extra_audit},
            ).scalar_one()
            orphan_transaction_ref = uuid4()
            orphan_fact_sha256 = connection.execute(
                text(
                    "SELECT public.r1_bank_statement_transaction_digest("
                    ":account, CAST('2026-01-05T00:00:00+00:00' AS timestamptz), "
                    "100, 507634, 'CNY', 'cp_orphan', NULL, NULL, NULL, "
                    "'SYNTHETIC-ORPHAN', 'transfer')"
                ),
                {"account": ACCOUNT_REF},
            ).scalar_one()
            orphan_audit = connection.execute(
                text(
                    "SELECT public.append_audit_event("
                    "'r1-test', 'bank_statement.transaction.import', "
                    "'orphan fact rejection', 'ledgerbridge.bank-statement.v1', "
                    "jsonb_build_object('transaction_ref', :transaction, "
                    "'managed_account_ref', :account, 'fact_sha256', "
                    "CAST(:fact_hex AS text)))"
                ),
                {
                    "transaction": orphan_transaction_ref,
                    "account": ACCOUNT_REF,
                    "fact_hex": orphan_fact_sha256.hex(),
                },
            ).scalar_one()
            orphan_audit_time = connection.execute(
                text("SELECT occurred_at FROM public.audit_event WHERE id = :audit"),
                {"audit": orphan_audit},
            ).scalar_one()

        _assert_db_rejection(
            engine,
            [
                (
                    "INSERT INTO public.bank_statement_observation("
                    "source_event_ref, statement_ref, managed_account_ref, "
                    "transaction_ref, source_row_number, source_row_sha256, "
                    "audit_event_id, created_at) VALUES ("
                    ":source_event, :statement, :account, :transaction, 99, "
                    ":row_sha, :audit, :audit_time)",
                    {
                        "source_event": extra_source_event,
                        "statement": STATEMENT_REF,
                        "account": ACCOUNT_REF,
                        "transaction": extra_transaction_ref,
                        "row_sha": extra_row_sha256,
                        "audit": extra_audit,
                        "audit_time": extra_audit_time,
                    },
                )
            ],
            sqlstate="23000",
            message="transaction set is incomplete",
        )
        _assert_db_rejection(
            engine,
            [
                (
                    "INSERT INTO public.bank_statement_transaction("
                    "transaction_ref, managed_account_ref, occurred_at, amount_minor, "
                    "balance_minor, currency, counterparty_ref, transaction_serial, "
                    "transaction_name, fact_sha256, audit_event_id, created_at) VALUES ("
                    ":transaction, :account, '2026-01-05T00:00:00+00:00', 100, "
                    "507634, 'CNY', 'cp_orphan', 'SYNTHETIC-ORPHAN', 'transfer', "
                    ":fact_sha, :audit, :audit_time)",
                    {
                        "transaction": orphan_transaction_ref,
                        "account": ACCOUNT_REF,
                        "fact_sha": orphan_fact_sha256,
                        "audit": orphan_audit,
                        "audit_time": orphan_audit_time,
                    },
                )
            ],
            sqlstate="23000",
            message="requires source observation",
        )
        _assert_db_rejection(
            engine,
            [
                (
                    "UPDATE public.bank_statement_transaction "
                    "SET transaction_name = 'tampered' "
                    "WHERE transaction_serial = 'SYNTHETIC-0001'",
                    None,
                )
            ],
            sqlstate="23000",
            message="append-only",
        )
        _assert_db_rejection(
            engine,
            [
                (
                    "DELETE FROM public.bank_statement_observation "
                    "WHERE statement_ref = :statement",
                    {"statement": STATEMENT_REF},
                )
            ],
            sqlstate="23000",
            message="append-only",
        )

        with (
            _temporarily_runtime_membership(database_url, "ledgerbridge_reader"),
            engine.connect() as connection,
        ):
            connection.execute(text("SET ROLE ledgerbridge_reader"))
            wrong_scope = connection.execute(
                text(
                    "SELECT * FROM internal_read.list_bank_statement_transactions("
                    ":statement, :entity, :sequence, :hash, 0, 200)"
                ),
                {
                    "statement": STATEMENT_REF,
                    "entity": uuid4(),
                    "sequence": horizon.sequence,
                    "hash": horizon.hash,
                },
            ).all()
            assert wrong_scope == []
            visible = connection.execute(
                text(
                    "SELECT * FROM internal_read.list_bank_statement_transactions("
                    ":statement, :entity, :sequence, :hash, 0, 200)"
                ),
                {
                    "statement": STATEMENT_REF,
                    "entity": ENTITY_REF,
                    "sequence": horizon.sequence,
                    "hash": horizon.hash,
                },
            ).all()
            assert len(visible) == 2
            assert visible[0].counterparty_account_masked == "************5678"
            assert "0000000000005678" not in repr(visible)

        engine.dispose()
        with pytest.raises(RuntimeError, match="discard bank statement facts"):
            command.downgrade(config, "20260830_0020")


def test_0021_concurrent_first_account_imports_serialize_on_account_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEDGERBRIDGE_ENV", raising=False)
    with _legacy_r1_database(reader=True) as database_url:
        command.upgrade(_upgrade_config(database_url), "head")
        engine = create_engine(database_url)
        first = _statement()
        overlap = _overlapping_statement()
        unit_ref = uuid4()
        second_evidence = uuid4()
        with engine.begin() as connection:
            _seed_statement_evidence(
                connection,
                entity_ref=ENTITY_REF,
                business_unit_ref=unit_ref,
                evidence_ref=EVIDENCE_REF,
                statement=first,
            )
            _seed_statement_evidence(
                connection,
                entity_ref=ENTITY_REF,
                business_unit_ref=unit_ref,
                evidence_ref=second_evidence,
                statement=overlap,
            )
        barrier = Barrier(2)

        def import_one(statement: MyBankStatement, evidence_ref: UUID) -> bool:
            barrier.wait(timeout=10)
            result = BankStatementImportService(lambda: Session(engine)).import_statement(
                statement,
                context=replace(_context(), evidence_ref=evidence_ref),
            )
            return result.created

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(import_one, first, EVIDENCE_REF),
                executor.submit(import_one, overlap, second_evidence),
            )
            assert [future.result(timeout=30) for future in futures] == [True, True]
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM public.managed_account")).scalar_one()
                == 1
            )
            assert (
                connection.execute(text("SELECT count(*) FROM public.bank_statement")).scalar_one()
                == 2
            )
        engine.dispose()


def test_0021_postgresql_rejects_malformed_json_before_casting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEDGERBRIDGE_ENV", raising=False)
    with _legacy_r1_database(reader=True) as database_url:
        command.upgrade(_upgrade_config(database_url), "head")
        engine = create_engine(database_url)
        valid = _build_request(_statement(), _context())
        valid_transactions = cast(list[dict[str, object]], valid["transactions"])

        def request_with(**changes: object) -> dict[str, object]:
            return {**valid, **changes}

        def transaction_with(**changes: object) -> dict[str, object]:
            transactions = [dict(item) for item in valid_transactions]
            transactions[0].update(changes)
            return request_with(transactions=transactions)

        missing_transactions = dict(valid)
        missing_transactions.pop("transactions")
        missing_institution = dict(valid)
        missing_institution.pop("institution_code")
        malformed: tuple[tuple[str, object, str], ...] = (
            ("non-object request", "scalar", "bank statement request is invalid"),
            ("missing transactions", missing_transactions, "bank statement request is invalid"),
            ("missing institution", missing_institution, "bank statement request is invalid"),
            (
                "invalid statement uuid",
                request_with(statement_ref="not-a-uuid"),
                "bank statement request is invalid",
            ),
            (
                "invalid source size",
                request_with(source_size="not-an-integer"),
                "bank statement request is invalid",
            ),
            (
                "invalid period",
                request_with(period_start="not-a-date"),
                "bank statement request is invalid",
            ),
            (
                "non-object transaction",
                request_with(transactions=[42, dict(valid_transactions[1])]),
                "bank statement transaction is invalid",
            ),
            (
                "invalid source event uuid",
                transaction_with(source_event_ref="not-a-uuid"),
                "bank statement transaction is invalid",
            ),
            (
                "invalid source row number",
                transaction_with(source_row_number="not-an-integer"),
                "bank statement transaction is invalid",
            ),
            (
                "invalid occurrence time",
                transaction_with(occurred_at="not-a-timestamp"),
                "bank statement transaction is invalid",
            ),
            (
                "invalid amount",
                transaction_with(amount_minor="not-an-integer"),
                "bank statement transaction is invalid",
            ),
            (
                "invalid balance",
                transaction_with(balance_minor="not-an-integer"),
                "bank statement transaction is invalid",
            ),
        )
        for label, request, message in malformed:
            with pytest.raises(SQLAlchemyError) as raised, engine.begin() as connection:
                connection.execute(
                    text("SELECT internal_import.import_bank_statement(CAST(:request AS jsonb))"),
                    {"request": json.dumps(request, separators=(",", ":"))},
                )
            database_error = getattr(raised.value, "orig", raised.value)
            assert getattr(database_error, "sqlstate", None) == "22023", label
            assert message in str(database_error), label
        engine.dispose()


def test_0021_postgresql_schema_contract_rejects_missing_unique_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEDGERBRIDGE_ENV", raising=False)
    with _legacy_r1_database(reader=True) as database_url:
        command.upgrade(_upgrade_config(database_url), "head")
        engine = create_engine(database_url)
        with engine.connect() as connection, connection.begin():
            owner = connection.execute(text("SELECT current_user")).scalar_one()
            observed = json.loads(
                connection.execute(text(BANK_STATEMENT_SECURITY_SQL)).scalar_one()
            )
            observed_roles = sorted(
                {row["role"] for row in observed["bank_statement_effective_table_privileges"]}
            )
            metadata = {
                "database_owner": owner,
                "r1_role_matrix": [{"role": role} for role in observed_roles],
                **observed,
            }
            _validate_bank_statement_security(metadata)

            connection.execute(
                text(
                    "ALTER TABLE public.bank_statement_review "
                    "DROP CONSTRAINT bank_statement_review_operation_id_key"
                )
            )
            drifted = json.loads(connection.execute(text(BANK_STATEMENT_SECURITY_SQL)).scalar_one())
            with pytest.raises(BackupError, match="constraints differ"):
                _validate_bank_statement_security(
                    {
                        "database_owner": owner,
                        "r1_role_matrix": [{"role": role} for role in observed_roles],
                        **drifted,
                    }
                )
        engine.dispose()


def test_import_rejects_account_key_that_does_not_match_verified_statement() -> None:
    service = BankStatementImportService(lambda: cast(Session, _StatementDatabase()))
    with pytest.raises(Exception, match="identity"):
        service.import_statement(
            _statement(),
            context=replace(_context(), account_key="mybank:company:7968"),
        )


def test_0021_migration_is_append_only_audit_bound_and_candidate_free() -> None:
    path = (
        Path(__file__).parents[1] / "alembic" / "versions" / "20260830_0021_bank_statement_facts.py"
    )
    source = path.read_text(encoding="utf-8")
    for table in (
        "managed_account",
        "managed_account_lifecycle",
        "bank_statement",
        "bank_statement_transaction",
        "bank_statement_observation",
        "bank_statement_review",
    ):
        assert f'"{table}"' in source
    assert "r1_bank_statement_append_only" in source
    assert "r1_validate_bank_statement" in source
    assert "r1_require_transaction_observation" in source
    assert "internal_import.import_bank_statement" in source
    assert "internal_command.review_bank_statement" in source
    assert "REFERENCES public.evidence_object(evidence_ref)" in source
    assert "status IN ('PENDING','CONFIRMED','REJECTED')" in source
    assert "UNIQUE (managed_account_ref, transaction_serial)" in source
    assert "r1_bank_statement_transaction_digest" in source
    assert "internal_read.get_bank_statement_summary" in source
    assert "internal_read.list_bank_statement_transactions" in source
    assert "TO ledgerbridge_worker" in source
    assert "TO ledgerbridge_api" not in source
    assert "TO ledgerbridge_reader" in source
    assert "p_operation_id uuid" in source
    assert "p_assertion_jti uuid" in source
    assert "p_workload_principal_ref text" in source
    assert "p_expected_revision integer" in source
    assert "UNIQUE (operation_id)" in source
    assert "UNIQUE (assertion_jti)" in source
    assert "occurred_at AT TIME ZONE 'Asia/Shanghai'" in source
    assert "'source_system'" in source
    for field in (
        "source_size",
        "declared_media_type",
        "currency",
        "period_start",
        "period_end",
    ):
        assert f"'{field}'" in source
    assert "NEW.effective_at IS DISTINCT FROM v_audit_time" in source
    assert "NEW.reviewed_at IS DISTINCT FROM v_audit_time" in source
    assert "INSERT INTO public.candidate" not in source
    assert "candidate_revision" not in source
    assert "journal_entry" not in source
