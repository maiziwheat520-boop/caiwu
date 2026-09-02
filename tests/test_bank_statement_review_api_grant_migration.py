from __future__ import annotations

from pathlib import Path


MIGRATION = Path("alembic/versions/20260902_0035_bank_statement_review_api_grant.py")


def test_bank_statement_review_command_is_granted_only_to_api_role() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "Revises: 20260902_0034" in source
    assert "GRANT EXECUTE ON FUNCTION" in source
    assert "TO ledgerbridge_api" in source
    assert "TO ledgerbridge_worker" not in source
    assert "TO ledgerbridge_reader" not in source
    assert "TO PUBLIC" not in source
    assert "REVOKE EXECUTE ON FUNCTION" in source
