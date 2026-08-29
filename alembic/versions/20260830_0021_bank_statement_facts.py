"""Persist statement-backed managed accounts and immutable statement facts.

Revision ID: 20260830_0021
Revises: 20260830_0020
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_0021"
down_revision: str | None = "20260830_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FACT_TABLES = (
    "managed_account",
    "managed_account_lifecycle",
    "bank_statement",
    "bank_statement_transaction",
    "bank_statement_observation",
    "bank_statement_review",
)


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


_UPGRADE_SQL = r"""
CREATE TABLE public.managed_account (
    managed_account_ref uuid PRIMARY KEY,
    entity_id uuid NOT NULL REFERENCES public.entity(id) ON DELETE RESTRICT,
    account_key varchar(200) NOT NULL,
    institution_code varchar(32) NOT NULL CHECK (institution_code = 'mybank'),
    account_suffix varchar(8) NOT NULL CHECK (account_suffix ~ '^[0-9]{4,8}$'),
    owner_ref varchar(200) NOT NULL,
    owner_kind varchar(16) NOT NULL CHECK (owner_kind IN ('PERSONAL','COMPANY')),
    account_kind varchar(32) NOT NULL,
    audit_event_id uuid NOT NULL UNIQUE REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT managed_account_key_format
        CHECK (account_key ~ '^[a-z0-9][a-z0-9._:-]{0,199}$'),
    CONSTRAINT managed_account_owner_ref_format
        CHECK (owner_ref ~ '^[a-z0-9][a-z0-9._:-]{0,199}$'),
    CONSTRAINT managed_account_kind_format
        CHECK (account_kind ~ '^[A-Z][A-Z0-9_]{0,31}$'),
    CONSTRAINT uq_managed_account_entity_key UNIQUE (entity_id, account_key)
);
CREATE TABLE public.managed_account_lifecycle (
    managed_account_ref uuid NOT NULL
        REFERENCES public.managed_account(managed_account_ref) ON DELETE RESTRICT,
    revision integer NOT NULL CHECK (revision > 0),
    status varchar(16) NOT NULL CHECK (status IN ('ACTIVE','INACTIVE','CLOSED')),
    effective_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    audit_event_id uuid NOT NULL UNIQUE REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    PRIMARY KEY (managed_account_ref, revision)
);
CREATE TABLE public.bank_statement (
    statement_ref uuid PRIMARY KEY,
    managed_account_ref uuid NOT NULL
        REFERENCES public.managed_account(managed_account_ref) ON DELETE RESTRICT,
    evidence_ref uuid NOT NULL UNIQUE
        REFERENCES public.evidence_object(evidence_ref) ON DELETE RESTRICT,
    source_system varchar(64) NOT NULL CHECK (
        source_system ~ '^[a-z0-9][a-z0-9_]{0,63}$'
    ),
    source_sha256 bytea NOT NULL UNIQUE CHECK (octet_length(source_sha256) = 32),
    source_size bigint NOT NULL CHECK (source_size > 0),
    declared_media_type varchar(200) NOT NULL,
    currency varchar(3) NOT NULL CHECK (currency = 'CNY'),
    period_start date NOT NULL,
    period_end date NOT NULL,
    transaction_count integer NOT NULL CHECK (transaction_count > 0),
    transaction_set_sha256 bytea NOT NULL
        CHECK (octet_length(transaction_set_sha256) = 32),
    audit_event_id uuid NOT NULL UNIQUE REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (period_start <= period_end),
    UNIQUE (statement_ref, managed_account_ref)
);
CREATE TABLE public.bank_statement_transaction (
    transaction_ref uuid PRIMARY KEY,
    managed_account_ref uuid NOT NULL
        REFERENCES public.managed_account(managed_account_ref) ON DELETE RESTRICT,
    occurred_at timestamptz NOT NULL,
    amount_minor bigint NOT NULL,
    balance_minor bigint NOT NULL,
    currency varchar(3) NOT NULL CHECK (currency = 'CNY'),
    counterparty_ref varchar(99) NOT NULL
        CHECK (counterparty_ref ~ '^cp_[a-z0-9_]{1,96}$'),
    counterparty_name varchar(300),
    counterparty_account varchar(300),
    counterparty_institution varchar(300),
    transaction_serial varchar(300) NOT NULL CHECK (btrim(transaction_serial) <> ''),
    transaction_name varchar(300) NOT NULL CHECK (btrim(transaction_name) <> ''),
    fact_sha256 bytea NOT NULL CHECK (octet_length(fact_sha256) = 32),
    audit_event_id uuid NOT NULL UNIQUE REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (transaction_ref, managed_account_ref),
    UNIQUE (managed_account_ref, transaction_serial)
);
CREATE TABLE public.bank_statement_observation (
    source_event_ref uuid PRIMARY KEY,
    statement_ref uuid NOT NULL,
    managed_account_ref uuid NOT NULL,
    transaction_ref uuid NOT NULL,
    source_row_number integer NOT NULL CHECK (source_row_number > 0),
    source_row_sha256 bytea NOT NULL CHECK (octet_length(source_row_sha256) = 32),
    audit_event_id uuid NOT NULL UNIQUE REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (statement_ref, managed_account_ref)
        REFERENCES public.bank_statement(statement_ref, managed_account_ref)
        ON DELETE RESTRICT,
    FOREIGN KEY (transaction_ref, managed_account_ref)
        REFERENCES public.bank_statement_transaction(transaction_ref, managed_account_ref)
        ON DELETE RESTRICT,
    UNIQUE (statement_ref, source_row_number),
    UNIQUE (statement_ref, transaction_ref)
);
CREATE TABLE public.bank_statement_review (
    statement_ref uuid NOT NULL
        REFERENCES public.bank_statement(statement_ref) ON DELETE RESTRICT,
    revision integer NOT NULL CHECK (revision > 0),
    status varchar(16) NOT NULL CHECK (status IN ('PENDING','CONFIRMED','REJECTED')),
    operation_id uuid,
    assertion_jti uuid,
    actor_ref varchar(200),
    workload_principal_ref varchar(200),
    expected_revision integer,
    command_sha256 bytea CHECK (
        command_sha256 IS NULL OR octet_length(command_sha256) = 32
    ),
    audit_event_id uuid NOT NULL UNIQUE REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    reviewed_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (statement_ref, revision),
    UNIQUE (operation_id),
    UNIQUE (assertion_jti),
    CHECK (
        (revision = 1 AND status = 'PENDING' AND operation_id IS NULL
            AND assertion_jti IS NULL AND actor_ref IS NULL
            AND workload_principal_ref IS NULL AND expected_revision IS NULL
            AND command_sha256 IS NULL)
        OR (revision > 1 AND status IN ('CONFIRMED','REJECTED')
            AND operation_id IS NOT NULL AND assertion_jti IS NOT NULL
            AND btrim(actor_ref) <> '' AND btrim(workload_principal_ref) <> ''
            AND expected_revision = revision - 1 AND command_sha256 IS NOT NULL)
    )
);

