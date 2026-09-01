"""Make the shared account-registry fact trigger record-safe.

Revision ID: 20260901_0027
Revises: 20260831_0026

The trigger is shared by four tables with different row types. PostgreSQL
cannot safely resolve direct ``NEW.<column>`` references for columns that are
absent from the row type currently invoking the function, even when the
reference appears in a different ``TG_TABLE_NAME`` branch. Converting the row
to JSONB keeps the existing audit bindings while making field lookup specific
to the active table record.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260901_0027"
down_revision: str | None = "20260831_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION public.account_registry_validate_fact()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_audit public.audit_event%ROWTYPE;
            v_action text;
            v_new jsonb;
        BEGIN
            v_new := to_jsonb(NEW);
            SELECT * INTO v_audit
              FROM public.audit_event
             WHERE id = (v_new->>'audit_event_id')::uuid;
            v_action := CASE TG_TABLE_NAME
                WHEN 'managed_account_alias' THEN 'account_registry.alias.register'
                WHEN 'account_business_unit_assignment' THEN 'account_registry.business_unit.assign'
                WHEN 'fact_business_unit_allocation_set' THEN 'account_registry.fact.allocate'
                WHEN 'account_registry_operation' THEN 'account_registry.plan.apply'
            END;
            IF v_audit.action IS DISTINCT FROM v_action
               OR v_audit.rule_version IS DISTINCT FROM 'ledgerbridge.account-registry.v1'
               OR (v_new->>'created_at')::timestamptz IS DISTINCT FROM v_audit.occurred_at THEN
                RAISE EXCEPTION 'account registry audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_TABLE_NAME = 'managed_account_alias' AND (
                v_audit.payload->>'alias_ref' IS DISTINCT FROM v_new->>'alias_ref'
                OR v_audit.payload->>'managed_account_ref'
                    IS DISTINCT FROM v_new->>'managed_account_ref'
                OR v_audit.payload->>'owner_entity_ref'
                    IS DISTINCT FROM v_new->>'owner_entity_id'
                OR v_audit.payload->>'alias_kind' IS DISTINCT FROM v_new->>'alias_kind'
                OR v_audit.payload->>'normalized_value'
                    IS DISTINCT FROM v_new->>'normalized_value'
            ) THEN
                RAISE EXCEPTION 'account registry alias audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            ELSIF TG_TABLE_NAME = 'account_business_unit_assignment' AND (
                v_audit.payload->>'assignment_ref' IS DISTINCT FROM v_new->>'assignment_ref'
                OR v_audit.payload->>'managed_account_ref'
                    IS DISTINCT FROM v_new->>'managed_account_ref'
                OR v_audit.payload->>'business_unit_id'
                    IS DISTINCT FROM v_new->>'business_unit_id'
                OR v_audit.payload->>'business_unit_ref_snapshot'
                    IS DISTINCT FROM v_new->>'business_unit_ref_snapshot'
                OR v_audit.payload->>'business_unit_label_snapshot'
                    IS DISTINCT FROM v_new->>'business_unit_label_snapshot'
                OR v_audit.payload->>'effective_from'
                    IS DISTINCT FROM v_new->>'effective_from'
                OR v_audit.payload->>'effective_to'
                    IS DISTINCT FROM coalesce(v_new->>'effective_to', '')
            ) THEN
                RAISE EXCEPTION 'account business-unit audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            ELSIF TG_TABLE_NAME = 'fact_business_unit_allocation_set' AND (
                v_audit.payload->>'allocation_set_ref'
                    IS DISTINCT FROM v_new->>'allocation_set_ref'
                OR v_audit.payload->>'managed_account_ref'
                    IS DISTINCT FROM v_new->>'managed_account_ref'
                OR v_audit.payload->>'fact_ref' IS DISTINCT FROM v_new->>'fact_ref'
                OR v_audit.payload->>'revision' IS DISTINCT FROM v_new->>'revision'
            ) THEN
                RAISE EXCEPTION 'fact allocation audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            ELSIF TG_TABLE_NAME = 'account_registry_operation' AND (
                v_audit.payload->>'operation_id' IS DISTINCT FROM v_new->>'operation_id'
                OR v_audit.payload->>'owner_entity_ref'
                    IS DISTINCT FROM v_new->>'owner_entity_id'
                OR v_audit.payload->>'registry_revision'
                    IS DISTINCT FROM v_new->>'registry_revision'
                OR v_audit.payload->>'request_sha256'
                    IS DISTINCT FROM encode((v_new->>'request_sha256')::bytea, 'hex')
                OR v_audit.payload->>'workload_principal_ref'
                    IS DISTINCT FROM v_new->>'workload_principal_ref'
                OR v_audit.payload->>'policy_generation'
                    IS DISTINCT FROM v_new->>'policy_generation'
            ) THEN
                RAISE EXCEPTION 'account registry operation audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        REVOKE ALL ON FUNCTION public.account_registry_validate_fact()
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker,
                 ledgerbridge_app;
        """
    )


def downgrade() -> None:
    raise RuntimeError("account registry trigger record-safety repair is forward-only")
