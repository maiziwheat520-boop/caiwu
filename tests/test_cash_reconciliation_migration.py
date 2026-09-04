from pathlib import Path

from scripts.backup_restore import (
    BANK_STATEMENT_SECURITY_SQL,
    CASH_RECONCILIATION_FUNCTION_KEYS,
    CASH_RECONCILIATION_REVISION,
    CASH_RECONCILIATION_TABLES,
    CASH_RECONCILIATION_TRIGGER_NAMES,
    CASH_RECONCILIATION_V2_FUNCTION_KEYS,
    CASH_RECONCILIATION_V2_REVISION,
    MYBANK_CUTOVER_SCHEMA_REVISIONS,
)

MIGRATION = Path("alembic/versions/20260903_0036_cash_reconciliation_rules.py")
V2_MIGRATION = Path("alembic/versions/20260903_0038_cash_reconciliation_v2.py")
CLASSIFICATION_SOURCE_MIGRATION = Path(
    "alembic/versions/20260904_0042_cash_reconciliation_classification_source.py"
)


def test_0036_is_supported_by_backup_inventory() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260903_0036"' in source
    assert 'down_revision: str | None = "20260902_0035"' in source
    assert CASH_RECONCILIATION_REVISION in MYBANK_CUTOVER_SCHEMA_REVISIONS
    assert set(CASH_RECONCILIATION_TABLES) == {
        "cash_reconciliation_rule",
        "cash_reconciliation_adjustment",
    }
    assert {
        ("internal_read", "cash_reconciliation_rules_v1"),
        ("internal_read", "cash_reconciliation_month_v1"),
    } == CASH_RECONCILIATION_FUNCTION_KEYS
    assert {
        "cash_reconciliation_rule_append_only",
        "cash_reconciliation_adjustment_append_only",
    } == CASH_RECONCILIATION_TRIGGER_NAMES
    assert all(table in BANK_STATEMENT_SECURITY_SQL for table in CASH_RECONCILIATION_TABLES)


def test_cash_reconciliation_v2_is_scoped_and_reports_excluded_facts() -> None:
    source = V2_MIGRATION.read_text(encoding="utf-8")

    assert f'revision: str = "{CASH_RECONCILIATION_V2_REVISION}"' in source
    assert 'down_revision: str | None = "20260903_0037"' in source
    assert CASH_RECONCILIATION_V2_REVISION in MYBANK_CUTOVER_SCHEMA_REVISIONS
    assert {
        ("internal_read", "cash_reconciliation_month_v2")
    } == CASH_RECONCILIATION_V2_FUNCTION_KEYS
    assert "account.entity_id = ANY(p_entity_ids)" in source
    assert "candidate.entity_id = ANY(p_entity_ids)" in source
    assert "latest.business_unit_id = ANY(p_business_unit_ids)" in source
    assert "fact.occurred_on >= rule.effective_from" in source
    assert "WHERE counts.match_count = 1" in source
    assert "'unmatched_fact_count'" in source
    assert "'conflicted_fact_count'" in source
    assert "'issues_truncated'" in source
    assert "LIMIT 500" in source
    assert "GRANT EXECUTE ON FUNCTION internal_read.cash_reconciliation_month_v2" in source


def test_company_cash_reconciliation_uses_confirmed_classifications_as_single_source() -> None:
    source = CLASSIFICATION_SOURCE_MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260904_0042"' in source
    assert 'down_revision: str | None = "20260904_0041"' in source
    assert "CASH_RECONCILIATION_CLASSIFICATION_REVISION" in Path(
        "scripts/backup_restore.py"
    ).read_text(encoding="utf-8")
    assert "account.owner_kind = 'PERSONAL'" in source
    assert "account.owner_kind = 'COMPANY'" in source
    assert "public.company_transaction_classification" in source
    assert "ADD COLUMN reporting_item_code" in source
    assert "backfill_company_transaction_reporting_item" in source
    assert "'BACKFILL'" in source
    assert "classification_status = 'CONFIRMED'" in source
    assert "assignment.business_unit_id = ANY(p_business_unit_ids)" in source
    assert "company_transaction_classification:" in source
    assert "WHEN 'FLIGGY' THEN '飞猪'" in source
    assert "WHEN 'WENJIE_RENT' THEN '文杰房租'" in source
    assert "WHEN 'PLATFORM_ROOM_REVENUE' THEN 'INCOME'" in source
    assert "WHEN 'PAYROLL' THEN 'EXPENSE'" in source
    assert "WHEN 'INTERNAL_TRANSFER' THEN 'CURRENT'" in source
    assert "FROM company_universe company" in source
    assert "ARRAY[]::varchar[]" in source
