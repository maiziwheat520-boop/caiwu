from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.artifacts import ArtifactStore, storage_key_for_digest
from ledgerbridge.candidate_contract import ReviewRiskCode
from ledgerbridge.counterparty import CounterpartyClass
from ledgerbridge.crypto import SecretStreamCipher, _parse_envelope
from ledgerbridge.encrypted_artifacts import EncryptedArtifactStore
from ledgerbridge.internal_read_contract import (
    CandidatePage,
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_cursor import ReadCursorSigner
from ledgerbridge.internal_read_service import (
    AccountingDimensionsInvalid,
    DatabaseInternalReadService,
    InternalReadBackendUnavailable,
    SyntheticInternalReadService,
)

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
BUSINESS_UNIT = UUID("11000000-0000-4000-8000-000000000001")


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._rows)


class _SqlStateError(SQLAlchemyError):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.orig = type("Orig", (), {"sqlstate": sqlstate})()


class _Session:
    def __init__(
        self,
        candidate_row: dict[str, Any],
        *,
        candidate_rows: list[dict[str, Any]] | None = None,
        reconciliation_row: dict[str, Any] | None = None,
        ledger_rows: list[dict[str, Any]] | None = None,
        evidence_row: dict[str, Any] | None = None,
        satisfaction_rows: list[dict[str, Any]] | None = None,
        counterparty_rows: list[dict[str, Any]] | None = None,
        accounting_dimensions: dict[str, Any] | None = None,
        failure: SQLAlchemyError | None = None,
        fail: bool = False,
    ) -> None:
        self.candidate_row = candidate_row
        self.candidate_rows = candidate_rows or [candidate_row]
        self.reconciliation_row = reconciliation_row
        self.ledger_rows = ledger_rows or []
        self.evidence_row = evidence_row
        self.satisfaction_rows = satisfaction_rows or []
        self.counterparty_rows = counterparty_rows or []
        self.accounting_dimensions = accounting_dimensions
        self.failure = failure
        self.fail = fail
        self.statements: list[str] = []
        self.parameters: list[dict[str, Any] | None] = []

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        self.parameters.append(params)
        if self.failure is not None:
            raise self.failure
        if self.fail:
            raise SQLAlchemyError("synthetic database failure")
        if "current_audit_horizon" in sql:
            return _Result([{"sequence": 7, "hash": b"h" * 32}])
        if "get_accounting_dimensions" in sql:
            return _Result(
                []
                if self.accounting_dimensions is None
                else [{"dimensions": self.accounting_dimensions}]
            )
        if "list_candidates_as_of" in sql:
            return _Result(self.candidate_rows)
        if "list_candidate_evidence_satisfactions" in sql:
            return _Result(self.satisfaction_rows)
        if "list_candidate_counterparty_facts" in sql:
            return _Result(self.counterparty_rows)
        if "get_reconciliation_as_of" in sql:
            return _Result([] if self.reconciliation_row is None else [self.reconciliation_row])
        if "get_ledger_summary_as_of" in sql:
            return _Result(self.ledger_rows)
        if "resolve_active_evidence_blob" in sql:
            return _Result([] if self.evidence_row is None else [self.evidence_row])
        raise AssertionError(f"unexpected SQL: {sql} / {params}")


class _FakeDecryptor:
    def __init__(self, content: bytes) -> None:
        self.content = content

    @contextmanager
    def open_verified(self, artifact: object, *, envelope_metadata: object) -> Iterator[io.BytesIO]:
        _ = artifact, envelope_metadata
        yield io.BytesIO(self.content)


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:database-test",
        san_uri="spiffe://ledgerbridge.test/database-test",
        policy_generation=1,
        capabilities=frozenset(
            {
                Capability.CANDIDATE_READ,
                Capability.CANDIDATE_DECIDE,
                Capability.SYSTEM_READ,
                Capability.EVIDENCE_READ,
                Capability.RECONCILIATION_READ,
            }
        ),
        grants=(
            EntityGrant(
                entity_ref=ENTITY,
                business_unit_refs=frozenset({"unit-demo-a"}),
                business_unit_ids=frozenset({BUSINESS_UNIT}),
                business_unit_bindings=(("unit-demo-a", BUSINESS_UNIT),),
            ),
        ),
    )


def _service(session: _Session) -> DatabaseInternalReadService:
    # The production adapter depends only on the small context-manager/execute
    # surface exercised here; cast the fake to SQLAlchemy's runtime factory
    # type without making the fixture inherit a live database session.
    def factory() -> Session:
        return cast(Session, session)

    return DatabaseInternalReadService(factory)


