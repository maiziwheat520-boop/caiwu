from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from test_mybank_statement_cutover import _safety_proof
from test_r1_database_migration import _legacy_r1_database, _upgrade_config

from alembic import command
from ledgerbridge.bank_statement_contract import (
    CCB_PERSONAL_XLS_V1,
    BankStatement,
    BankStatementParserProfile,
    BankStatementTransaction,
)
from ledgerbridge.bank_statement_cutover import (
    BankStatementCutoverGates,
    BankStatementCutoverReceipt,
    BankStatementExistingAccountRunner,
    ProductionCounts,
    run_transactional_database_bank_statement_existing_account_import,
)
from ledgerbridge.bank_statement_cutover_command import (
    BANK_STATEMENT_PREFLIGHT_RECEIPT_SCHEMA,
    run_bank_statement_cutover_command,
)
from ledgerbridge.bank_statement_cutover_plan import BankStatementExistingAccountPlan
from ledgerbridge.bank_statement_cutover_plan_builder import (
    BANK_STATEMENT_EXISTING_ACCOUNT_PLAN_SCHEMA,
)
from ledgerbridge.bank_statement_persistence import (
    BankStatementImportContext,
    BankStatementImportResult,
)
from ledgerbridge.ccb_statement import parse_ccb_personal_xls
from ledgerbridge.file_key_provider import bootstrap_file_key
from ledgerbridge.models import EntityType
from ledgerbridge.mybank_statement_cutover import (
    MyBankEvidenceDescriptor,
    MyBankEvidenceMode,
    MyBankExistingAccountStatementPlan,
    MyBankStatementCutoverError,
    _DatabaseEvidenceBoundary,
    _expected_after_existing_account,
    _read_production_counts,
    _require_expected_new_statement_fact_count,
)

STATEMENT_REF = UUID("72000000-0000-4000-8000-000000000001")
EVIDENCE_REF = UUID("72000000-0000-4000-8000-000000000002")
ENTITY_REF = UUID("72000000-0000-4000-8000-000000000003")
BUSINESS_UNIT_REF = UUID("72000000-0000-4000-8000-000000000004")
ACCOUNT_REF = UUID("72000000-0000-4000-8000-000000000005")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _statement() -> BankStatement:
    transactions = tuple(
        BankStatementTransaction(
            source_event_ref=UUID(f"72000000-0000-4000-8000-{index:012d}"),
            source_row_number=index,
            source_row_sha256=str(index) * 64,
            occurred_at=datetime(2026, 5, index, tzinfo=_SHANGHAI),
            amount_minor=index * 100,
            balance_minor=index * 1_000,
            counterparty_name=f"counterparty-{index}",
            counterparty_account=f"account-{index}",
            counterparty_institution="synthetic-bank",
            transaction_serial=f"serial-{index}",
            transaction_name=f"transfer-{index}",
        )
        for index in (1, 2)
    )
    return BankStatement(
        statement_ref=STATEMENT_REF,
        source_sha256="a" * 64,
        source_size=128,
        declared_media_type=CCB_PERSONAL_XLS_V1.declared_media_type,
        currency="CNY",
        institution_code="ccb",
        account_suffix="7564",
        worksheet_index=0,
        header_row_number=4,
        transactions=transactions,
        parser_profile=BankStatementParserProfile.CCB_PERSONAL_XLS_V1,
        source_system=CCB_PERSONAL_XLS_V1.source_system,
        parser_facts_sha256="b" * 64,
    )


def _plan(source: Path, statement: BankStatement) -> BankStatementExistingAccountPlan:
    return BankStatementExistingAccountPlan.bind(
        statement,
        source_path=source,
        evidence_ref=EVIDENCE_REF,
        entity_ref=ENTITY_REF,
        business_unit_ref=BUSINESS_UNIT_REF,
        managed_account_ref=ACCOUNT_REF,
        expected_owner_kind=EntityType.PERSON,
        actor="worker:ccb-cutover",
        reason="operator-confirmed synthetic CCB cutover",
    )


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _OverlapCountSession:
    def __init__(self, existing_count: int) -> None:
        self._existing_count = existing_count
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(
        self,
        statement: object,
        parameters: dict[str, object] | None = None,
    ) -> _ScalarResult:
        rendered = str(statement)
        values = parameters or {}
        self.calls.append((rendered, values))
        if "pg_advisory_xact_lock" in rendered:
            return _ScalarResult(None)
        return _ScalarResult(self._existing_count)


