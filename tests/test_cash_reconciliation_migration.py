from pathlib import Path

from scripts.backup_restore import MYBANK_CUTOVER_SCHEMA_REVISIONS

MIGRATION = Path("alembic/versions/20260903_0036_cash_reconciliation_rules.py")


def test_0036_is_supported_by_backup_inventory() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260903_0036"' in source
    assert 'down_revision: str | None = "20260902_0035"' in source
    assert "20260903_0036" in MYBANK_CUTOVER_SCHEMA_REVISIONS