CREATE FUNCTION public.r1_bank_statement_append_only()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
BEGIN
    RAISE EXCEPTION 'bank statement facts are append-only'
        USING ERRCODE = 'integrity_constraint_violation';
END
$function$;
CREATE TRIGGER managed_account_append_only
BEFORE UPDATE OR DELETE ON public.managed_account
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();
CREATE TRIGGER managed_account_lifecycle_append_only
BEFORE UPDATE OR DELETE ON public.managed_account_lifecycle
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();
CREATE TRIGGER bank_statement_append_only
BEFORE UPDATE OR DELETE ON public.bank_statement
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();
CREATE TRIGGER bank_statement_transaction_append_only
BEFORE UPDATE OR DELETE ON public.bank_statement_transaction
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();
CREATE TRIGGER bank_statement_observation_append_only
BEFORE UPDATE OR DELETE ON public.bank_statement_observation
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();
CREATE TRIGGER bank_statement_review_append_only
BEFORE UPDATE OR DELETE ON public.bank_statement_review
FOR EACH ROW EXECUTE FUNCTION public.r1_bank_statement_append_only();

CREATE FUNCTION public.r1_bank_statement_transaction_digest(
    p_managed_account_ref uuid,
    p_occurred_at timestamptz, p_amount_minor bigint, p_balance_minor bigint,
    p_currency text, p_counterparty_ref text, p_counterparty_name text,
    p_counterparty_account text, p_counterparty_institution text,
    p_transaction_serial text, p_transaction_name text
) RETURNS bytea LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, public
AS $function$
SELECT public.digest(
    convert_to(
        jsonb_build_array(
            p_managed_account_ref,
            to_char(p_occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
            p_amount_minor, p_balance_minor, p_currency, p_counterparty_ref,
            coalesce(p_counterparty_name, ''), coalesce(p_counterparty_account, ''),
            coalesce(p_counterparty_institution, ''), p_transaction_serial,
            p_transaction_name
        )::text,
        'UTF8'
    ),
    'sha256'
)
$function$;

CREATE FUNCTION public.r1_validate_bank_statement()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
DECLARE
    v_action text; v_rule text; v_payload jsonb; v_expected text;
    v_actor text; v_reason text; v_audit_time timestamptz;
    v_expected_revision integer; v_previous_status text;
BEGIN
    SELECT action, rule_version, payload, actor, reason, occurred_at
      INTO v_action, v_rule, v_payload, v_actor, v_reason, v_audit_time
      FROM public.audit_event WHERE id = NEW.audit_event_id;
    v_expected := CASE TG_TABLE_NAME
        WHEN 'managed_account' THEN 'managed_account.register'
        WHEN 'managed_account_lifecycle' THEN 'managed_account.lifecycle'
        WHEN 'bank_statement' THEN 'bank_statement.import'
        WHEN 'bank_statement_transaction' THEN 'bank_statement.transaction.import'
        WHEN 'bank_statement_observation' THEN 'bank_statement.observation.import'
        WHEN 'bank_statement_review' THEN 'bank_statement.review'
    END;
    IF v_action IS DISTINCT FROM v_expected
       OR v_rule IS DISTINCT FROM 'ledgerbridge.bank-statement.v1' THEN
        RAISE EXCEPTION 'bank statement audit action is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_TABLE_NAME = 'managed_account'
       AND (v_payload->>'managed_account_ref' IS DISTINCT FROM NEW.managed_account_ref::text
         OR v_payload->>'entity_ref' IS DISTINCT FROM NEW.entity_id::text
         OR v_payload->>'account_key' IS DISTINCT FROM NEW.account_key
         OR v_payload->>'institution_code' IS DISTINCT FROM NEW.institution_code
         OR v_payload->>'account_suffix' IS DISTINCT FROM NEW.account_suffix
         OR v_payload->>'owner_ref' IS DISTINCT FROM NEW.owner_ref
         OR v_payload->>'owner_kind' IS DISTINCT FROM NEW.owner_kind
         OR v_payload->>'account_kind' IS DISTINCT FROM NEW.account_kind
         OR NEW.created_at IS DISTINCT FROM v_audit_time) THEN
        RAISE EXCEPTION 'managed account audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    ELSIF TG_TABLE_NAME = 'managed_account_lifecycle'
       AND (v_payload->>'managed_account_ref' IS DISTINCT FROM NEW.managed_account_ref::text
         OR v_payload->>'revision' IS DISTINCT FROM NEW.revision::text
         OR v_payload->>'status' IS DISTINCT FROM NEW.status
         OR NEW.effective_at IS DISTINCT FROM v_audit_time) THEN
        RAISE EXCEPTION 'managed account lifecycle audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    ELSIF TG_TABLE_NAME = 'bank_statement'
       AND (v_payload->>'statement_ref' IS DISTINCT FROM NEW.statement_ref::text
         OR v_payload->>'managed_account_ref' IS DISTINCT FROM NEW.managed_account_ref::text
         OR v_payload->>'evidence_ref' IS DISTINCT FROM NEW.evidence_ref::text
         OR v_payload->>'source_system' IS DISTINCT FROM NEW.source_system
         OR v_payload->>'source_sha256' IS DISTINCT FROM encode(NEW.source_sha256, 'hex')
         OR v_payload->>'source_size' IS DISTINCT FROM NEW.source_size::text
         OR v_payload->>'declared_media_type' IS DISTINCT FROM NEW.declared_media_type
         OR v_payload->>'currency' IS DISTINCT FROM NEW.currency
         OR v_payload->>'period_start' IS DISTINCT FROM NEW.period_start::text
         OR v_payload->>'period_end' IS DISTINCT FROM NEW.period_end::text
         OR v_payload->>'transaction_count' IS DISTINCT FROM NEW.transaction_count::text
         OR v_payload->>'transaction_set_sha256'
              IS DISTINCT FROM encode(NEW.transaction_set_sha256, 'hex')
         OR NEW.created_at IS DISTINCT FROM v_audit_time) THEN
        RAISE EXCEPTION 'bank statement audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    ELSIF TG_TABLE_NAME = 'bank_statement_transaction'
       AND (v_payload->>'transaction_ref' IS DISTINCT FROM NEW.transaction_ref::text
         OR v_payload->>'managed_account_ref' IS DISTINCT FROM NEW.managed_account_ref::text
         OR v_payload->>'fact_sha256' IS DISTINCT FROM encode(NEW.fact_sha256, 'hex')
         OR NEW.fact_sha256 IS DISTINCT FROM
               public.r1_bank_statement_transaction_digest(
                  NEW.managed_account_ref, NEW.occurred_at,
                  NEW.amount_minor, NEW.balance_minor, NEW.currency,
                  NEW.counterparty_ref, NEW.counterparty_name,
                  NEW.counterparty_account, NEW.counterparty_institution,
                  NEW.transaction_serial, NEW.transaction_name
              )
         OR NEW.created_at IS DISTINCT FROM v_audit_time) THEN
        RAISE EXCEPTION 'bank statement transaction audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    ELSIF TG_TABLE_NAME = 'bank_statement_observation'
       AND (v_payload->>'source_event_ref' IS DISTINCT FROM NEW.source_event_ref::text
         OR v_payload->>'statement_ref' IS DISTINCT FROM NEW.statement_ref::text
         OR v_payload->>'managed_account_ref' IS DISTINCT FROM NEW.managed_account_ref::text
         OR v_payload->>'transaction_ref' IS DISTINCT FROM NEW.transaction_ref::text
         OR v_payload->>'source_row_number' IS DISTINCT FROM NEW.source_row_number::text
         OR v_payload->>'source_row_sha256'
              IS DISTINCT FROM encode(NEW.source_row_sha256, 'hex')
         OR NEW.created_at IS DISTINCT FROM v_audit_time) THEN
        RAISE EXCEPTION 'bank statement observation audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    ELSIF TG_TABLE_NAME = 'bank_statement_review'
       AND (v_payload->>'statement_ref' IS DISTINCT FROM NEW.statement_ref::text
         OR v_payload->>'revision' IS DISTINCT FROM NEW.revision::text
         OR v_payload->>'status' IS DISTINCT FROM NEW.status
         OR NEW.reviewed_at IS DISTINCT FROM v_audit_time
         OR (NEW.revision > 1 AND (
              v_actor IS DISTINCT FROM NEW.actor_ref
              OR v_payload->>'operation_id' IS DISTINCT FROM NEW.operation_id::text
              OR v_payload->>'assertion_jti' IS DISTINCT FROM NEW.assertion_jti::text
              OR v_payload->>'actor_ref' IS DISTINCT FROM NEW.actor_ref
              OR v_payload->>'workload_principal_ref'
                    IS DISTINCT FROM NEW.workload_principal_ref
              OR v_payload->>'expected_revision'
                    IS DISTINCT FROM NEW.expected_revision::text
              OR v_payload->>'command_sha256'
                    IS DISTINCT FROM encode(NEW.command_sha256, 'hex')
              OR NEW.command_sha256 IS DISTINCT FROM public.digest(
                    convert_to(
                        jsonb_build_array(
                            NEW.statement_ref, NEW.operation_id, NEW.assertion_jti,
                            NEW.actor_ref, NEW.workload_principal_ref,
                            NEW.expected_revision, NEW.status, v_reason
                        )::text,
                        'UTF8'
                    ),
                    'sha256'
              )
         ))) THEN
        RAISE EXCEPTION 'bank statement review audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_TABLE_NAME = 'managed_account_lifecycle' THEN
        SELECT coalesce(max(revision), 0) + 1 INTO v_expected_revision
          FROM public.managed_account_lifecycle
         WHERE managed_account_ref = NEW.managed_account_ref;
        IF NEW.revision IS DISTINCT FROM v_expected_revision THEN
            RAISE EXCEPTION 'managed account lifecycle revision is invalid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF TG_TABLE_NAME = 'bank_statement_review' THEN
        SELECT coalesce(max(revision), 0) + 1 INTO v_expected_revision
          FROM public.bank_statement_review
         WHERE statement_ref = NEW.statement_ref;
        SELECT status INTO v_previous_status
          FROM public.bank_statement_review
         WHERE statement_ref = NEW.statement_ref
         ORDER BY revision DESC LIMIT 1;
        IF NEW.revision IS DISTINCT FROM v_expected_revision
           OR (NEW.revision = 1 AND NEW.status <> 'PENDING')
           OR (NEW.revision > 1
               AND (v_previous_status <> 'PENDING'
                    OR NEW.status NOT IN ('CONFIRMED','REJECTED'))) THEN
            RAISE EXCEPTION 'bank statement review revision is invalid'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER validate_managed_account_audit
BEFORE INSERT ON public.managed_account
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_bank_statement();
CREATE TRIGGER validate_managed_account_lifecycle_audit
BEFORE INSERT ON public.managed_account_lifecycle
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_bank_statement();
CREATE TRIGGER validate_bank_statement_audit
BEFORE INSERT ON public.bank_statement
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_bank_statement();
CREATE TRIGGER validate_bank_statement_transaction_audit
BEFORE INSERT ON public.bank_statement_transaction
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_bank_statement();
CREATE TRIGGER validate_bank_statement_observation_audit
BEFORE INSERT ON public.bank_statement_observation
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_bank_statement();
CREATE TRIGGER validate_bank_statement_review_audit
BEFORE INSERT ON public.bank_statement_review
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_bank_statement();

CREATE FUNCTION public.r1_require_statement_backed_account()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.bank_statement
         WHERE managed_account_ref = NEW.managed_account_ref
    ) OR NOT EXISTS (
        SELECT 1 FROM public.managed_account_lifecycle
         WHERE managed_account_ref = NEW.managed_account_ref
           AND revision = 1 AND status = 'ACTIVE'
    ) THEN
        RAISE EXCEPTION 'managed account requires statement evidence and lifecycle'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;