def _metadata_row() -> dict[str, Any]:
    ciphertext_sha256 = b"c" * 32
    return {
        "evidence_ref": UUID("20000000-0000-4000-8000-000000000001"),
        "blob_ref": UUID("30000000-0000-4000-8000-000000000001"),
        "entity_id": ENTITY,
        "business_unit_id": BUSINESS_UNIT,
        "business_unit_ref": "unit-demo-a",
        "media_type": "application/octet-stream",
        "display_name": "receipt.bin",
        "object_ref": "a" * 64,
        "plaintext_sha256": b"p" * 32,
        "plaintext_size": 1,
        "ciphertext_sha256": ciphertext_sha256,
        "ciphertext_size": 1,
        "storage_key": storage_key_for_digest(ciphertext_sha256),
        "envelope_schema": "ledgerbridge.secretstream.v1",
        "algorithm": "xchacha20poly1305-secretstream",
        "chunk_size": 17,
        "stream_header": b"h" * 24,
        "wrapped_key_generation": "test",
        "wrapped_key_nonce": b"n" * 24,
        "wrapped_key_ciphertext": b"w" * 48,
        "purpose": "ledgerbridge-artifact-v2",
        "aad_scheme": "ledgerbridge.artifact.object.v2",
    }


def test_database_candidate_reader_uses_horizon_and_scoped_function() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    row = candidate.model_dump()
    row["entity_ref"] = ENTITY
    row["business_unit_ref"] = "unit-demo-a"
    session = _Session(row)
    service = _service(session)

    page = service.list_candidates(_principal(), month=candidate.accounting_month)

    assert [item.candidate_ref for item in page.items] == [candidate.candidate_ref]
    assert any("current_audit_horizon" in statement for statement in session.statements)
    assert any("list_candidates_as_of" in statement for statement in session.statements)
    assert all("public." not in statement for statement in session.statements)


def test_database_accounting_dimensions_use_exact_grant_bindings() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    session = _Session(
        candidate.model_dump(),
        accounting_dimensions={
            "contract_version": "ledgerbridge.accounting-dimensions.v1",
            "entity_ref": str(ENTITY),
            "business_units": [{"ref": "unit-demo-a", "label": "Demo unit A"}],
            "categories": [{"code": "SUPPLIES", "label": "Synthetic supplies"}],
        },
    )

    dimensions = _service(session).get_accounting_dimensions(_principal(), entity_ref=ENTITY)

    assert [(item.ref, item.label) for item in dimensions.business_units] == [
        ("unit-demo-a", "Demo unit A")
    ]
    assert [(item.code, item.label) for item in dimensions.categories] == [
        ("SUPPLIES", "Synthetic supplies")
    ]
    assert any("internal_read.get_accounting_dimensions" in sql for sql in session.statements)
    assert all("public." not in sql for sql in session.statements)
    dimensions_call = next(
        params
        for sql, params in zip(session.statements, session.parameters, strict=True)
        if "internal_read.get_accounting_dimensions" in sql
    )
    assert dimensions_call is not None
    assert dimensions_call["business_unit_ids"] == [BUSINESS_UNIT]
    assert dimensions_call["business_unit_refs"] == ["unit-demo-a"]


def test_database_accounting_dimensions_allow_retired_grant_bindings_to_be_omitted() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    retired_id = UUID("11000000-0000-4000-8000-000000000002")
    principal = _principal().model_copy(
        update={
            "grants": (
                EntityGrant(
                    entity_ref=ENTITY,
                    business_unit_refs=frozenset({"unit-demo-a", "unit-retired"}),
                    business_unit_ids=frozenset({BUSINESS_UNIT, retired_id}),
                    business_unit_bindings=(
                        ("unit-demo-a", BUSINESS_UNIT),
                        ("unit-retired", retired_id),
                    ),
                ),
            )
        }
    )
    session = _Session(
        candidate.model_dump(),
        accounting_dimensions={
            "contract_version": "ledgerbridge.accounting-dimensions.v1",
            "entity_ref": str(ENTITY),
            "business_units": [{"ref": "unit-demo-a", "label": "Demo unit A"}],
            "categories": [{"code": "SUPPLIES", "label": "Synthetic supplies"}],
        },
    )

    dimensions = _service(session).get_accounting_dimensions(principal, entity_ref=ENTITY)

    assert [item.ref for item in dimensions.business_units] == ["unit-demo-a"]


def test_database_accounting_dimensions_reject_unbound_returned_ref() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    session = _Session(
        candidate.model_dump(),
        accounting_dimensions={
            "contract_version": "ledgerbridge.accounting-dimensions.v1",
            "entity_ref": str(ENTITY),
            "business_units": [{"ref": "unit-other", "label": "Other unit"}],
            "categories": [],
        },
    )

    with pytest.raises(InternalReadBackendUnavailable, match="scope binding"):
        _service(session).get_accounting_dimensions(_principal(), entity_ref=ENTITY)


