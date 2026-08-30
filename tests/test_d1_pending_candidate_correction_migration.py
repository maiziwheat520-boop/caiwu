from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

MIGRATION = Path("alembic/versions/20260830_0022_pending_candidate_corrections.py")
CLOSURE_MIGRATION = Path("alembic/versions/20260824_0014_r1_fact_hardening.py")


def _migration_namespace() -> dict[str, Any]:
    return runpy.run_path(str(MIGRATION))


def test_0022_is_forward_migration_after_0021_and_has_precise_downgrade_guard() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "20260830_0022"' in source
    assert 'down_revision: str | None = "20260830_0021"' in source
    assert "WHERE action = 'CORRECT_AND_CONFIRM'" in source
    assert "AND from_status = 'PENDING'" in source
    assert "AND to_status = 'CONFIRMED'" in source
    assert "pending candidate correction events prevent destructive downgrade" in source
    precise_guard = source.index(
        "pending candidate correction events prevent destructive downgrade"
    )
    fact_guard = source.index("nonempty R1 fact database prevents destructive downgrade")
    assert precise_guard < fact_guard
    for relation in (
        "public.audit_event",
        "public.business_unit",
        "public.reporting_category",
        "public.evidence_object",
        "public.encrypted_blob_version",
        "public.candidate",
        "public.candidate_source",
        "public.candidate_revision",
        "public.candidate_blocker",
        "public.candidate_event",
        "public.candidate_field_change",
        "public.candidate_conflict_resolution",
        "public.candidate_evidence",
        "public.encrypted_object_identity",
        "public.journal_entry_attribution",
        "public.posting_attribution",
        "public.reconciliation_snapshot",
        "public.reconciliation_snapshot_blocker",
        "public.reconciliation_snapshot_proposal",
        "public.reconciliation_snapshot_suspense",
        "public.reconciliation_leg",
        "internal_read.evidence_read_receipt",
        "internal_import.controlled_batch_receipt",
        "public.candidate_evidence_link",
        "internal_import.hotel_payout_cutover_receipt",
        "public.counterparty_identity",
        "public.counterparty_classification",
        "public.candidate_counterparty",
        "public.managed_account",
        "public.managed_account_lifecycle",
        "public.bank_statement",
        "public.bank_statement_transaction",
        "public.bank_statement_observation",
        "public.bank_statement_review",
    ):
        assert f"SELECT 1 FROM {relation}" in source[precise_guard:fact_guard]


def test_0022_replaces_live_closure_validator_in_both_directions() -> None:
    namespace = _migration_namespace()
    render = namespace["_candidate_closure_sql"]
    upgrade_sql = render(allow_pending_corrections=True)
    downgrade_sql = render(allow_pending_corrections=False)
    installed_closure = CLOSURE_MIGRATION.read_text(encoding="utf-8")

    assert "pg_get_functiondef" in upgrade_sql
    assert "public.r1_check_candidate_closure(uuid,uuid)" in upgrade_sql
    assert "EXECUTE v_definition" in upgrade_sql
    for old, new in (
        (
            "AND v_event.action = 'CONFIRM')",
            "AND v_event.action IN ('CONFIRM','CORRECT_AND_CONFIRM'))",
        ),
        (
            "IF v_event.event_type = 'COMPLETE_FIELDS'\n"
            "                       AND v_normalized_changes < 1 THEN",
            "IF v_event.event_type IN ('COMPLETE_FIELDS','CORRECT_AND_CONFIRM')\n"
            "                       AND v_normalized_changes < 1 THEN",
        ),
        (
            "IF v_event.event_type IN ('COMPLETE_FIELDS','CONFIRM','IGNORE','SUPERSEDE')",
            "IF v_event.event_type IN "
            "('COMPLETE_FIELDS','CORRECT_AND_CONFIRM','CONFIRM','IGNORE','SUPERSEDE')",
        ),
    ):
        assert installed_closure.count(old) == 1
        assert old in upgrade_sql and new in upgrade_sql
        assert new in downgrade_sql and old in downgrade_sql


def test_0022_command_and_event_sql_keep_terminal_states_closed() -> None:
    namespace = _migration_namespace()
    command_sql = namespace["_command_functions_sql"](allow_pending_corrections=True)
    legacy_sql = namespace["_command_functions_sql"](allow_pending_corrections=False)
    constraint_sql = namespace["_event_constraints_sql"](allow_pending_corrections=True)

    assert "FROM pg_catalog.pg_constraint AS c" in constraint_sql
    assert "c.conkey = ARRAY[" in constraint_sql
    assert "v_type_count IS DISTINCT FROM 1" in constraint_sql
    assert "v_action_count IS DISTINCT FROM 1" in constraint_sql
    assert constraint_sql.count("DROP CONSTRAINT %I") == 2
    assert constraint_sql.count("ADD CONSTRAINT %I CHECK") == 2
    assert "DROP CONSTRAINT candidate_event_type_allowed" not in constraint_sql
    assert "DROP CONSTRAINT candidate_event_action_allowed" not in constraint_sql
    assert "WHEN 'CORRECT_AND_CONFIRM' THEN 'CONFIRMED'" in command_sql
    assert "p_action = 'CORRECT_AND_CONFIRM' AND v_previous.status = 'PENDING'" in command_sql
    assert "pending correction must change a normalized field" in command_sql
    assert "ELSIF v_current.status = 'PENDING' THEN" in command_sql
    assert "only open candidates can be corrected" in command_sql
    assert "bu.retired_at IS NULL" in command_sql
    assert "rc.retired_at IS NULL" in command_sql
    assert "active accounting dimension labels require registry governance" in command_sql
    assert "USING ERRCODE = 'LB005'" in command_sql
    assert command_sql.count("final business unit is not an active candidate dimension") == 1
    assert command_sql.count("final category is not an active candidate dimension") == 1
    assert "p_action = 'CORRECT_AND_CONFIRM' AND v_previous.status = 'PENDING'" not in legacy_sql
    assert "bu.retired_at IS NULL" not in legacy_sql
    assert constraint_sql.count("'CORRECT_AND_CONFIRM'") == 2


def test_0022_accounting_dimensions_are_scoped_active_and_reader_only() -> None:
    namespace = _migration_namespace()
    sql = namespace["_accounting_dimensions_sql"](install=True)

    assert "ledgerbridge.accounting-dimensions.v1" in sql
    assert "p_business_unit_ids uuid[]" in sql
    assert "p_business_unit_refs varchar[]" in sql
    assert "bu.id = bindings.id" in sql
    assert "bu.ref = bindings.ref" in sql
    assert "bu.retired_at IS NULL" in sql
    assert "rc.retired_at IS NULL" in sql
    assert "accounting dimension category limit exceeded" in sql
    assert "GROUP BY bu.label HAVING count(*) > 1" in sql
    assert "GROUP BY rc.label HAVING count(*) > 1" in sql
    assert "USING ERRCODE = 'LB005'" in sql
    assert "SECURITY DEFINER SET search_path = pg_catalog" in sql
    assert "FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker" in sql
    assert "'ledgerbridge_app','ledgerbridge_backup'" in sql
    assert "TO ledgerbridge_reader" in sql
