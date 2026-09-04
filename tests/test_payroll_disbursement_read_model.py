from __future__ import annotations

from pathlib import Path
from uuid import UUID

from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.payroll_disbursement_read_model import (
    DatabasePayrollDisbursementReadModel,
)
from scripts.backup_restore import (
    COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_EXECUTORS,
    COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_SIGNATURES,
    MYBANK_CUTOVER_SCHEMA_REVISIONS,
)

MIGRATION = Path("alembic/versions/20260905_0046_payroll_disbursement_read_model.py")
ENTITY_REF = UUID("10000000-0000-4000-8000-000000000001")


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="payroll-reader",
        san_uri="spiffe://ledgerbridge.test/payroll-reader",
        policy_generation=1,
        capabilities=frozenset({Capability.PAYROLL_LIVE_READ}),
        grants=(EntityGrant(entity_ref=ENTITY_REF, allow_unassigned_candidates=True),),
    )


def test_migration_exposes_a_narrow_source_backed_projection() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260905_0046"' in source
    assert 'down_revision: str | None = "20260904_0045"' in source
    assert "CREATE FUNCTION internal_read.list_payroll_disbursement_records_as_of" in source
    assert "current.category_code = 'PAYROLL'" in source
    assert "AT TIME ZONE 'Asia/Shanghai'" in source
    assert "interval '1 month'" in source
    assert "'parse_status', 'PARSED'" in source
    assert "'link_status'" in source
    assert "transaction.counterparty_account" in source
    assert "counterparty_account_masked" in source
    assert "'payable', false" in source
    assert "'submission_supported', false" in source
    assert "TO ledgerbridge_reader" in source
    assert "20260905_0046" in MYBANK_CUTOVER_SCHEMA_REVISIONS
    key = ("internal_read", "list_payroll_disbursement_records_as_of")
    assert key in COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_SIGNATURES
    assert COMPANY_TRANSACTION_CLASSIFICATION_FUNCTION_EXECUTORS[key] == "ledgerbridge_reader"


def test_reader_returns_persisted_records_without_source_file_parsing() -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []
    row = {
        "record_ref": "20000000-0000-4000-8000-000000000001",
        "entity_ref": str(ENTITY_REF),
        "company_name": "测试公司",
        "pay_period": "2026-07",
        "occurred_at": "2026-07-15T09:00:00+08:00",
        "actual_amount_minor": 550000,
        "direction": "OUTFLOW",
        "currency": "CNY",
        "source_channel": "MYBANK",
        "source_system": "mybank_company_statement",
        "source_artifact_ref": "30000000-0000-4000-8000-000000000001",
        "source_statement_ref": "40000000-0000-4000-8000-000000000001",
        "source_row_number": 9,
        "ingested_at": "2026-08-01T01:00:00Z",
        "managed_account_ref": "50000000-0000-4000-8000-000000000001",
        "disbursement_account_masked": "****7968",
        "counterparty_name": "批量代发",
        "counterparty_account_masked": None,
        "transaction_name": "批量代发",
        "classification_revision": 1,
        "classification_source": "AUTO_RULE",
        "classification_rule_version": "company-bank-classification.2026-09.v1",
        "period_assignment_source": "NEXT_MONTH_RULE",
        "period_assignment_rule_version": "payroll-next-month-disbursement.2026-09.v1",
        "parse_status": "PARSED",
        "link_status": "UNMATCHED",
        "payable": False,
        "submission_supported": False,
    }

    class Result:
        def __init__(self, values: list[dict[str, object]]) -> None:
            self.values = values

        def mappings(self) -> Result:
            return self

        def first(self) -> dict[str, object] | None:
            return self.values[0] if self.values else None

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(self.values)

    class Session:
        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement, params=None):  # type: ignore[no-untyped-def]
            sql = str(statement)
            calls.append((sql, params))
            if "current_audit_horizon" in sql:
                return Result([{"sequence": 12, "hash": b"h" * 32}])
            return Result([{"item": row}])

    service = DatabasePayrollDisbursementReadModel(lambda: Session())  # type: ignore[arg-type]
    page = service.list_for_period(
        _principal(),
        pay_period="2026-07",
        source_entity_refs=(ENTITY_REF,),
    )

    assert page.record_count == 1
    assert page.source_artifact_count == 1
    assert page.unmatched_count == 1
    assert page.records[0].counterparty_name == "批量代发"
    assert page.records[0].counterparty_account_masked is None
    query, parameters = calls[-1]
    assert "list_payroll_disbursement_records_as_of" in query
    assert parameters == {
        "entity_ref": ENTITY_REF,
        "pay_period": "2026-07",
        "sequence": 12,
        "horizon_hash": b"h" * 32,
    }
