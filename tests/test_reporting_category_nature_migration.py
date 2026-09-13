from pathlib import Path

MIGRATION = Path("alembic/versions/20260913_0052_reporting_category_nature.py")


def test_0052_adds_a_nullable_checked_nature_and_rebinds_existing_bodies() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "20260913_0052"' in source
    assert 'down_revision: str | None = "20260906_0051"' in source
    assert "ADD COLUMN nature varchar(16) NULL" in source
    assert "nature IS NULL OR nature IN ('INCOME','EXPENSE','TRANSFER')" in source
    # Only NULL -> value with every other column unchanged is admitted.
    assert "OLD.nature IS NULL" in source
    assert "NEW.nature IS NOT NULL" in source
    assert "(to_jsonb(NEW) - 'nature') = (to_jsonb(OLD) - 'nature')" in source
    assert "CREATE OR REPLACE FUNCTION public.r1_reporting_category_append_only()" in source
    # The dimensions function is patched from the catalog, not re-declared, so
    # SECURITY DEFINER, search_path, owner and grants are carried over.
    assert "pg_get_functiondef(to_regprocedure(" in source
    assert "CREATE FUNCTION internal_read" not in source
    assert "GRANT " not in source and "REVOKE " not in source
    assert "jsonb_build_object('code', rc.code, 'label', rc.label)" in source
    assert "jsonb_build_object('code', rc.code, 'label', rc.label, 'nature', rc.nature)" in source
    assert "accounting dimensions function baseline changed" in source
    assert "DROP COLUMN nature" in source
    assert "assigned reporting category natures prevent destructive downgrade" in source
