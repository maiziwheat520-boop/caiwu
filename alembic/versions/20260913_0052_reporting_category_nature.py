"""Record whether a reporting category is income, expense or transfer.

Revision ID: 20260913_0052
Revises: 20260906_0051

A personal summary cannot tell a transfer between the user's own accounts from
spending by the amount sign alone, so the category itself carries a nature.
The column is nullable and existing rows stay NULL: a nature is a decision the
owner makes per category, never one this migration guesses.

``reporting_category`` is append-only.  Its trigger function is rebound to
admit exactly one update shape: a NULL nature becoming one of the three values
with every other column unchanged.  A set nature can never be changed or
cleared, and deletes stay refused.

``internal_read.get_accounting_dimensions`` is rebound against its installed
definition, replacing only the category object so it also carries ``nature``.
Re-executing the catalog definition keeps SECURITY DEFINER, the search_path,
the owner and every grant exactly as they were; the migration refuses if that
baseline has moved.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260913_0052"
down_revision: str | None = "20260906_0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIMENSIONS_SIGNATURE = "internal_read.get_accounting_dimensions(uuid,uuid[],varchar[])"
_CATEGORY_OBJECT_WITHOUT_NATURE = "jsonb_build_object('code', rc.code, 'label', rc.label)"
_CATEGORY_OBJECT_WITH_NATURE = (
    "jsonb_build_object('code', rc.code, 'label', rc.label, 'nature', rc.nature)"
)


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.reporting_category
            ADD COLUMN nature varchar(16) NULL;
        ALTER TABLE public.reporting_category
            ADD CONSTRAINT reporting_category_nature_allowed CHECK (
                nature IS NULL OR nature IN ('INCOME','EXPENSE','TRANSFER')
            );
        """
    )
    op.execute(_append_only_sql(allow_nature_assignment=True))
    op.execute(
        _rebind_dimensions_sql(
            source=_CATEGORY_OBJECT_WITHOUT_NATURE,
            target=_CATEGORY_OBJECT_WITH_NATURE,
        )
    )


def downgrade() -> None:
    op.execute(
        """
        DO $guard$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.reporting_category WHERE nature IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'assigned reporting category natures prevent destructive downgrade';
            END IF;
        END
        $guard$;
        """
    )
    op.execute(
        _rebind_dimensions_sql(
            source=_CATEGORY_OBJECT_WITH_NATURE,
            target=_CATEGORY_OBJECT_WITHOUT_NATURE,
        )
    )
    op.execute(_append_only_sql(allow_nature_assignment=False))
    op.execute(
        """
        ALTER TABLE public.reporting_category
            DROP CONSTRAINT reporting_category_nature_allowed;
        ALTER TABLE public.reporting_category
            DROP COLUMN nature;
        """
    )


def _append_only_sql(*, allow_nature_assignment: bool) -> str:
    # The original body from 20260824_0012 refuses every UPDATE and DELETE.
    assignment = (
        """
            IF TG_OP = 'UPDATE'
               AND OLD.nature IS NULL
               AND NEW.nature IS NOT NULL
               AND (to_jsonb(NEW) - 'nature') = (to_jsonb(OLD) - 'nature') THEN
                RETURN NEW;
            END IF;
        """
        if allow_nature_assignment
        else ""
    )
    return f"""
        CREATE OR REPLACE FUNCTION public.r1_reporting_category_append_only()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            {assignment}
            RAISE EXCEPTION 'reporting_category is append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;
    """


def _rebind_dimensions_sql(*, source: str, target: str) -> str:
    return f"""
        DO $patch$
        DECLARE
            v_definition text;
            v_source text := $source${source}$source$;
            v_target text := $target${target}$target$;
        BEGIN
            SELECT pg_get_functiondef(to_regprocedure('{_DIMENSIONS_SIGNATURE}'))
              INTO v_definition;
            IF v_definition IS NULL THEN
                RAISE EXCEPTION 'accounting dimensions function is not installed';
            END IF;
            IF (length(v_definition) - length(replace(v_definition, v_source, '')))
                    / length(v_source) <> 1 THEN
                RAISE EXCEPTION 'accounting dimensions function baseline changed';
            END IF;
            EXECUTE replace(v_definition, v_source, v_target);
        END
        $patch$;
    """
