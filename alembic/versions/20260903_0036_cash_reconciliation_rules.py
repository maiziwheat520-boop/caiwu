# ruff: noqa: E501
"""Persist cash-basis reconciliation mapping rules and expose a scoped read projection.

Revision ID: 20260903_0036
Revises: 20260902_0035
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260903_0036"
down_revision: str | None = "20260902_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)


_UPGRADE_SQL = r"""
CREATE TABLE public.cash_reconciliation_rule (
    rule_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    rule_key varchar(100) NOT NULL,
    status varchar(16) NOT NULL CHECK (status IN ('ACTIVE','RETIRED')),
    source_kind varchar(24) NOT NULL CHECK (source_kind IN ('BANK_TRANSACTION','CANDIDATE')),
    account_key varchar(160),
    source_system_id varchar(100),
    flow_kind varchar(16) NOT NULL CHECK (flow_kind IN ('INCOME','EXPENSE','CURRENT')),
    business_unit_label varchar(100) NOT NULL,
    item_label varchar(100) NOT NULL,
    match_pattern varchar(300) NOT NULL,
    amount_direction varchar(8) NOT NULL CHECK (amount_direction IN ('CREDIT','DEBIT','ANY')),
    effective_from date NOT NULL,
    effective_to date,
    audit_event_id uuid NOT NULL REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (rule_id, revision),
    UNIQUE (rule_key, revision),
    CHECK ((source_kind = 'BANK_TRANSACTION' AND account_key IS NOT NULL AND source_system_id IS NULL)
        OR (source_kind = 'CANDIDATE' AND account_key IS NULL AND source_system_id IS NOT NULL)),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE TRIGGER cash_reconciliation_rule_append_only
BEFORE UPDATE OR DELETE ON public.cash_reconciliation_rule
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();

REVOKE ALL ON public.cash_reconciliation_rule FROM PUBLIC, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_reader;

CREATE TABLE public.cash_reconciliation_adjustment (
    adjustment_id uuid PRIMARY KEY,
    accounting_month date NOT NULL CHECK (accounting_month=date_trunc('month',accounting_month)::date),
    flow_kind varchar(16) NOT NULL CHECK (flow_kind IN ('INCOME','EXPENSE','CURRENT')),
    business_unit_label varchar(100) NOT NULL,
    item_label varchar(100) NOT NULL,
    amount_minor bigint NOT NULL CHECK (amount_minor > 0),
    note varchar(500) NOT NULL,
    audit_event_id uuid NOT NULL REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER cash_reconciliation_adjustment_append_only
BEFORE UPDATE OR DELETE ON public.cash_reconciliation_adjustment
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();
REVOKE ALL ON public.cash_reconciliation_adjustment FROM PUBLIC, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_reader;

CREATE OR REPLACE FUNCTION internal_read.cash_reconciliation_rules_v1()
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH latest AS (
    SELECT DISTINCT ON (r.rule_id) r.*
      FROM public.cash_reconciliation_rule r
     ORDER BY r.rule_id, r.revision DESC
)
SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'rule_key', rule_key,
    'source_kind', source_kind,
    'account_key', account_key,
    'source_system_id', source_system_id,
    'flow_kind', flow_kind,
    'business_unit_label', business_unit_label,
    'item_label', item_label,
    'match_pattern', match_pattern,
    'amount_direction', amount_direction,
    'effective_from', effective_from,
    'effective_to', effective_to
) ORDER BY business_unit_label, flow_kind, item_label, rule_key), '[]'::jsonb)
FROM latest WHERE status = 'ACTIVE';
$function$;

REVOKE ALL ON FUNCTION internal_read.cash_reconciliation_rules_v1() FROM PUBLIC, ledgerbridge_api, ledgerbridge_worker;
GRANT EXECUTE ON FUNCTION internal_read.cash_reconciliation_rules_v1() TO ledgerbridge_reader;

