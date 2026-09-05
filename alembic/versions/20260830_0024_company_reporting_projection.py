"""Add the basis-separated company reporting read projection.

Revision ID: 20260830_0024
Revises: 20260830_0023
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_0024"
down_revision: str | None = "20260830_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    backup_role_exists = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_backup')")
    ).scalar_one()
    upgrade_sql = _UPGRADE_SQL
    if not backup_role_exists:
        for optional_line in (
            "REVOKE ALL ON SCHEMA company_reporting_read FROM ledgerbridge_backup;\n",
            "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA company_reporting_read "
            "FROM ledgerbridge_backup;\n",
            (
                "REVOKE ALL ON FUNCTION public.r1_capture_journal_attribution_snapshot(),\n"
                "    public.r1_require_posted_business_unit_snapshot()\n"
                "    FROM ledgerbridge_backup;\n"
            ),
        ):
            if upgrade_sql.count(optional_line) != 1:
                raise RuntimeError("optional backup-role revocation contract is invalid")
            upgrade_sql = upgrade_sql.replace(optional_line, "")
    op.execute(upgrade_sql)


_UPGRADE_SQL = r"""
ALTER TABLE public.journal_entry_attribution
    ADD COLUMN business_unit_ref_snapshot varchar(100),
    ADD COLUMN business_unit_label_snapshot varchar(200),
    ADD CONSTRAINT journal_attribution_business_unit_snapshot_pair CHECK (
        (business_unit_ref_snapshot IS NULL) = (business_unit_label_snapshot IS NULL)
    );

CREATE FUNCTION public.r1_capture_journal_attribution_snapshot()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_ref varchar(100);
    v_label varchar(200);
BEGIN
    SELECT unit.ref, unit.label INTO STRICT v_ref, v_label
      FROM public.business_unit AS unit
     WHERE unit.id = NEW.business_unit_id
       AND unit.entity_id = NEW.entity_id;
    IF NEW.business_unit_ref_snapshot IS NULL
       AND NEW.business_unit_label_snapshot IS NULL THEN
        NEW.business_unit_ref_snapshot := v_ref;
        NEW.business_unit_label_snapshot := v_label;
    ELSIF NEW.business_unit_ref_snapshot IS DISTINCT FROM v_ref
       OR NEW.business_unit_label_snapshot IS DISTINCT FROM v_label THEN
        RAISE EXCEPTION 'journal attribution business-unit snapshot is contradictory'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER r1_capture_journal_attribution_snapshot
BEFORE INSERT ON public.journal_entry_attribution
FOR EACH ROW EXECUTE FUNCTION public.r1_capture_journal_attribution_snapshot();

CREATE FUNCTION public.r1_require_posted_business_unit_snapshot()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_entry uuid;
    v_new jsonb;
BEGIN
    -- The trigger is shared by two tables with different row types, and
    -- PostgreSQL resolves every direct NEW.<column> reference against the row
    -- type currently invoking the function -- including the branch not taken.
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
CREATE CONSTRAINT TRIGGER r1_posted_entry_business_unit_snapshot
AFTER INSERT OR UPDATE OF status ON public.journal_entry
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.r1_require_posted_business_unit_snapshot();
CREATE CONSTRAINT TRIGGER r1_posted_attribution_business_unit_snapshot
AFTER INSERT ON public.journal_entry_attribution
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.r1_require_posted_business_unit_snapshot();

CREATE SCHEMA company_reporting_read;

CREATE FUNCTION company_reporting_read.unavailable_balance_v1()
RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path = pg_catalog
AS $function$
    SELECT jsonb_build_object(
        'balance_basis', 'UNAVAILABLE',
        'opening_balance_minor', NULL,
        'closing_balance_minor', NULL,
        'gap', 'AUTHORITATIVE_BALANCE_UNAVAILABLE'
    )
$function$;

