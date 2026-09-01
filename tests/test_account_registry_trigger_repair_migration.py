from __future__ import annotations

import runpy
from pathlib import Path

MIGRATION = Path("alembic/versions/20260901_0027_account_registry_trigger_record_fix.py")


def test_0027_repairs_shared_trigger_record_access_without_weakening_audit_binding() -> None:
    namespace = runpy.run_path(str(MIGRATION))
    source = MIGRATION.read_text(encoding="utf-8")

    assert namespace["revision"] == "20260901_0027"
    assert namespace["down_revision"] == "20260831_0026"
    assert "CREATE OR REPLACE FUNCTION public.account_registry_validate_fact()" in source
    assert "v_new := to_jsonb(NEW);" in source
    assert "NEW.audit_event_id" not in source
    assert "NEW.assignment_ref" not in source
    assert "NEW.request_sha256" not in source
    assert "SECURITY DEFINER SET search_path = pg_catalog" in source
    assert "v_audit.rule_version IS DISTINCT FROM 'ledgerbridge.account-registry.v1'" in source
    assert "USING ERRCODE = 'integrity_constraint_violation'" in source
    assert "REVOKE ALL ON FUNCTION public.account_registry_validate_fact()" in source
