"""Persist auditable company bank-transaction classifications.

Revision ID: 20260903_0037
Revises: 20260903_0036
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260903_0037"
down_revision: str | None = "20260903_0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


_UPGRADE_SQL = r"""
CREATE TABLE public.company_transaction_classification (
    transaction_ref uuid NOT NULL
        REFERENCES public.bank_statement_transaction(transaction_ref) ON DELETE RESTRICT,
    revision integer NOT NULL CHECK (revision > 0),
    status varchar(16) NOT NULL CHECK (status IN ('PENDING','CONFIRMED')),
    category_code varchar(64),
    source varchar(16) NOT NULL CHECK (source IN ('AUTO_RULE','HUMAN_REVIEW')),
    rule_version varchar(100) NOT NULL CHECK (btrim(rule_version) <> ''),
    operation_id uuid NOT NULL UNIQUE,
    assertion_jti uuid UNIQUE,
    actor_ref varchar(200) NOT NULL CHECK (btrim(actor_ref) <> ''),
    workload_principal_ref varchar(200),
    expected_revision integer,
    command_sha256 bytea NOT NULL CHECK (octet_length(command_sha256) = 32),
    audit_event_id uuid NOT NULL UNIQUE REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    classified_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (transaction_ref, revision),
    CHECK (
        category_code IS NULL OR category_code IN (
            'PLATFORM_ROOM_REVENUE','RELATED_PARTY_CURRENT','PAYROLL','FINANCING',
            'BOTTLED_WATER','INTERNAL_TRANSFER','RENT','RENTAL_INCOME','BANK_INTEREST',
            'LINEN_LAUNDRY','OPERATING_FEE'
        )
    ),
    CHECK (
        (status = 'PENDING' AND category_code IS NULL)
        OR (status = 'CONFIRMED' AND category_code IS NOT NULL)
    ),
    CHECK (
        (revision = 1 AND source = 'AUTO_RULE' AND assertion_jti IS NULL
            AND workload_principal_ref IS NULL AND expected_revision IS NULL)
        OR (revision > 1 AND source = 'HUMAN_REVIEW' AND assertion_jti IS NOT NULL
            AND btrim(workload_principal_ref) <> '' AND expected_revision = revision - 1
            AND status = 'CONFIRMED')
    )
);

CREATE TRIGGER company_transaction_classification_append_only
BEFORE UPDATE OR DELETE ON public.company_transaction_classification
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();

CREATE FUNCTION public.r1_validate_company_transaction_classification()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
DECLARE
    v_action text; v_rule text; v_payload jsonb; v_actor text;
    v_audit_time timestamptz; v_expected_revision integer; v_previous_status text;
BEGIN
    SELECT action, rule_version, payload, actor, occurred_at
      INTO v_action, v_rule, v_payload, v_actor, v_audit_time
      FROM public.audit_event WHERE id = NEW.audit_event_id;
    IF v_action IS DISTINCT FROM (CASE WHEN NEW.revision = 1
            THEN 'company_transaction_classification.record'
            ELSE 'company_transaction_classification.review' END)
       OR v_rule IS DISTINCT FROM 'ledgerbridge.company-transaction-classification.v1'
       OR v_actor IS DISTINCT FROM NEW.actor_ref
       OR v_payload->>'transaction_ref' IS DISTINCT FROM NEW.transaction_ref::text
       OR v_payload->>'revision' IS DISTINCT FROM NEW.revision::text
       OR v_payload->>'status' IS DISTINCT FROM NEW.status
       OR v_payload->>'category_code' IS DISTINCT FROM NEW.category_code
       OR v_payload->>'source' IS DISTINCT FROM NEW.source
       OR v_payload->>'rule_version' IS DISTINCT FROM NEW.rule_version
       OR v_payload->>'operation_id' IS DISTINCT FROM NEW.operation_id::text
       OR v_payload->>'command_sha256' IS DISTINCT FROM encode(NEW.command_sha256, 'hex')
       OR NEW.classified_at IS DISTINCT FROM v_audit_time THEN
        RAISE EXCEPTION 'company transaction classification audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.revision > 1 AND (
        v_payload->>'assertion_jti' IS DISTINCT FROM NEW.assertion_jti::text
        OR v_payload->>'workload_principal_ref' IS DISTINCT FROM NEW.workload_principal_ref
        OR v_payload->>'expected_revision' IS DISTINCT FROM NEW.expected_revision::text
    ) THEN
        RAISE EXCEPTION 'company transaction review audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM public.bank_statement_transaction AS transaction
          JOIN public.managed_account AS account
            ON account.managed_account_ref = transaction.managed_account_ref
         WHERE transaction.transaction_ref = NEW.transaction_ref
           AND account.owner_kind = 'COMPANY'
    ) OR NOT EXISTS (
        SELECT 1
          FROM public.bank_statement_observation AS observation
          JOIN LATERAL (
                SELECT review.status
                  FROM public.bank_statement_review AS review
                 WHERE review.statement_ref = observation.statement_ref
                 ORDER BY review.revision DESC LIMIT 1
          ) AS latest ON true
         WHERE observation.transaction_ref = NEW.transaction_ref
           AND latest.status = 'CONFIRMED'
    ) THEN
        RAISE EXCEPTION 'classification requires a confirmed company statement transaction'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT coalesce(max(revision), 0) + 1,
           (array_agg(status ORDER BY revision DESC))[1]
      INTO v_expected_revision, v_previous_status
      FROM public.company_transaction_classification
     WHERE transaction_ref = NEW.transaction_ref;
    IF NEW.revision IS DISTINCT FROM v_expected_revision
       OR (NEW.revision > 1 AND v_previous_status IS DISTINCT FROM 'PENDING') THEN
        RAISE EXCEPTION 'company transaction classification revision is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER validate_company_transaction_classification