def test_database_accounting_dimensions_surface_duplicate_active_labels_for_governance() -> None:
    session = _Session({}, failure=_SqlStateError("LB005"))

    with pytest.raises(AccountingDimensionsInvalid, match="registry governance"):
        _service(session).get_accounting_dimensions(_principal(), entity_ref=ENTITY)


def test_database_candidate_reader_applies_audited_risk_satisfaction() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    row = candidate.model_dump()
    row["entity_ref"] = ENTITY
    row["business_unit_ref"] = "unit-demo-a"
    row["source"] = {
        **row["source"],
        "source_system": "hotel_bill_ocr",
    }
    row["category_code"] = "PHOTO_RECONCILIATION"
    row["summary"] = "OCR账单待复核: CTRIP_EBOOKING 1:2026-05-18:2026-05-24"
    session = _Session(
        row,
        satisfaction_rows=[
            {
                "candidate_id": row["candidate_ref"],
                "risk_code": ReviewRiskCode.HOTEL_PAYOUT_STATEMENT_REQUIRED.value,
            }
        ],
    )

    page = _service(session).list_candidates(_principal())

    assert page.items[0].review_risks == ()
    assert any(
        "list_candidate_evidence_satisfactions" in statement for statement in session.statements
    )


def test_database_candidate_reader_uses_registry_backed_counterparty_class() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    row = candidate.model_dump()
    row["entity_ref"] = ENTITY
    row["business_unit_ref"] = "unit-demo-a"
    row["source"] = {**row["source"], "source_system": "alipay_export"}
    row["category_code"] = "ALIPAY_TRANSACTION_REVIEW"
    row["summary"] = "支付宝 | 2026-05-08 | 支出 | 转账 | 关联公司 | 账户余额 | 交易成功"
    session = _Session(
        row,
        counterparty_rows=[
            {
                "candidate_id": row["candidate_ref"],
                "counterparty_ref": "cp_related_company",
                "counterparty_class": "related_party",
            }
        ],
    )

    page = _service(session).list_candidates(_principal())

    assert {risk.code for risk in page.items[0].review_risks} == {
        ReviewRiskCode.RELATED_ACCOUNT_STATEMENT_REQUIRED
    }
    assert page.items[0].counterparty_ref == "cp_related_company"
    assert page.items[0].counterparty_class is CounterpartyClass.RELATED_PARTY
    assert any("list_candidate_counterparty_facts" in sql for sql in session.statements)


def test_database_candidate_reader_rejects_entity_scope_drift() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    row = candidate.model_dump()
    row["entity_ref"] = UUID("10000000-0000-4000-8000-000000000002")
    row["business_unit_ref"] = "unit-demo-a"

    with pytest.raises(InternalReadBackendUnavailable, match="scope binding"):
        _service(_Session(row)).list_candidates(_principal(), month=candidate.accounting_month)


def test_database_reader_rejects_ref_only_grants_before_querying_facts() -> None:
    principal = _principal().model_copy(
        update={
            "grants": (
                EntityGrant(
                    entity_ref=ENTITY,
                    business_unit_refs=frozenset({"unit-demo-a"}),
                ),
            )
        }
    )
    session = _Session({})

    with pytest.raises(InternalReadBackendUnavailable, match="explicit business-unit"):
        _service(session).list_candidates(principal)
    assert session.statements == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity_id", "not-a-uuid"),
        ("evidence_ref", "not-a-uuid"),
        ("plaintext_sha256", b"short"),
        ("object_ref", "A" * 64),
        ("business_unit_ref", ""),
        ("storage_key", "sha256/00/00/not-canonical"),
        ("media_type", "text/plain"),
        ("display_name", "../unsafe"),
        ("plaintext_size", -1),
        ("ciphertext_size", 268435457),
        ("envelope_schema", "other"),
        ("algorithm", "other"),
        ("chunk_size", 0),
        ("stream_header", b"short"),
        ("wrapped_key_generation", "bad generation"),
        ("wrapped_key_nonce", b"short"),
        ("wrapped_key_ciphertext", b"short"),
        ("purpose", "other"),
        ("aad_scheme", "other"),
    ],
)
def test_database_evidence_metadata_parser_fails_closed(field: str, value: object) -> None:
    row = _metadata_row()
    row[field] = value
    with pytest.raises(ValueError):
        DatabaseInternalReadService._evidence_metadata(row)


