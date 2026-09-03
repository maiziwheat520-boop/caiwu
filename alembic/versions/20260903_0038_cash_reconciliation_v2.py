# ruff: noqa: E501
"""Scope cash reconciliation reads and expose unmatched/conflicting facts.

Revision ID: 20260903_0038
Revises: 20260903_0036
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260903_0038"
down_revision: str | None = "20260903_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)


_UPGRADE_SQL = r"""
CREATE OR REPLACE FUNCTION internal_read.cash_reconciliation_month_v2(
    p_month date,
    p_entity_ids uuid[],
    p_business_unit_ids uuid[]
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
    v_result jsonb;
BEGIN
    IF p_month IS NULL OR p_month IS DISTINCT FROM date_trunc('month', p_month)::date THEN
        RAISE EXCEPTION 'cash reconciliation month must be the first day of a month'
            USING ERRCODE = '22023';
    END IF;
    IF p_entity_ids IS NULL OR cardinality(p_entity_ids) = 0
       OR cardinality(p_entity_ids) > 100
       OR array_position(p_entity_ids, NULL) IS NOT NULL
       OR cardinality(p_entity_ids) IS DISTINCT FROM (
            SELECT count(DISTINCT value) FROM unnest(p_entity_ids) AS items(value)
       ) THEN
        RAISE EXCEPTION 'cash reconciliation entity scope is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF p_business_unit_ids IS NULL OR cardinality(p_business_unit_ids) > 1000
       OR array_position(p_business_unit_ids, NULL) IS NOT NULL
       OR cardinality(p_business_unit_ids) IS DISTINCT FROM (
            SELECT count(DISTINCT value) FROM unnest(p_business_unit_ids) AS items(value)
       ) THEN
        RAISE EXCEPTION 'cash reconciliation business-unit scope is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM unnest(p_business_unit_ids) AS allowed(id)
          LEFT JOIN public.business_unit unit ON unit.id = allowed.id
         WHERE unit.id IS NULL OR NOT (unit.entity_id = ANY(p_entity_ids))
    ) THEN
        RAISE EXCEPTION 'cash reconciliation business-unit scope is outside entity scope'
            USING ERRCODE = '22023';
    END IF;

    WITH bounds AS (
        SELECT p_month AS month_start, (p_month + interval '1 month')::date AS month_end
    ), latest_rules AS (
        SELECT DISTINCT ON (rule_id) rule.*
          FROM public.cash_reconciliation_rule rule
         ORDER BY rule_id, revision DESC
    ), active_rules AS (
        SELECT * FROM latest_rules WHERE status = 'ACTIVE'
    ), latest_candidates AS (
        SELECT DISTINCT ON (revision.candidate_id) revision.*
          FROM public.candidate_revision revision
         ORDER BY revision.candidate_id, revision.revision DESC
    ), rules AS (
        SELECT rule.*
          FROM active_rules rule, bounds month
         WHERE rule.effective_from < month.month_end
           AND (rule.effective_to IS NULL OR rule.effective_to >= month.month_start)
           AND (
                (rule.source_kind = 'BANK_TRANSACTION' AND EXISTS (
                    SELECT 1
                      FROM public.managed_account account
                     WHERE account.account_key = rule.account_key
                       AND account.entity_id = ANY(p_entity_ids)
                ))
                OR
                (rule.source_kind = 'CANDIDATE' AND EXISTS (
                    SELECT 1
                      FROM public.candidate_source source
                      JOIN public.candidate candidate ON candidate.id = source.candidate_id
                      JOIN latest_candidates latest ON latest.candidate_id = candidate.id
                     WHERE source.source_system_id = rule.source_system_id
                       AND candidate.entity_id = ANY(p_entity_ids)
                       AND latest.business_unit_id = ANY(p_business_unit_ids)
                ))
           )
    ), required_entities AS (
        SELECT DISTINCT account.entity_id
          FROM active_rules rule
          JOIN public.managed_account account ON account.account_key = rule.account_key
         WHERE rule.source_kind = 'BANK_TRANSACTION'
        UNION
        SELECT DISTINCT candidate.entity_id
          FROM active_rules rule
          JOIN public.candidate_source source ON source.source_system_id = rule.source_system_id
          JOIN public.candidate candidate ON candidate.id = source.candidate_id
         WHERE rule.source_kind = 'CANDIDATE'
    ), complete_scope AS (
        SELECT NOT EXISTS (
            SELECT 1 FROM required_entities required
             WHERE NOT (required.entity_id = ANY(p_entity_ids))
        ) AS value
    ), confirmed_statements AS (
        SELECT statement.statement_ref
          FROM public.bank_statement statement
         WHERE (
            SELECT review.status
              FROM public.bank_statement_review review
             WHERE review.statement_ref = statement.statement_ref
             ORDER BY review.revision DESC
             LIMIT 1
         ) = 'CONFIRMED'
    ), bank_universe AS (
        SELECT DISTINCT
               'BANK_TRANSACTION'::text AS source_kind,
               'BANK_TRANSACTION:' || transaction.transaction_ref::text AS unique_fact_ref,
               transaction.transaction_ref::text AS fact_ref,
               (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date AS occurred_on,
               'DAY'::text AS time_granularity,
               transaction.amount_minor,
               transaction.counterparty_name,
               transaction.transaction_name,
               transaction.counterparty_institution,
               account.account_key,
               NULL::text[] AS source_system_ids
          FROM public.bank_statement_transaction transaction
          JOIN public.managed_account account
            ON account.managed_account_ref = transaction.managed_account_ref
          JOIN bounds month
            ON (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date >= month.month_start
           AND (transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date < month.month_end
         WHERE account.entity_id = ANY(p_entity_ids)
           AND account.account_key IN (
                SELECT rule.account_key FROM rules rule WHERE rule.source_kind = 'BANK_TRANSACTION'
           )
           AND EXISTS (
                SELECT 1
                  FROM public.bank_statement_observation observation
                  JOIN confirmed_statements confirmed
                    ON confirmed.statement_ref = observation.statement_ref
                 WHERE observation.transaction_ref = transaction.transaction_ref
                   AND observation.managed_account_ref = transaction.managed_account_ref
           )
    ), candidate_universe AS (
        SELECT
               'CANDIDATE'::text AS source_kind,
               'CANDIDATE:' || latest.candidate_id::text AS unique_fact_ref,
               latest.candidate_id::text AS fact_ref,
               latest.accounting_month AS occurred_on,
               'MONTH'::text AS time_granularity,
               latest.amount_minor,
               latest.summary AS counterparty_name,
               string_agg(DISTINCT source.display_label, ' ' ORDER BY source.display_label)
                   AS transaction_name,
               NULL::text AS counterparty_institution,
               NULL::text AS account_key,
               array_agg(DISTINCT source.source_system_id ORDER BY source.source_system_id)
                   AS source_system_ids
          FROM latest_candidates latest
          JOIN public.candidate candidate ON candidate.id = latest.candidate_id
          JOIN public.candidate_source source ON source.candidate_id = latest.candidate_id
          JOIN bounds month ON latest.accounting_month = month.month_start
         WHERE latest.status = 'CONFIRMED'
           AND candidate.entity_id = ANY(p_entity_ids)
           AND latest.business_unit_id = ANY(p_business_unit_ids)
         GROUP BY latest.candidate_id, latest.accounting_month, latest.amount_minor,
                  latest.summary
        HAVING bool_or(source.source_system_id IN (
            SELECT rule.source_system_id FROM rules rule WHERE rule.source_kind = 'CANDIDATE'
        ))
    ), universe AS (
        SELECT * FROM bank_universe
        UNION ALL
        SELECT * FROM candidate_universe
    ), hits AS (
        SELECT fact.source_kind, fact.unique_fact_ref, fact.fact_ref, fact.occurred_on,
               fact.amount_minor, rule.rule_key
          FROM universe fact
          JOIN rules rule
            ON (rule.source_kind = 'BANK_TRANSACTION'
                AND fact.source_kind = 'BANK_TRANSACTION'
                AND rule.account_key = fact.account_key)
            OR (rule.source_kind = 'CANDIDATE'
                AND fact.source_kind = 'CANDIDATE'
                AND rule.source_system_id = ANY(fact.source_system_ids))
          JOIN bounds month ON true
         WHERE concat_ws(' ', fact.counterparty_name, fact.transaction_name,
                          fact.counterparty_institution) ~* rule.match_pattern
           AND (rule.amount_direction = 'ANY'
                OR (rule.amount_direction = 'CREDIT' AND fact.amount_minor > 0)
                OR (rule.amount_direction = 'DEBIT' AND fact.amount_minor < 0))
           AND (
                (fact.time_granularity = 'DAY'
                 AND fact.occurred_on >= rule.effective_from
                 AND (rule.effective_to IS NULL OR fact.occurred_on <= rule.effective_to))
                OR
                (fact.time_granularity = 'MONTH'
                 AND rule.effective_from < month.month_end
                 AND (rule.effective_to IS NULL OR rule.effective_to >= month.month_start))
           )
    ), match_counts AS (
        SELECT fact.source_kind, fact.unique_fact_ref, fact.fact_ref, fact.occurred_on,
               fact.amount_minor, count(hit.rule_key)::integer AS match_count,
               COALESCE(
                    array_agg(hit.rule_key ORDER BY hit.rule_key)
                        FILTER (WHERE hit.rule_key IS NOT NULL),
                    ARRAY[]::varchar[]
               ) AS matched_rule_keys
          FROM universe fact
          LEFT JOIN hits hit ON hit.unique_fact_ref = fact.unique_fact_ref
         GROUP BY fact.source_kind, fact.unique_fact_ref, fact.fact_ref,
                  fact.occurred_on, fact.amount_minor
    ), unique_hits AS (
        SELECT hit.rule_key, hit.fact_ref, hit.occurred_on, hit.amount_minor
          FROM hits hit
          JOIN match_counts counts ON counts.unique_fact_ref = hit.unique_fact_ref
         WHERE counts.match_count = 1
    ), aggregates AS (
        SELECT rule.rule_key, rule.flow_kind, rule.business_unit_label, rule.item_label,
               rule.source_kind,
               COALESCE(rule.account_key, rule.source_system_id)::text AS source_ref,
               count(hit.fact_ref)::integer AS transaction_count,
               COALESCE(sum(abs(hit.amount_minor)), 0)::bigint AS amount_minor,
               COALESCE(
                    jsonb_agg(jsonb_build_object(
                        'fact_ref', hit.fact_ref,
                        'occurred_on', hit.occurred_on,
                        'amount_minor', hit.amount_minor
                    ) ORDER BY hit.occurred_on, hit.fact_ref)
                        FILTER (WHERE hit.fact_ref IS NOT NULL),
                    '[]'::jsonb
               ) AS facts
          FROM rules rule
          LEFT JOIN unique_hits hit ON hit.rule_key = rule.rule_key
         GROUP BY rule.rule_key, rule.flow_kind, rule.business_unit_label,
                  rule.item_label, rule.source_kind, rule.account_key, rule.source_system_id
    ), adjustment_rows AS (
        SELECT 'adjustment:' || adjustment.adjustment_id::text AS rule_key,
               adjustment.flow_kind, adjustment.business_unit_label, adjustment.item_label,
               'ADJUSTMENT'::text AS source_kind, 'manual_adjustment'::text AS source_ref,
               1::integer AS transaction_count, adjustment.amount_minor,
               jsonb_build_array(jsonb_build_object(
                    'fact_ref', adjustment.adjustment_id::text,
                    'occurred_on', adjustment.accounting_month,
                    'amount_minor', adjustment.amount_minor
               )) AS facts
          FROM public.cash_reconciliation_adjustment adjustment, bounds month, complete_scope scope
         WHERE adjustment.accounting_month = month.month_start AND scope.value
    ), all_rows AS (
        SELECT * FROM aggregates
        UNION ALL
        SELECT * FROM adjustment_rows
    ), issue_rows AS (
        SELECT counts.*
          FROM match_counts counts
         WHERE counts.match_count <> 1
         ORDER BY counts.occurred_on, counts.unique_fact_ref
         LIMIT 500
    )
    SELECT jsonb_build_object(
        'contract_version', 'ledgerbridge.cash-reconciliation.v2',
        'accounting_month', to_char((SELECT month_start FROM bounds), 'YYYY-MM'),
        'rules', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'rule_key', rule.rule_key,
                'source_kind', rule.source_kind,
                'source_ref', COALESCE(rule.account_key, rule.source_system_id),
                'flow_kind', rule.flow_kind,
                'business_unit_label', rule.business_unit_label,
                'item_label', rule.item_label,
                'match_pattern', rule.match_pattern,
                'amount_direction', rule.amount_direction,
                'effective_from', rule.effective_from,
                'effective_to', rule.effective_to
            ) ORDER BY rule.business_unit_label, rule.flow_kind, rule.item_label, rule.rule_key)
              FROM rules rule
        ), '[]'::jsonb),
        'rows', COALESCE((
            SELECT jsonb_agg(to_jsonb(row) ORDER BY row.business_unit_label,
                             row.flow_kind, row.item_label, row.rule_key)
              FROM all_rows row
        ), '[]'::jsonb),
        'issues', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'issue_kind', CASE WHEN issue.match_count = 0
                                   THEN 'UNMATCHED' ELSE 'MULTIPLE_RULES' END,
                'source_kind', issue.source_kind,
                'fact_ref', issue.unique_fact_ref,
                'occurred_on', issue.occurred_on,
                'amount_minor', issue.amount_minor,
                'matched_rule_keys', to_jsonb(issue.matched_rule_keys)
            ) ORDER BY issue.occurred_on, issue.unique_fact_ref)
              FROM issue_rows issue
        ), '[]'::jsonb),
        'eligible_fact_count', (SELECT count(*) FROM match_counts),
        'matched_fact_count', (SELECT count(*) FROM match_counts WHERE match_count = 1),
        'unmatched_fact_count', (SELECT count(*) FROM match_counts WHERE match_count = 0),
        'conflicted_fact_count', (SELECT count(*) FROM match_counts WHERE match_count > 1),
        'issue_count', (SELECT count(*) FROM match_counts WHERE match_count <> 1),
        'issues_truncated', (SELECT count(*) > 500 FROM match_counts WHERE match_count <> 1),
        'totals', jsonb_build_object(
            'income_minor', COALESCE((SELECT sum(amount_minor) FROM all_rows WHERE flow_kind = 'INCOME'), 0),
            'expense_minor', COALESCE((SELECT sum(amount_minor) FROM all_rows WHERE flow_kind = 'EXPENSE'), 0),
            'current_minor', COALESCE((SELECT sum(amount_minor) FROM all_rows WHERE flow_kind = 'CURRENT'), 0)
        )
    ) INTO v_result;

    RETURN v_result;
END;
$function$;

REVOKE ALL ON FUNCTION internal_read.cash_reconciliation_month_v2(date, uuid[], uuid[])
FROM PUBLIC, ledgerbridge_api, ledgerbridge_worker;
GRANT EXECUTE ON FUNCTION internal_read.cash_reconciliation_month_v2(date, uuid[], uuid[])
TO ledgerbridge_reader;
"""


_DOWNGRADE_SQL = r"""
DROP FUNCTION internal_read.cash_reconciliation_month_v2(date, uuid[], uuid[]);
"""
