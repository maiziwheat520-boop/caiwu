"""Add durable worker-owned evidence import dispatch state."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260823_0005"
down_revision: str | None = "20260822_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DISPATCH_STATE = postgresql.ENUM(
    "PENDING",
    "RUNNING",
    "RETRY_WAIT",
    "SUCCEEDED",
    "FAILED",
    name="dispatch_state",
    create_type=False,
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TYPE public.dispatch_state AS ENUM
            ('PENDING', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED')
        """
    )

    op.create_table(
        "evidence_import_dispatch",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ingest_channel", sa.String(length=64), nullable=False),
        sa.Column("accepted_audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manifest_generation", sa.String(length=100), nullable=False),
        sa.Column("manifest_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "state",
            DISPATCH_STATE,
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("import_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("diagnostic_summary", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "btrim(manifest_generation) <> ''",
            name="ck_dispatch_manifest_generation_not_blank",
        ),
        sa.CheckConstraint(
            "octet_length(manifest_digest) = 32",
            name="ck_dispatch_manifest_digest_length",
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 16",
            name="ck_dispatch_attempt_count_bounded",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'",
            name="ck_dispatch_error_code_bounded",
        ),
        sa.CheckConstraint(
            "diagnostic_summary IS NULL OR btrim(diagnostic_summary) <> ''",
            name="ck_dispatch_diagnostic_not_blank",
        ),
        sa.CheckConstraint(
            "(state = 'PENDING' AND attempt_count = 0 AND lease_owner IS NULL "
            "AND lease_until IS NULL AND started_at IS NULL AND completed_at IS NULL "
            "AND import_job_id IS NULL AND error_code IS NULL AND diagnostic_summary IS NULL) OR "
            "(state = 'RUNNING' AND attempt_count > 0 AND lease_owner IS NOT NULL "
            "AND lease_until IS NOT NULL AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND import_job_id IS NULL AND error_code IS NULL "
            "AND diagnostic_summary IS NULL) OR "
            "(state = 'RETRY_WAIT' AND attempt_count > 0 AND lease_owner IS NULL "
            "AND lease_until IS NULL AND started_at IS NOT NULL "
            "AND completed_at IS NULL AND import_job_id IS NULL AND error_code IS NOT NULL "
            "AND diagnostic_summary IS NOT NULL) OR "
            "(state = 'SUCCEEDED' AND completed_at IS NOT NULL AND import_job_id IS NOT NULL "
            "AND lease_owner IS NULL AND lease_until IS NULL AND error_code IS NULL "
            "AND diagnostic_summary IS NULL) OR "
            "(state = 'FAILED' AND completed_at IS NOT NULL AND error_code IS NOT NULL "
            "AND diagnostic_summary IS NOT NULL AND lease_owner IS NULL AND lease_until IS NULL)",
            name="ck_dispatch_state_shape",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["raw_artifact.id"],
            name="fk_dispatch_artifact_raw_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ingest_channel"],
            ["ingest_channel.id"],
            name="fk_dispatch_ingest_channel_ingest_channel",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_audit_event_id"],
            ["audit_event.id"],
            name="fk_dispatch_acceptance_audit_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_job_id", "artifact_id"],
            ["import_job.id", "import_job.artifact_id"],
            name="fk_dispatch_import_job_artifact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evidence_import_dispatch"),
        sa.UniqueConstraint(
            "artifact_id",
            "ingest_channel",
            "manifest_generation",
            name="uq_dispatch_artifact_channel_generation",
        ),
        sa.UniqueConstraint(
            "accepted_audit_event_id",
            name="uq_dispatch_accepted_audit_event",
        ),
    )

    op.create_index(
        "ix_dispatch_available",
        "evidence_import_dispatch",
        ["available_at", "created_at", "id"],
        postgresql_where=sa.text("state IN ('PENDING', 'RETRY_WAIT')"),
    )
    op.create_index(
        "ix_dispatch_lease_expiry",
        "evidence_import_dispatch",
        ["lease_until", "id"],
        postgresql_where=sa.text("state = 'RUNNING'"),
    )
    op.create_index(
        "ix_dispatch_status_lookup",
        "evidence_import_dispatch",
        ["id", "accepted_audit_event_id", "artifact_id"],
    )

    op.execute(
        """
        CREATE FUNCTION public.evidence_import_dispatch_enforce_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state::text <> 'PENDING' THEN
                    RAISE EXCEPTION 'dispatch rows must start PENDING'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
               OR NEW.ingest_channel IS DISTINCT FROM OLD.ingest_channel
               OR NEW.accepted_audit_event_id IS DISTINCT FROM OLD.accepted_audit_event_id
               OR NEW.manifest_generation IS DISTINCT FROM OLD.manifest_generation
               OR NEW.manifest_digest IS DISTINCT FROM OLD.manifest_digest
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'dispatch identity is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF OLD.state::text IN ('SUCCEEDED', 'FAILED') THEN
                RAISE EXCEPTION 'terminal dispatch rows are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.state::text = 'PENDING'
               AND NEW.state::text NOT IN ('RUNNING', 'FAILED') THEN
                RAISE EXCEPTION 'illegal dispatch transition from PENDING to %', NEW.state
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.state::text = 'RUNNING'
               AND NEW.state::text NOT IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'RETRY_WAIT') THEN
                RAISE EXCEPTION 'illegal dispatch transition from RUNNING to %', NEW.state
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.state::text = 'RETRY_WAIT'
               AND NEW.state::text NOT IN ('RUNNING', 'FAILED') THEN
                RAISE EXCEPTION 'illegal dispatch transition from RETRY_WAIT to %', NEW.state
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF NEW.state::text = 'RUNNING'
               AND NEW.lease_until <= CURRENT_TIMESTAMP THEN
                RAISE EXCEPTION 'running dispatch lease must be in the future'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE TRIGGER evidence_import_dispatch_state_machine
        BEFORE INSERT OR UPDATE ON public.evidence_import_dispatch
        FOR EACH ROW EXECUTE FUNCTION public.evidence_import_dispatch_enforce_transition();
        """
    )

    op.execute("REVOKE ALL ON TABLE public.evidence_import_dispatch FROM PUBLIC")
    op.execute("REVOKE ALL ON TYPE public.dispatch_state FROM PUBLIC")
    op.execute("GRANT USAGE ON TYPE public.dispatch_state TO ledgerbridge_app")
    op.execute("GRANT SELECT, INSERT ON TABLE public.evidence_import_dispatch TO ledgerbridge_app")
    op.execute(
        """
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
        ) ON TABLE public.evidence_import_dispatch TO ledgerbridge_app
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.evidence_import_dispatch) THEN
                RAISE EXCEPTION
                    'dispatch data prevents destructive downgrade';
            END IF;
        END
        $ledgerbridge$;
        """
    )
    op.execute("REVOKE ALL ON TABLE public.evidence_import_dispatch FROM ledgerbridge_app")
    op.execute("REVOKE USAGE ON TYPE public.dispatch_state FROM ledgerbridge_app")
    op.execute(
        "DROP TRIGGER evidence_import_dispatch_state_machine ON public.evidence_import_dispatch"
    )
    op.execute("DROP FUNCTION public.evidence_import_dispatch_enforce_transition()")
    op.drop_index("ix_dispatch_lease_expiry", table_name="evidence_import_dispatch")
    op.drop_index("ix_dispatch_available", table_name="evidence_import_dispatch")
    op.drop_index("ix_dispatch_status_lookup", table_name="evidence_import_dispatch")
    op.drop_table("evidence_import_dispatch")
    op.execute("DROP TYPE public.dispatch_state")