def test_overlap_preflight_locks_account_and_matches_bound_new_count(tmp_path: Path) -> None:
    statement = _statement()
    plan = replace(
        _plan((tmp_path / "statement.xls").resolve(), statement),
        expected_new_transaction_count=1,
    )
    session = _OverlapCountSession(existing_count=1)

    _require_expected_new_statement_fact_count(
        session,  # type: ignore[arg-type]
        plan,
        statement,
    )

    assert len(session.calls) == 2
    assert "pg_advisory_xact_lock" in session.calls[0][0]
    assert session.calls[0][1] == {"account": ACCOUNT_REF}
    assert json.loads(str(session.calls[1][1]["serials"])) == ["serial-1", "serial-2"]


def test_overlap_preflight_rejects_bound_new_count_drift(tmp_path: Path) -> None:
    statement = _statement()
    plan = replace(
        _plan((tmp_path / "statement.xls").resolve(), statement),
        expected_new_transaction_count=0,
    )

    with pytest.raises(
        MyBankStatementCutoverError,
        match="statement overlap preflight count conflicts",
    ):
        _require_expected_new_statement_fact_count(
            _OverlapCountSession(existing_count=1),  # type: ignore[arg-type]
            plan,
            statement,
        )


def _counts() -> ProductionCounts:
    return ProductionCounts(
        evidence_objects=4,
        encrypted_object_identities=4,
        encrypted_blob_versions=4,
        managed_accounts=3,
        managed_account_lifecycles=3,
        account_registry_operations=3,
        managed_account_aliases=3,
        account_business_unit_assignments=3,
        fact_business_unit_allocation_sets=0,
        fact_business_unit_allocation_items=0,
        bank_statements=0,
        bank_statement_transactions=0,
        bank_statement_observations=0,
        bank_statement_reviews=0,
        candidates=7,
        latest_pending_candidates=2,
        audit_events=20,
    )


def _result(*, created: bool) -> BankStatementImportResult:
    return BankStatementImportResult(
        statement_ref=STATEMENT_REF,
        managed_account_ref=ACCOUNT_REF,
        created=created,
        transaction_count=2,
        review_status="PENDING",
        statement_review_count=1,
        accounting_candidate_count=0,
    )


def test_generic_existing_account_runner_keeps_evidence_and_registry_zero_delta(
    tmp_path: Path,
) -> None:
    source = (tmp_path / "statement.xls").resolve()
    source.write_bytes(b"synthetic")
    statement = _statement()
    plan = _plan(source, statement)
    before = _counts()
    after = _expected_after_existing_account(before, plan)
    evidence: list[object] = []
    authorized: list[object] = []
    imports = iter((_result(created=True), _result(created=False)))
    observed_counts = iter((before, after, after))

    class Parser:
        def __call__(self, *_args: object, **_kwargs: object) -> BankStatement:
            return statement

    class Authorizer:
        def authorize(self, received: object, parsed: object) -> None:
            authorized.append((received, parsed))

    class Importer:
        def import_statement(
            self,
            _statement: BankStatement,
            *,
            context: BankStatementImportContext,
        ) -> BankStatementImportResult:
            assert context.evidence_ref == EVIDENCE_REF
            return next(imports)

    runner = BankStatementExistingAccountRunner(
        parser=Parser(),
        evidence_writer=lambda path, descriptor: evidence.append((path, descriptor)),
        account_authorizer=Authorizer(),
        statement_importer=Importer(),
        counts_reader=lambda: next(observed_counts),
    )
    receipt = runner.run(
        plan,
        gates=BankStatementCutoverGates(
            schema_revision="20260902_0031",
            backup_verified=True,
            isolated_restore_verified=True,
            rollback_ready=True,
            expected_before=before,
        ),
    )

    assert receipt.created is True
    assert receipt.replay_created is False
    assert receipt.registry_created is False
    assert receipt.after_counts == receipt.replay_counts == after
    assert receipt.after_counts.evidence_objects == before.evidence_objects
    assert receipt.after_counts.managed_accounts == before.managed_accounts
    assert len(authorized) == 2
    assert len(evidence) == 1
    _, descriptor = evidence[0]  # type: ignore[misc]
    assert descriptor.display_name.endswith(".xls")


