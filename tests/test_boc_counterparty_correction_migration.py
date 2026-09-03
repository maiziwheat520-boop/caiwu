from pathlib import Path

MIGRATION = Path("alembic/versions/20260904_0039_boc_counterparty_corrections.py")


def test_migration_preserves_source_facts_and_adds_audited_projection() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "bank_statement_transaction_correction" in source
    assert "bank_statement.transaction.correct" in source
    assert "BOC_PDF_COUNTERPARTY_COLUMN_SPILL" in source
    assert "LEFT JOIN public.bank_statement_transaction_correction" in source
    assert "UPDATE public.bank_statement_transaction" not in source
    assert "DELETE FROM public.bank_statement_transaction" not in source


def test_migration_backfill_is_tightly_scoped_to_proven_shape() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "account.institution_code = 'boc'" in source
    assert "account.owner_kind = 'PERSONAL'" in source
    assert "transaction.counterparty_name ~ ' 6$'" in source
    assert "transaction.counterparty_account ~ '^[0-9]{16,30}'" in source
