"""Require an explicit operating-fee reporting item during human review.

Revision ID: 20260904_0046
Revises: 20260904_0045
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260904_0046"
down_revision: str | None = "20260904_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


_UPGRADE_SQL = r"""
DO $seed$
DECLARE
    v_item record;
    v_existing public.company_transaction_reporting_item%ROWTYPE;
    v_audit uuid;
BEGIN
    FOR v_item IN SELECT * FROM (VALUES
        ('DISINFECTION','消杀费用'),
        ('FIRE_SAFETY','消防费用'),
        ('ELEVATOR','电梯费用'),
        ('TAX','税费'),
        ('HOTEL_TECH','酒店智能设备'),
        ('BANK_FEES','银行手续费')
    ) AS seeded(item_code, item_label)
    LOOP
        SELECT * INTO v_existing
          FROM public.company_transaction_reporting_item item
         WHERE item.item_code = v_item.item_code
         ORDER BY item.revision DESC
         LIMIT 1;
        IF FOUND THEN
            IF v_existing.status IS DISTINCT FROM 'ACTIVE'
               OR v_existing.category_code IS DISTINCT FROM 'OPERATING_FEE'
               OR v_existing.item_label IS DISTINCT FROM v_item.item_label THEN
                RAISE EXCEPTION 'operating fee reporting item registry drift'
                    USING ERRCODE = '23514';
            END IF;
        ELSE
            v_audit := public.append_audit_event(
                'migration:20260904_0046',
                'company_transaction_reporting_item.record',
                'seed shared operating fee reporting item',
                'ledgerbridge.company-transaction-reporting-item.v1',
                jsonb_build_object(
                    'item_code', v_item.item_code,
                    'revision', 1,
                    'status', 'ACTIVE',
                    'category_code', 'OPERATING_FEE',
                    'item_label', v_item.item_label,
                    'match_counterparty_name', NULL
                )
            );
            INSERT INTO public.company_transaction_reporting_item(
                item_code, revision, status, category_code, item_label,
                match_counterparty_name, audit_event_id, created_at
            ) VALUES (
                v_item.item_code, 1, 'ACTIVE', 'OPERATING_FEE', v_item.item_label,
                NULL, v_audit,
                (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
            );
        END IF;
    END LOOP;
END
$seed$;

CREATE FUNCTION internal_command.review_company_transaction_classification_v2(
    p_transaction_ref uuid, p_entity_ref uuid, p_operation_id uuid,
    p_assertion_jti uuid, p_actor_ref text, p_workload_principal_ref text,
    p_expected_revision integer, p_category_code text,
    p_reporting_item_code text, p_reason text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_existing public.company_transaction_classification%ROWTYPE;
    v_current public.company_transaction_classification%ROWTYPE;
    v_command bytea;
    v_audit uuid;
    v_revision integer;
    v_item_code text;
    v_item_revision integer;
BEGIN
    IF p_transaction_ref IS NULL OR p_entity_ref IS NULL OR p_operation_id IS NULL
       OR p_assertion_jti IS NULL OR p_expected_revision IS NULL OR p_expected_revision < 1
       OR p_category_code IS NULL OR p_category_code NOT IN (
            'PLATFORM_ROOM_REVENUE','RELATED_PARTY_CURRENT','PAYROLL','FINANCING',
            'BOTTLED_WATER','INTERNAL_TRANSFER','RENT','RENTAL_INCOME','BANK_INTEREST',
            'LINEN_LAUNDRY','OPERATING_FEE')
       OR (p_category_code = 'OPERATING_FEE' AND (
            p_reporting_item_code IS NULL OR btrim(p_reporting_item_code) = ''
            OR p_reporting_item_code IS DISTINCT FROM btrim(p_reporting_item_code)
            OR length(p_reporting_item_code) > 100))
       OR (p_category_code <> 'OPERATING_FEE' AND p_reporting_item_code IS NOT NULL)
       OR p_actor_ref IS NULL OR btrim(p_actor_ref) = '' OR length(p_actor_ref) > 200
       OR p_workload_principal_ref IS NULL OR btrim(p_workload_principal_ref) = ''
       OR length(p_workload_principal_ref) > 200
       OR p_reason IS NULL OR btrim(p_reason) = '' OR length(p_reason) > 1000 THEN
        RAISE EXCEPTION 'company transaction review command is invalid' USING ERRCODE = 'LB003';
    END IF;
    v_command := public.digest(convert_to(jsonb_build_array(
        p_transaction_ref, p_entity_ref, p_operation_id, p_assertion_jti,
        p_actor_ref, p_workload_principal_ref, p_expected_revision,
        p_category_code, p_reporting_item_code, p_reason)::text, 'UTF8'), 'sha256');
    PERFORM pg_advisory_xact_lock(hashtextextended(p_transaction_ref::text, 0));
    SELECT * INTO v_existing FROM public.company_transaction_classification
     WHERE operation_id = p_operation_id OR assertion_jti = p_assertion_jti
     ORDER BY operation_id = p_operation_id DESC LIMIT 1;
    IF FOUND THEN
        IF v_existing.transaction_ref IS DISTINCT FROM p_transaction_ref
           OR v_existing.category_code IS DISTINCT FROM p_category_code
           OR (p_category_code = 'OPERATING_FEE'
               AND v_existing.reporting_item_code IS DISTINCT FROM p_reporting_item_code)
           OR v_existing.command_sha256 IS DISTINCT FROM v_command THEN
            RAISE EXCEPTION 'company transaction review idempotency conflict'
                USING ERRCODE = 'LB001';
        END IF;
        RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
            'status', v_existing.status, 'category_code', v_existing.category_code,
            'reporting_item_code', v_existing.reporting_item_code,
            'reporting_item_revision', v_existing.reporting_item_revision,
            'revision', v_existing.revision, 'created', false);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.bank_statement_transaction transaction
        JOIN public.managed_account account
          ON account.managed_account_ref = transaction.managed_account_ref
        WHERE transaction.transaction_ref = p_transaction_ref
          AND account.entity_id = p_entity_ref AND account.owner_kind = 'COMPANY') THEN
        RAISE EXCEPTION 'company transaction is outside authorized entity' USING ERRCODE = 'LB004';
    END IF;
    SELECT * INTO v_current FROM public.company_transaction_classification
     WHERE transaction_ref = p_transaction_ref ORDER BY revision DESC LIMIT 1 FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'company transaction classification is not visible' USING ERRCODE = 'LB004';
    END IF;
    IF v_current.revision IS DISTINCT FROM p_expected_revision THEN
        RAISE EXCEPTION 'company transaction classification revision is stale'
            USING ERRCODE = 'LB002';
    END IF;
    IF v_current.status IS DISTINCT FROM 'PENDING' THEN
        RAISE EXCEPTION 'company transaction classification is already terminal'
            USING ERRCODE = 'LB003';
    END IF;
    IF p_category_code = 'OPERATING_FEE' THEN
        SELECT registry.item_code, registry.revision
          INTO v_item_code, v_item_revision
          FROM public.company_transaction_reporting_item registry
         WHERE registry.category_code = 'OPERATING_FEE'
           AND registry.item_code = p_reporting_item_code
           AND registry.revision = (
                SELECT max(latest.revision)
                  FROM public.company_transaction_reporting_item latest
                 WHERE latest.item_code = registry.item_code)
           AND registry.status = 'ACTIVE';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'operating fee reporting item is unavailable'
                USING ERRCODE = 'LB003';
        END IF;
    ELSE
        SELECT * INTO v_item_code, v_item_revision
          FROM internal_import.resolve_company_transaction_reporting_item(
            p_transaction_ref, p_category_code, p_actor_ref, p_reason);
    END IF;
    v_revision := v_current.revision + 1;
    v_audit := public.append_audit_event(
        p_actor_ref, 'company_transaction_classification.review', p_reason,
        'ledgerbridge.company-transaction-classification.v1',
        jsonb_build_object('transaction_ref', p_transaction_ref, 'revision', v_revision,
            'status', 'CONFIRMED', 'category_code', p_category_code,
            'reporting_item_code', v_item_code,
            'reporting_item_revision', v_item_revision,
            'source', 'HUMAN_REVIEW', 'rule_version', 'human-review.v2',
            'operation_id', p_operation_id, 'assertion_jti', p_assertion_jti,
            'workload_principal_ref', p_workload_principal_ref,
            'expected_revision', p_expected_revision,
            'requested_reporting_item_code', p_reporting_item_code,
            'command_sha256', encode(v_command, 'hex')));
    INSERT INTO public.company_transaction_classification(
        transaction_ref, revision, status, category_code, reporting_item_code,
        reporting_item_revision, source, rule_version, operation_id, assertion_jti,
        actor_ref, workload_principal_ref, expected_revision, command_sha256,
        audit_event_id, classified_at)
    VALUES (p_transaction_ref, v_revision, 'CONFIRMED', p_category_code,
        v_item_code, v_item_revision, 'HUMAN_REVIEW', 'human-review.v2',
        p_operation_id, p_assertion_jti, p_actor_ref, p_workload_principal_ref,
        p_expected_revision, v_command, v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit));
    RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
        'status', 'CONFIRMED', 'category_code', p_category_code,
        'reporting_item_code', v_item_code,
        'reporting_item_revision', v_item_revision,
        'revision', v_revision, 'created', true);
