"""Grant separate API enqueue and worker execution database roles."""

from collections.abc import Sequence

from alembic import op

revision: str = "20260823_0006"
down_revision: str | None = "20260823_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Role identifiers are fixed deployment contract values; keeping this DDL
    # literal avoids constructing SQL from runtime input.
    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_api')
               OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_worker') THEN
                RAISE EXCEPTION
                    'runtime role split requires bootstrap roles';
            END IF;
        END
        $ledgerbridge$;
        """
    )
    op.execute(
        """
        REVOKE ALL ON TABLE public.raw_artifact, public.source_record,
            public.import_job, public.ingest_channel, public.source_system,
            public.audit_event, public.evidence_import_dispatch
            FROM ledgerbridge_api, ledgerbridge_worker;
        REVOKE ALL ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
            FROM ledgerbridge_api, ledgerbridge_worker;
        REVOKE ALL ON TYPE public.import_job_status, public.dispatch_state
            FROM ledgerbridge_api, ledgerbridge_worker;

        GRANT USAGE ON SCHEMA public TO ledgerbridge_api, ledgerbridge_worker;

        GRANT SELECT, INSERT ON TABLE public.raw_artifact TO ledgerbridge_api;
        GRANT SELECT ON TABLE public.ingest_channel, public.source_system,
            public.audit_event, public.import_job, public.evidence_import_dispatch
            TO ledgerbridge_api;
        GRANT INSERT ON TABLE public.evidence_import_dispatch TO ledgerbridge_api;
        GRANT USAGE ON TYPE public.dispatch_state TO ledgerbridge_api;
        GRANT EXECUTE ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
            TO ledgerbridge_api;

        GRANT SELECT, INSERT ON TABLE public.raw_artifact, public.source_record,
            public.import_job TO ledgerbridge_worker;
        GRANT SELECT ON TABLE public.ingest_channel, public.source_system,
            public.audit_event, public.evidence_import_dispatch TO ledgerbridge_worker;
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
        ) ON TABLE public.import_job TO ledgerbridge_worker;
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
        ) ON TABLE public.evidence_import_dispatch TO ledgerbridge_worker;
        GRANT USAGE ON TYPE public.import_job_status, public.dispatch_state
            TO ledgerbridge_worker;
        GRANT EXECUTE ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
            TO ledgerbridge_worker;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_api') THEN
                REVOKE ALL ON TABLE public.raw_artifact, public.source_record,
                    public.import_job, public.ingest_channel, public.source_system,
                    public.audit_event, public.evidence_import_dispatch
                    FROM ledgerbridge_api;
                REVOKE ALL ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
                    FROM ledgerbridge_api;
                REVOKE ALL ON TYPE public.import_job_status, public.dispatch_state
                    FROM ledgerbridge_api;
                REVOKE USAGE ON SCHEMA public FROM ledgerbridge_api;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_worker') THEN
                REVOKE ALL ON TABLE public.raw_artifact, public.source_record,
                    public.import_job, public.ingest_channel, public.source_system,
                    public.audit_event, public.evidence_import_dispatch
                    FROM ledgerbridge_worker;
                REVOKE ALL ON FUNCTION public.append_audit_event(text, text, text, text, jsonb)
                    FROM ledgerbridge_worker;
                REVOKE ALL ON TYPE public.import_job_status, public.dispatch_state
                    FROM ledgerbridge_worker;
                REVOKE USAGE ON SCHEMA public FROM ledgerbridge_worker;
            END IF;
        END
        $ledgerbridge$;
        """
    )
