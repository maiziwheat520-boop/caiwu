from pathlib import Path

from scripts.backup_restore import (
    BANK_STATEMENT_FUNCTION_RESULTS,
    BANK_STATEMENT_FUNCTION_SIGNATURES,
    BANK_STATEMENT_SECURITY_DEFINER_FUNCTIONS,
    BANK_STATEMENT_TRIGGER_CONTRACT,
    COMPANY_AUTO_CLASSIFICATION_REVISION,
    MYBANK_CUTOVER_SCHEMA_REVISIONS,
)

MIGRATION = Path("alembic/versions/20260904_0040_company_auto_classification.py")
FUNCTION = ("internal_import", "auto_classify_confirmed_company_statement")


def test_rules_are_explicit_fail_closed_and_post_confirmation() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision = "20260904_0039"' in source
    assert "AFTER INSERT ON public.bank_statement_review" in source
    assert "NEW.status <> 'CONFIRMED'" in source
    assert "account.owner_kind = 'COMPANY'" in source
    assert "%陈明哲%" in source
    assert "%企业代发过渡户%" in source and "%批量代发%" in source
    assert "%浙江网商银行%" in source and "%贷款还款%" in source
    assert "v_matches > 1" in source
    assert "seed_company_transaction_classification" in source
    assert "company-bank-classification.2026-09.v1" in source


def test_backup_restore_contract_covers_auto_classification() -> None:
    assert COMPANY_AUTO_CLASSIFICATION_REVISION in MYBANK_CUTOVER_SCHEMA_REVISIONS
    assert BANK_STATEMENT_FUNCTION_SIGNATURES[FUNCTION] == ""
    assert BANK_STATEMENT_FUNCTION_RESULTS[FUNCTION] == "trigger"
    assert FUNCTION in BANK_STATEMENT_SECURITY_DEFINER_FUNCTIONS
    assert BANK_STATEMENT_TRIGGER_CONTRACT["auto_classify_confirmed_company_statement"] == (
        "bank_statement_review",
        False,
        5,
        False,
        False,
        "auto_classify_confirmed_company_statement",
    )
