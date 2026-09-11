from pathlib import Path

MIGRATION = Path("alembic/versions/20260906_0051_boc_company_csv_pdf_profiles.py")


def test_0051_admits_only_the_exact_boc_company_csv_and_pdf_profiles() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "20260906_0051"' in source
    assert 'down_revision: str | None = "20260906_0050"' in source
    assert "'boc_company_csv_v1'" in source
    assert "'boc_company_csv_export'" in source
    assert "'text/csv'" in source
    assert "'boc_company_pdf_v1'" in source
    assert "'boc_company_pdf_statement'" in source
    assert "'application/pdf'" in source
    assert (
        "'boc_company_xls_v1',\n               'boc_company_csv_v1',\n"
        "               'boc_company_pdf_v1'" in source
    )
    assert "v_account_owner_kind <> 'COMPANY'" in source
    assert "bank statement import function baseline changed" in source
    assert "forward-only" in source