END
$function$;

CREATE FUNCTION internal_read.get_company_transaction_classification_summary_v2_as_of(
    p_entity_ref uuid, p_from_date date, p_to_date_exclusive date,
    p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea
) RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE v_result jsonb;
BEGIN
    IF p_entity_ref IS NULL OR p_from_date IS NULL OR p_to_date_exclusive IS NULL
       OR p_from_date >= p_to_date_exclusive OR p_audit_horizon_sequence IS NULL
       OR p_audit_horizon_hash IS NULL OR octet_length(p_audit_horizon_hash) <> 32
       OR NOT EXISTS (SELECT 1 FROM public.audit_event
            WHERE sequence = p_audit_horizon_sequence AND hash = p_audit_horizon_hash) THEN
        RAISE EXCEPTION 'company transaction classification summary query is invalid'
            USING ERRCODE = '22023';
    END IF;
    WITH current AS (
        SELECT transaction.transaction_ref, transaction.amount_minor,
               classification.status, classification.category_code,
               CASE WHEN classification.category_code = 'OPERATING_FEE'
                    THEN classification.reporting_item_code END AS reporting_item_code,
               CASE WHEN classification.category_code = 'OPERATING_FEE'
                    THEN registry.item_label END AS reporting_item_label
          FROM public.bank_statement_transaction AS transaction
          JOIN public.managed_account AS account
            ON account.managed_account_ref = transaction.managed_account_ref
          JOIN LATERAL (
                SELECT item.status, item.category_code,
                       item.reporting_item_code, item.reporting_item_revision
                  FROM public.company_transaction_classification AS item
                  JOIN public.audit_event AS event ON event.id = item.audit_event_id
                 WHERE item.transaction_ref = transaction.transaction_ref
                   AND event.sequence <= p_audit_horizon_sequence
                 ORDER BY item.revision DESC LIMIT 1
          ) AS classification ON true
          LEFT JOIN public.company_transaction_reporting_item AS registry
            ON registry.category_code = classification.category_code
           AND registry.item_code = classification.reporting_item_code
           AND registry.revision = classification.reporting_item_revision
         WHERE account.entity_id = p_entity_ref AND account.owner_kind = 'COMPANY'
           AND transaction.occurred_at >= p_from_date::timestamptz
           AND transaction.occurred_at < p_to_date_exclusive::timestamptz
    ), totals AS (
        SELECT count(*) FILTER (WHERE status = 'CONFIRMED')::bigint AS confirmed_count,
               count(*) FILTER (WHERE status = 'PENDING')::bigint AS pending_count,
               coalesce(sum(abs(amount_minor)) FILTER (
                   WHERE status = 'CONFIRMED'), 0)::bigint AS confirmed_gross_minor
          FROM current
    ), categories AS (
        SELECT category_code, reporting_item_code, reporting_item_label,
               count(*)::bigint AS transaction_count,
               coalesce(sum(amount_minor) FILTER (
                   WHERE amount_minor > 0), 0)::bigint AS inflow_minor,
               coalesce(-sum(amount_minor) FILTER (
                   WHERE amount_minor < 0), 0)::bigint AS outflow_minor,
               sum(amount_minor)::bigint AS net_minor,
               sum(abs(amount_minor))::bigint AS gross_minor
          FROM current
         WHERE status = 'CONFIRMED'
         GROUP BY category_code, reporting_item_code, reporting_item_label
    )
    SELECT jsonb_build_object(
        'entity_ref', p_entity_ref, 'from_date', p_from_date,
        'to_date_exclusive', p_to_date_exclusive,
        'confirmed_count', totals.confirmed_count,
        'pending_count', totals.pending_count,
        'confirmed_gross_minor', totals.confirmed_gross_minor,
        'categories', coalesce((SELECT jsonb_agg(jsonb_build_object(
            'category_code', categories.category_code,
            'reporting_item_code', categories.reporting_item_code,
            'reporting_item_label', categories.reporting_item_label,
            'cashflow_role', CASE categories.category_code
                WHEN 'PLATFORM_ROOM_REVENUE' THEN 'OPERATING_INCOME'
                WHEN 'BANK_INTEREST' THEN 'OPERATING_INCOME'
                WHEN 'RENTAL_INCOME' THEN 'OPERATING_INCOME'
                WHEN 'PAYROLL' THEN 'OPERATING_EXPENSE'
                WHEN 'BOTTLED_WATER' THEN 'OPERATING_EXPENSE'
                WHEN 'LINEN_LAUNDRY' THEN 'OPERATING_EXPENSE'
                WHEN 'RENT' THEN 'OPERATING_EXPENSE'
                WHEN 'OPERATING_FEE' THEN 'OPERATING_EXPENSE'
                ELSE 'NON_OPERATING' END,
            'transaction_count', categories.transaction_count,
            'inflow_minor', categories.inflow_minor,
            'outflow_minor', categories.outflow_minor,
            'net_minor', categories.net_minor,
            'gross_minor', categories.gross_minor,
            'transaction_share_ppm', CASE WHEN totals.confirmed_count = 0 THEN 0 ELSE
                round(categories.transaction_count * 1000000.0 /
                    totals.confirmed_count)::bigint END,
            'gross_share_ppm', CASE WHEN totals.confirmed_gross_minor = 0 THEN 0 ELSE
                round(categories.gross_minor * 1000000.0 /
                    totals.confirmed_gross_minor)::bigint END
        ) ORDER BY categories.category_code, categories.reporting_item_code)
        FROM categories), '[]'::jsonb)
    ) INTO v_result FROM totals;
    RETURN v_result;
