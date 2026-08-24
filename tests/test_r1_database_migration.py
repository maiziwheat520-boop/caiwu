from pathlib import Path

MIGRATION = Path("alembic/versions/20260824_0012_r1_candidate_evidence.py")
MIGRATION_B = Path("alembic/versions/20260824_0013_r1_ledger_reconciliation.py")


def test_r1_candidate_evidence_migration_is_forward_only_and_owner_written() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "20260824_0012"' in source
    assert 'down_revision: str | None = "20260824_0011"' in source
    for table in (
        "business_unit",
        "reporting_category",
        "evidence_object",
        "encrypted_blob_version",
        "candidate",
        "candidate_source",
        "candidate_revision",
        "candidate_event",
        "candidate_evidence",
    ):
        assert f'op.create_table(\n        "{table}"' in source
    assert "_append_only(table)" in source
    assert "REVOKE ALL ON TABLE" in source
    assert "GRANT INSERT" not in source
    assert "ledgerbridge_reader" in source
    assert "R1 Candidate/evidence data prevents destructive downgrade" in source


def test_r1_migration_pins_secretstream_and_candidate_scope_contracts() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for literal in (
        "ledgerbridge.secretstream.v1",
        "xchacha20poly1305-secretstream",
        "ledgerbridge-artifact-v2",
        "r1_validate_candidate_scope",
        "r1_validate_revision_dimensions",
        "candidate_revision_status_allowed",
        "candidate_event_audit_event",
    ):
        assert literal in source


def test_r1_migration_b_keeps_attribution_and_snapshot_facts_owner_written() -> None:
    source = MIGRATION_B.read_text(encoding="utf-8")
    assert 'revision: str = "20260824_0013"' in source
    assert 'down_revision: str | None = "20260824_0012"' in source
    for table in (
        "journal_entry_attribution",
        "posting_attribution",
        "reconciliation_snapshot",
        "reconciliation_snapshot_proposal",
        "reconciliation_snapshot_suspense",
    ):
        assert f'op.create_table(\n        "{table}"' in source
    assert "reconciliation_leg_primary_shape" in source
    assert "PRIMARY_LEG" in source
    assert "REVOKE ALL ON TABLE" in source
    assert "GRANT INSERT" not in source
    assert "v_posting_id := OLD.posting_id" in source
    assert "v_posting_id := NEW.posting_id" in source
    assert "COALESCE(NEW.posting_id, OLD.posting_id)" not in source