def _plan_payload(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": BANK_STATEMENT_EXISTING_ACCOUNT_PLAN_SCHEMA,
        "target_revision": "c" * 40,
        "parser": {"profile": "ccb_personal_xls_v1"},
        "source": {
            "path": str((tmp_path / "statement.xls").resolve()),
            "sha256": "a" * 64,
            "size": 128,
            "account_suffix": "7564",
            "period_start": "2026-05-01",
            "period_end": "2026-05-02",
            "transaction_count": 2,
            "transaction_set_sha256": "d" * 64,
            "parser_facts_sha256": "b" * 64,
            "monthly_transaction_counts": [{"month": "2026-05", "count": 2}],
        },
        "scope": {
            "evidence_ref": str(EVIDENCE_REF),
            "evidence_mode": "REUSE_EXISTING",
            "owner_entity_ref": str(ENTITY_REF),
            "business_unit_ref": str(BUSINESS_UNIT_REF),
            "owner_kind": "PERSON",
        },
        "account": {
            "managed_account_ref": str(ACCOUNT_REF),
            "institution_code": "ccb",
        },
        "audit": {
            "actor": "worker:ccb-cutover",
            "reason": "operator-confirmed synthetic CCB cutover",
        },
        "safety": {
            "backup_directory": str((tmp_path / "backup").resolve()),
            "restore_report": str(
                (tmp_path / "backup" / "restore-rehearsal-synthetic.json").resolve()
            ),
            "key_file": str((tmp_path / "key.json").resolve()),
            "artifact_root": str((tmp_path / "artifacts").resolve()),
        },
    }


def _command_receipt() -> BankStatementCutoverReceipt:
    before = _counts()
    after = replace(
        before,
        bank_statements=1,
        bank_statement_transactions=2,
        bank_statement_observations=2,
        bank_statement_reviews=1,
        audit_events=26,
    )
    return BankStatementCutoverReceipt(
        statement_ref=STATEMENT_REF,
        evidence_ref=EVIDENCE_REF,
        managed_account_ref=ACCOUNT_REF,
        registry_created=False,
        created=True,
        registry_replay_created=False,
        replay_created=False,
        transaction_count=2,
        review_status="PENDING",
        before_counts=before,
        after_counts=after,
        replay_counts=after,
        fact_conflict_rejected=True,
    )


def test_generic_command_binds_preflight_receipt_before_production(
    tmp_path: Path,
) -> None:
    plan_path = (tmp_path / "plan.json").resolve()
    receipt_path = (tmp_path / "receipt.json").resolve()
    plan_path.write_text(json.dumps(_plan_payload(tmp_path)), encoding="utf-8")
    if os.name != "nt":
        plan_path.chmod(0o600)
    calls: list[tuple[str, bool]] = []

    def execute(_loaded: object, database_url: str, *, commit: bool) -> object:
        calls.append((database_url, commit))
        return _command_receipt()

    common = {
        "LEDGERBRIDGE_BANK_STATEMENT_PRIVATE_PLAN": str(plan_path),
        "LEDGERBRIDGE_BANK_STATEMENT_PREFLIGHT_RECEIPT": str(receipt_path),
        "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_URL": "postgresql://isolated",
        "LEDGERBRIDGE_DEPLOYED_REVISION": "c" * 40,
    }
    assert (
        run_bank_statement_cutover_command(
            ["--preflight-only"],
            environ={
                **common,
                "LEDGERBRIDGE_ENV": "test",
                "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "isolated",
            },
            executor=execute,
        )
        == 0
    )
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == BANK_STATEMENT_PREFLIGHT_RECEIPT_SCHEMA

    assert (
        run_bank_statement_cutover_command(
            ["--execute-production"],
            environ={
                **common,
                "LEDGERBRIDGE_ENV": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_PRODUCTION_EXECUTION": ("execute-reviewed-cutover-v1"),
            },
            executor=execute,
        )
        == 0
    )
    assert calls == [("postgresql://isolated", False), ("postgresql://isolated", True)]