CREATE FUNCTION company_reporting_read.candidate_report_v1_as_of(
    p_entity_ref uuid, p_business_unit_ids uuid[], p_include_unassigned boolean,
    p_from_month date, p_to_month date, p_audit_sequence bigint
) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $function$
WITH candidate_tip AS (
    SELECT candidate.id AS candidate_id,
           candidate.entity_id,
           candidate.supersedes_candidate_id,
           revision.status,
           revision.business_unit_id,
           revision.business_unit_ref_snapshot,
           revision.business_unit_label_snapshot,
           revision.amount_minor,
           revision.accounting_month,
           source.source_system_id,
           source.source_event_ref
      FROM public.candidate AS candidate
      JOIN public.candidate_source AS source
        ON source.candidate_id = candidate.id
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
), relevant AS (
    SELECT *
      FROM candidate_tip
     WHERE status = 'CONFIRMED'
        OR status IN ('PENDING','INCOMPLETE','CONFLICTED')
), scoped_month AS (
    SELECT *
      FROM relevant
     WHERE accounting_month BETWEEN p_from_month AND p_to_month
), unit_aggregate AS (
    SELECT accounting_month, business_unit_id,
           business_unit_ref_snapshot AS business_unit_ref,
           business_unit_label_snapshot AS business_unit_label,
           COALESCE(sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED' AND amount_minor > 0
           ), 0)::bigint AS positive_minor,
           COALESCE(sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED' AND amount_minor < 0
           ), 0)::bigint AS negative_minor,
           count(*) FILTER (WHERE status = 'CONFIRMED')::bigint AS confirmed_count,
           count(DISTINCT (source_system_id, source_event_ref)) FILTER (
               WHERE status = 'CONFIRMED'
           )::bigint AS source_count,
           count(*) FILTER (
               WHERE status IN ('PENDING','INCOMPLETE','CONFLICTED')
           )::bigint AS pending_review_count
      FROM scoped_month
     WHERE business_unit_id IS NOT NULL
     GROUP BY accounting_month, business_unit_id,
              business_unit_ref_snapshot, business_unit_label_snapshot
), unit_json AS (
    SELECT accounting_month,
           jsonb_agg(
               jsonb_build_object(
                   'business_unit_ref', business_unit_ref,
                   'business_unit_label', business_unit_label,
                   'metrics', jsonb_build_object(
                       'basis', 'CONFIRMED_CANDIDATE',
                       'confirmed_positive_minor', positive_minor,
                       'confirmed_negative_minor', negative_minor,
                       'confirmed_net_minor', positive_minor + negative_minor,
                       'confirmed_count', confirmed_count,
                       'source_count', source_count
                   ),
                   'pending_review_count', pending_review_count,
                   'attribution_pending_count', confirmed_count,
                   'missing_material_count', NULL,
                   'taxonomy_version', NULL,
                   'balance', company_reporting_read.unavailable_balance_v1()
               ) ORDER BY business_unit_ref
           ) AS business_units
      FROM unit_aggregate
     GROUP BY accounting_month
), month_aggregate AS (
    SELECT accounting_month,
           COALESCE(sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED' AND amount_minor > 0
           ), 0)::bigint AS positive_minor,
           COALESCE(sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED' AND amount_minor < 0
           ), 0)::bigint AS negative_minor,
           count(*) FILTER (WHERE status = 'CONFIRMED')::bigint AS confirmed_count,
           count(DISTINCT (source_system_id, source_event_ref)) FILTER (
               WHERE status = 'CONFIRMED'
           )::bigint AS source_count,
           count(*) FILTER (
               WHERE status IN ('PENDING','INCOMPLETE','CONFLICTED')
           )::bigint AS pending_review_count
      FROM scoped_month
     GROUP BY accounting_month
), month_json AS (
    SELECT jsonb_agg(
               jsonb_build_object(
                   'month', to_char(month.accounting_month, 'YYYY-MM'),
                   'metrics', jsonb_build_object(
                       'basis', 'CONFIRMED_CANDIDATE',
                       'confirmed_positive_minor', month.positive_minor,
                       'confirmed_negative_minor', month.negative_minor,
                       'confirmed_net_minor', month.positive_minor + month.negative_minor,
                       'confirmed_count', month.confirmed_count,
                       'source_count', month.source_count
                   ),
                   'pending_review_count', month.pending_review_count,
                   'attribution_pending_count', month.confirmed_count,
                   'missing_material_count', NULL,
                   'taxonomy_version', NULL,
                   'balance', company_reporting_read.unavailable_balance_v1(),
                   'business_unit_breakdown_status', CASE
                       WHEN units.business_units IS NULL THEN 'EMPTY'
                       ELSE 'AVAILABLE'
                   END,
                   'business_units', COALESCE(units.business_units, '[]'::jsonb)
               ) ORDER BY month.accounting_month
           ) AS months
      FROM month_aggregate AS month
      LEFT JOIN unit_json AS units USING (accounting_month)
), company_aggregate AS (
    SELECT COALESCE(sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED'
                 AND accounting_month BETWEEN p_from_month AND p_to_month
                 AND amount_minor > 0
           ), 0)::bigint AS positive_minor,
           COALESCE(sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED'
                 AND accounting_month BETWEEN p_from_month AND p_to_month
                 AND amount_minor < 0
           ), 0)::bigint AS negative_minor,
           count(*) FILTER (
               WHERE status = 'CONFIRMED'
                 AND accounting_month BETWEEN p_from_month AND p_to_month
           )::bigint AS confirmed_count,
           count(DISTINCT (source_system_id, source_event_ref)) FILTER (
               WHERE status = 'CONFIRMED'
                 AND accounting_month BETWEEN p_from_month AND p_to_month
           )::bigint AS source_count,
           count(*) FILTER (
               WHERE status IN ('PENDING','INCOMPLETE','CONFLICTED')
                 AND (accounting_month IS NULL
                      OR accounting_month BETWEEN p_from_month AND p_to_month)
           )::bigint AS pending_review_count
      FROM relevant
)
SELECT jsonb_build_object(
           'company_ref', entity.id,
           'company_name', entity.name,
           'currency', 'CNY',
           'metrics', jsonb_build_object(
               'basis', 'CONFIRMED_CANDIDATE',
               'confirmed_positive_minor', company.positive_minor,
               'confirmed_negative_minor', company.negative_minor,
               'confirmed_net_minor', company.positive_minor + company.negative_minor,
               'confirmed_count', company.confirmed_count,
               'source_count', company.source_count
           ),
           'pending_review_count', company.pending_review_count,
           'attribution_pending_count', company.confirmed_count,
           'missing_material_count', NULL,
           'taxonomy_version', NULL,
           'balance', company_reporting_read.unavailable_balance_v1(),
           'business_unit_breakdown_status', CASE
               WHEN EXISTS (SELECT 1 FROM unit_aggregate) THEN 'AVAILABLE'
               ELSE 'EMPTY'
           END,
           'months', COALESCE(months.months, '[]'::jsonb)
       )
  FROM public.entity AS entity
 CROSS JOIN company_aggregate AS company
 CROSS JOIN month_json AS months
 WHERE entity.id = p_entity_ref
   AND entity.entity_type = 'COMPANY'
