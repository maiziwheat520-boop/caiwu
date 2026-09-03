"""Add replay-bound BOC projection corrections without mutating source facts.

Revision ID: 20260904_0041
Revises: 20260904_0040
"""

from __future__ import annotations

import os

from alembic import op

revision = "20260904_0041"
down_revision = "20260904_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
CREATE TABLE public.bank_statement_transaction_projection_correction (
    transaction_ref uuid PRIMARY KEY
        REFERENCES public.bank_statement_transaction(transaction_ref) ON DELETE RESTRICT,
    source_row_sha256 bytea NOT NULL CHECK (octet_length(source_row_sha256) = 32),
    parser_facts_sha256 bytea NOT NULL CHECK (octet_length(parser_facts_sha256) = 32),
    transaction_set_sha256 bytea NOT NULL CHECK (octet_length(transaction_set_sha256) = 32),
    command_sha256 bytea NOT NULL CHECK (octet_length(command_sha256) = 32),
    counterparty_name varchar(300),
    counterparty_account varchar(300),
    counterparty_institution varchar(300),
    transaction_serial varchar(300) NOT NULL,
    transaction_name varchar(300) NOT NULL,
    reason_code varchar(100) NOT NULL
        CHECK (reason_code = 'BOC_PDF_PARSER_REPLAY_V2'),
    audit_event_id uuid NOT NULL UNIQUE
        REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL
);

CREATE TRIGGER bank_statement_transaction_projection_correction_append_only
BEFORE UPDATE OR DELETE ON public.bank_statement_transaction_projection_correction
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();

CREATE FUNCTION public.r1_validate_bank_statement_transaction_projection_correction()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
DECLARE v_event public.audit_event%ROWTYPE;
BEGIN
    SELECT * INTO v_event FROM public.audit_event WHERE id = NEW.audit_event_id;
    IF v_event.action IS DISTINCT FROM 'bank_statement.transaction.projection.correct'
       OR v_event.rule_version IS DISTINCT FROM
            'ledgerbridge.bank-statement-projection-correction.v2'
       OR v_event.payload->>'transaction_ref' IS DISTINCT FROM NEW.transaction_ref::text
       OR v_event.payload->>'source_row_sha256'
            IS DISTINCT FROM encode(NEW.source_row_sha256, 'hex')
       OR v_event.payload->>'parser_facts_sha256'
            IS DISTINCT FROM encode(NEW.parser_facts_sha256, 'hex')
       OR v_event.payload->>'transaction_set_sha256'
            IS DISTINCT FROM encode(NEW.transaction_set_sha256, 'hex')
       OR v_event.payload->>'command_sha256'
            IS DISTINCT FROM encode(NEW.command_sha256, 'hex')
       OR v_event.payload->>'counterparty_name'
            IS DISTINCT FROM coalesce(NEW.counterparty_name, '')
       OR v_event.payload->>'counterparty_account'
            IS DISTINCT FROM coalesce(NEW.counterparty_account, '')
       OR v_event.payload->>'counterparty_institution'
            IS DISTINCT FROM coalesce(NEW.counterparty_institution, '')
       OR v_event.payload->>'transaction_serial' IS DISTINCT FROM NEW.transaction_serial
       OR v_event.payload->>'transaction_name' IS DISTINCT FROM NEW.transaction_name
       OR v_event.payload->>'reason_code' IS DISTINCT FROM NEW.reason_code
       OR NEW.created_at IS DISTINCT FROM v_event.occurred_at THEN
        RAISE EXCEPTION 'bank statement projection correction audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER validate_bank_statement_transaction_projection_correction_audit
BEFORE INSERT ON public.bank_statement_transaction_projection_correction
FOR EACH ROW EXECUTE FUNCTION
    public.r1_validate_bank_statement_transaction_projection_correction();