CREATE CONSTRAINT TRIGGER require_statement_backed_account
AFTER INSERT ON public.managed_account
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.r1_require_statement_backed_account();

CREATE FUNCTION public.r1_validate_statement_facts()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public
AS $function$
DECLARE
    v_count integer; v_start date; v_end date; v_digest bytea;
    v_statement public.bank_statement%ROWTYPE;
BEGIN
    SELECT * INTO STRICT v_statement
      FROM public.bank_statement
     WHERE statement_ref = NEW.statement_ref;
    SELECT count(*),
           min((transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date),
           max((transaction.occurred_at AT TIME ZONE 'Asia/Shanghai')::date),
           digest(
               string_agg(
                   observation.source_event_ref::text || ':' ||
                   encode(observation.source_row_sha256, 'hex') || ':' ||
                   observation.source_row_number::text,
                   '|' ORDER BY observation.source_row_number
               ),
               'sha256'
           )
      INTO v_count, v_start, v_end, v_digest
      FROM public.bank_statement_observation AS observation
      JOIN public.bank_statement_transaction AS transaction
        ON transaction.transaction_ref = observation.transaction_ref
       AND transaction.managed_account_ref = observation.managed_account_ref
     WHERE observation.statement_ref = NEW.statement_ref;
    IF v_count IS DISTINCT FROM v_statement.transaction_count
       OR v_start IS DISTINCT FROM v_statement.period_start
       OR v_end IS DISTINCT FROM v_statement.period_end
       OR v_digest IS DISTINCT FROM v_statement.transaction_set_sha256 THEN
        RAISE EXCEPTION 'bank statement transaction set is incomplete'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;
CREATE CONSTRAINT TRIGGER validate_statement_facts
AFTER INSERT ON public.bank_statement
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_statement_facts();
CREATE CONSTRAINT TRIGGER validate_statement_observation_set
AFTER INSERT ON public.bank_statement_observation
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_statement_facts();

CREATE FUNCTION public.r1_require_transaction_observation()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM public.bank_statement_observation
         WHERE transaction_ref = NEW.transaction_ref
           AND managed_account_ref = NEW.managed_account_ref
    ) THEN
        RAISE EXCEPTION 'bank statement transaction requires source observation'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;