$function$;

CREATE FUNCTION company_reporting_read.statement_report_v1_as_of(
    p_entity_ref uuid, p_business_unit_ids uuid[], p_include_unassigned boolean,
    p_from_month date, p_to_month date, p_audit_sequence bigint
) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $function$
WITH statement_tip AS (
    SELECT statement.statement_ref,
           statement.managed_account_ref,
           statement.source_system,
           review.status
      FROM public.bank_statement AS statement
      JOIN public.managed_account AS account
        ON account.managed_account_ref = statement.managed_account_ref
       AND account.entity_id = p_entity_ref
       AND account.owner_kind = 'COMPANY'
      JOIN public.audit_event AS account_audit
        ON account_audit.id = account.audit_event_id
       AND account_audit.sequence <= p_audit_sequence
      JOIN public.audit_event AS statement_audit
        ON statement_audit.id = statement.audit_event_id
       AND statement_audit.sequence <= p_audit_sequence
      JOIN LATERAL (
            SELECT item.status
              FROM public.bank_statement_review AS item
              JOIN public.audit_event AS review_audit
                ON review_audit.id = item.audit_event_id
               AND review_audit.sequence <= p_audit_sequence
             WHERE item.statement_ref = statement.statement_ref
             ORDER BY item.revision DESC
             LIMIT 1
      ) AS review ON true
), statement_fact_raw AS (
    SELECT tip.statement_ref,
           tip.status,
           tip.managed_account_ref,
           transaction.transaction_ref,
           date_trunc('month', transaction.occurred_at)::date AS accounting_month,
           transaction.amount_minor,
           source.source_system_id,
           source.source_event_ref,
           allocation_set.allocation_set_ref,
           allocation_stats.allocation_item_count,
           allocation_stats.allocation_basis_points,
           allocation_item.business_unit_id AS allocation_business_unit_id,
           allocation_item.business_unit_ref_snapshot AS allocation_business_unit_ref,
           allocation_item.business_unit_label_snapshot AS allocation_business_unit_label,
           assignment.business_unit_id AS assignment_business_unit_id,
           assignment.business_unit_ref_snapshot AS assignment_business_unit_ref,
           assignment.business_unit_label_snapshot AS assignment_business_unit_label
      FROM statement_tip AS tip
      JOIN public.bank_statement_observation AS observation
        ON observation.statement_ref = tip.statement_ref
       AND observation.managed_account_ref = tip.managed_account_ref
      JOIN public.bank_statement_transaction AS transaction
        ON transaction.transaction_ref = observation.transaction_ref
       AND transaction.managed_account_ref = observation.managed_account_ref
      JOIN public.candidate_source AS source
        ON source.source_system_id = tip.source_system
       AND source.source_event_ref = observation.source_event_ref
      JOIN public.audit_event AS observation_audit
        ON observation_audit.id = observation.audit_event_id
       AND observation_audit.sequence <= p_audit_sequence
      JOIN public.audit_event AS transaction_audit
        ON transaction_audit.id = transaction.audit_event_id
       AND transaction_audit.sequence <= p_audit_sequence
      LEFT JOIN LATERAL (
            SELECT allocation.allocation_set_ref
              FROM public.fact_business_unit_allocation_set AS allocation
              JOIN public.audit_event AS allocation_audit
                ON allocation_audit.id = allocation.audit_event_id
               AND allocation_audit.sequence <= p_audit_sequence
             WHERE allocation.owner_entity_id = p_entity_ref
               AND allocation.managed_account_ref = tip.managed_account_ref
               AND allocation.fact_ref = transaction.transaction_ref
             ORDER BY allocation.revision DESC
             LIMIT 1
      ) AS allocation_set ON true
      LEFT JOIN LATERAL (
            SELECT count(*)::integer AS allocation_item_count,
                   COALESCE(sum(item.basis_points), 0)::integer
                       AS allocation_basis_points
              FROM public.fact_business_unit_allocation_item AS item
             WHERE item.allocation_set_ref = allocation_set.allocation_set_ref
      ) AS allocation_stats ON allocation_set.allocation_set_ref IS NOT NULL
      LEFT JOIN public.fact_business_unit_allocation_item AS allocation_item
        ON allocation_item.allocation_set_ref = allocation_set.allocation_set_ref
       AND allocation_stats.allocation_item_count = 1
       AND allocation_stats.allocation_basis_points = 10000
      LEFT JOIN LATERAL (
            SELECT item.business_unit_id,
                   item.business_unit_ref_snapshot,
                   item.business_unit_label_snapshot
              FROM public.account_business_unit_assignment AS item
              JOIN public.audit_event AS assignment_audit
                ON assignment_audit.id = item.audit_event_id
               AND assignment_audit.sequence <= p_audit_sequence
             WHERE item.owner_entity_id = p_entity_ref
               AND item.managed_account_ref = tip.managed_account_ref
               AND transaction.occurred_at::date >= item.effective_from
               AND (item.effective_to IS NULL
                    OR transaction.occurred_at::date < item.effective_to)
             ORDER BY item.effective_from DESC, item.assignment_ref
             LIMIT 1
      ) AS assignment ON true
     WHERE tip.status IN ('CONFIRMED','PENDING')
), statement_fact AS (
    SELECT raw.*,
           CASE
               WHEN raw.allocation_set_ref IS NOT NULL THEN
                   CASE
                       WHEN raw.allocation_item_count = 1
                        AND raw.allocation_basis_points = 10000
                           THEN raw.allocation_business_unit_id
                   END
               ELSE raw.assignment_business_unit_id
           END AS resolved_business_unit_id,
           CASE
               WHEN raw.allocation_set_ref IS NOT NULL THEN
                   CASE
                       WHEN raw.allocation_item_count = 1
                        AND raw.allocation_basis_points = 10000
                        AND raw.allocation_business_unit_id = ANY(p_business_unit_ids)
                           THEN raw.allocation_business_unit_id
                   END
               WHEN raw.assignment_business_unit_id = ANY(p_business_unit_ids)
                   THEN raw.assignment_business_unit_id
           END AS business_unit_id,
           CASE
               WHEN raw.allocation_set_ref IS NOT NULL THEN
                   CASE
                       WHEN raw.allocation_item_count = 1
                        AND raw.allocation_basis_points = 10000
                        AND raw.allocation_business_unit_id = ANY(p_business_unit_ids)
                           THEN raw.allocation_business_unit_ref
                   END
               WHEN raw.assignment_business_unit_id = ANY(p_business_unit_ids)
                   THEN raw.assignment_business_unit_ref
           END AS business_unit_ref,
           CASE
               WHEN raw.allocation_set_ref IS NOT NULL THEN
                   CASE
                       WHEN raw.allocation_item_count = 1
                        AND raw.allocation_basis_points = 10000
                        AND raw.allocation_business_unit_id = ANY(p_business_unit_ids)
                           THEN raw.allocation_business_unit_label
                   END
               WHEN raw.assignment_business_unit_id = ANY(p_business_unit_ids)
                   THEN raw.assignment_business_unit_label
           END AS business_unit_label
      FROM statement_fact_raw AS raw
), scoped_fact AS (
    SELECT * FROM statement_fact
     WHERE accounting_month BETWEEN p_from_month AND p_to_month
       AND (
            resolved_business_unit_id = ANY(p_business_unit_ids)
            OR (resolved_business_unit_id IS NULL AND p_include_unassigned)
       )
), unit_aggregate AS (
    SELECT accounting_month, business_unit_id,
           business_unit_ref, business_unit_label,
           COALESCE(sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED' AND amount_minor >= 0
           ), 0)::bigint AS inflow_minor,
           COALESCE(-sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED' AND amount_minor < 0
           ), 0)::bigint AS outflow_minor,
           count(DISTINCT transaction_ref) FILTER (
               WHERE status = 'CONFIRMED'
           )::bigint AS confirmed_count,
           count(DISTINCT statement_ref) FILTER (
               WHERE status = 'CONFIRMED'
           )::bigint AS statement_count
      FROM scoped_fact
     WHERE business_unit_id IS NOT NULL
       AND business_unit_ref IS NOT NULL
       AND business_unit_label IS NOT NULL
     GROUP BY accounting_month, business_unit_id,
              business_unit_ref, business_unit_label
), unit_json AS (
    SELECT accounting_month,
           jsonb_agg(
               jsonb_build_object(
                   'business_unit_ref', business_unit_ref,
                   'business_unit_label', business_unit_label,
                   'metrics', jsonb_build_object(
                       'basis', 'ACCOUNT_STATEMENT',
                       'cash_inflow_minor', inflow_minor,
                       'cash_outflow_minor', outflow_minor,
                       'net_cash_flow_minor', inflow_minor - outflow_minor,
                       'confirmed_transaction_count', confirmed_count,
                       'statement_count', statement_count
                   ),
                   'pending_review_count', 0,
                   'attribution_pending_count', 0,
                   'missing_material_count', NULL,
                   'taxonomy_version', NULL,
                   'balance', company_reporting_read.unavailable_balance_v1()
               ) ORDER BY business_unit_ref
           ) AS business_units
      FROM unit_aggregate
     GROUP BY accounting_month
), month_aggregate AS (
    SELECT accounting_month,
           COALESCE(sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED' AND amount_minor >= 0
           ), 0)::bigint AS inflow_minor,
           COALESCE(-sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED' AND amount_minor < 0
           ), 0)::bigint AS outflow_minor,
           count(*) FILTER (WHERE status = 'CONFIRMED')::bigint AS confirmed_count,
           count(DISTINCT statement_ref) FILTER (
               WHERE status = 'CONFIRMED'
           )::bigint AS statement_count,
           count(DISTINCT statement_ref) FILTER (
               WHERE status = 'PENDING'
           )::bigint AS pending_review_count,
           count(*) FILTER (
               WHERE status = 'CONFIRMED' AND business_unit_id IS NULL
           )::bigint AS attribution_pending_count,
           count(*) FILTER (
               WHERE status = 'CONFIRMED'
                 AND business_unit_id IS NOT NULL
                 AND (business_unit_ref IS NULL OR business_unit_label IS NULL)
           )::bigint AS missing_snapshot_count
      FROM scoped_fact
     GROUP BY accounting_month
), month_json AS (
    SELECT jsonb_agg(
               jsonb_build_object(
                   'month', to_char(accounting_month, 'YYYY-MM'),
                   'metrics', jsonb_build_object(
                       'basis', 'ACCOUNT_STATEMENT',
                       'cash_inflow_minor', inflow_minor,
                       'cash_outflow_minor', outflow_minor,
                       'net_cash_flow_minor', inflow_minor - outflow_minor,
                       'confirmed_transaction_count', confirmed_count,
                       'statement_count', statement_count
                   ),
                   'pending_review_count', pending_review_count,
                   'attribution_pending_count', month.attribution_pending_count,
                   'missing_material_count', NULL,
                   'taxonomy_version', NULL,
                   'balance', company_reporting_read.unavailable_balance_v1(),
                   'business_unit_breakdown_status', CASE
                       WHEN month.confirmed_count = 0 THEN 'EMPTY'
                       WHEN month.attribution_pending_count > 0
                           THEN 'UNAVAILABLE_ATTRIBUTION_PENDING'
                       WHEN month.missing_snapshot_count > 0
                           THEN 'UNAVAILABLE_MISSING_SNAPSHOT'
                       ELSE 'AVAILABLE'
                   END,
                   'business_units', CASE
                       WHEN month.confirmed_count = 0 THEN '[]'::jsonb
                       WHEN month.attribution_pending_count > 0
                         OR month.missing_snapshot_count > 0 THEN NULL
                       ELSE COALESCE(units.business_units, '[]'::jsonb)
                   END
               ) ORDER BY month.accounting_month
           ) AS months
      FROM month_aggregate AS month
      LEFT JOIN unit_json AS units USING (accounting_month)
), company_aggregate AS (
    SELECT COALESCE(sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED' AND amount_minor >= 0
           ), 0)::bigint AS inflow_minor,
           COALESCE(-sum(amount_minor) FILTER (
               WHERE status = 'CONFIRMED' AND amount_minor < 0
           ), 0)::bigint AS outflow_minor,
           count(*) FILTER (WHERE status = 'CONFIRMED')::bigint AS confirmed_count,
           count(DISTINCT statement_ref) FILTER (
               WHERE status = 'CONFIRMED'
           )::bigint AS statement_count,
           count(DISTINCT statement_ref) FILTER (
               WHERE status = 'PENDING'
           )::bigint AS pending_review_count,
           count(*) FILTER (
               WHERE status = 'CONFIRMED' AND business_unit_id IS NULL
           )::bigint AS attribution_pending_count,
           count(*) FILTER (
               WHERE status = 'CONFIRMED'
                 AND business_unit_id IS NOT NULL
                 AND (business_unit_ref IS NULL OR business_unit_label IS NULL)
           )::bigint AS missing_snapshot_count
      FROM scoped_fact
)
SELECT jsonb_build_object(
           'company_ref', entity.id,
           'company_name', entity.name,
           'currency', 'CNY',
           'metrics', jsonb_build_object(
               'basis', 'ACCOUNT_STATEMENT',
               'cash_inflow_minor', company.inflow_minor,
               'cash_outflow_minor', company.outflow_minor,
               'net_cash_flow_minor', company.inflow_minor - company.outflow_minor,
               'confirmed_transaction_count', company.confirmed_count,
               'statement_count', company.statement_count
           ),
           'pending_review_count', company.pending_review_count,
           'attribution_pending_count', company.attribution_pending_count,
           'missing_material_count', NULL,
           'taxonomy_version', NULL,
           'balance', company_reporting_read.unavailable_balance_v1(),
           'business_unit_breakdown_status', CASE
               WHEN company.confirmed_count = 0 THEN 'EMPTY'
               WHEN company.attribution_pending_count > 0
                   THEN 'UNAVAILABLE_ATTRIBUTION_PENDING'
               WHEN company.missing_snapshot_count > 0
                   THEN 'UNAVAILABLE_MISSING_SNAPSHOT'
               ELSE 'AVAILABLE'
           END,
           'months', COALESCE(months.months, '[]'::jsonb)
       )
  FROM public.entity AS entity
 CROSS JOIN company_aggregate AS company
 CROSS JOIN month_json AS months
 WHERE entity.id = p_entity_ref
   AND entity.entity_type = 'COMPANY'
