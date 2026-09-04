from __future__ import annotations

from pathlib import Path

MIGRATION = Path("alembic/versions/20260904_0045_blank_counterparty_overlap.py")


def test_0045_only_relaxes_the_derived_ref_for_two_blank_counterparties() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260904_0045"' in source
    assert 'down_revision: str | None = "20260904_0044"' in source
    assert source.count("v_transaction.counterparty_name IS NULL") == 2
    assert source.count("nullif(v_item->>'counterparty_name','') IS NULL") == 2
    assert "v_transaction.occurred_at IS DISTINCT FROM v_occurred_at" in source
    assert "v_transaction.amount_minor IS DISTINCT FROM v_amount_minor" in source
    assert "v_transaction.balance_minor IS DISTINCT FROM v_balance_minor" in source
    assert "v_transaction.transaction_name" not in source
    assert "bank statement overlap baseline changed" in source
    assert "REVOKE ALL ON FUNCTION internal_import.import_bank_statement(jsonb)" in source
    assert "raise RuntimeError" in source
