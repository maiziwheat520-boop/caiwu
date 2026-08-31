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
    lock = source.index("ORDER BY candidate.id FOR UPDATE")
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
    assert "candidate classification batch member, risk signature, or group key drifted" in source
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
    grant = (
        "GRANT EXECUTE ON FUNCTION "
        "internal_command.replay_candidate_classification_batch(jsonb), "
        "internal_command.apply_candidate_classification_batch(jsonb) TO ledgerbridge_api;"
    )
    assert grant in source
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
    assert "render_candidate_classification_batch_receipt(v_operation_id, true)" in source


def test_batch_key_and_risk_are_recomputed_inside_the_locked_transaction() -> None:
    source = _source()

    assert "candidate_classification_group_key" in source
    assert "candidate_classification_risk_signature" in source
    assert "ledgerbridge.classification-key.v1" in source
    assert "v_member_group->>'group_ref' = v_group_ref" in source
    assert "target_business_unit_id = ANY(v_authorized_business_units)" in source
    assert "(v_member->>'target_business_unit_id')::uuid = v_target_business_unit_id" in source
    assert "current.business_unit_id = ANY(v_authorized_business_units)" in source
    assert "unit.retired_at IS NULL" in source
    assert "category.retired_at IS NULL" in source
    assert (
        "v_member_group#>'{conditions,risk_signature}' = to_jsonb(v_acknowledged_risks)"
    ) in source
    assert "acknowledged_risk_codes varchar(64)[] NOT NULL" in source
    assert "'acknowledged_risk_codes', to_jsonb(batch.acknowledged_risk_codes)" in source
    assert "'acknowledged_risk_codes', to_jsonb(v_acknowledged_risks)" in source
