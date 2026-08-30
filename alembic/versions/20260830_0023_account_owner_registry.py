"""Add the Accounting Owner and Managed Account registry.

Revision ID: 20260830_0023
Revises: 20260830_0022
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_0023"
down_revision: str | None = "20260830_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM public.managed_account)")
    ).scalar_one():
        raise RuntimeError("0023 refuses to infer Accounting Owners for existing managed accounts")
    op.execute(_UPGRADE_SQL)


_UPGRADE_SQL = r"""
ALTER TABLE public.evidence_object
    ADD CONSTRAINT uq_evidence_object_entity_ref UNIQUE (entity_id, evidence_ref);
ALTER TABLE public.managed_account
    ADD COLUMN admission_evidence_ref uuid NOT NULL;
ALTER TABLE public.managed_account
    ADD CONSTRAINT fk_managed_account_admission_evidence
        FOREIGN KEY (entity_id, admission_evidence_ref)
        REFERENCES public.evidence_object(entity_id, evidence_ref) ON DELETE RESTRICT;
ALTER TABLE public.managed_account
    ADD CONSTRAINT uq_managed_account_ref_entity UNIQUE (managed_account_ref, entity_id);
ALTER TABLE public.managed_account
    ADD CONSTRAINT managed_account_owner_ref_is_entity
        CHECK (owner_ref = entity_id::text);
ALTER TABLE public.managed_account
    DROP CONSTRAINT managed_account_institution_code_check;
ALTER TABLE public.managed_account
    ADD CONSTRAINT managed_account_institution_code_format
        CHECK (institution_code ~ '^[a-z0-9][a-z0-9_]{0,31}$');

DROP TRIGGER validate_managed_account_audit ON public.managed_account;
DROP TRIGGER require_statement_backed_account ON public.managed_account;

CREATE FUNCTION public.account_registry_normalize_alias(p_value text)
RETURNS text LANGUAGE sql IMMUTABLE SET search_path = pg_catalog
AS $function$
SELECT lower(regexp_replace(btrim(p_value), '[[:space:]-]+', '', 'g'))
$function$;