CREATE OR REPLACE FUNCTION internal_read.cash_reconciliation_month_v1(p_month date)
RETURNS jsonb
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH bounds AS (
    SELECT date_trunc('month', p_month)::date AS month_start,
           (date_trunc('month', p_month) + interval '1 month')::date AS month_end
), latest_rules AS (
    SELECT DISTINCT ON (r.rule_id) r.*
      FROM public.cash_reconciliation_rule r
     ORDER BY r.rule_id, r.revision DESC
), rules AS (
    SELECT r.* FROM latest_rules r, bounds b
     WHERE r.status = 'ACTIVE'
       AND r.effective_from < b.month_end
       AND (r.effective_to IS NULL OR r.effective_to >= b.month_start)
), confirmed_statements AS (
    SELECT s.statement_ref
      FROM public.bank_statement s
     WHERE (SELECT br.status FROM public.bank_statement_review br
             WHERE br.statement_ref=s.statement_ref ORDER BY br.revision DESC LIMIT 1)='CONFIRMED'
), bank_hits AS (
    SELECT r.rule_key, t.transaction_ref::text AS fact_ref,
           (t.occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS occurred_on,
           t.amount_minor
      FROM rules r
      JOIN public.managed_account ma ON ma.account_key=r.account_key
      JOIN public.bank_statement_transaction t ON t.managed_account_ref=ma.managed_account_ref
      JOIN bounds b ON (t.occurred_at AT TIME ZONE 'Asia/Shanghai')::date >= b.month_start
                   AND (t.occurred_at AT TIME ZONE 'Asia/Shanghai')::date < b.month_end
     WHERE r.source_kind='BANK_TRANSACTION'
       AND concat_ws(' ',t.counterparty_name,t.transaction_name,t.counterparty_institution) ~* r.match_pattern
       AND (r.amount_direction='ANY' OR (r.amount_direction='CREDIT' AND t.amount_minor>0)
            OR (r.amount_direction='DEBIT' AND t.amount_minor<0))
       AND EXISTS (SELECT 1 FROM public.bank_statement_observation o
                    JOIN confirmed_statements cs ON cs.statement_ref=o.statement_ref
                   WHERE o.transaction_ref=t.transaction_ref)
), latest_candidates AS (
    SELECT DISTINCT ON (cr.candidate_id) cr.* FROM public.candidate_revision cr
     ORDER BY cr.candidate_id, cr.revision DESC
), candidate_hits AS (
    SELECT r.rule_key, lc.candidate_id::text AS fact_ref, lc.accounting_month AS occurred_on,
           lc.amount_minor
      FROM rules r JOIN public.candidate_source cs ON cs.source_system_id=r.source_system_id
      JOIN latest_candidates lc ON lc.candidate_id=cs.candidate_id
      JOIN bounds b ON lc.accounting_month>=b.month_start AND lc.accounting_month<b.month_end
     WHERE r.source_kind='CANDIDATE' AND lc.status='CONFIRMED'
       AND concat_ws(' ',lc.summary,cs.display_label) ~* r.match_pattern
       AND (r.amount_direction='ANY' OR (r.amount_direction='CREDIT' AND lc.amount_minor>0)
            OR (r.amount_direction='DEBIT' AND lc.amount_minor<0))
), hits AS (SELECT 'BANK_TRANSACTION:'||fact_ref AS unique_fact_ref,* FROM bank_hits
            UNION ALL SELECT 'CANDIDATE:'||fact_ref AS unique_fact_ref,* FROM candidate_hits),
unique_hits AS (
    SELECT h.rule_key,h.fact_ref,h.occurred_on,h.amount_minor
      FROM hits h
     WHERE (SELECT count(*) FROM hits other WHERE other.unique_fact_ref=h.unique_fact_ref)=1
),
aggregates AS (
    SELECT r.rule_key,r.flow_kind,r.business_unit_label,r.item_label,r.source_kind,
           count(h.fact_ref)::integer AS transaction_count,
           COALESCE(sum(abs(h.amount_minor)),0)::bigint AS amount_minor,
           COALESCE(jsonb_agg(jsonb_build_object('fact_ref',h.fact_ref,'occurred_on',h.occurred_on,
               'amount_minor',h.amount_minor) ORDER BY h.occurred_on,h.fact_ref)
               FILTER (WHERE h.fact_ref IS NOT NULL),'[]'::jsonb) AS facts
      FROM rules r LEFT JOIN unique_hits h ON h.rule_key=r.rule_key
     GROUP BY r.rule_key,r.flow_kind,r.business_unit_label,r.item_label,r.source_kind
), adjustment_rows AS (
    SELECT 'adjustment:'||a.adjustment_id::text AS rule_key,a.flow_kind,a.business_unit_label,a.item_label,
           'ADJUSTMENT'::text AS source_kind,1::integer AS transaction_count,a.amount_minor,
           jsonb_build_array(jsonb_build_object('fact_ref',a.adjustment_id::text,
             'occurred_on',a.accounting_month,'amount_minor',a.amount_minor)) AS facts
      FROM public.cash_reconciliation_adjustment a,bounds b
     WHERE a.accounting_month=b.month_start
), all_rows AS (
    SELECT * FROM aggregates UNION ALL SELECT * FROM adjustment_rows
)
SELECT jsonb_build_object(
    'contract_version','ledgerbridge.cash-reconciliation.v1',
    'accounting_month',to_char((SELECT month_start FROM bounds),'YYYY-MM'),
    'rows',COALESCE((SELECT jsonb_agg(to_jsonb(a) ORDER BY a.business_unit_label,a.flow_kind,a.item_label) FROM all_rows a),'[]'::jsonb),
    'totals',jsonb_build_object(
        'income_minor',COALESCE((SELECT sum(amount_minor) FROM all_rows WHERE flow_kind='INCOME'),0),
        'expense_minor',COALESCE((SELECT sum(amount_minor) FROM all_rows WHERE flow_kind='EXPENSE'),0),
        'current_minor',COALESCE((SELECT sum(amount_minor) FROM all_rows WHERE flow_kind='CURRENT'),0)
    )
);
$function$;

REVOKE ALL ON FUNCTION internal_read.cash_reconciliation_month_v1(date) FROM PUBLIC, ledgerbridge_api, ledgerbridge_worker;
GRANT EXECUTE ON FUNCTION internal_read.cash_reconciliation_month_v1(date) TO ledgerbridge_reader;
"""


_DOWNGRADE_SQL = r"""
DROP FUNCTION internal_read.cash_reconciliation_month_v1(date);
DROP FUNCTION internal_read.cash_reconciliation_rules_v1();
DROP TABLE public.cash_reconciliation_adjustment;
DROP TABLE public.cash_reconciliation_rule;
"""