BEFORE INSERT ON public.company_transaction_classification
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_company_transaction_classification();

CREATE FUNCTION internal_import.seed_company_transaction_classification(
    p_transaction_ref uuid, p_operation_id uuid, p_status text,
    p_category_code text, p_actor_ref text, p_reason text, p_rule_version text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_existing public.company_transaction_classification%ROWTYPE;
    v_command bytea; v_audit uuid;
BEGIN
    IF p_transaction_ref IS NULL OR p_operation_id IS NULL OR p_status IS NULL
       OR p_status NOT IN ('PENDING','CONFIRMED')
       OR ((p_status = 'PENDING') <> (p_category_code IS NULL))
       OR p_category_code IS NOT NULL AND p_category_code NOT IN (
            'PLATFORM_ROOM_REVENUE','RELATED_PARTY_CURRENT','PAYROLL','FINANCING',
            'BOTTLED_WATER','INTERNAL_TRANSFER','RENT','RENTAL_INCOME','BANK_INTEREST',
            'LINEN_LAUNDRY','OPERATING_FEE')
       OR p_actor_ref IS NULL OR btrim(p_actor_ref) = '' OR length(p_actor_ref) > 200
       OR p_reason IS NULL OR btrim(p_reason) = '' OR length(p_reason) > 1000
       OR p_rule_version IS DISTINCT FROM 'company-bank-classification.2026-09.v1' THEN
        RAISE EXCEPTION 'company transaction classification seed is invalid'
            USING ERRCODE = 'LB003';
    END IF;
    v_command := public.digest(convert_to(jsonb_build_array(
        p_transaction_ref, p_operation_id, p_status, p_category_code,
        p_actor_ref, p_reason, p_rule_version
    )::text, 'UTF8'), 'sha256');
    SELECT * INTO v_existing
      FROM public.company_transaction_classification
     WHERE operation_id = p_operation_id;
    IF FOUND THEN
        IF v_existing.transaction_ref IS DISTINCT FROM p_transaction_ref
           OR v_existing.status IS DISTINCT FROM p_status
           OR v_existing.category_code IS DISTINCT FROM p_category_code
           OR v_existing.command_sha256 IS DISTINCT FROM v_command THEN
            RAISE EXCEPTION 'company transaction classification idempotency conflict'
                USING ERRCODE = 'LB001';
        END IF;
        RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
            'status', p_status, 'revision', v_existing.revision, 'created', false);
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(p_transaction_ref::text, 0));
    IF EXISTS (SELECT 1 FROM public.company_transaction_classification
                WHERE transaction_ref = p_transaction_ref) THEN
        RAISE EXCEPTION 'company transaction is already classified' USING ERRCODE = 'LB003';
    END IF;
    v_audit := public.append_audit_event(
        p_actor_ref, 'company_transaction_classification.record', p_reason,
        'ledgerbridge.company-transaction-classification.v1',
        jsonb_build_object(
            'transaction_ref', p_transaction_ref, 'revision', 1, 'status', p_status,
            'category_code', p_category_code, 'source', 'AUTO_RULE',
            'rule_version', p_rule_version, 'operation_id', p_operation_id,
            'command_sha256', encode(v_command, 'hex')
        )
    );
    INSERT INTO public.company_transaction_classification(
        transaction_ref, revision, status, category_code, source, rule_version,
        operation_id, actor_ref, command_sha256, audit_event_id, classified_at
    ) VALUES (
        p_transaction_ref, 1, p_status, p_category_code, 'AUTO_RULE', p_rule_version,
        p_operation_id, p_actor_ref, v_command, v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
    );
    RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
        'status', p_status, 'revision', 1, 'created', true);
