"""Make the shared POSTED business-unit snapshot trigger record-safe.

Revision ID: 20260906_0050
Revises: 20260905_0049

``public.r1_require_posted_business_unit_snapshot`` is shared by
``journal_entry`` and ``journal_entry_attribution``. PostgreSQL resolves every
direct ``NEW.<column>`` reference against the row type currently invoking the
function, including the ``CASE`` branch that is not taken, so the original
0024 definition rejected every ``journal_entry`` write with ``record "new" has
no field "entry_id"``. Reading the row through JSONB keeps the existing
snapshot rule while making field lookup specific to the active table record.

0024 now installs the record-safe definition directly, which repairs databases
created from scratch. This revision repairs the databases that already ran the
original 0024.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260906_0050"
down_revision: str | None = "20260905_0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION public.r1_require_posted_business_unit_snapshot()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_entry uuid;
            v_new jsonb;
        BEGIN
            v_new := to_jsonb(NEW);
            v_entry := CASE
                WHEN TG_TABLE_NAME = 'journal_entry' THEN (v_new->>'id')::uuid
                ELSE (v_new->>'entry_id')::uuid
            END;
            IF EXISTS (
                SELECT 1
                  FROM public.journal_entry AS entry
                  JOIN public.journal_entry_attribution AS attribution
                    ON attribution.entry_id = entry.id
                 WHERE entry.id = v_entry
                   AND entry.status = 'POSTED'
                   AND (attribution.business_unit_ref_snapshot IS NULL
                        OR attribution.business_unit_label_snapshot IS NULL)
            ) THEN
                RAISE EXCEPTION 'new POSTED entry requires an immutable business-unit snapshot'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        REVOKE ALL ON FUNCTION public.r1_require_posted_business_unit_snapshot()
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker,
                 ledgerbridge_app;
        """
    )


def downgrade() -> None:
    raise RuntimeError("posted snapshot trigger record-safety repair is forward-only")
