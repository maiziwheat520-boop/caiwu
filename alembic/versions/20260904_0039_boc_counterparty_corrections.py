"""Add an audited projection correction for proven BOC PDF column spill.

Revision ID: 20260904_0039
Revises: 20260903_0038
"""

from __future__ import annotations

import os

from alembic import op

revision = "20260904_0039"
down_revision = "20260903_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
CREATE TABLE public.bank_statement_transaction_correction (
    transaction_ref uuid PRIMARY KEY
        REFERENCES public.bank_statement_transaction(transaction_ref) ON DELETE RESTRICT,
    counterparty_name varchar(300) NOT NULL,
    counterparty_account varchar(300),
    counterparty_institution varchar(300),
    reason_code varchar(100) NOT NULL
        CHECK (reason_code = 'BOC_PDF_COUNTERPARTY_COLUMN_SPILL'),
    audit_event_id uuid NOT NULL UNIQUE
        REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL
);

CREATE TRIGGER bank_statement_transaction_correction_append_only
BEFORE UPDATE OR DELETE ON public.bank_statement_transaction_correction
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();

CREATE FUNCTION public.r1_validate_bank_statement_transaction_correction()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
DECLARE v_event public.audit_event%ROWTYPE;
BEGIN
    SELECT * INTO v_event FROM public.audit_event WHERE id = NEW.audit_event_id;
    IF v_event.action IS DISTINCT FROM 'bank_statement.transaction.correct'
       OR v_event.rule_version IS DISTINCT FROM 'ledgerbridge.bank-statement-correction.v1'
       OR v_event.payload->>'transaction_ref' IS DISTINCT FROM NEW.transaction_ref::text
       OR v_event.payload->>'counterparty_name' IS DISTINCT FROM NEW.counterparty_name
       OR v_event.payload->>'counterparty_account'
            IS DISTINCT FROM coalesce(NEW.counterparty_account, '')
       OR v_event.payload->>'counterparty_institution'
            IS DISTINCT FROM coalesce(NEW.counterparty_institution, '')
       OR v_event.payload->>'reason_code' IS DISTINCT FROM NEW.reason_code
       OR NEW.created_at IS DISTINCT FROM v_event.occurred_at THEN
        RAISE EXCEPTION 'bank statement transaction correction audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER validate_bank_statement_transaction_correction_audit
BEFORE INSERT ON public.bank_statement_transaction_correction
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_bank_statement_transaction_correction();