def test_database_evidence_metadata_uses_safe_default_filename() -> None:
    row = _metadata_row()
    row["display_name"] = None
    metadata = DatabaseInternalReadService._evidence_metadata(row)
    assert metadata.filename == "evidence-20000000000040008000000000000001.bin"


def test_database_reader_keeps_evidence_closed_without_decryptor() -> None:
    service = _service(_Session({}))
    principal = _principal()

    with pytest.raises(InternalReadBackendUnavailable, match="S1 decryptor"):
        service.get_evidence(principal, UUID("20000000-0000-4000-8000-000000000001"))


def test_database_ledger_summary_projects_posted_category_totals() -> None:
    ledger_principal = _principal().model_copy(
        update={"capabilities": frozenset({Capability.LEDGER_READ})}
    )
    session = _Session(
        {},
        ledger_rows=[
            {
                "entity_ref": ENTITY,
                "business_unit_ref": "unit-demo-a",
                "from_month": "2026-08",
                "to_month": "2026-08",
                "posting_status": "POSTED",
                "currency": "CNY",
                "category_code": "SUPPLIES",
                "amount_minor": -12345,
            }
        ],
    )
    summary = _service(session).get_ledger_summary(
        ledger_principal,
        entity_ref=ENTITY,
        business_unit_ref="unit-demo-a",
        from_month="2026-08",
        to_month="2026-08",
    )
    assert summary.totals_minor == {"SUPPLIES": -12345}
    assert any("get_ledger_summary_as_of" in statement for statement in session.statements)


def _ledger_principal() -> WorkloadPrincipal:
    return _principal().model_copy(update={"capabilities": frozenset({Capability.LEDGER_READ})})


def _ledger_row(**updates: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "entity_ref": ENTITY,
        "business_unit_ref": "unit-demo-a",
        "from_month": "2026-08",
        "to_month": "2026-08",
        "posting_status": "POSTED",
        "currency": "CNY",
        "category_code": "SUPPLIES",
        "amount_minor": -12345,
    }
    row.update(updates)
    return row


def test_database_ledger_summary_rejects_bad_rows_and_database_errors() -> None:
    for row in (
        _ledger_row(entity_ref=UUID("10000000-0000-4000-8000-000000000002")),
        _ledger_row(amount_minor="not-an-int"),
        _ledger_row(category_code=""),
        _ledger_row(amount_minor=10**30),
    ):
        with pytest.raises(InternalReadBackendUnavailable):
            _service(_Session({}, ledger_rows=[row])).get_ledger_summary(
                _ledger_principal(),
                entity_ref=ENTITY,
                business_unit_ref="unit-demo-a",
                from_month="2026-08",
                to_month="2026-08",
            )
    with pytest.raises(InternalReadBackendUnavailable, match="read failed"):
        _service(_Session({}, fail=True)).get_ledger_summary(
            _ledger_principal(),
            entity_ref=ENTITY,
            business_unit_ref="unit-demo-a",
            from_month="2026-08",
            to_month="2026-08",
        )


def test_database_evidence_decryptor_returns_verified_content(tmp_path: Any) -> None:
    from ledgerbridge.keyring import SyntheticKeyProvider

    provider = SyntheticKeyProvider({"test": b"k" * 32}, active_generation="test")
    cipher = SecretStreamCipher(provider, chunk_size=17)
    durable = ArtifactStore(tmp_path.resolve(), max_bytes=1_000_000)
    encrypted = EncryptedArtifactStore(durable, cipher, max_plaintext_bytes=1_000)
    published = encrypted.publish(io.BytesIO(b"verified-evidence"))
    with durable.open_verified(published.ciphertext) as ciphertext_stream:
        envelope = _parse_envelope(ciphertext_stream.read())
    evidence_ref = UUID("20000000-0000-4000-8000-000000000001")
    session = _Session(
        {},
        evidence_row={
            "evidence_ref": evidence_ref,
            "blob_ref": UUID("30000000-0000-4000-8000-000000000001"),
            "entity_id": ENTITY,
            "business_unit_id": BUSINESS_UNIT,
            "business_unit_ref": "unit-demo-a",
            "media_type": "application/octet-stream",
            "display_name": "receipt.bin",
            "object_ref": published.object_ref,
            "plaintext_sha256": published.plaintext_sha256,
            "plaintext_size": published.plaintext_size,
            "ciphertext_sha256": published.ciphertext.sha256,
            "ciphertext_size": published.ciphertext.byte_size,
            "storage_key": published.ciphertext.storage_key,
            "envelope_schema": "ledgerbridge.secretstream.v1",
            "algorithm": "xchacha20poly1305-secretstream",
            "chunk_size": envelope.header.chunk_size,
            "stream_header": envelope.header.stream_header,
            "wrapped_key_generation": envelope.header.wrapped_key.generation,
            "wrapped_key_nonce": envelope.header.wrapped_key.nonce,
            "wrapped_key_ciphertext": envelope.header.wrapped_key.ciphertext,
            "purpose": "ledgerbridge-artifact-v2",
            "aad_scheme": "ledgerbridge.artifact.object.v2",
        },
    )
    service = DatabaseInternalReadService(
        lambda: cast(Session, session), encrypted_artifact_store=encrypted
    )
    result = service.get_evidence(_principal(), evidence_ref)
    assert result.content == b"verified-evidence"
    assert result.filename == "receipt.bin"


