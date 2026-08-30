from __future__ import annotations

import re
import runpy
from pathlib import Path

from scripts.backup_restore import (
    EVIDENCE_UNLOCK_FUNCTION_SIGNATURES,
    EVIDENCE_UNLOCK_SECURITY_REVISION,
    EVIDENCE_UNLOCK_SECURITY_SQL,
    EVIDENCE_UNLOCK_TABLES,
)

ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "20260830_0025_evidence_unlock.py"
MIGRATION_0019 = ROOT / "alembic" / "versions" / "20260829_0019_candidate_evidence_links.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _sql() -> str:
    return re.sub(r"\s+", " ", _source()).lower()


def test_0025_is_the_linear_evidence_unlock_revision() -> None:
    namespace = runpy.run_path(str(MIGRATION))

    assert namespace["revision"] == "20260830_0025"
    assert namespace["down_revision"] == "20260830_0024"
    assert EVIDENCE_UNLOCK_SECURITY_REVISION == "20260830_0025"


def test_0025_persists_only_append_only_non_secret_unlock_facts() -> None:
    sql = _sql()

    for relation in EVIDENCE_UNLOCK_TABLES:
        assert relation in sql
    for relation in (
        "internal_import.evidence_unlock_source",
        "internal_command.evidence_unlock_operation",
        "internal_command.evidence_unlock_receipt",
        "internal_command.evidence_unlock_output",
    ):
        assert relation in sql
    assert "evidence_unlock_reject_mutation" in sql
    assert sql.count("before update or delete") >= 4
    assert "prepared_audit_event_id" in sql
    assert "completion_audit_event_id" in sql
    assert "reviewed_audit_event_id" in sql
    for forbidden in (
        "password varchar",
        "password text",
        "body_sha256",
        "password_hash",
        "password_verifier",
    ):
        assert forbidden not in sql


def test_0025_command_surface_is_closed_scoped_and_idempotent() -> None:
    sql = _sql()

    for function in (
        "internal_command.prepare_evidence_unlock",
        "internal_command.complete_evidence_unlock",
        "internal_command.reject_evidence_unlock",
    ):
        assert function in sql
        assert f"{function}(jsonb)" in sql
    assert "scope_bindings" in sql
    assert "jsonb_array_length" in sql
    assert "assertion_jti" in sql
    assert "replay_unlocked" in sql
    assert "replay_rejected" in sql
    assert "using errcode = 'lb004'" in sql
    assert "using errcode = 'lb005'" in sql
    assert "using errcode = 'lb006'" in sql
    assert "for update" in sql
    assert "public.append_audit_event" in sql


def test_0025_completion_atomically_registers_only_encrypted_evidence() -> None:
    sql = _sql()

    for relation in (
        "public.evidence_object",
        "public.encrypted_object_identity",
        "public.encrypted_blob_version",
    ):
        assert f"insert into {relation}" in sql
    assert "'evidence.object.create'" in sql
    assert "'evidence.blob.version'" in sql
    assert "'rotation_mode', 'genesis'" in sql
    assert "ledgerbridge.secretstream.v1" in sql
    assert "xchacha20poly1305-secretstream" in sql
    assert "ledgerbridge-artifact-v2" in sql
    assert "decode(v_item->>'plaintext_sha256', 'hex')" in sql
    assert "decode(v_item->>'ciphertext_sha256', 'hex')" in sql
    assert "source_record_id" in sql and "raw_artifact_id" in sql
    assert "v_output_facts" in sql


def test_0025_projection_is_authoritative_at_the_requested_audit_horizon() -> None:
    sql = _sql()

    assert "rename to list_candidates_base_as_of" in sql
    assert "rename to render_candidate_revision_base" in sql
    assert "internal_read.project_evidence_unlocks" in sql
    assert "'unlock_status'" in sql
    assert "'not_required'" in sql
    assert "'password_required'" in sql
    assert "'unlocked'" in sql
    assert "'source_ref'" in sql
    assert "reviewed.sequence <= p_audit_horizon_sequence" in sql
    assert "completed.sequence <= p_audit_horizon_sequence" in sql
    assert "'kind', 'attachment'" in sql
    assert "'download_available', true" in sql
    assert (
        "public.candidate_evidence"
        not in sql.split("create function internal_read.project_evidence_unlocks", maxsplit=1)[
            1
        ].split("$function$;", maxsplit=1)[0]
    )


def test_0025_grants_only_reader_projection_and_api_commands() -> None:
    sql = _sql()

    assert "security definer set search_path = pg_catalog" in sql
    assert "revoke all on all tables in schema internal_import" in sql
    assert "revoke all on all tables in schema internal_command" in sql
    assert "from public, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker" in sql
    for function in (
        "prepare_evidence_unlock",
        "complete_evidence_unlock",
        "reject_evidence_unlock",
    ):
        assert re.search(
            rf"grant execute on function internal_command\.{function}\(jsonb\) "
            r"to ledgerbridge_api",
            sql,
        )
    assert "list_candidates_as_of" in EVIDENCE_UNLOCK_FUNCTION_SIGNATURES
    assert "render_candidate_revision_base" in EVIDENCE_UNLOCK_FUNCTION_SIGNATURES


def test_0025_downgrade_is_production_closed_and_fact_guarded() -> None:
    source = _source()
    sql = _sql()

    assert "LEDGERBRIDGE_ENV" in source
    assert "production evidence unlock downgrade is forbidden" in source
    assert "evidence unlock facts prevent destructive downgrade" in source
    assert "drop function internal_read.list_candidates_as_of" in sql
    assert "rename to list_candidates_as_of" in sql
    assert "rename to render_candidate_revision" in sql


def test_backup_restore_closes_the_0025_security_boundary() -> None:
    backup_source = (ROOT / "scripts" / "backup_restore.py").read_text(encoding="utf-8")

    for key in (
        "evidence_unlock_row_counts",
        "evidence_unlock_tables",
        "evidence_unlock_functions",
        "evidence_unlock_triggers",
        "evidence_unlock_table_acls",
        "evidence_unlock_function_acls",
        "evidence_unlock_effective_table_privileges",
        "evidence_unlock_effective_function_privileges",
    ):
        assert key in EVIDENCE_UNLOCK_SECURITY_SQL
    assert "if revision >= EVIDENCE_UNLOCK_SECURITY_REVISION" in backup_source
    assert "_validate_evidence_unlock_security(metadata)" in backup_source


def test_linear_chain_0019_downgrade_removes_triggers_before_trigger_functions() -> None:
    source = MIGRATION_0019.read_text(encoding="utf-8")

    assert source.index('op.drop_table("hotel_payout_cutover_receipt"') < source.index(
        '"DROP FUNCTION IF EXISTS internal_import.hotel_payout_cutover_receipt_append_only()"'
    )
    assert source.index('op.drop_table("candidate_evidence_link")') < source.index(
        'op.execute("DROP FUNCTION IF EXISTS public.r1_validate_candidate_evidence_link()")'
    )