CREATE TABLE public.account_registry_operation (
    operation_id uuid PRIMARY KEY,
    owner_entity_id uuid NOT NULL REFERENCES public.entity(id) ON DELETE RESTRICT,
    registry_revision integer NOT NULL CHECK (registry_revision > 0),
    request_sha256 bytea NOT NULL CHECK (octet_length(request_sha256) = 32),
    result jsonb NOT NULL CHECK (jsonb_typeof(result) = 'object'),
    workload_principal_ref varchar(200) NOT NULL CHECK (
        btrim(workload_principal_ref) <> ''
    ),
    policy_generation integer NOT NULL CHECK (policy_generation > 0),
    audit_event_id uuid NOT NULL UNIQUE
        REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL,
    UNIQUE (owner_entity_id, registry_revision)
);
CREATE TABLE public.managed_account_alias (
    alias_ref uuid PRIMARY KEY,
    owner_entity_id uuid NOT NULL,
    managed_account_ref uuid NOT NULL,
    institution_code varchar(32) NOT NULL CHECK (
        institution_code ~ '^[a-z0-9][a-z0-9_]{0,31}$'
    ),
    alias_kind varchar(32) NOT NULL CHECK (alias_kind ~ '^[A-Z][A-Z0-9_]{0,31}$'),
    alias_value varchar(300) NOT NULL CHECK (btrim(alias_value) <> ''),
    normalized_value varchar(300) NOT NULL CHECK (btrim(normalized_value) <> ''),
    audit_event_id uuid NOT NULL UNIQUE
        REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL,
    FOREIGN KEY (managed_account_ref, owner_entity_id)
        REFERENCES public.managed_account(managed_account_ref, entity_id)
        ON DELETE RESTRICT,
    UNIQUE (institution_code, alias_kind, normalized_value)
);
CREATE TABLE public.account_business_unit_assignment (
    assignment_ref uuid PRIMARY KEY,
    owner_entity_id uuid NOT NULL,
    managed_account_ref uuid NOT NULL,
    business_unit_id uuid NOT NULL,
    business_unit_ref_snapshot varchar(100) NOT NULL CHECK (
        btrim(business_unit_ref_snapshot) <> ''
    ),
    business_unit_label_snapshot varchar(200) NOT NULL CHECK (
        btrim(business_unit_label_snapshot) <> ''
    ),
    effective_from date NOT NULL,
    effective_to date,
    audit_event_id uuid NOT NULL UNIQUE
        REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL,
    CHECK (effective_to IS NULL OR effective_from < effective_to),
    FOREIGN KEY (managed_account_ref, owner_entity_id)
        REFERENCES public.managed_account(managed_account_ref, entity_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (owner_entity_id, business_unit_id)
        REFERENCES public.business_unit(entity_id, id) ON DELETE RESTRICT
);
CREATE TABLE public.fact_business_unit_allocation_set (
    allocation_set_ref uuid PRIMARY KEY,
    owner_entity_id uuid NOT NULL,
    managed_account_ref uuid NOT NULL,
    fact_ref uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    audit_event_id uuid NOT NULL UNIQUE
        REFERENCES public.audit_event(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL,
    FOREIGN KEY (managed_account_ref, owner_entity_id)
        REFERENCES public.managed_account(managed_account_ref, entity_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (fact_ref, managed_account_ref)
        REFERENCES public.bank_statement_transaction(transaction_ref, managed_account_ref)
        ON DELETE RESTRICT,
    UNIQUE (fact_ref, revision)
);
CREATE TABLE public.fact_business_unit_allocation_item (
    allocation_set_ref uuid NOT NULL
        REFERENCES public.fact_business_unit_allocation_set(allocation_set_ref)
        ON DELETE RESTRICT,
    owner_entity_id uuid NOT NULL,
    business_unit_id uuid NOT NULL,
    business_unit_ref_snapshot varchar(100) NOT NULL CHECK (
        btrim(business_unit_ref_snapshot) <> ''
    ),
    business_unit_label_snapshot varchar(200) NOT NULL CHECK (
        btrim(business_unit_label_snapshot) <> ''
    ),
    basis_points integer NOT NULL CHECK (basis_points BETWEEN 1 AND 10000),
    PRIMARY KEY (allocation_set_ref, business_unit_id),
    FOREIGN KEY (owner_entity_id, business_unit_id)
        REFERENCES public.business_unit(entity_id, id) ON DELETE RESTRICT
);

CREATE FUNCTION public.account_registry_append_only()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
AS $function$
BEGIN
    RAISE EXCEPTION 'account registry facts are append-only'
        USING ERRCODE = 'integrity_constraint_violation';
END
$function$;
CREATE TRIGGER account_registry_operation_append_only
BEFORE UPDATE OR DELETE ON public.account_registry_operation
FOR EACH ROW EXECUTE FUNCTION public.account_registry_append_only();
CREATE TRIGGER managed_account_alias_append_only
BEFORE UPDATE OR DELETE ON public.managed_account_alias
FOR EACH ROW EXECUTE FUNCTION public.account_registry_append_only();
CREATE TRIGGER account_business_unit_assignment_append_only
BEFORE UPDATE OR DELETE ON public.account_business_unit_assignment
FOR EACH ROW EXECUTE FUNCTION public.account_registry_append_only();
CREATE TRIGGER fact_business_unit_allocation_set_append_only
BEFORE UPDATE OR DELETE ON public.fact_business_unit_allocation_set
FOR EACH ROW EXECUTE FUNCTION public.account_registry_append_only();
CREATE TRIGGER fact_business_unit_allocation_item_append_only
BEFORE UPDATE OR DELETE ON public.fact_business_unit_allocation_item
FOR EACH ROW EXECUTE FUNCTION public.account_registry_append_only();

CREATE FUNCTION public.account_registry_validate_managed_account()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_entity_type text; v_audit public.audit_event%ROWTYPE;
    v_expected_owner_kind text;
BEGIN
    SELECT entity_type::text INTO v_entity_type
      FROM public.entity WHERE id = NEW.entity_id;
    v_expected_owner_kind := CASE v_entity_type
        WHEN 'PERSON' THEN 'PERSONAL'
        WHEN 'COMPANY' THEN 'COMPANY'
    END;
    SELECT * INTO v_audit FROM public.audit_event WHERE id = NEW.audit_event_id;
    IF v_entity_type IS NULL
       OR NEW.owner_ref IS DISTINCT FROM NEW.entity_id::text
       OR NEW.owner_kind IS DISTINCT FROM v_expected_owner_kind
       OR NOT EXISTS (
            SELECT 1 FROM public.evidence_object
             WHERE entity_id = NEW.entity_id
               AND evidence_ref = NEW.admission_evidence_ref
       )
       OR v_audit.action IS DISTINCT FROM 'account_registry.account.register'
       OR v_audit.rule_version IS DISTINCT FROM 'ledgerbridge.account-registry.v1'
       OR v_audit.payload->>'managed_account_ref'
            IS DISTINCT FROM NEW.managed_account_ref::text
       OR v_audit.payload->>'owner_entity_ref' IS DISTINCT FROM NEW.entity_id::text
       OR v_audit.payload->>'owner_kind' IS DISTINCT FROM v_entity_type
       OR v_audit.payload->>'admission_evidence_ref'
            IS DISTINCT FROM NEW.admission_evidence_ref::text
       OR v_audit.payload->>'account_key' IS DISTINCT FROM NEW.account_key
       OR v_audit.payload->>'institution_code' IS DISTINCT FROM NEW.institution_code
       OR v_audit.payload->>'account_suffix' IS DISTINCT FROM NEW.account_suffix
       OR v_audit.payload->>'account_kind' IS DISTINCT FROM NEW.account_kind
       OR NEW.created_at IS DISTINCT FROM v_audit.occurred_at THEN
        RAISE EXCEPTION 'managed account registry binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER validate_managed_account_registry
BEFORE INSERT ON public.managed_account
FOR EACH ROW EXECUTE FUNCTION public.account_registry_validate_managed_account();

CREATE FUNCTION public.account_registry_validate_fact()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_audit public.audit_event%ROWTYPE; v_action text;
BEGIN
    SELECT * INTO v_audit FROM public.audit_event WHERE id = NEW.audit_event_id;
    v_action := CASE TG_TABLE_NAME
        WHEN 'managed_account_alias' THEN 'account_registry.alias.register'
        WHEN 'account_business_unit_assignment' THEN 'account_registry.business_unit.assign'
        WHEN 'fact_business_unit_allocation_set' THEN 'account_registry.fact.allocate'
        WHEN 'account_registry_operation' THEN 'account_registry.plan.apply'
    END;
    IF v_audit.action IS DISTINCT FROM v_action
       OR v_audit.rule_version IS DISTINCT FROM 'ledgerbridge.account-registry.v1'
       OR NEW.created_at IS DISTINCT FROM v_audit.occurred_at THEN
        RAISE EXCEPTION 'account registry audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_TABLE_NAME = 'managed_account_alias' AND (
        v_audit.payload->>'alias_ref' IS DISTINCT FROM NEW.alias_ref::text
        OR v_audit.payload->>'managed_account_ref'
            IS DISTINCT FROM NEW.managed_account_ref::text
        OR v_audit.payload->>'owner_entity_ref'
            IS DISTINCT FROM NEW.owner_entity_id::text
        OR v_audit.payload->>'alias_kind' IS DISTINCT FROM NEW.alias_kind
        OR v_audit.payload->>'normalized_value'
            IS DISTINCT FROM NEW.normalized_value
    ) THEN
        RAISE EXCEPTION 'account registry alias audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    ELSIF TG_TABLE_NAME = 'account_business_unit_assignment' AND (
        v_audit.payload->>'assignment_ref' IS DISTINCT FROM NEW.assignment_ref::text
        OR v_audit.payload->>'managed_account_ref'
            IS DISTINCT FROM NEW.managed_account_ref::text
        OR v_audit.payload->>'business_unit_id'
            IS DISTINCT FROM NEW.business_unit_id::text
        OR v_audit.payload->>'business_unit_ref_snapshot'
            IS DISTINCT FROM NEW.business_unit_ref_snapshot
        OR v_audit.payload->>'business_unit_label_snapshot'
            IS DISTINCT FROM NEW.business_unit_label_snapshot
        OR v_audit.payload->>'effective_from' IS DISTINCT FROM NEW.effective_from::text
        OR v_audit.payload->>'effective_to'
            IS DISTINCT FROM coalesce(NEW.effective_to::text, '')
    ) THEN
        RAISE EXCEPTION 'account business-unit audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    ELSIF TG_TABLE_NAME = 'fact_business_unit_allocation_set' AND (
        v_audit.payload->>'allocation_set_ref'
            IS DISTINCT FROM NEW.allocation_set_ref::text
        OR v_audit.payload->>'managed_account_ref'
            IS DISTINCT FROM NEW.managed_account_ref::text
        OR v_audit.payload->>'fact_ref' IS DISTINCT FROM NEW.fact_ref::text
        OR v_audit.payload->>'revision' IS DISTINCT FROM NEW.revision::text
    ) THEN
        RAISE EXCEPTION 'fact allocation audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    ELSIF TG_TABLE_NAME = 'account_registry_operation' AND (
        v_audit.payload->>'operation_id' IS DISTINCT FROM NEW.operation_id::text
        OR v_audit.payload->>'owner_entity_ref'
            IS DISTINCT FROM NEW.owner_entity_id::text
        OR v_audit.payload->>'registry_revision'
            IS DISTINCT FROM NEW.registry_revision::text
        OR v_audit.payload->>'request_sha256'
            IS DISTINCT FROM encode(NEW.request_sha256, 'hex')
        OR v_audit.payload->>'workload_principal_ref'
            IS DISTINCT FROM NEW.workload_principal_ref
        OR v_audit.payload->>'policy_generation'
            IS DISTINCT FROM NEW.policy_generation::text
    ) THEN
        RAISE EXCEPTION 'account registry operation audit binding is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER validate_managed_account_alias_registry