def test_database_evidence_receipt_is_required_before_returning_content(tmp_path: Any) -> None:
    from ledgerbridge.keyring import SyntheticKeyProvider

    provider = SyntheticKeyProvider({"test": b"k" * 32}, active_generation="test")
    cipher = SecretStreamCipher(provider, chunk_size=17)
    durable = ArtifactStore(tmp_path.resolve(), max_bytes=1_000_000)
    encrypted = EncryptedArtifactStore(durable, cipher, max_plaintext_bytes=1_000)
    published = encrypted.publish(io.BytesIO(b"verified-evidence"))
    with durable.open_verified(published.ciphertext) as ciphertext_stream:
        envelope = _parse_envelope(ciphertext_stream.read())
    row = _metadata_row()
    row.update(
        {
            "object_ref": published.object_ref,
            "plaintext_sha256": published.plaintext_sha256,
            "plaintext_size": published.plaintext_size,
            "ciphertext_sha256": published.ciphertext.sha256,
            "ciphertext_size": published.ciphertext.byte_size,
            "storage_key": published.ciphertext.storage_key,
            "chunk_size": envelope.header.chunk_size,
            "stream_header": envelope.header.stream_header,
            "wrapped_key_generation": envelope.header.wrapped_key.generation,
            "wrapped_key_nonce": envelope.header.wrapped_key.nonce,
            "wrapped_key_ciphertext": envelope.header.wrapped_key.ciphertext,
        }
    )

    class Sink:
        def __init__(self) -> None:
            self.receipts: list[Any] = []

        def append(self, receipt: Any) -> None:
            self.receipts.append(receipt)

    sink = Sink()
    service = DatabaseInternalReadService(
        lambda: cast(Session, _Session({}, evidence_row=row)),
        encrypted_artifact_store=encrypted,
        receipt_sink=cast(Any, sink),
    )
    result = service.get_evidence(_principal(), row["evidence_ref"])
    assert result.content == b"verified-evidence"
    assert sink.receipts[0].blob_ref == row["blob_ref"]
    assert sink.receipts[0].policy_generation == "1"

    class FailingSink:
        def append(self, _receipt: Any) -> None:
            raise RuntimeError("receipt unavailable")

    failing = DatabaseInternalReadService(
        lambda: cast(Session, _Session({}, evidence_row=row)),
        encrypted_artifact_store=encrypted,
        receipt_sink=cast(Any, FailingSink()),
    )
    with pytest.raises(InternalReadBackendUnavailable, match="receipt"):
        failing.get_evidence(_principal(), row["evidence_ref"])


def test_database_evidence_rejects_envelope_descriptor_drift(tmp_path: Any) -> None:
    from ledgerbridge.keyring import SyntheticKeyProvider

    provider = SyntheticKeyProvider({"test": b"k" * 32}, active_generation="test")
    cipher = SecretStreamCipher(provider, chunk_size=17)
    durable = ArtifactStore(tmp_path.resolve(), max_bytes=1_000_000)
    encrypted = EncryptedArtifactStore(durable, cipher, max_plaintext_bytes=1_000)
    published = encrypted.publish(io.BytesIO(b"verified-evidence"))
    with durable.open_verified(published.ciphertext) as ciphertext_stream:
        envelope = _parse_envelope(ciphertext_stream.read())
    session = _Session(
        {},
        evidence_row={
            "evidence_ref": UUID("20000000-0000-4000-8000-000000000001"),
            "entity_id": ENTITY,
            "business_unit_id": BUSINESS_UNIT,
            "business_unit_ref": "unit-demo-a",
            "media_type": "application/octet-stream",
            "display_name": "receipt.bin",
            "object_ref": published.object_ref,
            "plaintext_sha256": published.plaintext_sha256,
            "plaintext_size": published.plaintext_size,
            "ciphertext_sha256": published.ciphertext.sha256,
            "ciphertext_size": published.ciphertext.byte_size,
            "storage_key": published.ciphertext.storage_key,
            "envelope_schema": "ledgerbridge.secretstream.v1",
            "algorithm": "xchacha20poly1305-secretstream",
            "chunk_size": envelope.header.chunk_size,
            "stream_header": b"x" * 24,
            "wrapped_key_generation": envelope.header.wrapped_key.generation,
            "wrapped_key_nonce": envelope.header.wrapped_key.nonce,
            "wrapped_key_ciphertext": envelope.header.wrapped_key.ciphertext,
            "purpose": "ledgerbridge-artifact-v2",
            "aad_scheme": "ledgerbridge.artifact.object.v2",
        },
    )
    service = DatabaseInternalReadService(
        lambda: cast(Session, session), encrypted_artifact_store=encrypted
    )
    with pytest.raises(InternalReadBackendUnavailable, match="payload is invalid"):
        service.get_evidence(_principal(), UUID("20000000-0000-4000-8000-000000000001"))


