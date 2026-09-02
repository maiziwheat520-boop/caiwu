from pathlib import Path

from scripts.backup_restore import (
    BANK_STATEMENT_SECURITY_SQL,
    CASH_RECONCILIATION_FUNCTION_KEYS,
    CASH_RECONCILIATION_REVISION,
    CASH_RECONCILIATION_TABLES,
    CASH_RECONCILIATION_TRIGGER_NAMES,
    MYBANK_CUTOVER_SCHEMA_REVISIONS,
)

MIGRATION = Path("alembic/versions/20260903_0036_cash_reconciliation_rules.py")


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
