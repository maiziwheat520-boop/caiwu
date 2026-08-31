from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

MIGRATION = Path("alembic/versions/20260831_0026_candidate_classification_batches.py")


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _namespace() -> dict[str, Any]:
    return runpy.run_path(str(MIGRATION))


def test_0026_is_the_linear_forward_only_batch_boundary() -> None:
    namespace = _namespace()
    source = _source()

    assert namespace["revision"] == "20260831_0026"
    assert namespace["down_revision"] == "20260830_0025"
    assert "candidate classification batch facts prevent destructive downgrade" in source
    assert "DROP TABLE internal_command.candidate_classification_batch_receipt" in source


def test_batch_command_preflights_and_locks_every_member_before_writing() -> None:
    source = _source()
    lock = source.index("ORDER BY candidate.id\n             FOR UPDATE")
    preflight = source.index("Complete the read-only preflight for every member")
    write = source.index("Calls remain inside this transaction")
    member_command = source.index(
        "internal_command.apply_candidate_decision(",
        write,
    )
    batch_receipt = source.index(
        "INSERT INTO internal_command.candidate_classification_batch_receipt",
        member_command,
    )

    assert lock < preflight < member_command < batch_receipt
    assert "current.revision = (v_member->>'expected_revision')::integer" in source
    assert "current.status = 'PENDING'" in source
    assert "current.accounting_month = v_accounting_month" in source
    assert "candidate classification batch member or group key drifted" in source
    assert "GET DIAGNOSTICS v_count = ROW_COUNT" in source


def test_batch_receipts_and_assertion_bindings_are_append_only_and_closed() -> None:
    source = _source()

    for table in (
        "candidate_classification_batch_receipt",
        "candidate_classification_batch_member",
        "candidate_classification_batch_assertion_use",
    ):
        assert f"CREATE TABLE internal_command.{table}" in source
    assert source.count("EXECUTE FUNCTION internal_command.reject_mutation()") == 3
    assert "SECURITY DEFINER SET search_path = pg_catalog" in source
    assert (
        "GRANT EXECUTE ON FUNCTION\n"
        "            internal_command.apply_candidate_classification_batch(jsonb)\n"
        "            TO ledgerbridge_api;"
    ) in source
    assert "FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker" in source
    assert "candidate.classification.batch" in source


def test_batch_replay_binds_actor_content_members_and_cross_surface_jti() -> None:
    source = _source()

    assert "v_receipt.request_fingerprint IS DISTINCT FROM v_request_fingerprint" in source
    assert "v_receipt.member_operation_ids IS DISTINCT FROM v_member_operations" in source
    assert "FROM internal_command.candidate_assertion_use" in source
    assert "candidate_classification_batch_assertion_use" in source
    assert "candidate_assertion_cross_surface_guard" in source
    assert "candidate_classification_batch_assertion_cross_surface_guard" in source
    assert "assertion JTI was reused across command surfaces" in source
    assert (
        "render_candidate_classification_batch_receipt(\n                    v_operation_id, true"
    ) in source