BEFORE INSERT ON public.managed_account_alias
FOR EACH ROW EXECUTE FUNCTION public.account_registry_validate_fact();
CREATE TRIGGER validate_account_business_unit_registry
BEFORE INSERT ON public.account_business_unit_assignment
FOR EACH ROW EXECUTE FUNCTION public.account_registry_validate_fact();
CREATE TRIGGER validate_fact_business_unit_allocation_registry
BEFORE INSERT ON public.fact_business_unit_allocation_set
FOR EACH ROW EXECUTE FUNCTION public.account_registry_validate_fact();
CREATE TRIGGER validate_account_registry_operation
BEFORE INSERT ON public.account_registry_operation
FOR EACH ROW EXECUTE FUNCTION public.account_registry_validate_fact();

CREATE FUNCTION public.account_registry_validate_business_unit_snapshot()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.business_unit
         WHERE entity_id = NEW.owner_entity_id
           AND id = NEW.business_unit_id
           AND ref = NEW.business_unit_ref_snapshot
           AND label = NEW.business_unit_label_snapshot
    ) THEN
        RAISE EXCEPTION 'business-unit snapshot does not match current directory'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER validate_account_business_unit_snapshot
BEFORE INSERT ON public.account_business_unit_assignment
FOR EACH ROW EXECUTE FUNCTION public.account_registry_validate_business_unit_snapshot();
CREATE TRIGGER validate_fact_business_unit_snapshot
BEFORE INSERT ON public.fact_business_unit_allocation_item
FOR EACH ROW EXECUTE FUNCTION public.account_registry_validate_business_unit_snapshot();

CREATE FUNCTION public.account_registry_reject_assignment_overlap()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('account-unit:' || NEW.managed_account_ref::text, 0)
    );
    IF EXISTS (
        SELECT 1 FROM public.account_business_unit_assignment AS existing
         WHERE existing.managed_account_ref = NEW.managed_account_ref
           AND daterange(existing.effective_from, existing.effective_to, '[)')
               && daterange(NEW.effective_from, NEW.effective_to, '[)')
    ) THEN
        RAISE EXCEPTION 'account business-unit assignment overlaps'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER reject_account_business_unit_overlap
BEFORE INSERT ON public.account_business_unit_assignment
FOR EACH ROW EXECUTE FUNCTION public.account_registry_reject_assignment_overlap();

CREATE FUNCTION public.account_registry_validate_allocation_revision()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE v_expected integer;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('fact-allocation:' || NEW.fact_ref::text, 0));
    SELECT coalesce(max(revision), 0) + 1 INTO v_expected
      FROM public.fact_business_unit_allocation_set WHERE fact_ref = NEW.fact_ref;
    IF NEW.revision IS DISTINCT FROM v_expected THEN
        RAISE EXCEPTION 'fact allocation revision is invalid'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;
CREATE TRIGGER validate_fact_allocation_revision
BEFORE INSERT ON public.fact_business_unit_allocation_set
FOR EACH ROW EXECUTE FUNCTION public.account_registry_validate_allocation_revision();

CREATE FUNCTION public.account_registry_require_allocation_total()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE v_set uuid; v_total integer;
BEGIN
    v_set := CASE TG_TABLE_NAME
        WHEN 'fact_business_unit_allocation_set' THEN NEW.allocation_set_ref
        ELSE NEW.allocation_set_ref
    END;
    SELECT sum(basis_points) INTO v_total
      FROM public.fact_business_unit_allocation_item
     WHERE allocation_set_ref = v_set;
    IF v_total IS DISTINCT FROM 10000 THEN
        RAISE EXCEPTION 'fact allocation must total 10000 basis points'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;
CREATE CONSTRAINT TRIGGER require_fact_allocation_set_total
AFTER INSERT ON public.fact_business_unit_allocation_set
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.account_registry_require_allocation_total();
CREATE CONSTRAINT TRIGGER require_fact_allocation_item_total
AFTER INSERT ON public.fact_business_unit_allocation_item
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.account_registry_require_allocation_total();

ALTER FUNCTION internal_import.import_bank_statement(jsonb)
    RENAME TO import_bank_statement_0021;
REVOKE ALL ON FUNCTION internal_import.import_bank_statement_0021(jsonb)
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;