$function$;

CREATE FUNCTION company_reporting_read.posted_report_v1_as_of(
    p_entity_ref uuid, p_business_unit_ids uuid[], p_from_month date,
    p_to_month date, p_audit_sequence bigint
) RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $function$
WITH posting_fact AS (
    SELECT entry.id AS entry_id,
           entry.source_record_id,
           attribution.accounting_month,
           attribution.business_unit_id,
           attribution.business_unit_ref_snapshot AS business_unit_ref,
           attribution.business_unit_label_snapshot AS business_unit_label,
           account.account_class,
           posting.amount_minor
      FROM public.journal_entry AS entry
      JOIN public.audit_event AS posted_audit
        ON posted_audit.id = entry.posted_audit_event_id
       AND posted_audit.sequence <= p_audit_sequence
      JOIN public.journal_entry_attribution AS attribution
        ON attribution.entry_id = entry.id
       AND attribution.entity_id = p_entity_ref
       AND attribution.business_unit_id = ANY(p_business_unit_ids)
      JOIN public.posting AS posting
        ON posting.entry_id = entry.id
      JOIN public.account AS account
        ON account.id = posting.account_id
       AND account.entity_id = entry.entity_id
     WHERE entry.entity_id = p_entity_ref
       AND entry.status = 'POSTED'
       AND attribution.accounting_month BETWEEN p_from_month AND p_to_month
), unit_aggregate AS (
    SELECT accounting_month, business_unit_id,
           business_unit_ref, business_unit_label,
           COALESCE(-sum(amount_minor) FILTER (
               WHERE account_class = 'INCOME'
           ), 0)::bigint AS revenue_minor,
           COALESCE(sum(amount_minor) FILTER (
               WHERE account_class = 'EXPENSE'
           ), 0)::bigint AS expense_minor,
           count(DISTINCT entry_id)::bigint AS posted_entry_count,
           count(DISTINCT source_record_id)::bigint AS source_count
      FROM posting_fact
     WHERE business_unit_ref IS NOT NULL
       AND business_unit_label IS NOT NULL
     GROUP BY accounting_month, business_unit_id,
              business_unit_ref, business_unit_label
), unit_json AS (
    SELECT accounting_month,
           jsonb_agg(
               jsonb_build_object(
                   'business_unit_ref', business_unit_ref,
                   'business_unit_label', business_unit_label,
                   'metrics', jsonb_build_object(
                       'basis', 'POSTED_LEDGER',
                       'revenue_minor', revenue_minor,
                       'expense_minor', expense_minor,
                       'profit_minor', revenue_minor - expense_minor,
                       'posted_entry_count', posted_entry_count,
                       'source_count', source_count
                   ),
                   'pending_review_count', 0,
                   'attribution_pending_count', 0,
                   'missing_material_count', NULL,
                   'taxonomy_version', NULL,
                   'balance', company_reporting_read.unavailable_balance_v1()
               ) ORDER BY business_unit_ref
           ) AS business_units
      FROM unit_aggregate
     GROUP BY accounting_month
), month_aggregate AS (
    SELECT accounting_month,
           COALESCE(-sum(amount_minor) FILTER (
               WHERE account_class = 'INCOME'
           ), 0)::bigint AS revenue_minor,
           COALESCE(sum(amount_minor) FILTER (
               WHERE account_class = 'EXPENSE'
           ), 0)::bigint AS expense_minor,
           count(DISTINCT entry_id)::bigint AS posted_entry_count,
           count(DISTINCT source_record_id)::bigint AS source_count
           ,count(*) FILTER (
               WHERE business_unit_ref IS NULL OR business_unit_label IS NULL
           )::bigint AS missing_snapshot_count
      FROM posting_fact
     GROUP BY accounting_month
), month_json AS (
    SELECT jsonb_agg(
               jsonb_build_object(
                   'month', to_char(month.accounting_month, 'YYYY-MM'),
                   'metrics', jsonb_build_object(
                       'basis', 'POSTED_LEDGER',
                       'revenue_minor', month.revenue_minor,
                       'expense_minor', month.expense_minor,
                       'profit_minor', month.revenue_minor - month.expense_minor,
                       'posted_entry_count', month.posted_entry_count,
                       'source_count', month.source_count
                   ),
                   'pending_review_count', 0,
                   'attribution_pending_count', 0,
                   'missing_material_count', NULL,
                   'taxonomy_version', NULL,
                   'balance', company_reporting_read.unavailable_balance_v1(),
                   'business_unit_breakdown_status', CASE
                       WHEN month.missing_snapshot_count > 0
                           THEN 'UNAVAILABLE_MISSING_SNAPSHOT'
                       ELSE 'AVAILABLE'
                   END,
                   'business_units', CASE
                       WHEN month.missing_snapshot_count > 0 THEN NULL
                       ELSE COALESCE(units.business_units, '[]'::jsonb)
                   END
               ) ORDER BY month.accounting_month
           ) AS months
      FROM month_aggregate AS month
      LEFT JOIN unit_json AS units USING (accounting_month)
), company_aggregate AS (
    SELECT COALESCE(-sum(amount_minor) FILTER (
               WHERE account_class = 'INCOME'
           ), 0)::bigint AS revenue_minor,
           COALESCE(sum(amount_minor) FILTER (
               WHERE account_class = 'EXPENSE'
           ), 0)::bigint AS expense_minor,
           count(DISTINCT entry_id)::bigint AS posted_entry_count,
           count(DISTINCT source_record_id)::bigint AS source_count
           ,count(*) FILTER (
               WHERE business_unit_ref IS NULL OR business_unit_label IS NULL
           )::bigint AS missing_snapshot_count
      FROM posting_fact
)
SELECT jsonb_build_object(
           'company_ref', entity.id,
           'company_name', entity.name,
           'currency', 'CNY',
           'metrics', jsonb_build_object(
               'basis', 'POSTED_LEDGER',
               'revenue_minor', company.revenue_minor,
               'expense_minor', company.expense_minor,
               'profit_minor', company.revenue_minor - company.expense_minor,
               'posted_entry_count', company.posted_entry_count,
               'source_count', company.source_count
           ),
           'pending_review_count', 0,
           'attribution_pending_count', 0,
           'missing_material_count', NULL,
           'taxonomy_version', NULL,
           'balance', company_reporting_read.unavailable_balance_v1(),
           'business_unit_breakdown_status', CASE
               WHEN company.posted_entry_count = 0 THEN 'EMPTY'
               WHEN company.missing_snapshot_count > 0
                   THEN 'UNAVAILABLE_MISSING_SNAPSHOT'
               ELSE 'AVAILABLE'
           END,
           'months', COALESCE(months.months, '[]'::jsonb)
       )
  FROM public.entity AS entity
 CROSS JOIN company_aggregate AS company
 CROSS JOIN month_json AS months
 WHERE entity.id = p_entity_ref
   AND entity.entity_type = 'COMPANY'