END
$function$;

REVOKE ALL ON FUNCTION internal_command.review_company_transaction_classification(
    uuid,uuid,uuid,uuid,text,text,integer,text,text)
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
REVOKE ALL ON FUNCTION internal_command.review_company_transaction_classification_v2(
    uuid,uuid,uuid,uuid,text,text,integer,text,text,text)
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_worker, ledgerbridge_app;
GRANT EXECUTE ON FUNCTION internal_command.review_company_transaction_classification_v2(
    uuid,uuid,uuid,uuid,text,text,integer,text,text,text)
TO ledgerbridge_api;
REVOKE ALL ON FUNCTION internal_read.get_company_transaction_classification_summary_as_of(
    uuid,date,date,bigint,bytea)
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
REVOKE ALL ON FUNCTION internal_read.get_company_transaction_classification_summary_v2_as_of(
    uuid,date,date,bigint,bytea)
FROM PUBLIC, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
GRANT EXECUTE ON FUNCTION
    internal_read.get_company_transaction_classification_summary_v2_as_of(
        uuid,date,date,bigint,bytea)
TO ledgerbridge_reader;
"""


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)


_DOWNGRADE_SQL = r"""
REVOKE ALL ON FUNCTION internal_command.review_company_transaction_classification_v2(
    uuid,uuid,uuid,uuid,text,text,integer,text,text,text)
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
DROP FUNCTION internal_command.review_company_transaction_classification_v2(
    uuid,uuid,uuid,uuid,text,text,integer,text,text,text);
REVOKE ALL ON FUNCTION internal_read.get_company_transaction_classification_summary_v2_as_of(
    uuid,date,date,bigint,bytea)
FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
DROP FUNCTION internal_read.get_company_transaction_classification_summary_v2_as_of(
    uuid,date,date,bigint,bytea);
GRANT EXECUTE ON FUNCTION internal_command.review_company_transaction_classification(
    uuid,uuid,uuid,uuid,text,text,integer,text,text)
TO ledgerbridge_api;
GRANT EXECUTE ON FUNCTION internal_read.get_company_transaction_classification_summary_as_of(
    uuid,date,date,bigint,bytea)
TO ledgerbridge_reader;
"""