def test_database_evidence_rejects_noncanonical_storage_key(tmp_path: Any) -> None:
    from ledgerbridge.keyring import SyntheticKeyProvider

    provider = SyntheticKeyProvider({"test": b"k" * 32}, active_generation="test")
    encrypted = EncryptedArtifactStore(
        ArtifactStore(tmp_path.resolve(), max_bytes=1_000_000),
        SecretStreamCipher(provider),
        max_plaintext_bytes=1_000,
    )
    session = _Session(
        {},
        evidence_row={
            "evidence_ref": UUID("20000000-0000-4000-8000-000000000001"),
            "entity_id": ENTITY,
            "business_unit_id": BUSINESS_UNIT,
            "business_unit_ref": "unit-demo-a",
            "media_type": "application/octet-stream",
            "display_name": "receipt.bin",
            "object_ref": "a" * 64,
            "plaintext_sha256": b"p" * 32,
            "plaintext_size": 1,
            "ciphertext_sha256": b"c" * 32,
            "ciphertext_size": 1,
            "storage_key": "sha256/cc/cc/" + "c" * 64,
        },
    )
    service = DatabaseInternalReadService(
        lambda: cast(Session, session), encrypted_artifact_store=encrypted
    )
    with pytest.raises(InternalReadBackendUnavailable, match="payload is invalid"):
        service.get_evidence(_principal(), UUID("20000000-0000-4000-8000-000000000001"))


def test_database_evidence_missing_and_identity_drift_are_not_decrypted() -> None:
    decryptor = cast(Any, object())
    missing = DatabaseInternalReadService(
        lambda: cast(Session, _Session({})), encrypted_artifact_store=decryptor
    )
    with pytest.raises(ResourceNotVisible, match="resource was not found"):
        missing.get_evidence(_principal(), UUID("20000000-0000-4000-8000-000000000001"))

    row = _metadata_row()
    row["evidence_ref"] = UUID("20000000-0000-4000-8000-000000000002")
    drifted = DatabaseInternalReadService(
        lambda: cast(Session, _Session({}, evidence_row=row)),
        encrypted_artifact_store=decryptor,
    )
    with pytest.raises(InternalReadBackendUnavailable, match="identity"):
        drifted.get_evidence(_principal(), UUID("20000000-0000-4000-8000-000000000001"))

    failed = DatabaseInternalReadService(
        lambda: cast(Session, _Session({}, fail=True)), encrypted_artifact_store=decryptor
    )
    with pytest.raises(InternalReadBackendUnavailable, match="read failed"):
        failed.get_evidence(_principal(), UUID("20000000-0000-4000-8000-000000000001"))


def test_database_evidence_scope_and_plaintext_drift_fail_closed() -> None:
    row = _metadata_row()
    row["business_unit_id"] = UUID("11000000-0000-4000-8000-000000000002")
    scope_drift = DatabaseInternalReadService(
        lambda: cast(Session, _Session({}, evidence_row=row)),
        encrypted_artifact_store=cast(Any, object()),
    )
    with pytest.raises(InternalReadBackendUnavailable, match="scope binding"):
        scope_drift.get_evidence(_principal(), row["evidence_ref"])

    row = _metadata_row()
    plaintext_drift = DatabaseInternalReadService(
        lambda: cast(Session, _Session({}, evidence_row=row)),
        encrypted_artifact_store=cast(Any, _FakeDecryptor(b"x")),
    )
    with pytest.raises(InternalReadBackendUnavailable, match="plaintext"):
        plaintext_drift.get_evidence(_principal(), row["evidence_ref"])