END
$function$;

CREATE FUNCTION internal_command.review_company_transaction_classification(
    p_transaction_ref uuid, p_entity_ref uuid, p_operation_id uuid,
    p_assertion_jti uuid, p_actor_ref text, p_workload_principal_ref text,
    p_expected_revision integer, p_category_code text, p_reason text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_existing public.company_transaction_classification%ROWTYPE;
    v_current public.company_transaction_classification%ROWTYPE;
    v_command bytea; v_audit uuid; v_revision integer;
BEGIN
    IF p_transaction_ref IS NULL OR p_entity_ref IS NULL OR p_operation_id IS NULL
       OR p_assertion_jti IS NULL OR p_expected_revision IS NULL OR p_expected_revision < 1
       OR p_category_code IS NULL OR p_category_code NOT IN (
            'PLATFORM_ROOM_REVENUE','RELATED_PARTY_CURRENT','PAYROLL','FINANCING',
            'BOTTLED_WATER','INTERNAL_TRANSFER','RENT','RENTAL_INCOME','BANK_INTEREST',
            'LINEN_LAUNDRY','OPERATING_FEE')
       OR p_actor_ref IS NULL OR btrim(p_actor_ref) = '' OR length(p_actor_ref) > 200
       OR p_workload_principal_ref IS NULL OR btrim(p_workload_principal_ref) = ''
       OR length(p_workload_principal_ref) > 200
       OR p_reason IS NULL OR btrim(p_reason) = '' OR length(p_reason) > 1000 THEN
        RAISE EXCEPTION 'company transaction review command is invalid' USING ERRCODE = 'LB003';
    END IF;
    v_command := public.digest(convert_to(jsonb_build_array(
        p_transaction_ref, p_entity_ref, p_operation_id, p_assertion_jti,
        p_actor_ref, p_workload_principal_ref, p_expected_revision,
        p_category_code, p_reason
    )::text, 'UTF8'), 'sha256');
    SELECT * INTO v_existing FROM public.company_transaction_classification
     WHERE operation_id = p_operation_id OR assertion_jti = p_assertion_jti
     ORDER BY operation_id = p_operation_id DESC LIMIT 1;
    IF FOUND THEN
        IF v_existing.transaction_ref IS DISTINCT FROM p_transaction_ref
           OR v_existing.category_code IS DISTINCT FROM p_category_code
           OR v_existing.command_sha256 IS DISTINCT FROM v_command THEN
            RAISE EXCEPTION 'company transaction review idempotency conflict'
                USING ERRCODE = 'LB001';
        END IF;
        RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
            'status', v_existing.status, 'category_code', v_existing.category_code,
            'revision', v_existing.revision, 'created', false);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.bank_statement_transaction AS transaction
        JOIN public.managed_account AS account
          ON account.managed_account_ref = transaction.managed_account_ref
        WHERE transaction.transaction_ref = p_transaction_ref
          AND account.entity_id = p_entity_ref AND account.owner_kind = 'COMPANY'
    ) THEN
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
    v_revision := v_current.revision + 1;
    v_audit := public.append_audit_event(
        p_actor_ref, 'company_transaction_classification.review', p_reason,
        'ledgerbridge.company-transaction-classification.v1',
        jsonb_build_object(
            'transaction_ref', p_transaction_ref, 'revision', v_revision,
            'status', 'CONFIRMED', 'category_code', p_category_code,
            'source', 'HUMAN_REVIEW', 'rule_version', 'human-review.v1',
            'operation_id', p_operation_id, 'assertion_jti', p_assertion_jti,
            'workload_principal_ref', p_workload_principal_ref,
            'expected_revision', p_expected_revision,
            'command_sha256', encode(v_command, 'hex')
        )
    );
    INSERT INTO public.company_transaction_classification(
        transaction_ref, revision, status, category_code, source, rule_version,
        operation_id, assertion_jti, actor_ref, workload_principal_ref,
        expected_revision, command_sha256, audit_event_id, classified_at
    ) VALUES (
        p_transaction_ref, v_revision, 'CONFIRMED', p_category_code,
        'HUMAN_REVIEW', 'human-review.v1', p_operation_id, p_assertion_jti,
        p_actor_ref, p_workload_principal_ref, p_expected_revision, v_command,
        v_audit, (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
    );
    RETURN jsonb_build_object('transaction_ref', p_transaction_ref,
        'status', 'CONFIRMED', 'category_code', p_category_code,
        'revision', v_revision, 'created', true);
END
$function$;

CREATE FUNCTION internal_read.list_company_transaction_classifications_as_of(
    p_entity_ref uuid, p_status text, p_audit_horizon_sequence bigint,
    p_audit_horizon_hash bytea, p_limit integer
) RETURNS TABLE(item jsonb) LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
BEGIN
    IF p_entity_ref IS NULL OR p_status NOT IN ('PENDING','CONFIRMED')
       OR p_audit_horizon_sequence IS NULL OR p_audit_horizon_hash IS NULL
       OR octet_length(p_audit_horizon_hash) <> 32 OR p_limit IS NULL
       OR p_limit NOT BETWEEN 1 AND 200
       OR NOT EXISTS (SELECT 1 FROM public.audit_event
            WHERE sequence = p_audit_horizon_sequence AND hash = p_audit_horizon_hash) THEN
        RAISE EXCEPTION 'company transaction classification query is invalid'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT jsonb_build_object(
        'transaction_ref', transaction.transaction_ref,
        'entity_ref', account.entity_id,
        'occurred_at', transaction.occurred_at,
        'amount_minor', transaction.amount_minor,
        'currency', transaction.currency,
        'counterparty_name', transaction.counterparty_name,
        'transaction_name', transaction.transaction_name,
        'status', current.status,
        'category_code', current.category_code,
        'cashflow_role', CASE current.category_code
            WHEN 'PLATFORM_ROOM_REVENUE' THEN 'OPERATING_INCOME'
            WHEN 'BANK_INTEREST' THEN 'OPERATING_INCOME'
            WHEN 'RENTAL_INCOME' THEN 'OPERATING_INCOME'
            WHEN 'PAYROLL' THEN 'OPERATING_EXPENSE'
            WHEN 'BOTTLED_WATER' THEN 'OPERATING_EXPENSE'
            WHEN 'LINEN_LAUNDRY' THEN 'OPERATING_EXPENSE'
            WHEN 'RENT' THEN 'OPERATING_EXPENSE'
            WHEN 'OPERATING_FEE' THEN 'OPERATING_EXPENSE'
            ELSE CASE WHEN current.category_code IS NULL
                THEN NULL ELSE 'NON_OPERATING' END END,
        'revision', current.revision,
        'source', current.source,
        'rule_version', current.rule_version
    )
      FROM public.bank_statement_transaction AS transaction
      JOIN public.managed_account AS account
        ON account.managed_account_ref = transaction.managed_account_ref
      JOIN LATERAL (
            SELECT classification.*
              FROM public.company_transaction_classification AS classification
              JOIN public.audit_event AS event ON event.id = classification.audit_event_id
             WHERE classification.transaction_ref = transaction.transaction_ref
               AND event.sequence <= p_audit_horizon_sequence
             ORDER BY classification.revision DESC LIMIT 1
      ) AS current ON true
     WHERE account.entity_id = p_entity_ref AND account.owner_kind = 'COMPANY'
       AND current.status = p_status
     ORDER BY transaction.occurred_at DESC, transaction.transaction_ref DESC
     LIMIT p_limit;
END
$function$;

CREATE FUNCTION internal_read.get_company_transaction_classification_summary_as_of(
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
               classification.status, classification.category_code
          FROM public.bank_statement_transaction AS transaction
          JOIN public.managed_account AS account
            ON account.managed_account_ref = transaction.managed_account_ref
          JOIN LATERAL (
                SELECT item.status, item.category_code
                  FROM public.company_transaction_classification AS item
                  JOIN public.audit_event AS event ON event.id = item.audit_event_id
                 WHERE item.transaction_ref = transaction.transaction_ref
                   AND event.sequence <= p_audit_horizon_sequence
                 ORDER BY item.revision DESC LIMIT 1
          ) AS classification ON true
         WHERE account.entity_id = p_entity_ref AND account.owner_kind = 'COMPANY'
           AND transaction.occurred_at >= p_from_date::timestamptz
           AND transaction.occurred_at < p_to_date_exclusive::timestamptz
    ), totals AS (
        SELECT count(*) FILTER (WHERE status = 'CONFIRMED')::bigint AS confirmed_count,
               count(*) FILTER (WHERE status = 'PENDING')::bigint AS pending_count,
               coalesce(sum(abs(amount_minor)) FILTER (WHERE status = 'CONFIRMED'), 0)::bigint
                    AS confirmed_gross_minor
          FROM current
    ), categories AS (
        SELECT category_code, count(*)::bigint AS transaction_count,
               coalesce(sum(amount_minor) FILTER (
                   WHERE amount_minor > 0), 0)::bigint AS inflow_minor,
               coalesce(-sum(amount_minor) FILTER (
                   WHERE amount_minor < 0), 0)::bigint AS outflow_minor,
               sum(amount_minor)::bigint AS net_minor,
               sum(abs(amount_minor))::bigint AS gross_minor
          FROM current WHERE status = 'CONFIRMED' GROUP BY category_code
    )
    SELECT jsonb_build_object(
        'entity_ref', p_entity_ref, 'from_date', p_from_date,
        'to_date_exclusive', p_to_date_exclusive,
        'confirmed_count', totals.confirmed_count,
        'pending_count', totals.pending_count,
        'confirmed_gross_minor', totals.confirmed_gross_minor,
        'categories', coalesce((SELECT jsonb_agg(jsonb_build_object(
            'category_code', categories.category_code,
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
                round(categories.gross_minor * 1000000.0 / totals.confirmed_gross_minor)::bigint END
        ) ORDER BY categories.category_code) FROM categories), '[]'::jsonb)
    ) INTO v_result FROM totals;
    RETURN v_result;
END
$function$;

REVOKE ALL ON TABLE public.company_transaction_classification
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
REVOKE ALL ON FUNCTION public.r1_validate_company_transaction_classification(),
    internal_import.seed_company_transaction_classification(uuid,uuid,text,text,text,text,text),
    internal_command.review_company_transaction_classification(
        uuid,uuid,uuid,uuid,text,text,integer,text,text),
    internal_read.list_company_transaction_classifications_as_of(uuid,text,bigint,bytea,integer),
    internal_read.get_company_transaction_classification_summary_as_of(
        uuid,date,date,bigint,bytea)
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
GRANT EXECUTE ON FUNCTION
    internal_import.seed_company_transaction_classification(uuid,uuid,text,text,text,text,text)
    TO ledgerbridge_worker;
GRANT EXECUTE ON FUNCTION internal_command.review_company_transaction_classification(
    uuid,uuid,uuid,uuid,text,text,integer,text,text) TO ledgerbridge_api;
GRANT EXECUTE ON FUNCTION internal_read.list_company_transaction_classifications_as_of(
    uuid,text,bigint,bytea,integer) TO ledgerbridge_reader;
GRANT EXECUTE ON FUNCTION internal_read.get_company_transaction_classification_summary_as_of(
    uuid,date,date,bigint,bytea) TO ledgerbridge_reader;
"""


def downgrade() -> None:
    if os.getenv("LEDGERBRIDGE_ENV", "development").lower() == "production":
        raise RuntimeError("company transaction classifications are irreversible in production")
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM public.company_transaction_classification)")
    ).scalar_one():
        raise RuntimeError("development downgrade would discard classification facts")
    op.execute(_DOWNGRADE_SQL)


_DOWNGRADE_SQL = r"""
DROP FUNCTION internal_read.get_company_transaction_classification_summary_as_of(
    uuid,date,date,bigint,bytea);
DROP FUNCTION internal_read.list_company_transaction_classifications_as_of(
    uuid,text,bigint,bytea,integer);
DROP FUNCTION internal_command.review_company_transaction_classification(
    uuid,uuid,uuid,uuid,text,text,integer,text,text);
DROP FUNCTION internal_import.seed_company_transaction_classification(
    uuid,uuid,text,text,text,text,text);
DROP TABLE public.company_transaction_classification;
DROP FUNCTION public.r1_validate_company_transaction_classification();
"""
