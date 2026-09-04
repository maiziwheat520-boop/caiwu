from pathlib import Path


def test_migration_admits_only_company_xls_profile_and_keeps_worker_boundary() -> None:
    source = Path("alembic/versions/20260904_0043_abc_company_xls_profile.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision: str | None = "20260904_0042"' in source
    assert "abc_company_xls_export" in source
    assert "abc_company_xls_v1" in source
    assert "'mybank_company_range_xlsx_v3',\n               'abc_company_xls_v1'" in source
    assert "TO ledgerbridge_worker" in source
    assert "generic bank statement imports are forward-only" in source