def test_database_candidate_reader_issues_and_verifies_a_keyset_cursor() -> None:
    template = SyntheticInternalReadService()._fixture.candidates[1].model_dump()
    rows: list[dict[str, Any]] = []
    for index in range(101):
        row = dict(template)
        row["candidate_ref"] = UUID(f"30000000-0000-4000-8000-{index + 100:012d}")
        row["short_id"] = f"C-{index:05d}"
        row["created_at"] = datetime(2026, 8, 24, tzinfo=UTC) + timedelta(seconds=index)
        row["updated_at"] = row["created_at"]
        rows.append(row)
    session = _Session(rows[0], candidate_rows=rows)
    signer_key = "k" * 32
    service = DatabaseInternalReadService(
        lambda: cast(Session, session),
        cursor_signer=ReadCursorSigner(signer_key),
    )

    page = service.list_candidates(_principal())

    assert len(page.items) == 100
    assert page.next_cursor is not None
    claims = ReadCursorSigner(signer_key).verify(
        page.next_cursor, _principal(), month=None, status=None, business_unit=None
    )
    assert claims["horizon_sequence"] == 7
    assert claims["last_candidate_id"] == page.items[-1].candidate_ref


def test_database_reconciliation_reader_projects_rows_and_hides_missing() -> None:
    row = {
        "entity_ref": ENTITY,
        "business_unit_ref": "unit-demo-a",
        "month": "2026-08",
        "snapshot_revision": 1,
        "blockers": (),
        "proposals": (),
        "suspense": (),
        "posted_amount_minor": 123,
        "currency": "CNY",
    }
    session = _Session({}, reconciliation_row=row)
    service = _service(session)

    projection = service.get_reconciliation(
        _principal(), month="2026-08", entity_ref=ENTITY, business_unit_ref="unit-demo-a"
    )
    assert projection.posted_amount_minor == 123

    missing = _service(_Session({}))
    with pytest.raises(ResourceNotVisible, match="resource was not found"):
        missing.get_reconciliation(
            _principal(), month="2026-08", entity_ref=ENTITY, business_unit_ref="unit-demo-a"
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity_ref", UUID("10000000-0000-4000-8000-000000000002")),
        ("business_unit_ref", "unit-demo-b"),
        ("month", "2026-07"),
    ],
)
def test_database_reconciliation_reader_rejects_scope_drift(field: str, value: object) -> None:
    row = {
        "entity_ref": ENTITY,
        "business_unit_ref": "unit-demo-a",
        "month": "2026-08",
        "snapshot_revision": 1,
        "blockers": (),
        "proposals": (),
        "suspense": (),
        "posted_amount_minor": 123,
        "currency": "CNY",
    }
    row[field] = value

    with pytest.raises(InternalReadBackendUnavailable, match="out of scope"):
        _service(_Session({}, reconciliation_row=row)).get_reconciliation(
            _principal(), month="2026-08", entity_ref=ENTITY, business_unit_ref="unit-demo-a"
        )


def test_database_reader_translates_driver_and_projection_failures() -> None:
    failing = _service(_Session({}, fail=True))
    with pytest.raises(InternalReadBackendUnavailable, match="candidate read failed"):
        failing.list_candidates(_principal())

    malformed = _Session({}, reconciliation_row={"entity_ref": ENTITY})
    with pytest.raises(InternalReadBackendUnavailable, match="projection is invalid"):
        _service(malformed).get_reconciliation(
            _principal(), month="2026-08", entity_ref=ENTITY, business_unit_ref="unit-demo-a"
        )


def test_database_reader_rejects_malformed_horizon_and_unbound_business_unit() -> None:
    class BadHorizon(_Session):
        def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
            if "current_audit_horizon" in str(statement):
                return _Result([{"sequence": 0, "hash": b"h" * 31}])
            return super().execute(statement, params)

    with pytest.raises(InternalReadBackendUnavailable, match="audit horizon"):
        _service(BadHorizon({})).list_candidates(_principal())

    unbound = _principal().model_copy(
        update={
            "grants": (
                EntityGrant(
                    entity_ref=ENTITY,
                    business_unit_refs=frozenset({"unit-demo-a", "unit-demo-b"}),
                    business_unit_ids=frozenset(
                        {BUSINESS_UNIT, UUID("11000000-0000-4000-8000-000000000002")}
                    ),
                    business_unit_bindings=(
                        ("unit-demo-a", BUSINESS_UNIT),
                        ("unit-demo-b", UUID("11000000-0000-4000-8000-000000000002")),
                    ),
                ),
            )
        }
    )
    with pytest.raises(ResourceNotVisible, match="resource was not found"):
        _service(_Session({})).get_reconciliation(
            unbound, month="2026-08", entity_ref=ENTITY, business_unit_ref="unit-demo-a"
        )


