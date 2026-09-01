"""Add category-composition projection for company dashboards.

Revision ID: 20260901_0028
Revises: 20260901_0027
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260901_0028"
down_revision = "20260901_0027"
branch_labels = None
depends_on = None


_SIGNATURE = "uuid, uuid[], boolean, character varying, date, date, bigint, bytea"


def upgrade() -> None:
    connection = op.get_bind()
    backup_role_exists = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_backup')")
    ).scalar_one()
    upgrade_sql = (
        """
CREATE FUNCTION company_reporting_read.get_company_report_composition_v1_as_of(
    p_entity_ref uuid,
    p_business_unit_ids uuid[],
    p_include_unassigned boolean,
    p_basis varchar(32),
    p_from_month date,
    p_to_month date,
    p_audit_sequence bigint,
    p_audit_hash bytea
) RETURNS TABLE(composition jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_report jsonb;
    v_first_items jsonb := '[]'::jsonb;
    v_second_items jsonb := '[]'::jsonb;
    v_first_total bigint := 0;
    v_second_total bigint := 0;
    v_first_count bigint := 0;
    v_second_count bigint := 0;
    v_composition jsonb;
BEGIN
    IF p_basis IS NULL
       OR p_basis NOT IN ('CONFIRMED_CANDIDATE', 'POSTED_LEDGER') THEN
        RAISE EXCEPTION 'company report composition basis is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT report INTO v_report
      FROM company_reporting_read.get_company_report_v1_as_of(
        p_entity_ref, p_business_unit_ids, p_include_unassigned, p_basis,
        p_from_month, p_to_month, p_audit_sequence, p_audit_hash
      );
    IF v_report IS NULL THEN
        RETURN;
    END IF;

    IF p_basis = 'CONFIRMED_CANDIDATE' THEN
        WITH candidate_tip AS (
            SELECT candidate.id AS candidate_id,
                   revision.status,
                   revision.business_unit_id,
                   revision.category_code_snapshot AS category_code,
                   revision.category_label_snapshot AS category_label,
                   revision.amount_minor,
                   revision.accounting_month
              FROM public.candidate AS candidate
              JOIN LATERAL (
                    SELECT item.*
                      FROM public.candidate_revision AS item
                      JOIN public.candidate_event AS version_event
                        ON version_event.candidate_id = item.candidate_id
                       AND version_event.to_revision = item.revision
                      JOIN public.audit_event AS version_audit
                        ON version_audit.id = version_event.audit_event_id
                       AND version_audit.sequence <= p_audit_sequence
                     WHERE item.candidate_id = candidate.id
                     ORDER BY item.revision DESC
                     LIMIT 1
              ) AS revision ON true
             WHERE candidate.entity_id = p_entity_ref
               AND EXISTS (
                    SELECT 1
                      FROM public.candidate_event AS created_event
                      JOIN public.audit_event AS created_audit
                        ON created_audit.id = created_event.audit_event_id
                       AND created_audit.sequence <= p_audit_sequence
                     WHERE created_event.candidate_id = candidate.id
                       AND created_event.event_type = 'CREATE'
               )
               AND (
                    revision.business_unit_id = ANY(p_business_unit_ids)
                    OR (revision.business_unit_id IS NULL AND p_include_unassigned)
               )
               AND revision.status = 'CONFIRMED'
               AND revision.accounting_month BETWEEN p_from_month AND p_to_month
               AND revision.amount_minor > 0
        ), grouped AS (
            SELECT category_code, category_label,
                   sum(amount_minor)::bigint AS amount_minor,
                   count(*)::bigint AS fact_count
              FROM candidate_tip
             GROUP BY category_code, category_label
        )
        SELECT COALESCE(
                   jsonb_agg(
                       jsonb_build_object(
                           'category_code', category_code,
                           'category_label', category_label,
                           'amount_minor', amount_minor,
                           'fact_count', fact_count
                       ) ORDER BY amount_minor DESC, category_code NULLS LAST, category_label
                   ),
                   '[]'::jsonb
               ),
               COALESCE(sum(amount_minor), 0)::bigint,
               COALESCE(sum(fact_count), 0)::bigint
          INTO v_first_items, v_first_total, v_first_count
          FROM grouped;

        WITH candidate_tip AS (
            SELECT candidate.id AS candidate_id,
                   revision.status,
                   revision.business_unit_id,
                   revision.category_code_snapshot AS category_code,
                   revision.category_label_snapshot AS category_label,
                   revision.amount_minor,
                   revision.accounting_month
              FROM public.candidate AS candidate
              JOIN LATERAL (
                    SELECT item.*
                      FROM public.candidate_revision AS item
                      JOIN public.candidate_event AS version_event
                        ON version_event.candidate_id = item.candidate_id
                       AND version_event.to_revision = item.revision
                      JOIN public.audit_event AS version_audit
                        ON version_audit.id = version_event.audit_event_id
                       AND version_audit.sequence <= p_audit_sequence
                     WHERE item.candidate_id = candidate.id
                     ORDER BY item.revision DESC
                     LIMIT 1
              ) AS revision ON true
             WHERE candidate.entity_id = p_entity_ref
               AND EXISTS (
                    SELECT 1
                      FROM public.candidate_event AS created_event
                      JOIN public.audit_event AS created_audit
                        ON created_audit.id = created_event.audit_event_id
                       AND created_audit.sequence <= p_audit_sequence
                     WHERE created_event.candidate_id = candidate.id
                       AND created_event.event_type = 'CREATE'
               )
               AND (
                    revision.business_unit_id = ANY(p_business_unit_ids)
                    OR (revision.business_unit_id IS NULL AND p_include_unassigned)
               )
               AND revision.status = 'CONFIRMED'
               AND revision.accounting_month BETWEEN p_from_month AND p_to_month
               AND revision.amount_minor < 0
        ), grouped AS (
            SELECT category_code, category_label,
                   -sum(amount_minor)::bigint AS amount_minor,
                   count(*)::bigint AS fact_count
              FROM candidate_tip
             GROUP BY category_code, category_label
        )
        SELECT COALESCE(
                   jsonb_agg(
                       jsonb_build_object(
                           'category_code', category_code,
                           'category_label', category_label,
                           'amount_minor', amount_minor,
                           'fact_count', fact_count
                       ) ORDER BY amount_minor DESC, category_code NULLS LAST, category_label
                   ),
                   '[]'::jsonb
               ),
               COALESCE(sum(amount_minor), 0)::bigint,
               COALESCE(sum(fact_count), 0)::bigint
          INTO v_second_items, v_second_total, v_second_count
          FROM grouped;

        IF jsonb_array_length(v_first_items) > 100
           OR jsonb_array_length(v_second_items) > 100 THEN
            RAISE EXCEPTION 'candidate category composition exceeds the category limit'
                USING ERRCODE = 'program_limit_exceeded';
        END IF;
        IF v_first_total <> (v_report #>> '{metrics,confirmed_positive_minor}')::bigint
           OR v_second_total <> -(v_report #>> '{metrics,confirmed_negative_minor}')::bigint THEN
            RAISE EXCEPTION 'candidate category composition does not reconcile to report totals'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        v_composition := jsonb_build_object(
            'company_ref', v_report->'company_ref',
            'company_name', v_report->'company_name',
            'currency', v_report->'currency',
            'basis', p_basis,
            'positive', jsonb_build_object(
                'total_minor', v_first_total,
                'fact_count', v_first_count,
                'items', v_first_items
            ),
            'negative', jsonb_build_object(
                'total_minor', v_second_total,
                'fact_count', v_second_count,
                'items', v_second_items
            )
        );
    ELSE
        WITH posting_fact AS (
            SELECT attribution.category_code_snapshot AS category_code,
                   attribution.category_label_snapshot AS category_label,
                   posting.amount_minor
              FROM public.journal_entry AS entry
              JOIN public.audit_event AS posted_audit
                ON posted_audit.id = entry.posted_audit_event_id
               AND posted_audit.sequence <= p_audit_sequence
              JOIN public.journal_entry_attribution AS entry_attribution
                ON entry_attribution.entry_id = entry.id
               AND entry_attribution.entity_id = p_entity_ref
               AND entry_attribution.business_unit_id = ANY(p_business_unit_ids)
              JOIN public.posting AS posting ON posting.entry_id = entry.id
              JOIN public.account AS account
                ON account.id = posting.account_id
               AND account.entity_id = entry.entity_id
              LEFT JOIN public.posting_attribution AS attribution
                ON attribution.posting_id = posting.id
             WHERE entry.entity_id = p_entity_ref
               AND entry.status = 'POSTED'
               AND entry_attribution.accounting_month BETWEEN p_from_month AND p_to_month
               AND account.account_class = 'INCOME'
               AND posting.amount_minor < 0
        ), grouped AS (
            SELECT category_code, category_label,
                   -sum(amount_minor)::bigint AS amount_minor,
                   count(*)::bigint AS fact_count
              FROM posting_fact
             GROUP BY category_code, category_label
        )
        SELECT COALESCE(
                   jsonb_agg(
                       jsonb_build_object(
                           'category_code', category_code,
                           'category_label', category_label,
                           'amount_minor', amount_minor,
                           'fact_count', fact_count
                       ) ORDER BY amount_minor DESC, category_code NULLS LAST, category_label
                   ),
                   '[]'::jsonb
               ),
               COALESCE(sum(amount_minor), 0)::bigint,
               COALESCE(sum(fact_count), 0)::bigint
          INTO v_first_items, v_first_total, v_first_count
          FROM grouped;

        WITH posting_fact AS (
            SELECT attribution.category_code_snapshot AS category_code,
                   attribution.category_label_snapshot AS category_label,
                   posting.amount_minor
              FROM public.journal_entry AS entry
              JOIN public.audit_event AS posted_audit
                ON posted_audit.id = entry.posted_audit_event_id
               AND posted_audit.sequence <= p_audit_sequence
              JOIN public.journal_entry_attribution AS entry_attribution
                ON entry_attribution.entry_id = entry.id
               AND entry_attribution.entity_id = p_entity_ref
               AND entry_attribution.business_unit_id = ANY(p_business_unit_ids)
              JOIN public.posting AS posting ON posting.entry_id = entry.id
              JOIN public.account AS account
                ON account.id = posting.account_id
               AND account.entity_id = entry.entity_id
              LEFT JOIN public.posting_attribution AS attribution
                ON attribution.posting_id = posting.id
             WHERE entry.entity_id = p_entity_ref
               AND entry.status = 'POSTED'
               AND entry_attribution.accounting_month BETWEEN p_from_month AND p_to_month
               AND account.account_class = 'EXPENSE'
               AND posting.amount_minor > 0
        ), grouped AS (
            SELECT category_code, category_label,
                   sum(amount_minor)::bigint AS amount_minor,
                   count(*)::bigint AS fact_count
              FROM posting_fact
             GROUP BY category_code, category_label
        )
        SELECT COALESCE(
                   jsonb_agg(
                       jsonb_build_object(
                           'category_code', category_code,
                           'category_label', category_label,
                           'amount_minor', amount_minor,
                           'fact_count', fact_count
                       ) ORDER BY amount_minor DESC, category_code NULLS LAST, category_label
                   ),
                   '[]'::jsonb
               ),
               COALESCE(sum(amount_minor), 0)::bigint,
               COALESCE(sum(fact_count), 0)::bigint
          INTO v_second_items, v_second_total, v_second_count
          FROM grouped;

        IF jsonb_array_length(v_first_items) > 100
           OR jsonb_array_length(v_second_items) > 100 THEN
            RAISE EXCEPTION 'posted category composition exceeds the category limit'
                USING ERRCODE = 'program_limit_exceeded';
        END IF;
        IF v_first_total <> (v_report #>> '{metrics,revenue_minor}')::bigint
           OR v_second_total <> (v_report #>> '{metrics,expense_minor}')::bigint THEN
            RAISE EXCEPTION 'posted category composition does not reconcile to report totals'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        v_composition := jsonb_build_object(
            'company_ref', v_report->'company_ref',
            'company_name', v_report->'company_name',
            'currency', v_report->'currency',
            'basis', p_basis,
            'revenue', jsonb_build_object(
                'total_minor', v_first_total,
                'fact_count', v_first_count,
                'items', v_first_items
            ),
            'expense', jsonb_build_object(
                'total_minor', v_second_total,
                'fact_count', v_second_count,
                'items', v_second_items
            )
        );
    END IF;

    RETURN QUERY SELECT v_composition;
END
$function$;

REVOKE ALL ON FUNCTION company_reporting_read.get_company_report_composition_v1_as_of(
    uuid, uuid[], boolean, character varying, date, date, bigint, bytea
) FROM PUBLIC;
REVOKE ALL ON FUNCTION company_reporting_read.get_company_report_composition_v1_as_of(
    uuid, uuid[], boolean, character varying, date, date, bigint, bytea
) FROM ledgerbridge_api;
REVOKE ALL ON FUNCTION company_reporting_read.get_company_report_composition_v1_as_of(
    uuid, uuid[], boolean, character varying, date, date, bigint, bytea
) FROM ledgerbridge_worker;
REVOKE ALL ON FUNCTION company_reporting_read.get_company_report_composition_v1_as_of(
    uuid, uuid[], boolean, character varying, date, date, bigint, bytea
) FROM ledgerbridge_app;
REVOKE ALL ON FUNCTION company_reporting_read.get_company_report_composition_v1_as_of(
    uuid, uuid[], boolean, character varying, date, date, bigint, bytea
) FROM ledgerbridge_backup;
REVOKE ALL ON FUNCTION company_reporting_read.get_company_report_composition_v1_as_of(
    uuid, uuid[], boolean, character varying, date, date, bigint, bytea
) FROM ledgerbridge_reader;
GRANT EXECUTE ON FUNCTION company_reporting_read.get_company_report_composition_v1_as_of(
    uuid, uuid[], boolean, character varying, date, date, bigint, bytea
) TO ledgerbridge_reader;
"""
    )
    if not backup_role_exists:
        optional_fragment = (
            "REVOKE ALL ON FUNCTION "
            "company_reporting_read.get_company_report_composition_v1_as_of(\n"
            "    uuid, uuid[], boolean, character varying, date, date, bigint, bytea\n"
            ") FROM ledgerbridge_backup;\n"
        )
        if upgrade_sql.count(optional_fragment) != 1:
            raise RuntimeError("optional backup-role revocation contract is invalid")
        upgrade_sql = upgrade_sql.replace(optional_fragment, "")
    op.execute(upgrade_sql)


def downgrade() -> None:
    op.execute(
        f"""
REVOKE EXECUTE ON FUNCTION
    company_reporting_read.get_company_report_composition_v1_as_of({_SIGNATURE})
    FROM ledgerbridge_reader;
DROP FUNCTION company_reporting_read.get_company_report_composition_v1_as_of({_SIGNATURE});
"""
    )
