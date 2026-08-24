"""Bind dispatch rows to the exact acceptance audit event created with them."""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "20260823_0008"
down_revision: str | None = "20260823_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION public.evidence_import_dispatch_bind_acceptance()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_audit_xid xid;
            v_action text;
            v_payload jsonb;
            v_expected jsonb;
            v_artifact_source text;
        BEGIN
            SELECT e.xmin, e.action, e.payload
              INTO v_audit_xid, v_action, v_payload
              FROM public.audit_event AS e
             WHERE e.id = NEW.accepted_audit_event_id;

            SELECT a.source
              INTO v_artifact_source
              FROM public.raw_artifact AS a
             WHERE a.id = NEW.artifact_id;
            IF v_artifact_source IS DISTINCT FROM NEW.ingest_channel THEN
                RAISE EXCEPTION 'dispatch artifact and ingest channel provenance is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            v_expected := jsonb_build_object(
                'operation_id', NEW.id::text,
                'artifact_id', NEW.artifact_id::text,
                'ingest_channel', NEW.ingest_channel,
                'manifest_generation', NEW.manifest_generation,
                'manifest_digest', encode(NEW.manifest_digest, 'hex')
            );

            IF v_audit_xid IS NULL
               OR pg_xact_status(v_audit_xid::text::xid8)
                    IS DISTINCT FROM 'in progress'
               OR v_action IS DISTINCT FROM 'import.dispatch.accepted'
               OR v_payload IS DISTINCT FROM v_expected THEN
                RAISE EXCEPTION 'dispatch acceptance audit binding is invalid'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE TRIGGER evidence_import_dispatch_acceptance_binding
        BEFORE INSERT ON public.evidence_import_dispatch
        FOR EACH ROW EXECUTE FUNCTION public.evidence_import_dispatch_bind_acceptance();

        CREATE FUNCTION public.evidence_import_dispatch_enqueue(
            p_operation_id uuid,
            p_artifact_id uuid,
            p_ingest_channel text,
            p_manifest_generation text,
            p_manifest_digest bytea,
            p_actor text,
            p_reason text
        )
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_audit_id uuid;
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM public.raw_artifact AS a
                 WHERE a.id = p_artifact_id
                   AND a.source = p_ingest_channel
            ) THEN
                RAISE EXCEPTION 'dispatch artifact does not exist for ingest channel'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.ingest_channel AS c WHERE c.id = p_ingest_channel
            ) THEN
                RAISE EXCEPTION 'dispatch ingest channel is not registered'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF octet_length(p_manifest_digest) <> 32 THEN
                RAISE EXCEPTION 'dispatch manifest digest must contain 32 bytes'
                    USING ERRCODE = 'check_violation';
            END IF;

            v_audit_id := public.append_audit_event(
                p_actor,
                'import.dispatch.accepted',
                p_reason,
                NULL,
                jsonb_build_object(
                    'operation_id', p_operation_id::text,
                    'artifact_id', p_artifact_id::text,
                    'ingest_channel', p_ingest_channel,
                    'manifest_generation', p_manifest_generation,
                    'manifest_digest', encode(p_manifest_digest, 'hex')
                )
            );

            INSERT INTO public.evidence_import_dispatch (
                id,
                artifact_id,
                ingest_channel,
                accepted_audit_event_id,
                manifest_generation,
                manifest_digest,
                state
            ) VALUES (
                p_operation_id,
                p_artifact_id,
                p_ingest_channel,
                v_audit_id,
                p_manifest_generation,
                p_manifest_digest,
                'PENDING'::public.dispatch_state
            );
            RETURN p_operation_id;
        END
        $function$;

        REVOKE ALL ON FUNCTION public.evidence_import_dispatch_enqueue(
            uuid, uuid, text, text, bytea, text, text
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.evidence_import_dispatch_enqueue(
            uuid, uuid, text, text, bytea, text, text
        ) TO ledgerbridge_api;
        REVOKE INSERT ON TABLE public.evidence_import_dispatch FROM ledgerbridge_api;
        """
    )
    if os.environ.get("LEDGERBRIDGE_ENV", "development") != "production":
        op.execute(
            """
            GRANT EXECUTE ON FUNCTION public.evidence_import_dispatch_enqueue(
                uuid, uuid, text, text, bytea, text, text
            ) TO ledgerbridge_app;
            """
        )


def downgrade() -> None:
    op.execute(
        """
        GRANT INSERT ON TABLE public.evidence_import_dispatch TO ledgerbridge_api;
        REVOKE ALL ON FUNCTION public.evidence_import_dispatch_enqueue(
            uuid, uuid, text, text, bytea, text, text
        ) FROM PUBLIC, ledgerbridge_api, ledgerbridge_app;
        DROP FUNCTION public.evidence_import_dispatch_enqueue(
            uuid, uuid, text, text, bytea, text, text
        );
        DROP TRIGGER evidence_import_dispatch_acceptance_binding
            ON public.evidence_import_dispatch;
        DROP FUNCTION public.evidence_import_dispatch_bind_acceptance();
        """
    )
