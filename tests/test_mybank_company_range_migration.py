from pathlib import Path

MIGRATION = Path("alembic/versions/20260902_0034_mybank_company_range_statement_profile.py")


def test_0034_admits_only_the_company_range_profile() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260902_0034"' in source
    assert 'down_revision: str | None = "20260902_0033"' in source
    assert "mybank_company_range_xlsx_v3" in source
    assert "mybank_company_statement" in source
    assert "v_account_owner_kind <> 'COMPANY'" in source
    assert "bank statement import function baseline changed" in source
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source


def test_0034_is_forward_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "generic bank statement imports are forward-only" in source
