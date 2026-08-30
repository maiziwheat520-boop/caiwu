from __future__ import annotations

import runpy
from pathlib import Path

from scripts.backup_restore import (
    ACCOUNT_REGISTRY_FUNCTION_SIGNATURES,
    ACCOUNT_REGISTRY_SECURITY_REVISION,
    ACCOUNT_REGISTRY_SECURITY_SQL,
    ACCOUNT_REGISTRY_TABLES,
)

MIGRATION_PATH = (
    Path(__file__).parents[1] / "alembic" / "versions" / "20260830_0023_account_owner_registry.py"
)


def test_0023_is_reserved_after_central_0022_and_closes_implicit_account_creation() -> None:
    namespace = runpy.run_path(str(MIGRATION_PATH))
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert namespace["revision"] == "20260830_0023"
    assert namespace["down_revision"] == "20260830_0022"
    assert "account_registry_operation" in source
    assert "managed_account_alias" in source
    assert "account_business_unit_assignment" in source
    assert "fact_business_unit_allocation_set" in source
    assert "internal_command.apply_account_registry_plan" in source
    assert "internal_read.get_account_registry_projection" in source
    assert "import_bank_statement_0021" in source
    assert "managed account must be registered before statement import" in source


def test_0023_derives_compatibility_owner_fields_from_entity() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "owner_ref = entity_id::text" in source
    assert "v_owner_kind := CASE v_entity_type" in source
    assert "expected_owner_kind" in source
    assert "admission_evidence_ref" in source
    assert "account registry alias already belongs to another account" in source
    assert "account business-unit assignment overlaps" in source
    assert "fact allocation must total 10000 basis points" in source
    assert "business_unit_ref_snapshot" in source
    assert "business_unit_label_snapshot" in source
    assert "ref = v_assignment_item->>'business_unit_ref_snapshot'" in source
    assert "account_registry_validate_business_unit_snapshot" in source


def test_backup_restore_contract_covers_the_0023_registry_surface() -> None:
    assert ACCOUNT_REGISTRY_SECURITY_REVISION == "20260830_0023"
    for table in ACCOUNT_REGISTRY_TABLES:
        assert f"FROM public.{table}" in ACCOUNT_REGISTRY_SECURITY_SQL
    for (schema, name), args in ACCOUNT_REGISTRY_FUNCTION_SIGNATURES.items():
        assert f"('{schema}', '{name}', '{args}')" in ACCOUNT_REGISTRY_SECURITY_SQL
