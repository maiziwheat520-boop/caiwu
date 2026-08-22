"""Add canonical ingestion and financial source registries.

Revision ID: 20260822_0004
Revises: 20260821_0003
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260822_0004"
down_revision: str | None = "20260821_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_channel",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "id ~ '^[a-z][a-z0-9_]{0,63}$'",
            name=op.f("ck_ingest_channel_ingest_channel_id_canonical"),
        ),
        sa.CheckConstraint(
            "btrim(description) <> ''",
            name=op.f("ck_ingest_channel_ingest_channel_description_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_channel")),
    )
    op.create_table(
        "source_system",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "id ~ '^[a-z][a-z0-9_]{0,63}$'",
            name=op.f("ck_source_system_source_system_id_canonical"),
        ),
        sa.CheckConstraint(
            "btrim(description) <> ''",
            name=op.f("ck_source_system_source_system_description_not_blank"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_system")),
    )

    op.execute(
        """
        INSERT INTO public.ingest_channel (id, description)
        VALUES
            ('manual_upload', 'Operator-provided upload channel'),
            ('synthetic_upload', 'Synthetic test-only upload channel');

        INSERT INTO public.source_system (id, description)
        VALUES ('synthetic', 'Synthetic test-only financial source');
        """
    )
    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.raw_artifact AS artifact
                LEFT JOIN public.ingest_channel AS channel
                  ON channel.id = artifact.source
                WHERE channel.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'Phase 3 requires every raw artifact source to be a registered channel';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM public.source_record AS record
                LEFT JOIN public.source_system AS system
                  ON system.id = record.source
                WHERE system.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'Phase 3 requires every source record source to be a registered system';
            END IF;
        END
        $ledgerbridge$;
        """
    )

    op.alter_column(
        "raw_artifact",
        "source",
        existing_type=sa.String(length=200),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "source_record",
        "source",
        existing_type=sa.String(length=200),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.create_foreign_key(
        "fk_raw_artifact_source_ingest_channel",
        "raw_artifact",
        "ingest_channel",
        ["source"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_source_record_source_source_system",
        "source_record",
        "source_system",
        ["source"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.execute(
        """
        CREATE FUNCTION public.registry_block_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION 'canonical source registries are append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;

        CREATE TRIGGER ingest_channel_no_update_delete
        BEFORE UPDATE OR DELETE ON public.ingest_channel
        FOR EACH ROW EXECUTE FUNCTION public.registry_block_mutation();

        CREATE TRIGGER source_system_no_update_delete
        BEFORE UPDATE OR DELETE ON public.source_system
        FOR EACH ROW EXECUTE FUNCTION public.registry_block_mutation();

        REVOKE ALL ON TABLE public.ingest_channel, public.source_system FROM PUBLIC;
        GRANT SELECT ON TABLE public.ingest_channel, public.source_system
            TO ledgerbridge_app;
        """
    )

    op.add_column(
        "import_job",
        sa.Column("source_system", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "import_job",
        sa.Column(
            "terminal_audit_event_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.source_record
                GROUP BY import_job_id, artifact_id
                HAVING count(DISTINCT source) > 1
                    OR count(DISTINCT parser_version) > 1
            ) THEN
                RAISE EXCEPTION
                    'Phase 3 cannot bind a job with mixed source or parser provenance';
            END IF;
        END
        $ledgerbridge$;

        UPDATE public.import_job AS job
        SET source_system = records.source_system
        FROM (
            SELECT import_job_id, artifact_id, min(source) AS source_system
            FROM public.source_record
            GROUP BY import_job_id, artifact_id
        ) AS records
        WHERE job.id = records.import_job_id
          AND job.artifact_id = records.artifact_id
          AND job.connector_name NOT LIKE 'ledgerbridge.%'
          AND job.source_system IS NULL;

        UPDATE public.import_job
        SET source_system = 'synthetic'
        WHERE connector_name NOT LIKE 'ledgerbridge.%'
          AND source_system IS NULL;

        DO $ledgerbridge$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM public.source_record AS record
                JOIN public.import_job AS job
                  ON job.id = record.import_job_id
                 AND job.artifact_id = record.artifact_id
                WHERE record.source <> job.source_system
                   OR record.parser_version <> job.connector_version
            ) THEN
                RAISE EXCEPTION
                    'Phase 3 found import rows with mismatched job provenance';
            END IF;
        END
        $ledgerbridge$;

        ALTER TABLE public.import_job
            ADD CONSTRAINT fk_import_job_source_system
                FOREIGN KEY (source_system)
                REFERENCES public.source_system (id)
                ON DELETE RESTRICT,
            ADD CONSTRAINT fk_import_job_terminal_audit_event
                FOREIGN KEY (terminal_audit_event_id)
                REFERENCES public.audit_event (id)
                ON DELETE RESTRICT,
            ADD CONSTRAINT uq_import_job_terminal_audit_event_id
                UNIQUE (terminal_audit_event_id),
            ADD CONSTRAINT uq_import_job_provenance_identity
                UNIQUE (id, artifact_id, source_system, connector_version),
            ADD CONSTRAINT ck_import_job_source_system_required
                CHECK (connector_name LIKE 'ledgerbridge.%' OR source_system IS NOT NULL);

        ALTER TABLE public.source_record
            DROP CONSTRAINT IF EXISTS fk_source_record_job_artifact,
            ADD CONSTRAINT fk_source_record_job_provenance
                FOREIGN KEY (import_job_id, artifact_id, source, parser_version)
                REFERENCES public.import_job (
                    id, artifact_id, source_system, connector_version
                )
                ON DELETE RESTRICT;

        ALTER TABLE public.import_job
            DROP CONSTRAINT IF EXISTS ck_import_job_import_job_state_timestamps,
            ADD CONSTRAINT ck_import_job_import_job_state_timestamps
            CHECK (
                (status = 'PENDING' AND started_at IS NULL AND completed_at IS NULL
                 AND terminal_audit_event_id IS NULL AND error_code IS NULL
                 AND parsed_count = 0 AND created_count = 0 AND duplicate_count = 0)
                OR
                (status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL
                 AND terminal_audit_event_id IS NULL AND error_code IS NULL
                 AND parsed_count = 0 AND created_count = 0 AND duplicate_count = 0)
                OR
                (status = 'SUCCEEDED' AND started_at IS NOT NULL AND completed_at IS NOT NULL
                 AND terminal_audit_event_id IS NOT NULL AND error_code IS NULL)
                OR
                (status = 'FAILED' AND completed_at IS NOT NULL
                 AND terminal_audit_event_id IS NOT NULL AND error_code IS NOT NULL)
                OR
                (status = 'NEEDS_REVIEW' AND completed_at IS NOT NULL
                 AND terminal_audit_event_id IS NOT NULL AND error_code IS NOT NULL)
            );

        CREATE FUNCTION public.import_job_validate_terminal_audit()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_action text;
            v_job_id text;
            v_artifact_id text;
            v_status text;
        BEGIN
            IF NEW.status::text IN ('SUCCEEDED', 'FAILED', 'NEEDS_REVIEW') THEN
                SELECT event.action,
                       event.payload ->> 'job_id',
                       event.payload ->> 'artifact_id',
                       event.payload ->> 'status'
                INTO v_action, v_job_id, v_artifact_id, v_status
                FROM public.audit_event AS event
                WHERE event.id = NEW.terminal_audit_event_id;
                IF NOT FOUND
                   OR v_action <> 'import.complete'
                   OR v_job_id <> NEW.id::text
                   OR v_artifact_id <> NEW.artifact_id::text
                   OR v_status <> NEW.status::text THEN
                    RAISE EXCEPTION
                        'terminal import job requires a matching import.complete audit event'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE CONSTRAINT TRIGGER import_job_terminal_audit_binding
        AFTER INSERT OR UPDATE OF status, terminal_audit_event_id, artifact_id
        ON public.import_job
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.import_job_validate_terminal_audit();
        """
    )
    op.execute(
        """
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
        ) ON TABLE public.import_job TO ledgerbridge_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.raw_artifact)
               OR EXISTS (SELECT 1 FROM public.source_record)
               OR EXISTS (
                    SELECT 1 FROM public.ingest_channel
                    WHERE id NOT IN ('manual_upload', 'synthetic_upload')
               )
               OR EXISTS (
                    SELECT 1 FROM public.source_system
                    WHERE id <> 'synthetic'
               ) THEN
                RAISE EXCEPTION
                    'Phase 3 registry data prevents destructive downgrade';
            END IF;
        END
        $ledgerbridge$;

        DO $ledgerbridge$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_app') THEN
                REVOKE ALL ON TABLE public.ingest_channel, public.source_system
                    FROM ledgerbridge_app;
            END IF;
        END
        $ledgerbridge$;
        """
    )

    op.execute(
        """
        DROP TRIGGER IF EXISTS import_job_terminal_audit_binding ON public.import_job;
        DROP FUNCTION IF EXISTS public.import_job_validate_terminal_audit();
        ALTER TABLE public.source_record
            DROP CONSTRAINT IF EXISTS fk_source_record_job_provenance,
            ADD CONSTRAINT fk_source_record_job_artifact
                FOREIGN KEY (import_job_id, artifact_id)
                REFERENCES public.import_job (id, artifact_id)
                ON DELETE RESTRICT;
        ALTER TABLE public.import_job
            DROP CONSTRAINT IF EXISTS ck_import_job_import_job_state_timestamps,
            ADD CONSTRAINT ck_import_job_import_job_state_timestamps
            CHECK (
                (status = 'PENDING' AND started_at IS NULL AND completed_at IS NULL
                 AND error_code IS NULL AND parsed_count = 0 AND created_count = 0
                 AND duplicate_count = 0)
                OR
                (status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL
                 AND error_code IS NULL AND parsed_count = 0 AND created_count = 0
                 AND duplicate_count = 0)
                OR
                (status = 'SUCCEEDED' AND started_at IS NOT NULL AND completed_at IS NOT NULL
                 AND error_code IS NULL)
                OR
                (status = 'FAILED' AND completed_at IS NOT NULL AND error_code IS NOT NULL)
                OR
                (status = 'NEEDS_REVIEW' AND completed_at IS NOT NULL
                 AND error_code IS NOT NULL)
            );
        ALTER TABLE public.import_job
            DROP CONSTRAINT IF EXISTS ck_import_job_source_system_required,
            DROP CONSTRAINT IF EXISTS uq_import_job_terminal_audit_event_id,
            DROP CONSTRAINT IF EXISTS uq_import_job_provenance_identity,
            DROP CONSTRAINT IF EXISTS fk_import_job_terminal_audit_event,
            DROP CONSTRAINT IF EXISTS fk_import_job_source_system;
        """
    )
    op.drop_column("import_job", "terminal_audit_event_id")
    op.drop_column("import_job", "source_system")

    op.drop_constraint(
        "fk_source_record_source_source_system",
        "source_record",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_raw_artifact_source_ingest_channel",
        "raw_artifact",
        type_="foreignkey",
    )
    op.alter_column(
        "source_record",
        "source",
        existing_type=sa.String(length=64),
        type_=sa.String(length=200),
        existing_nullable=False,
    )
    op.alter_column(
        "raw_artifact",
        "source",
        existing_type=sa.String(length=64),
        type_=sa.String(length=200),
        existing_nullable=False,
    )

    op.execute("DROP TRIGGER source_system_no_update_delete ON public.source_system")
    op.execute("DROP TRIGGER ingest_channel_no_update_delete ON public.ingest_channel")
    op.execute("DROP FUNCTION public.registry_block_mutation()")
    op.drop_table("source_system")
    op.drop_table("ingest_channel")