def test_database_ccb_existing_account_create_replay_and_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_value = os.environ.get("LEDGERBRIDGE_CCB_TEST_SOURCE")
    if source_value is None:
        pytest.skip("real CCB source is required for the isolated cutover test")
    source = Path(source_value).resolve(strict=True)
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    statement = parse_ccb_personal_xls(
        source,
        expected_sha256=digest,
        managed_account_suffix="7564",
    )
    entity_ref = uuid4()
    business_unit_ref = uuid4()
    evidence_ref = uuid4()
    managed_account_ref = uuid4()
    assignment_ref = uuid4()
    plan = BankStatementExistingAccountPlan.bind(
        statement,
        source_path=source,
        evidence_ref=evidence_ref,
        entity_ref=entity_ref,
        business_unit_ref=business_unit_ref,
        managed_account_ref=managed_account_ref,
        expected_owner_kind=EntityType.PERSON,
        actor="worker:ccb-isolated-cutover",
        reason="isolated CCB existing-account import verification",
    )
    monkeypatch.delenv("LEDGERBRIDGE_ENV", raising=False)

    with _legacy_r1_database(reader=True) as database_url:
        command.upgrade(_upgrade_config(database_url), "head")
        engine = create_engine(database_url)
        key_file = (tmp_path / "ccb-evidence-key.json").resolve()
        bootstrap_file_key(key_file, generation="ccb-isolated-v1")
        artifact_root = (tmp_path / "ccb-artifacts").resolve()
        artifact_root.mkdir(mode=0o700)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO public.entity (id, entity_type, name) "
                    "VALUES (:entity, 'PERSON', 'Isolated CCB owner')"
                ),
                {"entity": entity_ref},
            )
            connection.execute(
                text(
                    "INSERT INTO public.business_unit (id, entity_id, ref, label) "
                    "VALUES (:unit, :entity, 'ccb-isolated', 'CCB Isolated')"
                ),
                {"unit": business_unit_ref, "entity": entity_ref},
            )

        evidence_seed_plan = MyBankExistingAccountStatementPlan(
            source_path=source,
            expected_sha256=digest,
            expected_size=len(raw),
            evidence_ref=evidence_ref,
            evidence_mode=MyBankEvidenceMode.CREATE_NEW,
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
            managed_account_ref=managed_account_ref,
            account_suffix="7564",
            expected_transaction_count=len(statement.transactions),
            expected_owner_kind=EntityType.PERSON,
            actor=plan.actor,
            reason=plan.reason,
        )
        boundary = _DatabaseEvidenceBoundary(
            engine,
            plan=evidence_seed_plan,
            key_file=key_file,
            artifact_root=artifact_root,
        )
        boundary.write(
            source,
            MyBankEvidenceDescriptor(
                evidence_ref=evidence_ref,
                entity_ref=entity_ref,
                business_unit_ref=business_unit_ref,
                plaintext_sha256=digest,
                plaintext_size=len(raw),
                declared_media_type=statement.declared_media_type,
                display_name=f"ccb-statement-{statement.statement_ref}.xls",
            ),
        )
        evidence_session = boundary.session()
        evidence_session.commit()
        evidence_session.close()
        boundary.commit_publication()

        with engine.begin() as connection:

            def append_audit(
                action: str,
                payload: dict[str, object],
                *,
                rule_version: str,
            ) -> UUID:
                audit_ref = connection.execute(
                    text(
                        "SELECT public.append_audit_event("
                        ":actor, :action, :reason, :rule_version, "
                        "CAST(:payload AS jsonb))"
                    ),
                    {
                        "actor": plan.actor,
                        "action": action,
                        "reason": plan.reason,
                        "rule_version": rule_version,
                        "payload": json.dumps(payload),
                    },
                ).scalar_one()
                assert isinstance(audit_ref, UUID)
                return audit_ref

            account_audit = append_audit(
                "account_registry.account.register",
                {
                    "managed_account_ref": str(managed_account_ref),
                    "owner_entity_ref": str(entity_ref),
                    "owner_kind": "PERSON",
                    "admission_evidence_ref": str(evidence_ref),
                    "account_key": f"managed-account:{managed_account_ref}",
                    "institution_code": "ccb",
                    "account_suffix": "7564",
                    "account_kind": "BANK_CHECKING",
                },
                rule_version="ledgerbridge.account-registry.v1",
            )
            connection.execute(
                text(
                    "INSERT INTO public.managed_account "
                    "(managed_account_ref, entity_id, account_key, institution_code, "
                    "account_suffix, owner_ref, owner_kind, account_kind, "
                    "admission_evidence_ref, audit_event_id, created_at) VALUES "
                    "(:account, :entity, :account_key, 'ccb', '7564', :owner_ref, "
                    "'PERSONAL', 'BANK_CHECKING', :evidence, :audit, "
                    "(SELECT occurred_at FROM public.audit_event WHERE id=:audit))"
                ),
                {
                    "account": managed_account_ref,
                    "entity": entity_ref,
                    "account_key": f"managed-account:{managed_account_ref}",
                    "owner_ref": str(entity_ref),
                    "evidence": evidence_ref,
                    "audit": account_audit,
                },
            )
            lifecycle_audit = append_audit(
                "managed_account.lifecycle",
                {
                    "managed_account_ref": str(managed_account_ref),
                    "revision": 1,
                    "status": "ACTIVE",
                },
                rule_version="ledgerbridge.bank-statement.v1",
            )
            connection.execute(
                text(
                    "INSERT INTO public.managed_account_lifecycle "
                    "(managed_account_ref, revision, status, audit_event_id, effective_at) "
                    "VALUES (:account, 1, 'ACTIVE', :audit, "
                    "(SELECT occurred_at FROM public.audit_event WHERE id=:audit))"
                ),
                {
                    "account": managed_account_ref,
                    "audit": lifecycle_audit,
                },
            )
            assignment_audit = append_audit(
                "account_registry.business_unit.assign",
                {
                    "assignment_ref": str(assignment_ref),
                    "managed_account_ref": str(managed_account_ref),
                    "business_unit_id": str(business_unit_ref),
                    "business_unit_ref_snapshot": "ccb-isolated",
                    "business_unit_label_snapshot": "CCB Isolated",
                    "effective_from": statement.period_start.isoformat(),
                    "effective_to": "",
                },
                rule_version="ledgerbridge.account-registry.v1",
            )
            connection.execute(
                text(
                    "INSERT INTO public.account_business_unit_assignment "
                    "(assignment_ref, owner_entity_id, managed_account_ref, business_unit_id, "
                    "business_unit_ref_snapshot, business_unit_label_snapshot, effective_from, "
                    "effective_to, audit_event_id, created_at) VALUES "
                    "(:assignment, :entity, :account, :unit, 'ccb-isolated', "
                    "'CCB Isolated', :effective_from, NULL, :audit, "
                    "(SELECT occurred_at FROM public.audit_event WHERE id=:audit))"
                ),
                {
                    "assignment": assignment_ref,
                    "entity": entity_ref,
                    "account": managed_account_ref,
                    "unit": business_unit_ref,
                    "effective_from": statement.period_start,
                    "audit": assignment_audit,
                },
            )

        before = _read_production_counts(engine)
        gates = BankStatementCutoverGates(
            schema_revision="20260902_0031",
            backup_verified=True,
            isolated_restore_verified=True,
            rollback_ready=True,
            expected_before=before,
            verify_fact_conflict=True,
        )
        proof = _safety_proof(
            tmp_path,
            before,
            schema_revision="20260902_0031",
        )
        preflight = run_transactional_database_bank_statement_existing_account_import(
            engine,
            plan,
            gates=gates,
            safety_proof=proof,
            key_file=key_file,
            artifact_root=artifact_root,
            commit=False,
        )
        assert preflight.created is True
        assert preflight.fact_conflict_rejected is True
        assert _read_production_counts(engine) == before

        receipt = run_transactional_database_bank_statement_existing_account_import(
            engine,
            plan,
            gates=gates,
            safety_proof=proof,
            key_file=key_file,
            artifact_root=artifact_root,
            commit=True,
        )
        assert receipt.created is True
        assert receipt.fact_conflict_rejected is True
        assert receipt.after_counts.evidence_objects == before.evidence_objects
        assert receipt.after_counts.managed_accounts == before.managed_accounts
        assert receipt.after_counts.bank_statement_transactions == (
            before.bank_statement_transactions + len(statement.transactions)
        )

        completed = _read_production_counts(engine)
        replay = run_transactional_database_bank_statement_existing_account_import(
            engine,
            plan,
            gates=gates,
            safety_proof=proof,
            key_file=key_file,
            artifact_root=artifact_root,
            commit=True,
        )
        assert replay.created is False
        assert replay.fact_conflict_rejected is True
        assert replay.after_counts == replay.replay_counts == completed
        assert _read_production_counts(engine) == completed
        engine.dispose()
