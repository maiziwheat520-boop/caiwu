"""Read bank-statement facts without requiring an Accounting Candidate source.

Revision ID: 20260902_0033
Revises: 20260902_0032
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260902_0033"
down_revision: str | None = "20260902_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


_UPGRADE_SQL = r"""
DO $migration$
DECLARE
    v_before text;
    v_after text;
    v_without_source_columns text;
    v_source_columns text := E'           transaction.amount_minor,\n'
        '           source.source_system_id,\n'
        '           source.source_event_ref,\n'
        '           allocation_set.allocation_set_ref,';
    v_fact_columns text := E'           transaction.amount_minor,\n'
        '           allocation_set.allocation_set_ref,';
    v_candidate_source_join text := E'      JOIN public.candidate_source AS source\n'
        '        ON source.source_system_id = tip.source_system\n'
        '       AND source.source_event_ref = observation.source_event_ref\n';
BEGIN
    SELECT pg_get_functiondef(
        'company_reporting_read.statement_report_v1_as_of('
        'uuid,uuid[],boolean,date,date,bigint)'::regprocedure
    ) INTO STRICT v_before;

    IF strpos(v_before, v_source_columns) = 0
       OR strpos(v_before, v_candidate_source_join) = 0 THEN
        RAISE EXCEPTION 'statement report dependency shape is not the reviewed revision'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    v_without_source_columns := replace(v_before, v_source_columns, v_fact_columns);
    IF v_without_source_columns = v_before THEN
        RAISE EXCEPTION 'statement report source columns were not removed'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    v_after := replace(v_without_source_columns, v_candidate_source_join, '');
    IF v_after = v_without_source_columns
       OR strpos(v_after, 'public.candidate_source AS source') <> 0
       OR strpos(v_after, 'source.source_system_id') <> 0
       OR strpos(v_after, 'source.source_event_ref') <> 0 THEN
        RAISE EXCEPTION 'statement report candidate-source dependency was not removed'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    EXECUTE v_after;
END
$migration$;
"""


def downgrade() -> None:
    raise RuntimeError("statement fact independence is forward-only")
