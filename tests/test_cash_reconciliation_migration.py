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
