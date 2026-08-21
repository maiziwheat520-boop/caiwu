"""Create Phase 2 evidence provenance and import framework.

Revision ID: 20260821_0003
Revises: 20260821_0002
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260821_0003"
down_revision: str | None = "20260821_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMPORT_JOB_STATUS = postgresql.ENUM(
    "PENDING",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "NEEDS_REVIEW",
    name="import_job_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    IMPORT_JOB_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "raw_artifact",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("sha256", sa.LargeBinary(length=32), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(length=96), nullable=False),
        sa.Column("audit_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "octet_length(sha256) = 32",
            name=op.f("ck_raw_artifact_raw_artifact_sha256_length"),
        ),
        sa.CheckConstraint(
            "btrim(source) <> ''", name=op.f("ck_raw_artifact_raw_artifact_source_not_blank")
        ),
        sa.CheckConstraint(
            "btrim(original_filename) <> ''",
            name=op.f("ck_raw_artifact_raw_artifact_original_filename_not_blank"),
        ),
        sa.CheckConstraint(
            "btrim(media_type) <> ''",
            name=op.f("ck_raw_artifact_raw_artifact_media_type_not_blank"),
        ),
        sa.CheckConstraint(
            "byte_size >= 0", name=op.f("ck_raw_artifact_raw_artifact_byte_size_nonnegative")
        ),
        sa.CheckConstraint(
            "storage_key ~ '^sha256/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$'",
            name=op.f("ck_raw_artifact_raw_artifact_storage_key_content_addressed"),
        ),
        sa.CheckConstraint(
            "storage_key = 'sha256/' || substr(encode(sha256, 'hex'), 1, 2) || '/' "
            "|| substr(encode(sha256, 'hex'), 3, 2) || '/' || encode(sha256, 'hex')",
            name=op.f("ck_raw_artifact_raw_artifact_storage_key_matches_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["audit_event_id"],
            ["audit_event.id"],
            name="fk_raw_artifact_audit_event_id_audit_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_raw_artifact"),
        sa.UniqueConstraint("sha256", name="uq_raw_artifact_sha256"),
        sa.UniqueConstraint("storage_key", name="uq_raw_artifact_storage_key"),
        sa.UniqueConstraint("audit_event_id", name="uq_raw_artifact_audit_event_id"),
    )

    op.create_table(
        "import_job",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_name", sa.String(length=100), nullable=False),
        sa.Column("connector_version", sa.String(length=100), nullable=False),
        sa.Column("status", IMPORT_JOB_STATUS, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parsed_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("duplicate_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("diagnostic_summary", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "btrim(connector_name) <> ''",
            name=op.f("ck_import_job_import_job_connector_not_blank"),
        ),
        sa.CheckConstraint(
            "btrim(connector_version) <> ''",
            name=op.f("ck_import_job_import_job_connector_version_not_blank"),
        ),
        sa.CheckConstraint(
            "parsed_count >= 0 AND created_count >= 0 AND duplicate_count >= 0",
            name=op.f("ck_import_job_import_job_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'",
            name=op.f("ck_import_job_import_job_error_code_bounded"),
        ),
        sa.CheckConstraint(
            "diagnostic_summary IS NULL OR btrim(diagnostic_summary) <> ''",
            name=op.f("ck_import_job_import_job_diagnostic_not_blank"),
        ),
        sa.CheckConstraint(
            "(status = 'PENDING' AND started_at IS NULL AND completed_at IS NULL "
            "AND error_code IS NULL AND parsed_count = 0 AND created_count = 0 "
            "AND duplicate_count = 0) OR "
            "(status = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL "
            "AND error_code IS NULL AND parsed_count = 0 AND created_count = 0 "
            "AND duplicate_count = 0) OR "
            "(status = 'SUCCEEDED' AND started_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND error_code IS NULL) OR "
            "(status = 'FAILED' AND completed_at IS NOT NULL AND error_code IS NOT NULL) OR "
            "(status = 'NEEDS_REVIEW' AND completed_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name=op.f("ck_import_job_import_job_state_timestamps"),
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["raw_artifact.id"],
            name="fk_import_job_artifact_id_raw_artifact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_job"),
        sa.UniqueConstraint(
            "artifact_id",
            "connector_name",
            "connector_version",
            name="uq_import_job_artifact_connector_version",
        ),
        sa.UniqueConstraint("id", "artifact_id", name="uq_import_job_id_artifact"),
    )

    op.create_table(
        "source_record",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("record_locator", sa.String(length=500), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column("parser_version", sa.String(length=100), nullable=False),
        sa.Column("raw_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("normalized_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_transaction_id", sa.String(length=300), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(record_locator) <> ''",
            name=op.f("ck_source_record_source_record_locator_not_blank"),
        ),
        sa.CheckConstraint(
            "btrim(source) <> ''", name=op.f("ck_source_record_source_record_source_not_blank")
        ),
        sa.CheckConstraint(
            "btrim(parser_version) <> ''",
            name=op.f("ck_source_record_source_record_parser_version_not_blank"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(raw_fields) = 'object'",
            name=op.f("ck_source_record_source_record_raw_fields_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(normalized_fields) = 'object'",
            name=op.f("ck_source_record_source_record_normalized_fields_object"),
        ),
        sa.CheckConstraint(
            "external_transaction_id IS NULL OR btrim(external_transaction_id) <> ''",
            name=op.f("ck_source_record_source_record_external_id_not_blank"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["account.id"],
            name="fk_source_record_account_id_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["raw_artifact.id"],
            name="fk_source_record_artifact_id_raw_artifact",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_job_id", "artifact_id"],
            ["import_job.id", "import_job.artifact_id"],
            name="fk_source_record_job_artifact",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_record"),
        sa.UniqueConstraint(
            "artifact_id", "record_locator", name="uq_source_record_artifact_locator"
        ),
    )
    op.create_index(
        "uq_source_record_external_identity",
        "source_record",
        ["account_id", "source", "external_transaction_id"],
        unique=True,
        postgresql_where=sa.text("account_id IS NOT NULL AND external_transaction_id IS NOT NULL"),
    )

    op.add_column(
        "journal_entry",
        sa.Column("posted_audit_event_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_journal_entry_source_record_id_source_record",
        "journal_entry",
        "source_record",
        ["source_record_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_journal_entry_posted_audit_event_id_audit_event",
        "journal_entry",
        "audit_event",
        ["posted_audit_event_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_journal_entry_posted_audit_event_id",
        "journal_entry",
        ["posted_audit_event_id"],
    )
    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (SELECT 1 FROM journal_entry WHERE status = 'POSTED') THEN
                RAISE EXCEPTION
                    'Phase 2 requires explicit audit binding for every existing POSTED entry';
            END IF;
        END
        $ledgerbridge$;
        """
    )
    op.create_check_constraint(
        op.f("ck_journal_entry_journal_entry_posted_audit_binding"),
        "journal_entry",
        "(status = 'POSTED') = (posted_audit_event_id IS NOT NULL)",
    )

    op.execute(
        """
        CREATE FUNCTION raw_artifact_block_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'raw_artifact metadata is immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;

        CREATE TRIGGER raw_artifact_no_update_delete
        BEFORE UPDATE OR DELETE ON raw_artifact
        FOR EACH ROW EXECUTE FUNCTION raw_artifact_block_mutation();

        CREATE FUNCTION public.raw_artifact_validate_audit()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_action text;
            v_payload jsonb;
            v_xmin xid;
        BEGIN
            SELECT action, payload, xmin
            INTO v_action, v_payload, v_xmin
            FROM public.audit_event
            WHERE id = NEW.audit_event_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'artifact audit evidence does not exist'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF v_xmin <> pg_current_xact_id()::text::xid THEN
                RAISE EXCEPTION 'artifact audit evidence must be appended in this transaction'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF v_action <> 'artifact.ingest' THEN
                RAISE EXCEPTION 'artifact audit action must be artifact.ingest'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF v_payload ->> 'sha256' IS DISTINCT FROM encode(NEW.sha256, 'hex')
               OR v_payload ->> 'byte_size' IS DISTINCT FROM NEW.byte_size::text
               OR v_payload ->> 'storage_key' IS DISTINCT FROM NEW.storage_key
               OR v_payload ->> 'source' IS DISTINCT FROM NEW.source
               OR v_payload ->> 'media_type' IS DISTINCT FROM NEW.media_type THEN
                RAISE EXCEPTION 'artifact audit payload does not match artifact metadata'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE TRIGGER raw_artifact_audit_binding
        BEFORE INSERT ON public.raw_artifact
        FOR EACH ROW EXECUTE FUNCTION public.raw_artifact_validate_audit();

        CREATE FUNCTION source_record_block_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            RAISE EXCEPTION 'source_record is permanent and immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;

        CREATE TRIGGER source_record_no_update_delete
        BEFORE UPDATE OR DELETE ON source_record
        FOR EACH ROW EXECUTE FUNCTION source_record_block_mutation();
        """
    )

    op.execute(
        """
        CREATE FUNCTION import_job_enforce_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'PENDING' THEN
                    RAISE EXCEPTION 'import jobs must start PENDING'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.artifact_id IS DISTINCT FROM OLD.artifact_id
               OR NEW.connector_name IS DISTINCT FROM OLD.connector_name
               OR NEW.connector_version IS DISTINCT FROM OLD.connector_version
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'import job identity is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;

            IF OLD.status IN ('SUCCEEDED', 'FAILED', 'NEEDS_REVIEW') THEN
                RAISE EXCEPTION 'terminal import jobs are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.status = 'PENDING'
               AND NEW.status NOT IN ('RUNNING', 'FAILED', 'NEEDS_REVIEW') THEN
                RAISE EXCEPTION 'illegal import job transition from PENDING to %', NEW.status
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF OLD.status = 'RUNNING'
               AND NEW.status NOT IN ('SUCCEEDED', 'FAILED', 'NEEDS_REVIEW') THEN
                RAISE EXCEPTION 'illegal import job transition from RUNNING to %', NEW.status
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE TRIGGER import_job_state_machine
        BEFORE INSERT OR UPDATE ON import_job
        FOR EACH ROW EXECUTE FUNCTION import_job_enforce_transition();
        """
    )

    op.execute(
        """
        CREATE FUNCTION public.journal_entry_validate_post_audit()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            v_action text;
            v_payload jsonb;
            v_xmin xid;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status = 'POSTED' OR NEW.posted_audit_event_id IS NOT NULL THEN
                    RAISE EXCEPTION 'journal entries must be created before they are POSTED'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.status = 'DRAFT' AND NEW.status = 'POSTED' THEN
                IF NEW.posted_audit_event_id IS NULL THEN
                    RAISE EXCEPTION 'POSTED transition requires journal.post audit evidence'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NEW.posted_audit_event_id = NEW.audit_event_id THEN
                    RAISE EXCEPTION 'creation audit evidence cannot authorize POSTED transition'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM public.journal_entry
                    WHERE audit_event_id = NEW.posted_audit_event_id
                ) THEN
                    RAISE EXCEPTION
                        'a journal creation event cannot be reused for POSTED transition'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;

                SELECT action, payload, xmin
                INTO v_action, v_payload, v_xmin
                FROM public.audit_event
                WHERE id = NEW.posted_audit_event_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'POSTED audit evidence does not exist'
                        USING ERRCODE = 'foreign_key_violation';
                END IF;
                IF v_xmin <> pg_current_xact_id()::text::xid THEN
                    RAISE EXCEPTION 'POSTED audit evidence must be appended in this transaction'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF v_action <> 'journal.post' THEN
                    RAISE EXCEPTION 'POSTED audit action must be journal.post'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF v_payload ->> 'journal_entry_id' IS DISTINCT FROM NEW.id::text THEN
                    RAISE EXCEPTION 'POSTED audit target does not match journal entry'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            ELSIF NEW.status = 'POSTED' AND OLD.status <> 'DRAFT' THEN
                RAISE EXCEPTION 'only DRAFT journal entries may transition to POSTED'
                    USING ERRCODE = 'integrity_constraint_violation';
            ELSIF NEW.status <> 'POSTED' AND NEW.posted_audit_event_id IS NOT NULL THEN
                RAISE EXCEPTION 'non-POSTED journal entries cannot bind POSTED audit evidence'
                    USING ERRCODE = 'integrity_constraint_violation';
            ELSIF NEW.status = OLD.status
               AND NEW.posted_audit_event_id IS DISTINCT FROM OLD.posted_audit_event_id THEN
                RAISE EXCEPTION 'POSTED audit binding changes only with the status transition'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;

        CREATE TRIGGER journal_entry_post_audit_binding
        BEFORE INSERT OR UPDATE OF status, posted_audit_event_id, audit_event_id
        ON public.journal_entry
        FOR EACH ROW EXECUTE FUNCTION public.journal_entry_validate_post_audit();
        """
    )

    op.execute(
        """
        REVOKE ALL ON TABLE raw_artifact, import_job, source_record FROM PUBLIC;

        GRANT USAGE ON TYPE import_job_status TO ledgerbridge_app;
        GRANT SELECT, INSERT ON TABLE raw_artifact, source_record TO ledgerbridge_app;
        GRANT SELECT, INSERT ON TABLE import_job TO ledgerbridge_app;
        GRANT UPDATE (
            status,
            started_at,
            completed_at,
            parsed_count,
            created_count,
            duplicate_count,
            error_code,
            diagnostic_summary
        ) ON TABLE import_job TO ledgerbridge_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $ledgerbridge$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_app') THEN
                REVOKE ALL ON TABLE raw_artifact, import_job, source_record
                FROM ledgerbridge_app;
                REVOKE USAGE ON TYPE import_job_status FROM ledgerbridge_app;
            END IF;
        END
        $ledgerbridge$;
        """
    )

    op.execute("DROP TRIGGER journal_entry_post_audit_binding ON public.journal_entry")
    op.execute("DROP FUNCTION public.journal_entry_validate_post_audit()")
    op.drop_constraint(
        op.f("ck_journal_entry_journal_entry_posted_audit_binding"),
        "journal_entry",
        type_="check",
    )
    op.drop_constraint("uq_journal_entry_posted_audit_event_id", "journal_entry", type_="unique")
    op.drop_constraint(
        "fk_journal_entry_posted_audit_event_id_audit_event",
        "journal_entry",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_journal_entry_source_record_id_source_record",
        "journal_entry",
        type_="foreignkey",
    )
    op.drop_column("journal_entry", "posted_audit_event_id")

    op.execute("DROP TRIGGER import_job_state_machine ON import_job")
    op.execute("DROP FUNCTION import_job_enforce_transition()")
    op.execute("DROP TRIGGER source_record_no_update_delete ON source_record")
    op.execute("DROP FUNCTION source_record_block_mutation()")
    op.execute("DROP TRIGGER raw_artifact_audit_binding ON public.raw_artifact")
    op.execute("DROP FUNCTION public.raw_artifact_validate_audit()")
    op.execute("DROP TRIGGER raw_artifact_no_update_delete ON raw_artifact")
    op.execute("DROP FUNCTION raw_artifact_block_mutation()")

    op.drop_index("uq_source_record_external_identity", table_name="source_record")
    op.drop_table("source_record")
    op.drop_table("import_job")
    op.drop_table("raw_artifact")
    IMPORT_JOB_STATUS.drop(op.get_bind(), checkfirst=True)