CREATE FUNCTION internal_import.import_bank_statement(p_request jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_owner uuid; v_account uuid; v_account_row public.managed_account%ROWTYPE;
    v_request jsonb;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ?| ARRAY['owner_ref','owner_kind','account_kind','account_key','entity_ref']
       OR btrim(coalesce(p_request->>'owner_entity_ref','')) = ''
       OR btrim(coalesce(p_request->>'managed_account_ref','')) = ''
       OR (p_request->>'institution_code') IS DISTINCT FROM 'mybank'
       OR coalesce(p_request->>'account_suffix','') !~ '^[0-9]{4,8}$' THEN
        RAISE EXCEPTION 'bank statement request is invalid' USING ERRCODE = '22023';
    END IF;
    BEGIN
        v_owner := (p_request->>'owner_entity_ref')::uuid;
        v_account := (p_request->>'managed_account_ref')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION 'bank statement request is invalid' USING ERRCODE = '22023';
    END;
    SELECT * INTO v_account_row FROM public.managed_account
     WHERE managed_account_ref = v_account AND entity_id = v_owner;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'managed account must be registered before statement import'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF v_account_row.institution_code IS DISTINCT FROM p_request->>'institution_code'
       OR v_account_row.account_suffix IS DISTINCT FROM p_request->>'account_suffix'
       OR NOT EXISTS (
            SELECT 1 FROM public.managed_account_lifecycle
             WHERE managed_account_ref = v_account AND status = 'ACTIVE'
             ORDER BY revision DESC LIMIT 1
       )
       OR (
            SELECT status <> 'ACTIVE' FROM public.managed_account_lifecycle
             WHERE managed_account_ref = v_account ORDER BY revision DESC LIMIT 1
       ) THEN
        RAISE EXCEPTION 'managed account conflicts with registered identity'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    v_request := (p_request - 'owner_entity_ref') || jsonb_build_object(
        'entity_ref', v_owner,
        'account_key', v_account_row.account_key,
        'owner_ref', v_account_row.owner_ref,
        'owner_kind', v_account_row.owner_kind,
        'account_kind', v_account_row.account_kind,
        'lifecycle_status', 'ACTIVE'
    );
    RETURN internal_import.import_bank_statement_0021(v_request);
END
$function$;

CREATE FUNCTION internal_command.apply_account_registry_plan(p_request jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_operation_id uuid; v_owner uuid; v_expected_revision integer;
    v_policy_generation integer; v_entity_type text; v_owner_kind text;
    v_request_digest bytea; v_current_revision integer; v_revision integer;
    v_operation public.account_registry_operation%ROWTYPE;
    v_account_row public.managed_account%ROWTYPE;
    v_alias_row public.managed_account_alias%ROWTYPE;
    v_account_item jsonb; v_alias_item jsonb; v_assignment_item jsonb;
    v_allocation_item jsonb; v_allocation_part jsonb;
    v_account uuid; v_evidence uuid; v_alias uuid; v_assignment uuid;
    v_business_unit uuid; v_allocation_set uuid; v_fact uuid;
    v_effective_from date; v_effective_to date; v_basis_points integer;
    v_total integer; v_allocation_revision integer; v_audit uuid;
    v_result jsonb; v_account_refs uuid[] := ARRAY[]::uuid[];
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR (p_request->>'contract_version')
            IS DISTINCT FROM 'ledgerbridge.account-registry.v1'
       OR jsonb_typeof(p_request->'accounts') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_request->'business_unit_assignments') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_request->'fact_allocations') IS DISTINCT FROM 'array'
       OR (
            jsonb_array_length(p_request->'accounts')
            + jsonb_array_length(p_request->'business_unit_assignments')
            + jsonb_array_length(p_request->'fact_allocations')
       ) NOT BETWEEN 1 AND 1000
       OR coalesce(p_request->>'expected_owner_kind','') NOT IN ('PERSON','COMPANY')
       OR coalesce(p_request->>'expected_registry_revision','') !~ '^[0-9]{1,10}$'
       OR coalesce(p_request->>'policy_generation','') !~ '^[0-9]{1,10}$'
       OR btrim(coalesce(p_request->>'actor_ref','')) = ''
       OR length(p_request->>'actor_ref') > 200
       OR btrim(coalesce(p_request->>'workload_principal_ref','')) = ''
       OR length(p_request->>'workload_principal_ref') > 200
       OR btrim(coalesce(p_request->>'reason','')) = ''
       OR length(p_request->>'reason') > 1000 THEN
        RAISE EXCEPTION 'account registry plan is invalid' USING ERRCODE = '22023';
    END IF;
    BEGIN
        v_operation_id := (p_request->>'operation_id')::uuid;
        v_owner := (p_request->>'owner_entity_ref')::uuid;
        v_expected_revision := (p_request->>'expected_registry_revision')::integer;
        v_policy_generation := (p_request->>'policy_generation')::integer;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'account registry plan is invalid' USING ERRCODE = '22023';
    END;
    IF v_operation_id IS NULL OR v_owner IS NULL OR v_expected_revision < 0
       OR v_policy_generation < 1 THEN
        RAISE EXCEPTION 'account registry plan is invalid' USING ERRCODE = '22023';
    END IF;
    v_request_digest := public.digest(convert_to(p_request::text, 'UTF8'), 'sha256');
    PERFORM pg_advisory_xact_lock(
        hashtextextended('account-registry-operation:' || v_operation_id::text, 0)
    );
    SELECT * INTO v_operation FROM public.account_registry_operation
     WHERE operation_id = v_operation_id;
    IF FOUND THEN
        IF v_operation.request_sha256 IS DISTINCT FROM v_request_digest THEN
            RAISE EXCEPTION 'account registry operation replay conflicts with plan'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN v_operation.result || jsonb_build_object('created', false);
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('account-registry:' || v_owner::text, 0));
    SELECT entity_type::text INTO v_entity_type FROM public.entity WHERE id = v_owner;
    IF v_entity_type IS NULL THEN
        RAISE EXCEPTION 'accounting owner does not exist'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF v_entity_type IS DISTINCT FROM p_request->>'expected_owner_kind' THEN
        RAISE EXCEPTION 'accounting owner kind does not match Entity'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    v_owner_kind := CASE v_entity_type
        WHEN 'PERSON' THEN 'PERSONAL'
        WHEN 'COMPANY' THEN 'COMPANY'
    END;
    SELECT coalesce(max(registry_revision), 0) INTO v_current_revision
      FROM public.account_registry_operation WHERE owner_entity_id = v_owner;
    IF v_current_revision IS DISTINCT FROM v_expected_revision THEN
        RAISE EXCEPTION 'account registry revision is stale'
            USING ERRCODE = 'serialization_failure';
    END IF;

    FOR v_account_item IN SELECT value FROM jsonb_array_elements(p_request->'accounts') LOOP
        IF jsonb_typeof(v_account_item) IS DISTINCT FROM 'object'
           OR jsonb_typeof(v_account_item->'aliases') IS DISTINCT FROM 'array'
           OR jsonb_array_length(v_account_item->'aliases') NOT BETWEEN 1 AND 100
           OR coalesce(v_account_item->>'account_key','')
                !~ '^[a-z0-9][a-z0-9._:-]{0,199}$'
           OR coalesce(v_account_item->>'institution_code','')
                !~ '^[a-z0-9][a-z0-9_]{0,31}$'
           OR coalesce(v_account_item->>'account_suffix','') !~ '^[0-9]{4,8}$'
           OR coalesce(v_account_item->>'account_kind','') !~ '^[A-Z][A-Z0-9_]{0,31}$' THEN
            RAISE EXCEPTION 'managed account registration is invalid' USING ERRCODE = '22023';
        END IF;
        BEGIN
            v_account := (v_account_item->>'managed_account_ref')::uuid;
            v_evidence := (v_account_item->>'admission_evidence_ref')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'managed account registration is invalid' USING ERRCODE = '22023';
        END;
        IF v_account IS NULL OR v_evidence IS NULL OR v_account = ANY(v_account_refs) THEN
            RAISE EXCEPTION 'managed account registration is invalid' USING ERRCODE = '22023';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM public.evidence_object
             WHERE evidence_ref = v_evidence AND entity_id = v_owner
        ) THEN
            RAISE EXCEPTION 'managed account admission evidence is not owner-scoped'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT * INTO v_account_row FROM public.managed_account
         WHERE managed_account_ref = v_account
            OR (entity_id = v_owner AND account_key = v_account_item->>'account_key');
        IF FOUND THEN
            IF v_account_row.managed_account_ref IS DISTINCT FROM v_account
               OR v_account_row.entity_id IS DISTINCT FROM v_owner
               OR v_account_row.admission_evidence_ref IS DISTINCT FROM v_evidence
               OR v_account_row.account_key IS DISTINCT FROM v_account_item->>'account_key'
               OR v_account_row.institution_code
                    IS DISTINCT FROM v_account_item->>'institution_code'
               OR v_account_row.account_suffix
                    IS DISTINCT FROM v_account_item->>'account_suffix'
               OR v_account_row.account_kind IS DISTINCT FROM v_account_item->>'account_kind'
               OR v_account_row.owner_ref IS DISTINCT FROM v_owner::text
               OR v_account_row.owner_kind IS DISTINCT FROM v_owner_kind THEN
                RAISE EXCEPTION 'managed account conflicts with registered identity'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        ELSE
            v_audit := public.append_audit_event(
                p_request->>'actor_ref', 'account_registry.account.register',
                p_request->>'reason', 'ledgerbridge.account-registry.v1',
                jsonb_build_object(
                    'managed_account_ref', v_account,
                    'owner_entity_ref', v_owner,
                    'owner_kind', v_entity_type,
                    'admission_evidence_ref', v_evidence,
                    'account_key', v_account_item->>'account_key',
                    'institution_code', v_account_item->>'institution_code',
                    'account_suffix', v_account_item->>'account_suffix',
                    'account_kind', v_account_item->>'account_kind'
                )
            );
            INSERT INTO public.managed_account(
                managed_account_ref, entity_id, account_key, institution_code,
                account_suffix, owner_ref, owner_kind, account_kind,
                admission_evidence_ref, audit_event_id, created_at
            ) VALUES (
                v_account, v_owner, v_account_item->>'account_key',
                v_account_item->>'institution_code', v_account_item->>'account_suffix',
                v_owner::text, v_owner_kind, v_account_item->>'account_kind',
                v_evidence, v_audit,
                (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
            );
            v_audit := public.append_audit_event(
                p_request->>'actor_ref', 'managed_account.lifecycle',
                p_request->>'reason', 'ledgerbridge.bank-statement.v1',
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
        FOR v_alias_item IN SELECT value
          FROM jsonb_array_elements(v_account_item->'aliases') LOOP
            IF jsonb_typeof(v_alias_item) IS DISTINCT FROM 'object'
               OR coalesce(v_alias_item->>'alias_kind','') !~ '^[A-Z][A-Z0-9_]{0,31}$'
               OR btrim(coalesce(v_alias_item->>'alias_value','')) = ''
               OR length(v_alias_item->>'alias_value') > 300 THEN
                RAISE EXCEPTION 'managed account alias is invalid' USING ERRCODE = '22023';
            END IF;
            BEGIN
                v_alias := (v_alias_item->>'alias_ref')::uuid;
            EXCEPTION WHEN invalid_text_representation THEN
                RAISE EXCEPTION 'managed account alias is invalid' USING ERRCODE = '22023';
            END;
            SELECT * INTO v_alias_row FROM public.managed_account_alias
             WHERE alias_ref = v_alias OR (
                institution_code = v_account_item->>'institution_code'
                AND alias_kind = v_alias_item->>'alias_kind'
                AND normalized_value = public.account_registry_normalize_alias(
                    v_alias_item->>'alias_value'
                )
             );
            IF FOUND THEN
                IF v_alias_row.alias_ref IS DISTINCT FROM v_alias
                   OR v_alias_row.managed_account_ref IS DISTINCT FROM v_account
                   OR v_alias_row.owner_entity_id IS DISTINCT FROM v_owner THEN
                    RAISE EXCEPTION 'account registry alias already belongs to another account'
                        USING ERRCODE = 'unique_violation';
                END IF;
            ELSE
                v_audit := public.append_audit_event(
                    p_request->>'actor_ref', 'account_registry.alias.register',
                    p_request->>'reason', 'ledgerbridge.account-registry.v1',
                    jsonb_build_object(
                        'alias_ref', v_alias, 'managed_account_ref', v_account,
                        'owner_entity_ref', v_owner,
                        'alias_kind', v_alias_item->>'alias_kind',
                        'normalized_value', public.account_registry_normalize_alias(
                            v_alias_item->>'alias_value'
                        )
                    )
                );
                INSERT INTO public.managed_account_alias(
                    alias_ref, owner_entity_id, managed_account_ref, institution_code,
                    alias_kind, alias_value, normalized_value, audit_event_id, created_at
                ) VALUES (
                    v_alias, v_owner, v_account, v_account_item->>'institution_code',
                    v_alias_item->>'alias_kind', v_alias_item->>'alias_value',
                    public.account_registry_normalize_alias(v_alias_item->>'alias_value'),
                    v_audit, (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
                );
            END IF;
        END LOOP;
        v_account_refs := array_append(v_account_refs, v_account);
    END LOOP;

    FOR v_assignment_item IN
      SELECT value FROM jsonb_array_elements(p_request->'business_unit_assignments') LOOP
        IF jsonb_typeof(v_assignment_item) IS DISTINCT FROM 'object'
           OR coalesce(v_assignment_item->>'effective_from','')
                !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
           OR (
                nullif(v_assignment_item->>'effective_to','') IS NOT NULL
                AND v_assignment_item->>'effective_to'
                    !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
           ) THEN
            RAISE EXCEPTION 'account business-unit assignment is invalid'
                USING ERRCODE = '22023';
        END IF;
        BEGIN
            v_assignment := (v_assignment_item->>'assignment_ref')::uuid;
            v_account := (v_assignment_item->>'managed_account_ref')::uuid;
            v_business_unit := (v_assignment_item->>'business_unit_id')::uuid;
            v_effective_from := (v_assignment_item->>'effective_from')::date;
            v_effective_to := nullif(v_assignment_item->>'effective_to','')::date;
        EXCEPTION WHEN invalid_text_representation OR invalid_datetime_format
            OR datetime_field_overflow THEN
            RAISE EXCEPTION 'account business-unit assignment is invalid'
                USING ERRCODE = '22023';
        END;
        IF v_effective_to IS NOT NULL AND v_effective_to <= v_effective_from THEN
            RAISE EXCEPTION 'account business-unit assignment is invalid'
                USING ERRCODE = '22023';
        END IF;
        IF btrim(coalesce(v_assignment_item->>'business_unit_ref_snapshot','')) = ''
           OR length(v_assignment_item->>'business_unit_ref_snapshot') > 100
           OR btrim(coalesce(v_assignment_item->>'business_unit_label_snapshot','')) = ''
           OR length(v_assignment_item->>'business_unit_label_snapshot') > 200
           OR NOT EXISTS (
            SELECT 1 FROM public.managed_account
             WHERE managed_account_ref = v_account AND entity_id = v_owner
        ) OR NOT EXISTS (
            SELECT 1 FROM public.business_unit
             WHERE id = v_business_unit AND entity_id = v_owner
               AND ref = v_assignment_item->>'business_unit_ref_snapshot'
               AND label = v_assignment_item->>'business_unit_label_snapshot'
        ) THEN
            RAISE EXCEPTION 'account business-unit assignment crosses owner scope'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.account_business_unit_assignment
             WHERE assignment_ref = v_assignment
        ) THEN
            RAISE EXCEPTION 'account business-unit assignment ref already exists'
                USING ERRCODE = 'unique_violation';
        END IF;
        v_audit := public.append_audit_event(
            p_request->>'actor_ref', 'account_registry.business_unit.assign',
            p_request->>'reason', 'ledgerbridge.account-registry.v1',
            jsonb_build_object(
                'assignment_ref', v_assignment, 'managed_account_ref', v_account,
                'business_unit_id', v_business_unit,
                'business_unit_ref_snapshot',
                    v_assignment_item->>'business_unit_ref_snapshot',
                'business_unit_label_snapshot',
                    v_assignment_item->>'business_unit_label_snapshot',
                'effective_from', v_effective_from,
                'effective_to', coalesce(v_effective_to::text, '')
            )
        );
        INSERT INTO public.account_business_unit_assignment(
            assignment_ref, owner_entity_id, managed_account_ref, business_unit_id,
            business_unit_ref_snapshot, business_unit_label_snapshot,
            effective_from, effective_to, audit_event_id, created_at
        ) VALUES (
            v_assignment, v_owner, v_account, v_business_unit,
            v_assignment_item->>'business_unit_ref_snapshot',
            v_assignment_item->>'business_unit_label_snapshot',
            v_effective_from, v_effective_to, v_audit,
            (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
        );
        IF NOT v_account = ANY(v_account_refs) THEN
            v_account_refs := array_append(v_account_refs, v_account);
        END IF;
    END LOOP;

    FOR v_allocation_item IN
      SELECT value FROM jsonb_array_elements(p_request->'fact_allocations') LOOP
        IF jsonb_typeof(v_allocation_item) IS DISTINCT FROM 'object'
           OR jsonb_typeof(v_allocation_item->'items') IS DISTINCT FROM 'array'
           OR jsonb_array_length(v_allocation_item->'items') NOT BETWEEN 1 AND 100 THEN
            RAISE EXCEPTION 'fact allocation is invalid' USING ERRCODE = '22023';
        END IF;
        BEGIN
            v_allocation_set := (v_allocation_item->>'allocation_set_ref')::uuid;
            v_account := (v_allocation_item->>'managed_account_ref')::uuid;
            v_fact := (v_allocation_item->>'fact_ref')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION 'fact allocation is invalid' USING ERRCODE = '22023';
        END;
        IF NOT EXISTS (
            SELECT 1 FROM public.managed_account
             WHERE managed_account_ref = v_account AND entity_id = v_owner
        ) OR NOT EXISTS (
            SELECT 1 FROM public.bank_statement_transaction
             WHERE transaction_ref = v_fact AND managed_account_ref = v_account
        ) THEN
            RAISE EXCEPTION 'fact allocation crosses account owner scope'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.fact_business_unit_allocation_set
             WHERE allocation_set_ref = v_allocation_set
        ) THEN
            RAISE EXCEPTION 'fact allocation set ref already exists'
                USING ERRCODE = 'unique_violation';
        END IF;
        v_total := 0;
        FOR v_allocation_part IN
          SELECT value FROM jsonb_array_elements(v_allocation_item->'items') LOOP
            IF jsonb_typeof(v_allocation_part) IS DISTINCT FROM 'object'
               OR coalesce(v_allocation_part->>'basis_points','') !~ '^[0-9]{1,5}$' THEN
                RAISE EXCEPTION 'fact allocation is invalid' USING ERRCODE = '22023';
            END IF;
            BEGIN
                v_business_unit := (v_allocation_part->>'business_unit_id')::uuid;
                v_basis_points := (v_allocation_part->>'basis_points')::integer;
            EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                RAISE EXCEPTION 'fact allocation is invalid' USING ERRCODE = '22023';
            END;
            IF v_basis_points NOT BETWEEN 1 AND 10000
               OR btrim(coalesce(v_allocation_part->>'business_unit_ref_snapshot','')) = ''
               OR length(v_allocation_part->>'business_unit_ref_snapshot') > 100
               OR btrim(coalesce(v_allocation_part->>'business_unit_label_snapshot','')) = ''
               OR length(v_allocation_part->>'business_unit_label_snapshot') > 200
               OR NOT EXISTS (
                SELECT 1 FROM public.business_unit
                 WHERE id = v_business_unit AND entity_id = v_owner
                   AND ref = v_allocation_part->>'business_unit_ref_snapshot'
                   AND label = v_allocation_part->>'business_unit_label_snapshot'
            ) THEN
                RAISE EXCEPTION 'fact allocation business unit is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            v_total := v_total + v_basis_points;
        END LOOP;
        IF v_total <> 10000 THEN
            RAISE EXCEPTION 'fact allocation must total 10000 basis points'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT coalesce(max(revision), 0) + 1 INTO v_allocation_revision
          FROM public.fact_business_unit_allocation_set WHERE fact_ref = v_fact;
        v_audit := public.append_audit_event(
            p_request->>'actor_ref', 'account_registry.fact.allocate',
            p_request->>'reason', 'ledgerbridge.account-registry.v1',
            jsonb_build_object(
                'allocation_set_ref', v_allocation_set,
                'managed_account_ref', v_account, 'fact_ref', v_fact,
                'revision', v_allocation_revision
            )
        );
        INSERT INTO public.fact_business_unit_allocation_set(
            allocation_set_ref, owner_entity_id, managed_account_ref, fact_ref,
            revision, audit_event_id, created_at
        ) VALUES (
            v_allocation_set, v_owner, v_account, v_fact, v_allocation_revision,
            v_audit, (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
        );
        FOR v_allocation_part IN
          SELECT value FROM jsonb_array_elements(v_allocation_item->'items') LOOP
            INSERT INTO public.fact_business_unit_allocation_item(
                allocation_set_ref, owner_entity_id, business_unit_id,
                business_unit_ref_snapshot, business_unit_label_snapshot, basis_points
            ) VALUES (
                v_allocation_set, v_owner,
                (v_allocation_part->>'business_unit_id')::uuid,
                v_allocation_part->>'business_unit_ref_snapshot',
                v_allocation_part->>'business_unit_label_snapshot',
                (v_allocation_part->>'basis_points')::integer
            );
        END LOOP;
        IF NOT v_account = ANY(v_account_refs) THEN
            v_account_refs := array_append(v_account_refs, v_account);
        END IF;
    END LOOP;

    v_revision := v_current_revision + 1;
    v_result := jsonb_build_object(
        'contract_version', 'ledgerbridge.account-registry.v1',
        'operation_id', v_operation_id, 'owner_entity_ref', v_owner,
        'registry_revision', v_revision, 'created', true,
        'managed_account_refs', to_jsonb(v_account_refs)
    );
    v_audit := public.append_audit_event(
        p_request->>'actor_ref', 'account_registry.plan.apply', p_request->>'reason',
        'ledgerbridge.account-registry.v1',
        jsonb_build_object(
            'operation_id', v_operation_id, 'owner_entity_ref', v_owner,
            'registry_revision', v_revision,
            'request_sha256', encode(v_request_digest, 'hex'),
            'workload_principal_ref', p_request->>'workload_principal_ref',
            'policy_generation', v_policy_generation
        )
    );
    INSERT INTO public.account_registry_operation(
        operation_id, owner_entity_id, registry_revision, request_sha256, result,
        workload_principal_ref, policy_generation, audit_event_id, created_at
    ) VALUES (
        v_operation_id, v_owner, v_revision, v_request_digest, v_result,
        p_request->>'workload_principal_ref', v_policy_generation, v_audit,
        (SELECT occurred_at FROM public.audit_event WHERE id = v_audit)
    );
    RETURN v_result;
END
$function$;

CREATE FUNCTION internal_read.get_account_registry_projection(
    p_owner_entity_ref uuid, p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE v_owner_kind text; v_revision integer; v_accounts jsonb;
BEGIN
    IF p_owner_entity_ref IS NULL OR p_audit_horizon_sequence IS NULL
       OR p_audit_horizon_hash IS NULL OR octet_length(p_audit_horizon_hash) <> 32
       OR NOT EXISTS (
            SELECT 1 FROM public.audit_event
             WHERE sequence = p_audit_horizon_sequence AND hash = p_audit_horizon_hash
       ) THEN
        RAISE EXCEPTION 'account registry projection horizon is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT entity_type::text INTO v_owner_kind
      FROM public.entity WHERE id = p_owner_entity_ref;
    IF v_owner_kind IS NULL THEN
        RAISE EXCEPTION 'accounting owner does not exist'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT coalesce(max(operation.registry_revision), 0) INTO v_revision
      FROM public.account_registry_operation AS operation
      JOIN public.audit_event AS event ON event.id = operation.audit_event_id
     WHERE operation.owner_entity_id = p_owner_entity_ref
       AND event.sequence <= p_audit_horizon_sequence;
    SELECT coalesce(jsonb_agg(
        jsonb_build_object(
            'managed_account_ref', account.managed_account_ref,
            'admission_evidence_ref', account.admission_evidence_ref,
            'account_key', account.account_key,
            'institution_code', account.institution_code,
            'account_suffix', account.account_suffix,
            'account_kind', account.account_kind,
            'aliases', coalesce((
                SELECT jsonb_agg(jsonb_build_object(
                    'alias_ref', alias.alias_ref, 'alias_kind', alias.alias_kind,
                    'masked_value', CASE
                        WHEN length(alias.alias_value) <= 4
                            THEN repeat('*', length(alias.alias_value))
                        ELSE repeat('*', length(alias.alias_value) - 4)
                            || right(alias.alias_value, 4)
                    END
                ) ORDER BY alias.alias_kind, alias.alias_ref)
                  FROM public.managed_account_alias AS alias
                  JOIN public.audit_event AS alias_audit
                    ON alias_audit.id = alias.audit_event_id
                 WHERE alias.managed_account_ref = account.managed_account_ref
                   AND alias_audit.sequence <= p_audit_horizon_sequence
            ), '[]'::jsonb),
            'business_unit_assignments', coalesce((
                SELECT jsonb_agg(jsonb_build_object(
                    'assignment_ref', assignment.assignment_ref,
                    'business_unit_id', assignment.business_unit_id,
                    'business_unit_ref_snapshot', assignment.business_unit_ref_snapshot,
                    'business_unit_label_snapshot', assignment.business_unit_label_snapshot,
                    'effective_from', assignment.effective_from,
                    'effective_to', assignment.effective_to
                ) ORDER BY assignment.effective_from, assignment.assignment_ref)
                  FROM public.account_business_unit_assignment AS assignment
                  JOIN public.audit_event AS assignment_audit
                    ON assignment_audit.id = assignment.audit_event_id
                 WHERE assignment.managed_account_ref = account.managed_account_ref
                   AND assignment_audit.sequence <= p_audit_horizon_sequence
            ), '[]'::jsonb),
            'fact_allocations', coalesce((
                SELECT jsonb_agg(jsonb_build_object(
                    'allocation_set_ref', allocation.allocation_set_ref,
                    'fact_ref', allocation.fact_ref,
                    'revision', allocation.revision,
                    'items', (
                        SELECT jsonb_agg(jsonb_build_object(
                            'business_unit_id', item.business_unit_id,
                            'business_unit_ref_snapshot', item.business_unit_ref_snapshot,
                            'business_unit_label_snapshot', item.business_unit_label_snapshot,
                            'basis_points', item.basis_points
                        ) ORDER BY item.business_unit_id)
                          FROM public.fact_business_unit_allocation_item AS item
                         WHERE item.allocation_set_ref = allocation.allocation_set_ref
                    )
                ) ORDER BY allocation.fact_ref)
                  FROM public.fact_business_unit_allocation_set AS allocation
                  JOIN public.audit_event AS allocation_audit
                    ON allocation_audit.id = allocation.audit_event_id
                 WHERE allocation.managed_account_ref = account.managed_account_ref
                   AND allocation_audit.sequence <= p_audit_horizon_sequence
                   AND NOT EXISTS (
                        SELECT 1
                          FROM public.fact_business_unit_allocation_set AS newer
                          JOIN public.audit_event AS newer_audit
                            ON newer_audit.id = newer.audit_event_id
                         WHERE newer.fact_ref = allocation.fact_ref
                           AND newer.revision > allocation.revision
                           AND newer_audit.sequence <= p_audit_horizon_sequence
                   )
            ), '[]'::jsonb)
        ) ORDER BY account.account_key), '[]'::jsonb)
      INTO v_accounts
      FROM public.managed_account AS account
      JOIN public.audit_event AS account_audit ON account_audit.id = account.audit_event_id
     WHERE account.entity_id = p_owner_entity_ref
       AND account_audit.sequence <= p_audit_horizon_sequence;
    RETURN jsonb_build_object(
        'contract_version', 'ledgerbridge.account-registry.v1',
        'owner_entity_ref', p_owner_entity_ref, 'owner_kind', v_owner_kind,
        'registry_revision', v_revision, 'accounts', v_accounts
    );
END
$function$;

REVOKE ALL ON TABLE public.account_registry_operation,
    public.managed_account_alias, public.account_business_unit_assignment,
    public.fact_business_unit_allocation_set,
    public.fact_business_unit_allocation_item
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
REVOKE ALL ON FUNCTION public.account_registry_normalize_alias(text),
    public.account_registry_append_only(),
    public.account_registry_validate_managed_account(),
    public.account_registry_validate_fact(),
    public.account_registry_validate_business_unit_snapshot(),
    public.account_registry_reject_assignment_overlap(),
    public.account_registry_validate_allocation_revision(),
    public.account_registry_require_allocation_total(),
    internal_import.import_bank_statement(jsonb),
    internal_command.apply_account_registry_plan(jsonb),
    internal_read.get_account_registry_projection(uuid,bigint,bytea)
    FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
GRANT USAGE ON SCHEMA internal_command TO ledgerbridge_api;
GRANT EXECUTE ON FUNCTION internal_command.apply_account_registry_plan(jsonb)
    TO ledgerbridge_api;
GRANT USAGE ON SCHEMA internal_read TO ledgerbridge_reader;
GRANT EXECUTE ON FUNCTION internal_read.get_account_registry_projection(uuid,bigint,bytea)
    TO ledgerbridge_reader;
GRANT USAGE ON SCHEMA internal_import TO ledgerbridge_worker;
GRANT EXECUTE ON FUNCTION internal_import.import_bank_statement(jsonb)
    TO ledgerbridge_worker;
"""


def downgrade() -> None:
    if os.getenv("LEDGERBRIDGE_ENV", "development").lower() == "production":
        raise RuntimeError("account owner registry is irreversible in production")
    connection = op.get_bind()
    if connection.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM public.account_registry_operation) "
            "OR EXISTS (SELECT 1 FROM public.managed_account)"
        )
    ).scalar_one():
        raise RuntimeError("development downgrade would discard account registry facts")
    op.execute(_DOWNGRADE_SQL)


_DOWNGRADE_SQL = r"""
REVOKE ALL ON FUNCTION internal_command.apply_account_registry_plan(jsonb)
    FROM ledgerbridge_api;
REVOKE ALL ON FUNCTION internal_read.get_account_registry_projection(uuid,bigint,bytea)
    FROM ledgerbridge_reader;
REVOKE ALL ON FUNCTION internal_import.import_bank_statement(jsonb)
    FROM ledgerbridge_worker;
DROP FUNCTION internal_read.get_account_registry_projection(uuid,bigint,bytea);
DROP FUNCTION internal_command.apply_account_registry_plan(jsonb);
DROP FUNCTION internal_import.import_bank_statement(jsonb);
ALTER FUNCTION internal_import.import_bank_statement_0021(jsonb)
    RENAME TO import_bank_statement;
GRANT EXECUTE ON FUNCTION internal_import.import_bank_statement(jsonb)
    TO ledgerbridge_worker;

DROP TABLE public.fact_business_unit_allocation_item;
DROP TABLE public.fact_business_unit_allocation_set;
DROP TABLE public.account_business_unit_assignment;
DROP TABLE public.managed_account_alias;
DROP TABLE public.account_registry_operation;
DROP FUNCTION public.account_registry_require_allocation_total();
DROP FUNCTION public.account_registry_validate_allocation_revision();
DROP FUNCTION public.account_registry_reject_assignment_overlap();
DROP FUNCTION public.account_registry_validate_business_unit_snapshot();
DROP FUNCTION public.account_registry_validate_fact();
DROP TRIGGER validate_managed_account_registry ON public.managed_account;
DROP FUNCTION public.account_registry_validate_managed_account();
DROP FUNCTION public.account_registry_append_only();
DROP FUNCTION public.account_registry_normalize_alias(text);

ALTER TABLE public.managed_account
    DROP CONSTRAINT managed_account_owner_ref_is_entity;
ALTER TABLE public.managed_account
    DROP CONSTRAINT managed_account_institution_code_format;
ALTER TABLE public.managed_account
    ADD CONSTRAINT managed_account_institution_code_check
        CHECK (institution_code = 'mybank');
ALTER TABLE public.managed_account
    DROP CONSTRAINT uq_managed_account_ref_entity;
ALTER TABLE public.managed_account
    DROP CONSTRAINT fk_managed_account_admission_evidence;
ALTER TABLE public.managed_account DROP COLUMN admission_evidence_ref;
ALTER TABLE public.evidence_object DROP CONSTRAINT uq_evidence_object_entity_ref;

CREATE TRIGGER validate_managed_account_audit
BEFORE INSERT ON public.managed_account
FOR EACH ROW EXECUTE FUNCTION public.r1_validate_bank_statement();
CREATE CONSTRAINT TRIGGER require_statement_backed_account
AFTER INSERT ON public.managed_account
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.r1_require_statement_backed_account();
"""
