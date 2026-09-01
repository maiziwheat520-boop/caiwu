"""Admit allowlisted bank parser profiles through the existing import seam.

Revision ID: 20260902_0030
Revises: 20260901_0028
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260902_0030"
# Production Core is still at 0028; 0030 intentionally advances that single head.
down_revision: str | None = "20260901_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


_UPGRADE_SQL = r"""
CREATE OR REPLACE FUNCTION internal_import.import_bank_statement(p_request jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_statement uuid; v_account uuid; v_owner uuid; v_evidence uuid;
    v_source_size bigint; v_transaction_count integer;
    v_period_start date; v_period_end date;
    v_source bytea; v_set_digest bytea;
    v_existing public.bank_statement%ROWTYPE;
    v_evidence_row public.evidence_object%ROWTYPE;
    v_transaction public.bank_statement_transaction%ROWTYPE;
    v_item jsonb; v_audit uuid; v_count integer := 0; v_review text;
    v_transaction_ref uuid; v_fact_digest bytea;
    v_source_event uuid; v_source_row_number integer;
    v_occurred_at timestamptz; v_amount_minor bigint; v_balance_minor bigint;
    v_profile text; v_lifecycle_status text; v_statement_payload jsonb;
    v_account_institution text; v_account_suffix text; v_account_owner_kind text;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_request->'transactions') IS DISTINCT FROM 'array'
       OR p_request ?| ARRAY['owner_ref','owner_kind','account_kind','account_key','entity_ref']
       OR btrim(coalesce(p_request->>'owner_entity_ref','')) = ''
       OR btrim(coalesce(p_request->>'managed_account_ref','')) = '' THEN
        RAISE EXCEPTION 'bank statement request is invalid' USING ERRCODE = '22023';
    END IF;
    v_profile := nullif(p_request->>'parser_profile', '');
    IF (p_request->>'institution_code') = 'mybank'
       AND (p_request->>'source_system') = 'mybank_xlsx_export'
       AND (p_request->>'declared_media_type') =
           'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
       AND (v_profile IS NULL OR v_profile = 'mybank_xlsx_v1') THEN
        v_profile := 'mybank_xlsx_v1';
    ELSIF (p_request->>'institution_code') = 'ccb'
       AND (p_request->>'source_system') = 'ccb_personal_xls_export'
       AND (p_request->>'declared_media_type') = 'application/vnd.ms-excel'
       AND v_profile = 'ccb_personal_xls_v1' THEN
        NULL;
    ELSE
        RAISE EXCEPTION 'bank statement parser profile is invalid' USING ERRCODE = '22023';
    END IF;
    IF coalesce(p_request->>'source_sha256','') !~ '^[0-9a-f]{64}$'
       OR coalesce(p_request->>'transaction_set_sha256','') !~ '^[0-9a-f]{64}$'
       OR coalesce(p_request->>'account_suffix','') !~ '^[0-9]{4,8}$'
       OR (p_request->>'currency') IS DISTINCT FROM 'CNY'
       OR jsonb_array_length(p_request->'transactions') NOT BETWEEN 1 AND 100000
       OR coalesce(p_request->>'transaction_count','') !~ '^[0-9]{1,6}$'
       OR coalesce(p_request->>'source_size','') !~ '^[0-9]{1,20}$'
       OR coalesce(p_request->>'period_start','') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
       OR coalesce(p_request->>'period_end','') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
       OR btrim(coalesce(p_request->>'actor','')) = ''
       OR length(p_request->>'actor') > 200
       OR btrim(coalesce(p_request->>'reason','')) = ''
       OR length(p_request->>'reason') > 1000 THEN
        RAISE EXCEPTION 'bank statement request is invalid' USING ERRCODE = '22023';
    END IF;
    BEGIN
        v_statement := (p_request->>'statement_ref')::uuid;
        v_account := (p_request->>'managed_account_ref')::uuid;
        v_owner := (p_request->>'owner_entity_ref')::uuid;
        v_evidence := (p_request->>'evidence_ref')::uuid;
        v_transaction_count := (p_request->>'transaction_count')::integer;
        v_source_size := (p_request->>'source_size')::bigint;
        v_period_start := (p_request->>'period_start')::date;
        v_period_end := (p_request->>'period_end')::date;
    EXCEPTION
        WHEN invalid_text_representation OR numeric_value_out_of_range
             OR invalid_datetime_format OR datetime_field_overflow THEN
            RAISE EXCEPTION 'bank statement request is invalid' USING ERRCODE = '22023';
    END;
    IF v_statement IS NULL OR v_account IS NULL OR v_owner IS NULL OR v_evidence IS NULL
       OR v_transaction_count IS NULL
       OR v_transaction_count <> jsonb_array_length(p_request->'transactions')
       OR v_source_size IS NULL OR v_source_size <= 0
       OR v_period_start IS NULL OR v_period_end IS NULL
       OR v_period_start > v_period_end THEN
        RAISE EXCEPTION 'bank statement request is invalid' USING ERRCODE = '22023';
    END IF;
    FOR v_item IN SELECT value FROM jsonb_array_elements(p_request->'transactions') LOOP
        IF jsonb_typeof(v_item) IS DISTINCT FROM 'object'
           OR coalesce(v_item->>'source_row_sha256','') !~ '^[0-9a-f]{64}$'
           OR coalesce(v_item->>'counterparty_ref','') !~ '^cp_[a-z0-9_]{1,96}$'
           OR coalesce(v_item->>'source_row_number','') !~ '^[0-9]{1,10}$'
           OR coalesce(v_item->>'amount_minor','') !~ '^-?[0-9]{1,20}$'
           OR coalesce(v_item->>'balance_minor','') !~ '^-?[0-9]{1,20}$'
           OR coalesce(v_item->>'occurred_at','') !~ 'T.*(Z|[+-][0-9]{2}:[0-9]{2})$'
           OR length(coalesce(v_item->>'occurred_at','')) > 64
           OR btrim(coalesce(v_item->>'source_event_ref','')) = ''
           OR length(v_item->>'source_event_ref') > 64
           OR btrim(coalesce(v_item->>'transaction_serial','')) = ''
           OR length(v_item->>'transaction_serial') > 300
           OR btrim(coalesce(v_item->>'transaction_name','')) = ''
           OR length(v_item->>'transaction_name') > 300
           OR length(coalesce(v_item->>'counterparty_name','')) > 300
           OR length(coalesce(v_item->>'counterparty_account','')) > 300
           OR length(coalesce(v_item->>'counterparty_institution','')) > 300 THEN
            RAISE EXCEPTION 'bank statement transaction is invalid' USING ERRCODE = '22023';
        END IF;
        BEGIN
            v_source_event := (v_item->>'source_event_ref')::uuid;
            v_source_row_number := (v_item->>'source_row_number')::integer;
            v_occurred_at := (v_item->>'occurred_at')::timestamptz;
            v_amount_minor := (v_item->>'amount_minor')::bigint;
            v_balance_minor := (v_item->>'balance_minor')::bigint;
        EXCEPTION
            WHEN invalid_text_representation OR numeric_value_out_of_range
                 OR invalid_datetime_format OR datetime_field_overflow THEN
                RAISE EXCEPTION 'bank statement transaction is invalid'
                    USING ERRCODE = '22023';
        END;
        IF v_source_event IS NULL OR v_source_row_number IS NULL
           OR v_source_row_number <= 0 OR v_occurred_at IS NULL
           OR v_amount_minor IS NULL OR v_balance_minor IS NULL THEN
            RAISE EXCEPTION 'bank statement transaction is invalid' USING ERRCODE = '22023';
        END IF;
    END LOOP;

    PERFORM pg_advisory_xact_lock(hashtextextended(
        'managed-account-ref:' || v_account::text, 0
    ));
    SELECT ma.institution_code, ma.account_suffix, ma.owner_kind, latest.status
      INTO v_account_institution, v_account_suffix, v_account_owner_kind,
           v_lifecycle_status
      FROM public.managed_account ma
      JOIN LATERAL (
        SELECT status FROM public.managed_account_lifecycle lifecycle
         WHERE lifecycle.managed_account_ref=ma.managed_account_ref
         ORDER BY lifecycle.revision DESC LIMIT 1
      ) latest ON true
     WHERE ma.managed_account_ref=v_account AND ma.entity_id=v_owner;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'managed account must be registered before statement import'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF v_account_institution IS DISTINCT FROM p_request->>'institution_code'
       OR v_account_suffix IS DISTINCT FROM p_request->>'account_suffix'
       OR v_lifecycle_status IS DISTINCT FROM 'ACTIVE'
       OR (v_profile = 'ccb_personal_xls_v1' AND v_account_owner_kind <> 'PERSONAL') THEN
        RAISE EXCEPTION 'managed account conflicts with registered identity'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    v_source := decode(p_request->>'source_sha256', 'hex');
    v_set_digest := decode(p_request->>'transaction_set_sha256', 'hex');
    PERFORM pg_advisory_xact_lock(hashtextextended(p_request->>'source_sha256', 0));
    SELECT * INTO v_evidence_row FROM public.evidence_object
     WHERE evidence_ref = v_evidence;
    IF NOT FOUND
       OR v_evidence_row.entity_id IS DISTINCT FROM v_owner
       OR v_evidence_row.plaintext_sha256 IS DISTINCT FROM v_source
       OR v_evidence_row.plaintext_size IS DISTINCT FROM v_source_size
       OR v_evidence_row.media_type IS DISTINCT FROM p_request->>'declared_media_type' THEN
        RAISE EXCEPTION 'bank statement evidence binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT * INTO v_existing FROM public.bank_statement
     WHERE statement_ref = v_statement OR source_sha256 = v_source;
    IF FOUND THEN
        IF v_existing.statement_ref IS DISTINCT FROM v_statement
           OR v_existing.managed_account_ref IS DISTINCT FROM v_account
           OR v_existing.evidence_ref IS DISTINCT FROM v_evidence
           OR v_existing.source_sha256 IS DISTINCT FROM v_source
           OR v_existing.source_system IS DISTINCT FROM p_request->>'source_system'
           OR v_existing.source_size IS DISTINCT FROM v_source_size
           OR v_existing.declared_media_type IS DISTINCT FROM p_request->>'declared_media_type'
           OR v_existing.currency IS DISTINCT FROM p_request->>'currency'
           OR v_existing.period_start IS DISTINCT FROM v_period_start
           OR v_existing.period_end IS DISTINCT FROM v_period_end
           OR v_existing.transaction_count IS DISTINCT FROM v_transaction_count
           OR v_existing.transaction_set_sha256 IS DISTINCT FROM v_set_digest THEN
            RAISE EXCEPTION 'bank statement replay conflicts with persisted facts'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT status INTO v_review FROM public.bank_statement_review
         WHERE statement_ref = v_statement ORDER BY revision DESC LIMIT 1;
        RETURN jsonb_build_object(
            'statement_ref', v_statement, 'managed_account_ref', v_account,
            'created', false, 'transaction_count', v_existing.transaction_count,
            'review_status', coalesce(v_review, 'PENDING'),
            'statement_review_count', 1, 'accounting_candidate_count', 0
        );
    END IF;

    v_statement_payload := jsonb_build_object(
        'statement_ref', v_statement, 'managed_account_ref', v_account,
        'evidence_ref', v_evidence,
        'source_system', p_request->>'source_system',
        'source_sha256', p_request->>'source_sha256',
        'source_size', v_source_size,
        'declared_media_type', p_request->>'declared_media_type',
        'currency', p_request->>'currency',
        'period_start', p_request->>'period_start',
        'period_end', p_request->>'period_end',
        'transaction_count', v_transaction_count,
        'transaction_set_sha256', p_request->>'transaction_set_sha256'
    );
    IF v_profile <> 'mybank_xlsx_v1' THEN
        v_statement_payload := v_statement_payload || jsonb_build_object(
            'parser_profile', v_profile
        );
    END IF;
    v_audit := public.append_audit_event(
        p_request->>'actor', 'bank_statement.import', p_request->>'reason',
        'ledgerbridge.bank-statement.v1', v_statement_payload
    );
    INSERT INTO public.bank_statement(
        statement_ref, managed_account_ref, evidence_ref, source_system,
        source_sha256, source_size, declared_media_type, currency,
        period_start, period_end, transaction_count, transaction_set_sha256,
        audit_event_id, created_at
    ) VALUES (
        v_statement, v_account, v_evidence, p_request->>'source_system',
        v_source, v_source_size, p_request->>'declared_media_type',
        p_request->>'currency', v_period_start, v_period_end,
        v_transaction_count, v_set_digest, v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
    );

    FOR v_item IN SELECT value FROM jsonb_array_elements(p_request->'transactions') LOOP
        v_source_event := (v_item->>'source_event_ref')::uuid;
        v_source_row_number := (v_item->>'source_row_number')::integer;
        v_occurred_at := (v_item->>'occurred_at')::timestamptz;
        v_amount_minor := (v_item->>'amount_minor')::bigint;
        v_balance_minor := (v_item->>'balance_minor')::bigint;
        v_fact_digest := public.r1_bank_statement_transaction_digest(
            v_account, v_occurred_at, v_amount_minor, v_balance_minor,
            'CNY', v_item->>'counterparty_ref',
            nullif(v_item->>'counterparty_name',''),
            nullif(v_item->>'counterparty_account',''),
            nullif(v_item->>'counterparty_institution',''),
            v_item->>'transaction_serial', v_item->>'transaction_name'
        );
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'bank-statement-transaction:' || v_account::text || ':' ||
            (v_item->>'transaction_serial'), 0
        ));
        SELECT * INTO v_transaction
          FROM public.bank_statement_transaction
         WHERE managed_account_ref = v_account
           AND transaction_serial = v_item->>'transaction_serial'
         FOR UPDATE;
        IF FOUND THEN
            IF v_transaction.fact_sha256 IS DISTINCT FROM v_fact_digest
               OR v_transaction.occurred_at IS DISTINCT FROM v_occurred_at
               OR v_transaction.amount_minor IS DISTINCT FROM v_amount_minor
               OR v_transaction.balance_minor IS DISTINCT FROM v_balance_minor
               OR v_transaction.currency IS DISTINCT FROM 'CNY'
               OR v_transaction.counterparty_ref IS DISTINCT FROM v_item->>'counterparty_ref'
               OR v_transaction.counterparty_name
                    IS DISTINCT FROM nullif(v_item->>'counterparty_name','')
               OR v_transaction.counterparty_account
                    IS DISTINCT FROM nullif(v_item->>'counterparty_account','')
               OR v_transaction.counterparty_institution
                    IS DISTINCT FROM nullif(v_item->>'counterparty_institution','')
               OR v_transaction.transaction_name
                    IS DISTINCT FROM v_item->>'transaction_name' THEN
                RAISE EXCEPTION 'overlapping bank statement transaction conflicts with fact'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            v_transaction_ref := v_transaction.transaction_ref;
        ELSE
            v_transaction_ref := v_source_event;
            v_audit := public.append_audit_event(
                p_request->>'actor', 'bank_statement.transaction.import',
                p_request->>'reason', 'ledgerbridge.bank-statement.v1',
                jsonb_build_object(
                    'transaction_ref', v_transaction_ref,
                    'managed_account_ref', v_account,
                    'fact_sha256', encode(v_fact_digest, 'hex')
                )
            );
            INSERT INTO public.bank_statement_transaction(
                transaction_ref, managed_account_ref, occurred_at, amount_minor,
                balance_minor, currency, counterparty_ref,
                counterparty_name, counterparty_account, counterparty_institution,
                transaction_serial, transaction_name, fact_sha256,
                audit_event_id, created_at
            ) VALUES (
                v_transaction_ref, v_account, v_occurred_at,
                v_amount_minor, v_balance_minor, 'CNY', v_item->>'counterparty_ref',
                nullif(v_item->>'counterparty_name',''),
                nullif(v_item->>'counterparty_account',''),
                nullif(v_item->>'counterparty_institution',''),
                v_item->>'transaction_serial', v_item->>'transaction_name',
                v_fact_digest, v_audit,
                (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
            );
        END IF;
        v_audit := public.append_audit_event(
            p_request->>'actor', 'bank_statement.observation.import',
            p_request->>'reason', 'ledgerbridge.bank-statement.v1',
            jsonb_build_object(
                'source_event_ref', v_source_event,
                'statement_ref', v_statement,
                'managed_account_ref', v_account,
                'transaction_ref', v_transaction_ref,
                'source_row_number', v_source_row_number,
                'source_row_sha256', v_item->>'source_row_sha256'
            )
        );
        INSERT INTO public.bank_statement_observation(
            source_event_ref, statement_ref, managed_account_ref, transaction_ref,
            source_row_number, source_row_sha256, audit_event_id, created_at
        ) VALUES (
            v_source_event, v_statement, v_account, v_transaction_ref,
            v_source_row_number, decode(v_item->>'source_row_sha256', 'hex'),
            v_audit, (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
        );
        v_count := v_count + 1;
    END LOOP;
    IF v_count <> v_transaction_count THEN
        RAISE EXCEPTION 'bank statement transaction count changed'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    v_audit := public.append_audit_event(
        p_request->>'actor', 'bank_statement.review', p_request->>'reason',
        'ledgerbridge.bank-statement.v1',
        jsonb_build_object(
            'statement_ref', v_statement, 'revision', 1, 'status', 'PENDING'
        )
    );
    INSERT INTO public.bank_statement_review(
        statement_ref, revision, status, audit_event_id, reviewed_at
    ) VALUES (
        v_statement, 1, 'PENDING', v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
    );
    RETURN jsonb_build_object(
        'statement_ref', v_statement, 'managed_account_ref', v_account,
        'created', true, 'transaction_count', v_count,
        'review_status', 'PENDING', 'statement_review_count', 1,
        'accounting_candidate_count', 0
    );
END
$function$;

REVOKE ALL ON FUNCTION internal_import.import_bank_statement(jsonb)
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_app;
GRANT EXECUTE ON FUNCTION internal_import.import_bank_statement(jsonb)
    TO ledgerbridge_worker;
"""


def downgrade() -> None:
    raise RuntimeError("generic bank statement imports are forward-only")
