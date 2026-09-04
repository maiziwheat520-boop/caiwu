from pathlib import Path

MIGRATION = Path("alembic/versions/20260904_0044_boc_company_xls_profile.py")


def test_0044_admits_only_the_exact_boc_company_xls_profile() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "20260904_0044"' in source
    assert 'down_revision: str | None = "20260904_0043"' in source
    assert "'boc_company_xls_v1'" in source
    assert "'boc_company_xls_export'" in source
    assert "'application/vnd.ms-excel'" in source
    assert "'abc_company_xls_v1',\n               'boc_company_xls_v1'" in source
    assert "v_account_owner_kind <> 'COMPANY'" in source
    assert "bank statement import function baseline changed" in source
    assert "forward-only" in source