$function$;

CREATE FUNCTION company_reporting_read.get_company_report_v1_as_of(
    p_entity_ref uuid,
    p_business_unit_ids uuid[],
    p_include_unassigned boolean,
    p_basis varchar(32),
    p_from_month date,
    p_to_month date,
    p_audit_sequence bigint,
    p_audit_hash bytea
) RETURNS TABLE(report jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_report jsonb;
BEGIN
    IF p_entity_ref IS NULL
       OR p_business_unit_ids IS NULL
       OR p_include_unassigned IS NULL
       OR p_basis IS NULL
       OR p_basis NOT IN ('CONFIRMED_CANDIDATE','ACCOUNT_STATEMENT','POSTED_LEDGER')
       OR p_from_month IS NULL OR p_to_month IS NULL
       OR p_from_month <> date_trunc('month', p_from_month)::date
       OR p_to_month <> date_trunc('month', p_to_month)::date
       OR ((extract(year FROM p_to_month)::integer * 12
            + extract(month FROM p_to_month)::integer)
           - (extract(year FROM p_from_month)::integer * 12
              + extract(month FROM p_from_month)::integer)) NOT BETWEEN 0 AND 23
       OR cardinality(p_business_unit_ids) > 50
       OR (cardinality(p_business_unit_ids) = 0 AND NOT p_include_unassigned)
       OR EXISTS (SELECT 1 FROM unnest(p_business_unit_ids) AS value WHERE value IS NULL)
       OR (SELECT count(*) FROM unnest(p_business_unit_ids) AS value)
          <> (SELECT count(DISTINCT value) FROM unnest(p_business_unit_ids) AS value)
       OR p_audit_sequence IS NULL OR p_audit_sequence < 1
       OR p_audit_hash IS NULL OR octet_length(p_audit_hash) <> 32
       OR NOT EXISTS (
            SELECT 1 FROM public.audit_event AS horizon
             WHERE horizon.sequence = p_audit_sequence
               AND horizon.hash = p_audit_hash
       ) THEN
        RAISE EXCEPTION 'company report request is invalid' USING ERRCODE = '22023';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM public.entity
         WHERE id = p_entity_ref AND entity_type = 'COMPANY'
    ) THEN
        RETURN;
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(p_business_unit_ids) AS requested(id)
         WHERE NOT EXISTS (
             SELECT 1 FROM public.business_unit AS unit
              WHERE unit.id = requested.id
                AND unit.entity_id = p_entity_ref
         )
    ) THEN
        RAISE EXCEPTION 'company report business-unit scope is invalid'
            USING ERRCODE = '22023';
    END IF;

    IF p_basis = 'CONFIRMED_CANDIDATE' THEN
        v_report := company_reporting_read.candidate_report_v1_as_of(
            p_entity_ref, p_business_unit_ids, p_include_unassigned,
            p_from_month, p_to_month, p_audit_sequence
        );
    ELSIF p_basis = 'ACCOUNT_STATEMENT' THEN
        v_report := company_reporting_read.statement_report_v1_as_of(
            p_entity_ref, p_business_unit_ids, p_include_unassigned, p_from_month,
            p_to_month, p_audit_sequence
        );
    ELSIF p_basis = 'POSTED_LEDGER' THEN
        PERFORM public.r1_assert_posted_total_integrity();
        IF EXISTS (
            SELECT 1
              FROM public.journal_entry AS entry
              JOIN public.audit_event AS posted_audit
                ON posted_audit.id = entry.posted_audit_event_id
               AND posted_audit.sequence <= p_audit_sequence
              JOIN public.journal_entry_attribution AS attribution
                ON attribution.entry_id = entry.id
               AND attribution.entity_id = p_entity_ref
               AND attribution.business_unit_id = ANY(p_business_unit_ids)
              JOIN public.posting AS posting ON posting.entry_id = entry.id
              JOIN public.account AS account ON account.id = posting.account_id
             WHERE entry.status = 'POSTED'
               AND attribution.accounting_month BETWEEN p_from_month AND p_to_month
               AND ((account.account_class = 'INCOME' AND posting.amount_minor > 0)
                    OR (account.account_class = 'EXPENSE' AND posting.amount_minor < 0))
        ) THEN
            RAISE EXCEPTION 'posted report contains an unclassified contra or refund effect'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        v_report := company_reporting_read.posted_report_v1_as_of(
            p_entity_ref, p_business_unit_ids, p_from_month,
            p_to_month, p_audit_sequence
        );
    END IF;

    IF v_report IS NULL THEN
        RAISE EXCEPTION 'company report projection is unavailable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN QUERY SELECT v_report;
