# ruff: noqa: E501

"""Add the owner-only controlled-import receipt and production SAN parity."""

from collections.abc import Sequence

from alembic import op

revision: str = "20260828_0017"
down_revision: str | None = "20260828_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        DO $preflight$
        DECLARE v_owner oid;
        BEGIN
            SELECT datdba INTO v_owner
              FROM pg_database WHERE datname = current_database();
            IF v_owner IS DISTINCT FROM (SELECT oid FROM pg_roles WHERE rolname = current_user)
               OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_api')
               OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_reader')
               OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_worker') THEN
                RAISE EXCEPTION 'controlled import migration requires fixed owner/runtime roles'
                    USING ERRCODE = 'invalid_authorization_specification';
            END IF;
        END
        $preflight$;

        DO $schema$
        BEGIN
            EXECUTE format('CREATE SCHEMA internal_import AUTHORIZATION %I', current_user);
        END
        $schema$;

        CREATE TABLE internal_import.controlled_batch_receipt (
            batch_ref uuid PRIMARY KEY,
            source_manifest_sha256 bytea NOT NULL,
            prepared_manifest_sha256 bytea NOT NULL,
            entity_id uuid NOT NULL REFERENCES public.entity(id) ON DELETE RESTRICT,
            business_unit_id uuid NOT NULL REFERENCES public.business_unit(id) ON DELETE RESTRICT,
            evidence_count integer NOT NULL,
            candidate_count integer NOT NULL,
            audit_horizon_sequence bigint NOT NULL,
            audit_horizon_hash bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT controlled_batch_receipt_source_sha_length
                CHECK (octet_length(source_manifest_sha256) = 32),
            CONSTRAINT controlled_batch_receipt_prepared_sha_length
                CHECK (octet_length(prepared_manifest_sha256) = 32),
            CONSTRAINT controlled_batch_receipt_counts_positive
                CHECK (evidence_count > 0 AND candidate_count > 0),
            CONSTRAINT controlled_batch_receipt_horizon_positive
                CHECK (audit_horizon_sequence > 0),
            CONSTRAINT controlled_batch_receipt_horizon_hash_length
                CHECK (octet_length(audit_horizon_hash) = 32),
            CONSTRAINT controlled_batch_receipt_scope
                FOREIGN KEY (entity_id, business_unit_id)
                REFERENCES public.business_unit(entity_id, id) ON DELETE RESTRICT
        );

        CREATE FUNCTION internal_import.reject_mutation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION 'controlled import receipts are append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;

        CREATE FUNCTION internal_import.validate_receipt_horizon()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.audit_event AS a
                 WHERE a.sequence = NEW.audit_horizon_sequence
                   AND a.hash = NEW.audit_horizon_hash
            ) THEN
                RAISE EXCEPTION 'controlled import receipt horizon is not an exact audit row'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE TRIGGER controlled_batch_receipt_append_only
            BEFORE UPDATE OR DELETE ON internal_import.controlled_batch_receipt
            FOR EACH ROW EXECUTE FUNCTION internal_import.reject_mutation();
        CREATE TRIGGER controlled_batch_receipt_horizon
            AFTER INSERT ON internal_import.controlled_batch_receipt
            FOR EACH ROW EXECUTE FUNCTION internal_import.validate_receipt_horizon();

        DO $san_parity$
        DECLARE
            v_definition text;
            v_updated text;
        BEGIN
            SELECT pg_get_functiondef(p.oid) INTO STRICT v_definition
              FROM pg_proc AS p
              JOIN pg_namespace AS n ON n.oid = p.pronamespace
             WHERE n.nspname = 'internal_read'
               AND p.proname = 'append_internal_evidence_read_audit'
               AND pg_get_function_identity_arguments(p.oid) =
                   'p_operation_id uuid, p_principal_ref character varying, p_verified_san character varying, p_policy_generation character varying, p_evidence_ref uuid, p_entity_id uuid, p_business_unit_id uuid, p_blob_ref uuid, p_byte_size bigint, p_plaintext_sha256 bytea';
            v_updated := replace(
                v_definition,
                $old$p_verified_san !~ '^spiffe://ledgerbridge(\.test)?/[a-z0-9/_-]+$'$old$,
                $new$p_verified_san !~ '^spiffe://ledgerbridge(\.test|\.local)?/[a-z0-9/_-]+$'$new$
            );
            IF v_updated = v_definition THEN
                RAISE EXCEPTION 'evidence read SAN policy definition did not match expected 0015 body'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            EXECUTE v_updated;
        END
        $san_parity$;

        REVOKE ALL ON SCHEMA internal_import FROM PUBLIC;
        REVOKE ALL ON ALL TABLES IN SCHEMA internal_import
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
        REVOKE ALL ON ALL FUNCTIONS IN SCHEMA internal_import
            FROM PUBLIC, ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker;
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        DO $guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM internal_import.controlled_batch_receipt) THEN
                RAISE EXCEPTION 'controlled import receipts prevent destructive downgrade';
            END IF;
        END
        $guard$;

        DO $san_parity$
        DECLARE
            v_definition text;
            v_updated text;
        BEGIN
            SELECT pg_get_functiondef(p.oid) INTO STRICT v_definition
              FROM pg_proc AS p
              JOIN pg_namespace AS n ON n.oid = p.pronamespace
             WHERE n.nspname = 'internal_read'
               AND p.proname = 'append_internal_evidence_read_audit'
               AND pg_get_function_identity_arguments(p.oid) =
                   'p_operation_id uuid, p_principal_ref character varying, p_verified_san character varying, p_policy_generation character varying, p_evidence_ref uuid, p_entity_id uuid, p_business_unit_id uuid, p_blob_ref uuid, p_byte_size bigint, p_plaintext_sha256 bytea';
            v_updated := replace(
                v_definition,
                $old$p_verified_san !~ '^spiffe://ledgerbridge(\.test|\.local)?/[a-z0-9/_-]+$'$old$,
                $new$p_verified_san !~ '^spiffe://ledgerbridge(\.test)?/[a-z0-9/_-]+$'$new$
            );
            IF v_updated = v_definition THEN
                RAISE EXCEPTION 'evidence read SAN policy definition did not match expected 0017 body'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            EXECUTE v_updated;
        END
        $san_parity$;

        DROP SCHEMA internal_import CASCADE;
        """
    )