CREATE FUNCTION internal_import.repair_boc_statement_projection(p_request jsonb)
RETURNS TABLE(created boolean, correction_count integer, transaction_count integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_statement public.bank_statement%ROWTYPE;
    v_item jsonb;
    v_transaction public.bank_statement_transaction%ROWTYPE;
    v_existing public.bank_statement_transaction_projection_correction%ROWTYPE;
    v_old public.bank_statement_transaction_correction%ROWTYPE;
    v_command bytea;
    v_parser_facts bytea;
    v_transaction_set bytea;
    v_row_sha bytea;
    v_name text;
    v_account text;
    v_institution text;
    v_serial text;
    v_transaction_name text;
    v_audit uuid;
    v_inserted integer := 0;
    v_expected integer;
BEGIN
    IF p_request IS NULL OR jsonb_typeof(p_request) <> 'object'
       OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_request) key)
          IS DISTINCT FROM ARRAY[
            'parser_facts_sha256','parser_profile','source_sha256','statement_ref',
            'transaction_set_sha256','transactions'
          ]::text[]
       OR p_request->>'parser_profile' <> 'boc_personal_pdf_v1'
       OR p_request->>'source_sha256' !~ '^[0-9a-f]{64}$'
       OR p_request->>'parser_facts_sha256' !~ '^[0-9a-f]{64}$'
       OR p_request->>'transaction_set_sha256' !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(p_request->'transactions') <> 'array' THEN
        RAISE EXCEPTION 'BOC projection repair request is invalid' USING ERRCODE = '22023';
    END IF;
    BEGIN
        SELECT * INTO STRICT v_statement FROM public.bank_statement
         WHERE statement_ref = (p_request->>'statement_ref')::uuid
           AND source_system = 'boc_transaction_statement'
           AND source_sha256 = decode(p_request->>'source_sha256', 'hex');
    EXCEPTION WHEN no_data_found OR invalid_text_representation THEN
        RAISE EXCEPTION 'BOC projection repair statement identity is invalid'
            USING ERRCODE = '22023';
    END;
    IF NOT EXISTS (
        SELECT 1 FROM public.managed_account account
         WHERE account.managed_account_ref = v_statement.managed_account_ref
           AND account.institution_code = 'boc' AND account.owner_kind = 'PERSONAL'
    ) THEN
        RAISE EXCEPTION 'BOC projection repair account scope is invalid'
            USING ERRCODE = '22023';
    END IF;
    v_expected := jsonb_array_length(p_request->'transactions');
    IF v_expected <> v_statement.transaction_count
       OR v_expected <> (SELECT count(*) FROM public.bank_statement_observation
                           WHERE statement_ref = v_statement.statement_ref)
       OR v_expected <> (
            SELECT count(DISTINCT (item->>'source_row_number')::integer)
              FROM jsonb_array_elements(p_request->'transactions') item
       ) THEN
        RAISE EXCEPTION 'BOC projection repair row set is incomplete'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    v_command := public.digest(convert_to(p_request::text, 'UTF8'), 'sha256');
    v_parser_facts := decode(p_request->>'parser_facts_sha256', 'hex');
    v_transaction_set := decode(p_request->>'transaction_set_sha256', 'hex');

    FOR v_item IN SELECT value FROM jsonb_array_elements(p_request->'transactions')
    LOOP
        IF jsonb_typeof(v_item) <> 'object'
           OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(v_item) key)
              IS DISTINCT FROM ARRAY[
                'amount_minor','balance_minor','counterparty_account',
                'counterparty_institution','counterparty_name','occurred_at',
                'source_row_number','source_row_sha256','transaction_name',
                'transaction_serial'
              ]::text[]
           OR v_item->>'source_row_sha256' !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'BOC projection repair row is invalid' USING ERRCODE = '22023';
        END IF;
        SELECT transaction.* INTO v_transaction
          FROM public.bank_statement_observation observation
          JOIN public.bank_statement_transaction transaction
            ON transaction.transaction_ref = observation.transaction_ref
           AND transaction.managed_account_ref = observation.managed_account_ref
         WHERE observation.statement_ref = v_statement.statement_ref
           AND observation.source_row_number = (v_item->>'source_row_number')::integer;
        IF NOT FOUND
           OR v_transaction.occurred_at IS DISTINCT FROM (v_item->>'occurred_at')::timestamptz
           OR v_transaction.amount_minor IS DISTINCT FROM (v_item->>'amount_minor')::bigint
           OR v_transaction.balance_minor IS DISTINCT FROM (v_item->>'balance_minor')::bigint THEN
            RAISE EXCEPTION 'BOC projection repair source facts conflict'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        v_row_sha := decode(v_item->>'source_row_sha256', 'hex');
        v_name := nullif(v_item->>'counterparty_name', '');
        v_account := nullif(v_item->>'counterparty_account', '');
        v_institution := nullif(v_item->>'counterparty_institution', '');
        v_serial := v_item->>'transaction_serial';
        v_transaction_name := v_item->>'transaction_name';
        IF length(coalesce(v_name,'')) > 300 OR length(coalesce(v_account,'')) > 300
           OR length(coalesce(v_institution,'')) > 300 OR length(v_serial) NOT BETWEEN 1 AND 300
           OR length(v_transaction_name) NOT BETWEEN 1 AND 300 THEN
            RAISE EXCEPTION 'BOC projection repair text is invalid' USING ERRCODE = '22023';
        END IF;
        SELECT * INTO v_old FROM public.bank_statement_transaction_correction
         WHERE transaction_ref = v_transaction.transaction_ref;
        SELECT * INTO v_existing
          FROM public.bank_statement_transaction_projection_correction
         WHERE transaction_ref = v_transaction.transaction_ref;
        IF FOUND THEN
            IF (v_existing.source_row_sha256, v_existing.parser_facts_sha256,
                v_existing.transaction_set_sha256, v_existing.command_sha256,
                v_existing.counterparty_name, v_existing.counterparty_account,
                v_existing.counterparty_institution, v_existing.transaction_serial,
                v_existing.transaction_name)
               IS DISTINCT FROM
               (v_row_sha, v_parser_facts, v_transaction_set, v_command,
                v_name, v_account, v_institution, v_serial, v_transaction_name) THEN
                RAISE EXCEPTION 'BOC projection repair replay conflicts'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            CONTINUE;
        END IF;
        IF (v_name, v_account, v_institution, v_serial, v_transaction_name)
           IS NOT DISTINCT FROM (
             coalesce(v_old.counterparty_name, v_transaction.counterparty_name),
             coalesce(v_old.counterparty_account, v_transaction.counterparty_account),
             coalesce(v_old.counterparty_institution, v_transaction.counterparty_institution),
             v_transaction.transaction_serial, v_transaction.transaction_name
           ) THEN
            CONTINUE;
        END IF;
        v_audit := public.append_audit_event(
            'operator:boc-projection-repair-v2',
            'bank_statement.transaction.projection.correct',
            'Reconcile historical BOC description projection to verified parser replay',
            'ledgerbridge.bank-statement-projection-correction.v2',
            jsonb_build_object(
                'transaction_ref', v_transaction.transaction_ref,
                'source_row_sha256', encode(v_row_sha,'hex'),
                'parser_facts_sha256', encode(v_parser_facts,'hex'),
                'transaction_set_sha256', encode(v_transaction_set,'hex'),
                'command_sha256', encode(v_command,'hex'),
                'counterparty_name', coalesce(v_name,''),
                'counterparty_account', coalesce(v_account,''),
                'counterparty_institution', coalesce(v_institution,''),
                'transaction_serial', v_serial, 'transaction_name', v_transaction_name,
                'reason_code', 'BOC_PDF_PARSER_REPLAY_V2'
            )
        );
        INSERT INTO public.bank_statement_transaction_projection_correction(
            transaction_ref, source_row_sha256, parser_facts_sha256,
            transaction_set_sha256, command_sha256, counterparty_name,
            counterparty_account, counterparty_institution, transaction_serial,
            transaction_name, reason_code, audit_event_id, created_at
        ) VALUES (
            v_transaction.transaction_ref, v_row_sha, v_parser_facts,
            v_transaction_set, v_command, v_name, v_account, v_institution, v_serial,
            v_transaction_name, 'BOC_PDF_PARSER_REPLAY_V2', v_audit,
            (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
        );
        v_inserted := v_inserted + 1;
    END LOOP;
    RETURN QUERY SELECT v_inserted > 0, v_inserted, v_expected;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RAISE EXCEPTION 'BOC projection repair row is invalid' USING ERRCODE = '22023';
END
$function$;

CREATE OR REPLACE FUNCTION internal_read.list_bank_statement_transactions(
    p_statement_ref uuid, p_entity_ref uuid, p_audit_horizon_sequence bigint,
    p_audit_horizon_hash bytea, p_after_row integer, p_limit integer
) RETURNS TABLE(
    source_row_number integer, occurred_at timestamptz,
    amount_minor bigint, balance_minor bigint, currency varchar(3),
    counterparty_ref varchar(99), counterparty_name varchar(300),
    counterparty_account_masked varchar(300),
    counterparty_institution varchar(300), transaction_serial varchar(300),
    transaction_name varchar(300)
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    IF p_statement_ref IS NULL OR p_entity_ref IS NULL
       OR p_audit_horizon_sequence IS NULL OR p_audit_horizon_hash IS NULL
       OR octet_length(p_audit_horizon_hash) <> 32 OR p_after_row IS NULL OR p_after_row < 0
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200
       OR NOT EXISTS (SELECT 1 FROM public.audit_event
            WHERE sequence=p_audit_horizon_sequence AND hash=p_audit_horizon_hash) THEN
        RAISE EXCEPTION 'bank statement transaction page request is invalid'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT observation.source_row_number, transaction.occurred_at,
           transaction.amount_minor, transaction.balance_minor, transaction.currency,
           transaction.counterparty_ref,
           (CASE WHEN projection_audit.sequence <= p_audit_horizon_sequence
                 THEN projection.counterparty_name
                 WHEN correction_audit.sequence <= p_audit_horizon_sequence
                 THEN correction.counterparty_name
                 ELSE transaction.counterparty_name END)::varchar(300),
           (CASE WHEN effective.counterparty_account IS NULL THEN NULL
                 WHEN length(effective.counterparty_account) <= 4
                    THEN repeat('*',length(effective.counterparty_account))
                 ELSE repeat('*',length(effective.counterparty_account)-4) ||
                    right(effective.counterparty_account,4) END)::varchar(300),
           (CASE WHEN projection_audit.sequence <= p_audit_horizon_sequence
                 THEN projection.counterparty_institution
                 WHEN correction_audit.sequence <= p_audit_horizon_sequence
                 THEN coalesce(correction.counterparty_institution,
                               transaction.counterparty_institution)
                 ELSE transaction.counterparty_institution END)::varchar(300),
           (CASE WHEN projection_audit.sequence <= p_audit_horizon_sequence
                 THEN projection.transaction_serial
                 ELSE transaction.transaction_serial END)::varchar(300),
           (CASE WHEN projection_audit.sequence <= p_audit_horizon_sequence
                 THEN projection.transaction_name
                 ELSE transaction.transaction_name END)::varchar(300)
      FROM public.bank_statement statement
      JOIN public.managed_account account USING(managed_account_ref)
      JOIN public.bank_statement_observation observation USING(statement_ref,managed_account_ref)
      JOIN public.bank_statement_transaction transaction USING(transaction_ref,managed_account_ref)
      LEFT JOIN public.bank_statement_transaction_correction correction USING(transaction_ref)
      LEFT JOIN public.audit_event correction_audit ON correction_audit.id=correction.audit_event_id
      LEFT JOIN public.bank_statement_transaction_projection_correction projection
        USING(transaction_ref)
      LEFT JOIN public.audit_event projection_audit ON projection_audit.id=projection.audit_event_id
      CROSS JOIN LATERAL (
        SELECT CASE WHEN projection_audit.sequence <= p_audit_horizon_sequence
                    THEN projection.counterparty_account
                    WHEN correction_audit.sequence <= p_audit_horizon_sequence
                    THEN coalesce(correction.counterparty_account,
                                  transaction.counterparty_account)
                    ELSE transaction.counterparty_account END AS counterparty_account
      ) effective
      JOIN public.audit_event imported ON imported.id=statement.audit_event_id
      JOIN public.audit_event observed ON observed.id=observation.audit_event_id
      JOIN public.audit_event recorded ON recorded.id=transaction.audit_event_id
     WHERE statement.statement_ref=p_statement_ref AND account.entity_id=p_entity_ref
       AND imported.sequence<=p_audit_horizon_sequence
       AND observed.sequence<=p_audit_horizon_sequence
       AND recorded.sequence<=p_audit_horizon_sequence
       AND observation.source_row_number>p_after_row
     ORDER BY observation.source_row_number LIMIT p_limit;
END
$function$;

REVOKE ALL ON TABLE public.bank_statement_transaction_projection_correction
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker,
         ledgerbridge_app;
REVOKE ALL ON FUNCTION public.r1_validate_bank_statement_transaction_projection_correction(),
    internal_import.repair_boc_statement_projection(jsonb)
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker,
         ledgerbridge_app;
GRANT EXECUTE ON FUNCTION internal_read.list_bank_statement_transactions(
    uuid,uuid,bigint,bytea,integer,integer
) TO ledgerbridge_reader;
"""
    )


def downgrade() -> None:
    if os.getenv("LEDGERBRIDGE_ENV", "development").lower() == "production":
        raise RuntimeError("BOC projection corrections are irreversible in production")
    op.execute(
        r"""
DROP FUNCTION internal_read.list_bank_statement_transactions(
    uuid,uuid,bigint,bytea,integer,integer
);
DROP FUNCTION IF EXISTS internal_import.repair_boc_statement_projection(jsonb);
DROP TRIGGER IF EXISTS validate_bank_statement_transaction_projection_correction_audit
    ON public.bank_statement_transaction_projection_correction;
DROP TRIGGER IF EXISTS bank_statement_transaction_projection_correction_append_only
    ON public.bank_statement_transaction_projection_correction;
DROP FUNCTION IF EXISTS
    public.r1_validate_bank_statement_transaction_projection_correction();
DROP TABLE IF EXISTS public.bank_statement_transaction_projection_correction;

CREATE FUNCTION internal_read.list_bank_statement_transactions(
    p_statement_ref uuid, p_entity_ref uuid, p_audit_horizon_sequence bigint,
    p_audit_horizon_hash bytea, p_after_row integer, p_limit integer
) RETURNS TABLE(
    source_row_number integer, occurred_at timestamptz,
    amount_minor bigint, balance_minor bigint, currency varchar(3),
    counterparty_ref varchar(99), counterparty_name varchar(300),
    counterparty_account_masked varchar(300),
    counterparty_institution varchar(300), transaction_serial varchar(300),
    transaction_name varchar(300)
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    IF p_statement_ref IS NULL OR p_entity_ref IS NULL
       OR p_audit_horizon_sequence IS NULL OR p_audit_horizon_hash IS NULL
       OR octet_length(p_audit_horizon_hash) <> 32
       OR p_after_row IS NULL OR p_after_row < 0
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200
       OR NOT EXISTS (SELECT 1 FROM public.audit_event
            WHERE sequence=p_audit_horizon_sequence AND hash=p_audit_horizon_hash) THEN
        RAISE EXCEPTION 'bank statement transaction page request is invalid'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT observation.source_row_number, transaction.occurred_at,
           transaction.amount_minor, transaction.balance_minor, transaction.currency,
           transaction.counterparty_ref,
           coalesce(correction.counterparty_name,
                    transaction.counterparty_name)::varchar(300),
           CASE
               WHEN coalesce(correction.counterparty_account,
                             transaction.counterparty_account) IS NULL THEN NULL
               WHEN length(coalesce(correction.counterparty_account,
                                    transaction.counterparty_account)) <= 4
                    THEN repeat('*', length(coalesce(correction.counterparty_account,
                                                     transaction.counterparty_account)))
               ELSE repeat('*', length(coalesce(correction.counterparty_account,
                                                transaction.counterparty_account)) - 4) ||
                    right(coalesce(correction.counterparty_account,
                                   transaction.counterparty_account), 4)
           END::varchar(300),
           coalesce(correction.counterparty_institution,
                    transaction.counterparty_institution)::varchar(300),
           transaction.transaction_serial, transaction.transaction_name
      FROM public.bank_statement statement
      JOIN public.managed_account account USING(managed_account_ref)
      JOIN public.bank_statement_observation observation
        USING(statement_ref,managed_account_ref)
      JOIN public.bank_statement_transaction transaction
        USING(transaction_ref,managed_account_ref)
      LEFT JOIN public.bank_statement_transaction_correction correction
        USING(transaction_ref)
      JOIN public.audit_event imported ON imported.id=statement.audit_event_id
      JOIN public.audit_event observed ON observed.id=observation.audit_event_id
      JOIN public.audit_event recorded ON recorded.id=transaction.audit_event_id
      LEFT JOIN public.audit_event corrected ON corrected.id=correction.audit_event_id
     WHERE statement.statement_ref=p_statement_ref AND account.entity_id=p_entity_ref
       AND imported.sequence<=p_audit_horizon_sequence
       AND observed.sequence<=p_audit_horizon_sequence
       AND recorded.sequence<=p_audit_horizon_sequence
       AND (corrected.sequence IS NULL OR corrected.sequence<=p_audit_horizon_sequence)
       AND observation.source_row_number>p_after_row
     ORDER BY observation.source_row_number LIMIT p_limit;
END
$function$;
REVOKE ALL ON FUNCTION internal_read.list_bank_statement_transactions(
    uuid,uuid,bigint,bytea,integer,integer
) FROM PUBLIC, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app,
       ledgerbridge_reader;
GRANT EXECUTE ON FUNCTION internal_read.list_bank_statement_transactions(
    uuid,uuid,bigint,bytea,integer,integer
) TO ledgerbridge_reader;
"""
    )