END
$function$;

REVOKE ALL ON SCHEMA company_reporting_read FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA company_reporting_read FROM PUBLIC;
REVOKE ALL ON SCHEMA company_reporting_read FROM ledgerbridge_api;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA company_reporting_read FROM ledgerbridge_api;
REVOKE ALL ON SCHEMA company_reporting_read FROM ledgerbridge_worker;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA company_reporting_read FROM ledgerbridge_worker;
REVOKE ALL ON SCHEMA company_reporting_read FROM ledgerbridge_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA company_reporting_read FROM ledgerbridge_app;
REVOKE ALL ON SCHEMA company_reporting_read FROM ledgerbridge_backup;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA company_reporting_read FROM ledgerbridge_backup;
REVOKE ALL ON SCHEMA company_reporting_read FROM ledgerbridge_reader;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA company_reporting_read FROM ledgerbridge_reader;
REVOKE ALL ON FUNCTION public.r1_capture_journal_attribution_snapshot(),
    public.r1_require_posted_business_unit_snapshot()
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker,
         ledgerbridge_app;
REVOKE ALL ON FUNCTION public.r1_capture_journal_attribution_snapshot(),
    public.r1_require_posted_business_unit_snapshot()
    FROM ledgerbridge_backup;
GRANT USAGE ON SCHEMA company_reporting_read TO ledgerbridge_reader;
GRANT EXECUTE ON FUNCTION company_reporting_read.get_company_report_v1_as_of(
    uuid,uuid[],boolean,varchar,date,date,bigint,bytea
) TO ledgerbridge_reader;
"""


def downgrade() -> None:
    op.execute(
        """
        DO $guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.business_unit)
               OR EXISTS (SELECT 1 FROM public.reporting_category)
               OR EXISTS (SELECT 1 FROM public.evidence_object)
               OR EXISTS (SELECT 1 FROM public.encrypted_blob_version)
               OR EXISTS (SELECT 1 FROM public.candidate)
               OR EXISTS (SELECT 1 FROM public.candidate_source)
               OR EXISTS (SELECT 1 FROM public.candidate_revision)
               OR EXISTS (SELECT 1 FROM public.candidate_blocker)
               OR EXISTS (SELECT 1 FROM public.candidate_event)
               OR EXISTS (SELECT 1 FROM public.candidate_field_change)
               OR EXISTS (SELECT 1 FROM public.candidate_conflict_resolution)
               OR EXISTS (SELECT 1 FROM public.candidate_evidence)
               OR EXISTS (SELECT 1 FROM public.encrypted_object_identity)
               OR EXISTS (SELECT 1 FROM public.journal_entry)
               OR EXISTS (SELECT 1 FROM public.journal_entry_attribution)
               OR EXISTS (SELECT 1 FROM public.posting)
               OR EXISTS (SELECT 1 FROM public.posting_attribution)
               OR EXISTS (SELECT 1 FROM public.reconciliation_snapshot)
               OR EXISTS (SELECT 1 FROM public.reconciliation_snapshot_blocker)
               OR EXISTS (SELECT 1 FROM public.reconciliation_snapshot_proposal)
               OR EXISTS (SELECT 1 FROM public.reconciliation_snapshot_suspense)
               OR EXISTS (SELECT 1 FROM public.reconciliation_leg)
               OR EXISTS (SELECT 1 FROM internal_read.evidence_read_receipt)
               OR EXISTS (SELECT 1 FROM public.managed_account)
               OR EXISTS (SELECT 1 FROM public.managed_account_lifecycle)
               OR EXISTS (SELECT 1 FROM public.bank_statement)
               OR EXISTS (SELECT 1 FROM public.bank_statement_transaction)
               OR EXISTS (SELECT 1 FROM public.bank_statement_observation)
               OR EXISTS (SELECT 1 FROM public.bank_statement_review)
               OR EXISTS (SELECT 1 FROM public.account_business_unit_assignment)
               OR EXISTS (SELECT 1 FROM public.fact_business_unit_allocation_set)
               OR EXISTS (SELECT 1 FROM public.fact_business_unit_allocation_item) THEN
                RAISE EXCEPTION
                    'nonempty R1 fact database prevents destructive company-reporting downgrade';
            END IF;
        END
        $guard$;
        """
    )
    op.execute(
        """
        REVOKE EXECUTE ON FUNCTION company_reporting_read.get_company_report_v1_as_of(
            uuid,uuid[],boolean,varchar,date,date,bigint,bytea
        ) FROM ledgerbridge_reader;
        REVOKE USAGE ON SCHEMA company_reporting_read FROM ledgerbridge_reader;
        DROP FUNCTION company_reporting_read.get_company_report_v1_as_of(
            uuid,uuid[],boolean,varchar,date,date,bigint,bytea
        );
        DROP FUNCTION company_reporting_read.posted_report_v1_as_of(
            uuid,uuid[],date,date,bigint
        );
        DROP FUNCTION company_reporting_read.statement_report_v1_as_of(
            uuid,uuid[],boolean,date,date,bigint
        );
        DROP FUNCTION company_reporting_read.candidate_report_v1_as_of(
            uuid,uuid[],boolean,date,date,bigint
        );
        DROP FUNCTION company_reporting_read.unavailable_balance_v1();
        DROP SCHEMA company_reporting_read;
        DROP TRIGGER r1_posted_attribution_business_unit_snapshot
            ON public.journal_entry_attribution;
        DROP TRIGGER r1_posted_entry_business_unit_snapshot
            ON public.journal_entry;
        DROP TRIGGER r1_capture_journal_attribution_snapshot
            ON public.journal_entry_attribution;
        DROP FUNCTION public.r1_require_posted_business_unit_snapshot();
        DROP FUNCTION public.r1_capture_journal_attribution_snapshot();
        ALTER TABLE public.journal_entry_attribution
            DROP CONSTRAINT journal_attribution_business_unit_snapshot_pair,
            DROP COLUMN business_unit_label_snapshot,
            DROP COLUMN business_unit_ref_snapshot;
        """
    )
