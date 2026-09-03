from pathlib import Path

MIGRATION = Path("alembic/versions/20260904_0041_boc_projection_repair_v2.py")


def test_repair_is_append_only_audited_and_does_not_mutate_source_facts() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "bank_statement_transaction_projection_correction" in source
    assert "internal_import.repair_boc_statement_projection" in source
    assert "ledgerbridge.bank-statement-projection-correction.v2" in source
    assert "CREATE TRIGGER bank_statement_transaction_projection_correction_append_only" in source
    assert "UPDATE public.bank_statement_transaction" not in source
    assert "DELETE FROM public.bank_statement_transaction" not in source


def test_repair_validates_every_source_row_before_writing() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "jsonb_array_length(p_request->'transactions')" in source
    assert "source_row_sha256" in source
    assert "occurred_at" in source
    assert "amount_minor" in source
    assert "balance_minor" in source
    assert "BOC projection repair source facts conflict" in source
    assert "BOC projection repair row set is incomplete" in source


def test_reader_prefers_v2_then_v1_then_immutable_fact() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "projection.counterparty_name" in source
    assert "correction.counterparty_name" in source
    assert "projection.transaction_serial" in source
    assert "projection.transaction_name" in source
    assert "projection_audit.sequence <= p_audit_horizon_sequence" in source
    assert "AND (projection_audit.sequence IS NULL" not in source


def test_repair_is_idempotent_and_runtime_roles_cannot_call_it() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "command_sha256" in source
    assert "created boolean" in source
    assert "FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker" in source
