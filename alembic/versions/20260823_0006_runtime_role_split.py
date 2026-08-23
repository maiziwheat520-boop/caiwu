"""Grant separate API enqueue and worker execution database roles."""

from collections.abc import Sequence

from alembic import op

revision: str = "20260823_0006"
down_revision: str | None = "20260823_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

API_ROLE = "ledgerbridge_api"
WORKER_ROLE = "ledgerbridge_worker"


def upgrade() -> None:
    op.execute(
        f"""
        DO $ledgerbridge$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{API_ROLE}')
               OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
                RAISE EXCEPTION
                    'runtime role split requires {API_ROLE} and {WORKER_ROLE} bootstrap roles';
            END IF;
        END
        $ledgerbridge$;
        """
    )
    op.execute(
        f"""
        REVOKE ALL ON TABLE public.raw_artifact, public.source_record,
            public.import_job, public.ingest_channel, public.source_system,
            public.audit_event, public.evidence_import_dispatch
            FROM {API_ROLE}, {WORKER_ROLE};
        REVOKE ALL ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
            FROM {API_ROLE}, {WORKER_ROLE};
        REVOKE ALL ON TYPE public.import_job_status, public.dispatch_state
            FROM {API_ROLE}, {WORKER_ROLE};

        GRANT USAGE ON SCHEMA public TO {API_ROLE}, {WORKER_ROLE};

        GRANT SELECT, INSERT ON TABLE public.raw_artifact TO {API_ROLE};
        GRANT SELECT ON TABLE public.ingest_channel, public.source_system,
            public.audit_event, public.import_job, public.evidence_import_dispatch
            TO {API_ROLE};
        GRANT INSERT ON TABLE public.evidence_import_dispatch TO {API_ROLE};
        GRANT USAGE ON TYPE public.dispatch_state TO {API_ROLE};
        GRANT EXECUTE ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
            TO {API_ROLE};

        GRANT SELECT, INSERT ON TABLE public.raw_artifact, public.source_record,
            public.import_job TO {WORKER_ROLE};
        GRANT SELECT ON TABLE public.ingest_channel, public.source_system,
            public.audit_event, public.evidence_import_dispatch TO {WORKER_ROLE};
        GRANT UPDATE (
            status,
            started_at,
            completed_at,
            terminal_audit_event_id,
            parsed_count,
            created_count,
            duplicate_count,
            error_code,
            diagnostic_summary
        ) ON TABLE public.import_job TO {WORKER_ROLE};
        GRANT UPDATE (
            state,
            attempt_count,
            available_at,
            lease_owner,
            lease_until,
            started_at,
            completed_at,
            import_job_id,
            error_code,
            diagnostic_summary
        ) ON TABLE public.evidence_import_dispatch TO {WORKER_ROLE};
        GRANT USAGE ON TYPE public.import_job_status, public.dispatch_state
            TO {WORKER_ROLE};
        GRANT EXECUTE ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
            TO {WORKER_ROLE};
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{API_ROLE}') THEN
                REVOKE ALL ON TABLE public.raw_artifact, public.source_record,
                    public.import_job, public.ingest_channel, public.source_system,
                    public.audit_event, public.evidence_import_dispatch
                    FROM {API_ROLE};
                REVOKE ALL ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
                    FROM {API_ROLE};
                REVOKE ALL ON TYPE public.import_job_status, public.dispatch_state
                    FROM {API_ROLE};
                REVOKE USAGE ON SCHEMA public FROM {API_ROLE};
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
                REVOKE ALL ON TABLE public.raw_artifact, public.source_record,
                    public.import_job, public.ingest_channel, public.source_system,
                    public.audit_event, public.evidence_import_dispatch
                    FROM {WORKER_ROLE};
                REVOKE ALL ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
                    FROM {WORKER_ROLE};
                REVOKE ALL ON TYPE public.import_job_status, public.dispatch_state
                    FROM {WORKER_ROLE};
                REVOKE USAGE ON SCHEMA public FROM {WORKER_ROLE};
            END IF;
        END
        $ledgerbridge$;
        """
    )