def test_database_reader_rejects_multiple_scopes_and_missing_cursor_signer() -> None:
    multi = _principal().model_copy(
        update={
            "grants": (
                EntityGrant(
                    entity_ref=ENTITY,
                    business_unit_refs=frozenset({"unit-demo-a", "unit-demo-b"}),
                    business_unit_ids=frozenset(
                        {BUSINESS_UNIT, UUID("11000000-0000-4000-8000-000000000002")}
                    ),
                    business_unit_bindings=(
                        ("unit-demo-a", BUSINESS_UNIT),
                        ("unit-demo-b", UUID("11000000-0000-4000-8000-000000000002")),
                    ),
                ),
            )
        }
    )
    with pytest.raises(InternalReadBackendUnavailable, match="one bound"):
        _service(_Session({})).list_candidates(multi)
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    row = candidate.model_dump()
    row["entity_ref"] = ENTITY
    row["business_unit_ref"] = "unit-demo-a"
    scoped = _service(_Session(row)).list_candidates(multi, business_unit="unit-demo-a")
    assert [item.candidate_ref for item in scoped.items] == [candidate.candidate_ref]

    unassigned = _principal().model_copy(
        update={
            "grants": (
                _principal().grants[0].model_copy(update={"allow_unassigned_candidates": True}),
            )
        }
    )
    with pytest.raises(InternalReadBackendUnavailable, match="multiple scopes"):
        _service(_Session({})).list_candidates(unassigned)

    with pytest.raises(InternalReadBackendUnavailable, match="signed cursor"):
        _service(_Session({})).list_candidates(_principal(), cursor="invalid")


def test_database_reader_verifies_cursor_and_row_scope_before_returning() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    row = candidate.model_dump()
    row["entity_ref"] = ENTITY
    row["business_unit_ref"] = "unit-demo-a"
    session = _Session(row)
    signer = ReadCursorSigner("k" * 32)
    principal = _principal()
    token = signer.issue(
        principal,
        month=None,
        status=None,
        business_unit=None,
        horizon_sequence=7,
        horizon_hash=b"h" * 32,
        last_created_at=datetime(2026, 8, 23, tzinfo=UTC),
        last_candidate_id=UUID("30000000-0000-4000-8000-000000000001"),
    )
    page = DatabaseInternalReadService(lambda: cast(Session, session), signer).list_candidates(
        principal, cursor=token
    )
    assert len(page.items) == 1

    row["business_unit_ref"] = "unit-demo-b"
    with pytest.raises(InternalReadBackendUnavailable, match="scope binding"):
        DatabaseInternalReadService(lambda: cast(Session, _Session(row)), signer).list_candidates(
            principal
        )


def test_database_candidate_maps_canonical_channel_to_wire_contract() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    row = candidate.model_dump()
    row["source"] = {
        **row["source"],
        "ingest_channel": "controlled_upload",
        "source_event_ref": str(row["source"]["source_event_ref"]),
    }
    row["evidence"] = [
        {**item, "evidence_ref": str(item["evidence_ref"])} for item in row["evidence"]
    ]
    row["blockers"] = list(row["blockers"])

    projection = DatabaseInternalReadService._candidate(row)

    assert projection.source.ingest_channel.value == "CONTROLLED_UPLOAD"


def test_database_reader_scans_past_nonmatching_month_rows() -> None:
    template = SyntheticInternalReadService()._fixture.candidates[1].model_dump()
    rows = [dict(template) for _ in range(101)]
    for index, row in enumerate(rows):
        row["candidate_ref"] = UUID(f"30000000-0000-4000-8000-{index + 200:012d}")

    class PagedSession(_Session):
        calls = 0

        def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
            if "list_candidates_as_of" in str(statement):
                self.calls += 1
                return _Result(rows if self.calls == 1 else [])
            return super().execute(statement, params)

    session = PagedSession(rows[0])
    page = _service(session).list_candidates(_principal(), month="2026-09")
    assert page.items == ()
    assert session.calls == 2


def test_database_candidate_detail_follows_issued_cursors() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]

    class PagedService(DatabaseInternalReadService):
        calls = 0

        def list_candidates(self, principal: WorkloadPrincipal, **kwargs: Any) -> CandidatePage:
            self.calls += 1
            if self.calls == 1:
                return CandidatePage(items=(), next_cursor="next")
            return CandidatePage(items=(candidate,))

    service = PagedService(lambda: cast(Session, _Session({})), ReadCursorSigner("k" * 32))
    assert service.get_candidate(_principal(), candidate.candidate_ref) == candidate