CREATE CONSTRAINT TRIGGER require_transaction_observation
AFTER INSERT ON public.bank_statement_transaction
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.r1_require_transaction_observation();

CREATE FUNCTION internal_import.import_bank_statement(p_request jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
DECLARE
    v_statement uuid := (p_request->>'statement_ref')::uuid;
    v_account uuid := (p_request->>'managed_account_ref')::uuid;
    v_entity uuid := (p_request->>'entity_ref')::uuid;
    v_evidence uuid := (p_request->>'evidence_ref')::uuid;
    v_source bytea; v_set_digest bytea;
    v_existing public.bank_statement%ROWTYPE;
    v_account_row public.managed_account%ROWTYPE;
    v_evidence_row public.evidence_object%ROWTYPE;
    v_transaction public.bank_statement_transaction%ROWTYPE;
    v_item jsonb; v_audit uuid; v_count integer := 0; v_review text;
    v_transaction_ref uuid; v_fact_digest bytea;
BEGIN
    IF jsonb_typeof(p_request) <> 'object'
       OR jsonb_typeof(p_request->'transactions') <> 'array'
       OR coalesce(p_request->>'source_sha256','') !~ '^[0-9a-f]{64}$'
       OR coalesce(p_request->>'transaction_set_sha256','') !~ '^[0-9a-f]{64}$'
       OR coalesce(p_request->>'account_key','') !~ '^[a-z0-9][a-z0-9._:-]{0,199}$'
       OR p_request->>'institution_code' <> 'mybank'
       OR coalesce(p_request->>'account_suffix','') !~ '^[0-9]{4,8}$'
       OR p_request->>'account_key' <> concat(
            'mybank:', lower(p_request->>'owner_kind'), ':',
            p_request->>'account_suffix'
       )
       OR coalesce(p_request->>'owner_ref','') !~ '^[a-z0-9][a-z0-9._:-]{0,199}$'
       OR p_request->>'owner_kind' NOT IN ('PERSONAL','COMPANY')
       OR coalesce(p_request->>'account_kind','') !~ '^[A-Z][A-Z0-9_]{0,31}$'
       OR p_request->>'lifecycle_status' <> 'ACTIVE'
       OR p_request->>'source_system' <> 'mybank_xlsx_export'
       OR p_request->>'currency' <> 'CNY'
       OR jsonb_array_length(p_request->'transactions') NOT BETWEEN 1 AND 100000
       OR (p_request->>'transaction_count')::integer
            <> jsonb_array_length(p_request->'transactions')
       OR (p_request->>'source_size')::bigint <= 0
       OR (p_request->>'period_start')::date > (p_request->>'period_end')::date
       OR btrim(coalesce(p_request->>'actor','')) = ''
       OR length(p_request->>'actor') > 200
       OR btrim(coalesce(p_request->>'reason','')) = ''
       OR length(p_request->>'reason') > 1000 THEN
        RAISE EXCEPTION 'bank statement request is invalid' USING ERRCODE = '22023';
    END IF;
    v_source := decode(p_request->>'source_sha256', 'hex');
    v_set_digest := decode(p_request->>'transaction_set_sha256', 'hex');
    PERFORM pg_advisory_xact_lock(hashtextextended(p_request->>'source_sha256', 0));
    SELECT * INTO v_evidence_row FROM public.evidence_object
     WHERE evidence_ref = v_evidence;
    IF NOT FOUND
       OR v_evidence_row.entity_id IS DISTINCT FROM v_entity
       OR v_evidence_row.plaintext_sha256 IS DISTINCT FROM v_source
       OR v_evidence_row.plaintext_size
            IS DISTINCT FROM (p_request->>'source_size')::bigint
       OR v_evidence_row.media_type
            IS DISTINCT FROM p_request->>'declared_media_type' THEN
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
           OR v_existing.source_size IS DISTINCT FROM (p_request->>'source_size')::bigint
           OR v_existing.declared_media_type
                IS DISTINCT FROM p_request->>'declared_media_type'
           OR v_existing.currency IS DISTINCT FROM p_request->>'currency'
           OR v_existing.period_start IS DISTINCT FROM (p_request->>'period_start')::date
           OR v_existing.period_end IS DISTINCT FROM (p_request->>'period_end')::date
           OR v_existing.transaction_count
                IS DISTINCT FROM (p_request->>'transaction_count')::integer
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

    PERFORM pg_advisory_xact_lock(hashtextextended(
        'managed-account:' || v_entity::text || ':' || p_request->>'account_key',
        0
    ));
    SELECT * INTO v_account_row FROM public.managed_account
     WHERE managed_account_ref = v_account
        OR (entity_id = v_entity AND account_key = p_request->>'account_key');
    IF FOUND THEN
        IF v_account_row.managed_account_ref IS DISTINCT FROM v_account
           OR v_account_row.entity_id IS DISTINCT FROM v_entity
           OR v_account_row.account_key IS DISTINCT FROM p_request->>'account_key'
           OR v_account_row.institution_code IS DISTINCT FROM p_request->>'institution_code'
           OR v_account_row.account_suffix IS DISTINCT FROM p_request->>'account_suffix'
           OR v_account_row.owner_ref IS DISTINCT FROM p_request->>'owner_ref'
           OR v_account_row.owner_kind IS DISTINCT FROM p_request->>'owner_kind'
           OR v_account_row.account_kind IS DISTINCT FROM p_request->>'account_kind'
           OR NOT EXISTS (
                SELECT 1 FROM public.managed_account_lifecycle
                 WHERE managed_account_ref = v_account
                 ORDER BY revision DESC LIMIT 1
           )
           OR (
                SELECT status <> 'ACTIVE'
                  FROM public.managed_account_lifecycle
                 WHERE managed_account_ref = v_account
                 ORDER BY revision DESC LIMIT 1
           ) THEN
            RAISE EXCEPTION 'managed account conflicts with persisted identity'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSE
        v_audit := public.append_audit_event(
            p_request->>'actor', 'managed_account.register', p_request->>'reason',
            'ledgerbridge.bank-statement.v1',
            jsonb_build_object(
                'managed_account_ref', v_account, 'entity_ref', v_entity,
                'account_key', p_request->>'account_key',
                'institution_code', p_request->>'institution_code',
                'account_suffix', p_request->>'account_suffix',
                'owner_ref', p_request->>'owner_ref',
                'owner_kind', p_request->>'owner_kind',
                'account_kind', p_request->>'account_kind'
            )
        );
        INSERT INTO public.managed_account(
            managed_account_ref, entity_id, account_key,
            institution_code, account_suffix, owner_ref,
            owner_kind, account_kind, audit_event_id, created_at
        ) VALUES (
            v_account, v_entity, p_request->>'account_key',
            p_request->>'institution_code', p_request->>'account_suffix',
            p_request->>'owner_ref',
            p_request->>'owner_kind', p_request->>'account_kind', v_audit,
            (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
        );
        v_audit := public.append_audit_event(
            p_request->>'actor', 'managed_account.lifecycle', p_request->>'reason',
            'ledgerbridge.bank-statement.v1',
            jsonb_build_object(
                'managed_account_ref', v_account, 'revision', 1, 'status', 'ACTIVE'
            )
        );
        INSERT INTO public.managed_account_lifecycle(
            managed_account_ref, revision, status, audit_event_id, effective_at
        ) VALUES (
            v_account, 1, 'ACTIVE', v_audit,
            (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
        );
    END IF;

    v_audit := public.append_audit_event(
        p_request->>'actor', 'bank_statement.import', p_request->>'reason',
        'ledgerbridge.bank-statement.v1',
        jsonb_build_object(
            'statement_ref', v_statement, 'managed_account_ref', v_account,
            'evidence_ref', v_evidence,
            'source_system', p_request->>'source_system',
            'source_sha256', p_request->>'source_sha256',
            'source_size', (p_request->>'source_size')::bigint,
            'declared_media_type', p_request->>'declared_media_type',
            'currency', p_request->>'currency',
            'period_start', p_request->>'period_start',
            'period_end', p_request->>'period_end',
            'transaction_count', (p_request->>'transaction_count')::integer,
            'transaction_set_sha256', p_request->>'transaction_set_sha256'
        )
    );
    INSERT INTO public.bank_statement(
        statement_ref, managed_account_ref, evidence_ref, source_system,
        source_sha256, source_size, declared_media_type, currency,
        period_start, period_end, transaction_count, transaction_set_sha256,
        audit_event_id, created_at
    ) VALUES (
        v_statement, v_account, v_evidence, p_request->>'source_system',
        v_source, (p_request->>'source_size')::bigint,
        p_request->>'declared_media_type', p_request->>'currency',
        (p_request->>'period_start')::date, (p_request->>'period_end')::date,
        (p_request->>'transaction_count')::integer, v_set_digest, v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
    );

    FOR v_item IN SELECT value FROM jsonb_array_elements(p_request->'transactions') LOOP
        IF coalesce(v_item->>'source_row_sha256','') !~ '^[0-9a-f]{64}$'
           OR coalesce(v_item->>'counterparty_ref','') !~ '^cp_[a-z0-9_]{1,96}$'
           OR (v_item->>'source_row_number')::integer <= 0
           OR btrim(coalesce(v_item->>'transaction_serial','')) = ''
           OR length(v_item->>'transaction_serial') > 300
           OR btrim(coalesce(v_item->>'transaction_name','')) = ''
           OR length(v_item->>'transaction_name') > 300
           OR length(coalesce(v_item->>'counterparty_name','')) > 300
           OR length(coalesce(v_item->>'counterparty_account','')) > 300
           OR length(coalesce(v_item->>'counterparty_institution','')) > 300 THEN
            RAISE EXCEPTION 'bank statement transaction is invalid'
                USING ERRCODE = '22023';
        END IF;
        v_fact_digest := public.r1_bank_statement_transaction_digest(
            v_account, (v_item->>'occurred_at')::timestamptz,
            (v_item->>'amount_minor')::bigint, (v_item->>'balance_minor')::bigint,
            'CNY', v_item->>'counterparty_ref',
            nullif(v_item->>'counterparty_name',''),
            nullif(v_item->>'counterparty_account',''),
            nullif(v_item->>'counterparty_institution',''),
            v_item->>'transaction_serial', v_item->>'transaction_name'
        );
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'bank-statement-transaction:' || v_account::text || ':' ||
            (v_item->>'transaction_serial'),
            0
        ));
        SELECT * INTO v_transaction
          FROM public.bank_statement_transaction
         WHERE managed_account_ref = v_account
           AND transaction_serial = v_item->>'transaction_serial'
         FOR UPDATE;
        IF FOUND THEN
            IF v_transaction.fact_sha256 IS DISTINCT FROM v_fact_digest
               OR v_transaction.occurred_at
                    IS DISTINCT FROM (v_item->>'occurred_at')::timestamptz
               OR v_transaction.amount_minor
                    IS DISTINCT FROM (v_item->>'amount_minor')::bigint
               OR v_transaction.balance_minor
                    IS DISTINCT FROM (v_item->>'balance_minor')::bigint
               OR v_transaction.currency IS DISTINCT FROM 'CNY'
               OR v_transaction.counterparty_ref
                    IS DISTINCT FROM v_item->>'counterparty_ref'
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
            v_transaction_ref := (v_item->>'source_event_ref')::uuid;
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
                v_transaction_ref, v_account,
                (v_item->>'occurred_at')::timestamptz,
                (v_item->>'amount_minor')::bigint, (v_item->>'balance_minor')::bigint,
                'CNY', v_item->>'counterparty_ref',
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
                'source_event_ref', (v_item->>'source_event_ref')::uuid,
                'statement_ref', v_statement,
                'managed_account_ref', v_account,
                'transaction_ref', v_transaction_ref,
                'source_row_number', (v_item->>'source_row_number')::integer,
                'source_row_sha256', v_item->>'source_row_sha256'
            )
        );
        INSERT INTO public.bank_statement_observation(
            source_event_ref, statement_ref, managed_account_ref, transaction_ref,
            source_row_number, source_row_sha256, audit_event_id, created_at
        ) VALUES (
            (v_item->>'source_event_ref')::uuid, v_statement, v_account,
            v_transaction_ref, (v_item->>'source_row_number')::integer,
            decode(v_item->>'source_row_sha256', 'hex'), v_audit,
            (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
        );
        v_count := v_count + 1;
    END LOOP;
    IF v_count <> (p_request->>'transaction_count')::integer THEN
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

CREATE FUNCTION internal_command.review_bank_statement(
    p_statement_ref uuid, p_operation_id uuid, p_assertion_jti uuid,
    p_actor_ref text, p_workload_principal_ref text,
    p_expected_revision integer, p_decision text, p_reason text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
AS $function$
DECLARE
    v_existing public.bank_statement_review%ROWTYPE;
    v_status text; v_revision integer; v_audit uuid; v_command bytea;
BEGIN
    IF p_statement_ref IS NULL OR p_operation_id IS NULL OR p_assertion_jti IS NULL
       OR p_decision NOT IN ('CONFIRMED','REJECTED')
       OR btrim(coalesce(p_actor_ref,'')) = '' OR length(p_actor_ref) > 200
       OR btrim(coalesce(p_workload_principal_ref,'')) = ''
       OR length(p_workload_principal_ref) > 200
       OR p_expected_revision IS NULL OR p_expected_revision < 1
       OR btrim(coalesce(p_reason,'')) = '' OR length(p_reason) > 1000 THEN
        RAISE EXCEPTION 'bank statement review request is invalid'
            USING ERRCODE = '22023';
    END IF;
    v_command := public.digest(
        convert_to(
            jsonb_build_array(
                p_statement_ref, p_operation_id, p_assertion_jti,
                p_actor_ref, p_workload_principal_ref,
                p_expected_revision, p_decision, p_reason
            )::text,
            'UTF8'
        ),
        'sha256'
    );
    PERFORM pg_advisory_xact_lock(
        hashtextextended('bank-statement-review:' || p_operation_id::text, 0)
    );
    PERFORM pg_advisory_xact_lock(
        hashtextextended('bank-statement-assertion:' || p_assertion_jti::text, 0)
    );
    SELECT * INTO v_existing
      FROM public.bank_statement_review
     WHERE operation_id = p_operation_id;
    IF FOUND THEN
        IF v_existing.statement_ref IS DISTINCT FROM p_statement_ref
           OR v_existing.assertion_jti IS DISTINCT FROM p_assertion_jti
           OR v_existing.actor_ref IS DISTINCT FROM p_actor_ref
           OR v_existing.workload_principal_ref IS DISTINCT FROM p_workload_principal_ref
           OR v_existing.expected_revision IS DISTINCT FROM p_expected_revision
           OR v_existing.status IS DISTINCT FROM p_decision
           OR v_existing.command_sha256 IS DISTINCT FROM v_command THEN
            RAISE EXCEPTION 'bank statement review idempotency conflict'
                USING ERRCODE = 'unique_violation';
        END IF;
        RETURN jsonb_build_object(
            'statement_ref', p_statement_ref, 'decision', v_existing.status,
            'revision', v_existing.revision, 'created', false
        );
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.bank_statement_review
         WHERE assertion_jti = p_assertion_jti
    ) THEN
        RAISE EXCEPTION 'bank statement review assertion was already used'
            USING ERRCODE = 'unique_violation';
    END IF;
    PERFORM 1 FROM public.bank_statement
     WHERE statement_ref = p_statement_ref FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'bank statement was not found' USING ERRCODE = 'no_data_found';
    END IF;
    SELECT status, revision INTO v_status, v_revision
      FROM public.bank_statement_review
     WHERE statement_ref = p_statement_ref
     ORDER BY revision DESC LIMIT 1 FOR UPDATE;
    IF NOT FOUND OR v_revision IS DISTINCT FROM p_expected_revision THEN
        RAISE EXCEPTION 'bank statement review expected revision is stale'
            USING ERRCODE = 'serialization_failure';
    END IF;
    IF v_status IS DISTINCT FROM 'PENDING' THEN
        RAISE EXCEPTION 'bank statement review is already terminal'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    v_revision := v_revision + 1;
    v_audit := public.append_audit_event(
        p_actor_ref, 'bank_statement.review', p_reason,
        'ledgerbridge.bank-statement.v1',
        jsonb_build_object(
            'statement_ref', p_statement_ref, 'revision', v_revision,
            'status', p_decision, 'operation_id', p_operation_id,
            'assertion_jti', p_assertion_jti, 'actor_ref', p_actor_ref,
            'workload_principal_ref', p_workload_principal_ref,
            'expected_revision', p_expected_revision,
            'command_sha256', encode(v_command, 'hex')
        )
    );
    INSERT INTO public.bank_statement_review(
        statement_ref, revision, status, operation_id, assertion_jti,
        actor_ref, workload_principal_ref, expected_revision, command_sha256,
        audit_event_id, reviewed_at
    ) VALUES (
        p_statement_ref, v_revision, p_decision, p_operation_id, p_assertion_jti,
        p_actor_ref, p_workload_principal_ref, p_expected_revision, v_command,
        v_audit, (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
    );
    RETURN jsonb_build_object(
        'statement_ref', p_statement_ref, 'decision', p_decision,
        'revision', v_revision, 'created', true
    );
END
$function$;

CREATE FUNCTION internal_read.get_bank_statement_summary(
    p_statement_ref uuid, p_entity_ref uuid, p_audit_horizon_sequence bigint,
    p_audit_horizon_hash bytea
) RETURNS TABLE(
    statement_ref uuid, managed_account_ref uuid, evidence_ref uuid,
    period_start date, period_end date, transaction_count integer,
    review_status varchar(16), review_revision integer
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    IF p_statement_ref IS NULL OR p_entity_ref IS NULL
       OR p_audit_horizon_sequence IS NULL
       OR p_audit_horizon_hash IS NULL OR octet_length(p_audit_horizon_hash) <> 32
       OR NOT EXISTS (
            SELECT 1 FROM public.audit_event
             WHERE sequence = p_audit_horizon_sequence
               AND hash = p_audit_horizon_hash
       ) THEN
        RAISE EXCEPTION 'bank statement summary horizon is invalid'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT statement.statement_ref, statement.managed_account_ref,
           statement.evidence_ref, statement.period_start, statement.period_end,
           statement.transaction_count, review.status, review.revision
      FROM public.bank_statement AS statement
      JOIN public.managed_account AS account
        ON account.managed_account_ref = statement.managed_account_ref
      JOIN public.audit_event AS imported ON imported.id = statement.audit_event_id
      JOIN LATERAL (
            SELECT item.status, item.revision
              FROM public.bank_statement_review AS item
              JOIN public.audit_event AS event ON event.id = item.audit_event_id
             WHERE item.statement_ref = statement.statement_ref
               AND event.sequence <= p_audit_horizon_sequence
             ORDER BY item.revision DESC LIMIT 1
      ) AS review ON true
     WHERE statement.statement_ref = p_statement_ref
       AND account.entity_id = p_entity_ref
       AND imported.sequence <= p_audit_horizon_sequence;
END
$function$;

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
       OR p_audit_horizon_sequence IS NULL
       OR p_audit_horizon_hash IS NULL OR octet_length(p_audit_horizon_hash) <> 32
       OR p_after_row IS NULL OR p_after_row < 0
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 200
       OR NOT EXISTS (
            SELECT 1 FROM public.audit_event
             WHERE sequence = p_audit_horizon_sequence
               AND hash = p_audit_horizon_hash
       ) THEN
        RAISE EXCEPTION 'bank statement transaction page request is invalid'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    SELECT observation.source_row_number, transaction.occurred_at,
           transaction.amount_minor, transaction.balance_minor, transaction.currency,
           transaction.counterparty_ref, transaction.counterparty_name,
           CASE
               WHEN transaction.counterparty_account IS NULL THEN NULL
               WHEN length(transaction.counterparty_account) <= 4
                    THEN repeat('*', length(transaction.counterparty_account))
               ELSE repeat('*', length(transaction.counterparty_account) - 4) ||
                    right(transaction.counterparty_account, 4)
           END::varchar(300),
           transaction.counterparty_institution, transaction.transaction_serial,
           transaction.transaction_name
      FROM public.bank_statement AS statement
      JOIN public.managed_account AS account
        ON account.managed_account_ref = statement.managed_account_ref
      JOIN public.bank_statement_observation AS observation
        ON observation.statement_ref = statement.statement_ref
       AND observation.managed_account_ref = statement.managed_account_ref
      JOIN public.bank_statement_transaction AS transaction
        ON transaction.transaction_ref = observation.transaction_ref
       AND transaction.managed_account_ref = observation.managed_account_ref
      JOIN public.audit_event AS imported ON imported.id = statement.audit_event_id
      JOIN public.audit_event AS observed ON observed.id = observation.audit_event_id
      JOIN public.audit_event AS recorded ON recorded.id = transaction.audit_event_id
     WHERE statement.statement_ref = p_statement_ref
       AND account.entity_id = p_entity_ref
       AND imported.sequence <= p_audit_horizon_sequence
       AND observed.sequence <= p_audit_horizon_sequence
       AND recorded.sequence <= p_audit_horizon_sequence
       AND observation.source_row_number > p_after_row
     ORDER BY observation.source_row_number
     LIMIT p_limit;
END
$function$;

REVOKE ALL ON TABLE public.managed_account,
    public.managed_account_lifecycle, public.bank_statement,
    public.bank_statement_transaction, public.bank_statement_observation,
    public.bank_statement_review
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker,
         ledgerbridge_app, ledgerbridge_backup;
REVOKE ALL ON FUNCTION public.r1_bank_statement_append_only(),
    public.r1_bank_statement_transaction_digest(
        uuid,timestamptz,bigint,bigint,text,text,text,text,text,text,text
    ),
    public.r1_validate_bank_statement(), public.r1_validate_statement_facts(),
    public.r1_require_transaction_observation(),
    public.r1_require_statement_backed_account(),
    internal_import.import_bank_statement(jsonb),
    internal_command.review_bank_statement(
        uuid,uuid,uuid,text,text,integer,text,text
    ),
    internal_read.get_bank_statement_summary(uuid,uuid,bigint,bytea),
    internal_read.list_bank_statement_transactions(uuid,uuid,bigint,bytea,integer,integer)
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker,
         ledgerbridge_app, ledgerbridge_backup;
GRANT USAGE ON SCHEMA internal_import TO ledgerbridge_worker;
GRANT EXECUTE ON FUNCTION internal_import.import_bank_statement(jsonb)
    TO ledgerbridge_worker;
GRANT USAGE ON SCHEMA internal_read TO ledgerbridge_reader;
GRANT EXECUTE ON FUNCTION internal_read.get_bank_statement_summary(uuid,uuid,bigint,bytea)
    TO ledgerbridge_reader;
GRANT EXECUTE ON FUNCTION internal_read.list_bank_statement_transactions(
    uuid,uuid,bigint,bytea,integer,integer
) TO ledgerbridge_reader;
"""


def downgrade() -> None:
    if os.getenv("LEDGERBRIDGE_ENV", "development").lower() == "production":
        raise RuntimeError("bank statement facts are irreversible in production")
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM public.bank_statement)")
    ).scalar_one():
        raise RuntimeError("development downgrade would discard bank statement facts")
    op.execute(_DOWNGRADE_SQL)


_DOWNGRADE_SQL = r"""
DROP FUNCTION IF EXISTS internal_read.list_bank_statement_transactions(
    uuid,uuid,bigint,bytea,integer,integer
);
DROP FUNCTION IF EXISTS internal_read.get_bank_statement_summary(uuid,uuid,bigint,bytea);
DROP FUNCTION IF EXISTS internal_command.review_bank_statement(
    uuid,uuid,uuid,text,text,integer,text,text
);
DROP FUNCTION IF EXISTS internal_import.import_bank_statement(jsonb);
DROP TABLE public.bank_statement_review;
DROP TABLE public.bank_statement_observation;
DROP TABLE public.bank_statement_transaction;
DROP TABLE public.bank_statement;
DROP TABLE public.managed_account_lifecycle;
DROP TABLE public.managed_account;
DROP FUNCTION IF EXISTS public.r1_require_transaction_observation();
DROP FUNCTION IF EXISTS public.r1_validate_statement_facts();
DROP FUNCTION IF EXISTS public.r1_require_statement_backed_account();
DROP FUNCTION IF EXISTS public.r1_validate_bank_statement();
DROP FUNCTION IF EXISTS public.r1_bank_statement_transaction_digest(
    uuid,timestamptz,bigint,bigint,text,text,text,text,text,text,text
);
DROP FUNCTION IF EXISTS public.r1_bank_statement_append_only();
"""