DO $block$
DECLARE item record; v_audit uuid; v_name text; v_account text; v_institution text;
BEGIN
    FOR item IN
        SELECT transaction.transaction_ref, transaction.counterparty_name,
               transaction.counterparty_account, transaction.counterparty_institution
          FROM public.bank_statement_transaction AS transaction
          JOIN public.managed_account AS account
            ON account.managed_account_ref = transaction.managed_account_ref
         WHERE account.institution_code = 'boc'
           AND account.owner_kind = 'PERSONAL'
           AND transaction.counterparty_name ~ ' 6$'
           AND transaction.counterparty_account ~ '^[0-9]{16,30}'
         ORDER BY transaction.transaction_ref
    LOOP
        v_name := left(item.counterparty_name, length(item.counterparty_name) - 2);
        v_account := '6' || substring(item.counterparty_account from '^([0-9]{16,30})');
        v_institution := nullif(btrim(
            coalesce(substring(item.counterparty_account from '^[0-9]{16,30}(.*)$'), '') ||
            coalesce(item.counterparty_institution, '')
        ), '');
        IF v_name = '' OR v_account !~ '^[0-9]{17,30}$' THEN
            RAISE EXCEPTION 'BOC correction derivation is invalid';
        END IF;
        v_audit := public.append_audit_event(
            'migration:20260904_0039', 'bank_statement.transaction.correct',
            'Repair proven BOC PDF fixed-column counterparty spill without mutating source facts',
            'ledgerbridge.bank-statement-correction.v1',
            jsonb_build_object(
                'transaction_ref', item.transaction_ref,
                'counterparty_name', v_name,
                'counterparty_account', v_account,
                'counterparty_institution', coalesce(v_institution, ''),
                'reason_code', 'BOC_PDF_COUNTERPARTY_COLUMN_SPILL'
            )
        );
        INSERT INTO public.bank_statement_transaction_correction(
            transaction_ref, counterparty_name, counterparty_account,
            counterparty_institution, reason_code, audit_event_id, created_at
        ) VALUES (
            item.transaction_ref, v_name, v_account, v_institution,
            'BOC_PDF_COUNTERPARTY_COLUMN_SPILL', v_audit,
            (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
        );
    END LOOP;
END
$block$;

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
       OR p_audit_horizon_sequence IS NULL
       OR p_audit_horizon_hash IS NULL OR octet_length(p_audit_horizon_hash) <> 32
       OR p_after_row IS NULL OR p_after_row < 0
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200
       OR NOT EXISTS (SELECT 1 FROM public.audit_event
            WHERE sequence = p_audit_horizon_sequence AND hash = p_audit_horizon_hash) THEN
        RAISE EXCEPTION 'bank statement transaction page request is invalid'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT observation.source_row_number, transaction.occurred_at,
           transaction.amount_minor, transaction.balance_minor, transaction.currency,
           transaction.counterparty_ref,
           coalesce(correction.counterparty_name, transaction.counterparty_name)::varchar(300),
           CASE
               WHEN coalesce(correction.counterparty_account, transaction.counterparty_account)
                    IS NULL THEN NULL
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
      FROM public.bank_statement AS statement
      JOIN public.managed_account AS account
        ON account.managed_account_ref = statement.managed_account_ref
      JOIN public.bank_statement_observation AS observation
        ON observation.statement_ref = statement.statement_ref
       AND observation.managed_account_ref = statement.managed_account_ref
      JOIN public.bank_statement_transaction AS transaction
        ON transaction.transaction_ref = observation.transaction_ref
       AND transaction.managed_account_ref = observation.managed_account_ref
      LEFT JOIN public.bank_statement_transaction_correction AS correction
        ON correction.transaction_ref = transaction.transaction_ref
      JOIN public.audit_event AS imported ON imported.id = statement.audit_event_id
      JOIN public.audit_event AS observed ON observed.id = observation.audit_event_id
      JOIN public.audit_event AS recorded ON recorded.id = transaction.audit_event_id
      LEFT JOIN public.audit_event AS corrected ON corrected.id = correction.audit_event_id
     WHERE statement.statement_ref = p_statement_ref
       AND account.entity_id = p_entity_ref
       AND imported.sequence <= p_audit_horizon_sequence
       AND observed.sequence <= p_audit_horizon_sequence
       AND recorded.sequence <= p_audit_horizon_sequence
       AND (corrected.sequence IS NULL OR corrected.sequence <= p_audit_horizon_sequence)
       AND observation.source_row_number > p_after_row
     ORDER BY observation.source_row_number LIMIT p_limit;
END
$function$;

REVOKE ALL ON TABLE public.bank_statement_transaction_correction
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker,
         ledgerbridge_app;
REVOKE ALL ON FUNCTION public.r1_validate_bank_statement_transaction_correction()
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker,
         ledgerbridge_app;
GRANT EXECUTE ON FUNCTION internal_read.list_bank_statement_transactions(
    uuid,uuid,bigint,bytea,integer,integer
) TO ledgerbridge_reader;
"""
    )


def downgrade() -> None:
    if os.getenv("LEDGERBRIDGE_ENV", "development").lower() == "production":
        raise RuntimeError("BOC transaction corrections are irreversible in production")
    op.execute(
        "DROP TRIGGER IF EXISTS validate_bank_statement_transaction_correction_audit "
        "ON public.bank_statement_transaction_correction; "
        "DROP TRIGGER IF EXISTS bank_statement_transaction_correction_append_only "
        "ON public.bank_statement_transaction_correction; "
        "DROP FUNCTION IF EXISTS public.r1_validate_bank_statement_transaction_correction(); "
        "DROP TABLE IF EXISTS public.bank_statement_transaction_correction;"
    )
