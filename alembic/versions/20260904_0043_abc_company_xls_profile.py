"""Admit the Agricultural Bank company XLS parser profile.

Revision ID: 20260904_0043
Revises: 20260904_0042
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260904_0043"
down_revision: str | None = "20260904_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


_UPGRADE_SQL = r"""
DO $migration$
DECLARE
    v_definition text;
    v_profile_anchor text := $anchor$    ELSIF (p_request->>'institution_code') = 'ccb'
       AND (p_request->>'source_system') = 'ccb_personal_xls_export'$anchor$;
    v_profile_replacement text := $replacement$    ELSIF (p_request->>'institution_code') = 'abc'
       AND (p_request->>'source_system') = 'abc_company_xls_export'
       AND (p_request->>'declared_media_type') = 'application/vnd.ms-excel'
       AND v_profile = 'abc_company_xls_v1' THEN
        NULL;
    ELSIF (p_request->>'institution_code') = 'ccb'
       AND (p_request->>'source_system') = 'ccb_personal_xls_export'$replacement$;
    v_owner_anchor text := $anchor$               'mybank_company_daily_xlsx_v2',
               'mybank_company_range_xlsx_v3'
           )$anchor$;
    v_owner_replacement text := $replacement$               'mybank_company_daily_xlsx_v2',
               'mybank_company_range_xlsx_v3',
               'abc_company_xls_v1'
           )$replacement$;
BEGIN
    SELECT pg_get_functiondef(
        'internal_import.import_bank_statement(jsonb)'::regprocedure
    ) INTO STRICT v_definition;
    IF strpos(v_definition, v_profile_anchor) = 0
       OR strpos(v_definition, v_owner_anchor) = 0
       OR strpos(v_definition, 'abc_company_xls_v1') <> 0 THEN
        RAISE EXCEPTION 'bank statement import function baseline changed';
    END IF;
    v_definition := replace(v_definition, v_profile_anchor, v_profile_replacement);
    v_definition := replace(v_definition, v_owner_anchor, v_owner_replacement);
    EXECUTE v_definition;
END
$migration$;

REVOKE ALL ON FUNCTION internal_import.import_bank_statement(jsonb)
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_app;
GRANT EXECUTE ON FUNCTION internal_import.import_bank_statement(jsonb)
    TO ledgerbridge_worker;
"""


def downgrade() -> None:
    raise RuntimeError("generic bank statement imports are forward-only")
