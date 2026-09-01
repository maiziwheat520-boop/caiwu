from pathlib import Path

MIGRATION = Path("alembic/versions/20260902_0030_generic_bank_statement_import.py")


def test_0030_replaces_only_the_registered_account_import_seam() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260902_0030"' in source
    assert 'down_revision: str | None = "20260901_0028"' in source
    assert (
        "CREATE OR REPLACE FUNCTION internal_import.import_bank_statement(p_request jsonb)"
        in source
    )
    assert "SECURITY DEFINER SET search_path = pg_catalog" in source
    assert "internal_import.import_bank_statement_0021" not in source
    assert "INSERT INTO public.managed_account" not in source
    assert "managed account must be registered before statement import" in source
    assert "TO ledgerbridge_worker" in source
    assert "ledgerbridge_reader, ledgerbridge_api, ledgerbridge_app" in source


def test_0030_profile_allowlist_is_closed_and_ccb_is_person_only() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    for value in (
        "mybank_xlsx_v1",
        "mybank_xlsx_export",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ccb_personal_xls_v1",
        "ccb_personal_xls_export",
        "application/vnd.ms-excel",
    ):
        assert value in source
    assert "bank statement parser profile is invalid" in source
    assert "v_account_owner_kind <> 'PERSONAL'" in source
    assert "coalesce(p_request->>'institution_code','') ~" not in source


def test_0030_keeps_statement_facts_review_and_candidates_contract() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "bank statement evidence binding is invalid" in source
    assert "bank statement replay conflicts with persisted facts" in source
    assert "overlapping bank statement transaction conflicts with fact" in source
    assert "'status', 'PENDING'" in source
    assert "'accounting_candidate_count', 0" in source
    assert "generic bank statement imports are forward-only" in source
