# ruff: noqa: E501

"""Create the first R1 Candidate/evidence fact tables.

This migration is a database contract foundation only.  It deliberately does
not create a reader role, views, command functions, or runtime grants.  All new
facts are owner-written and append-only; later migrations add the closed read
surface and the remaining ledger/reconciliation attribution.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260824_0012"
down_revision: str | None = "20260824_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def _append_only(table: str) -> None:
    function = f"r1_{table}_append_only"
    trigger = f"r1_{table}_append_only_trigger"
    op.execute(
        f"""
        CREATE FUNCTION public.{function}()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION '{table} is append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END
        $function$;
        CREATE TRIGGER {trigger}
        BEFORE UPDATE OR DELETE ON public.{table}
        FOR EACH ROW EXECUTE FUNCTION public.{function}();
        """
    )


def upgrade() -> None:
    op.create_table(
        "business_unit",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("ref", sa.String(100), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("btrim(ref) <> ''", name="business_unit_ref_not_blank"),
        sa.CheckConstraint("btrim(label) <> ''", name="business_unit_label_not_blank"),
        sa.CheckConstraint(
            "retired_at IS NULL OR retired_at >= created_at",
            name="business_unit_retired_after_created",
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entity.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_business_unit"),
        sa.UniqueConstraint("id", "entity_id", name="uq_business_unit_id_entity"),
        sa.UniqueConstraint("entity_id", "ref", name="uq_business_unit_entity_ref"),
    )
    op.create_table(
        "reporting_category",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("btrim(code) <> ''", name="reporting_category_code_not_blank"),
        sa.CheckConstraint("btrim(label) <> ''", name="reporting_category_label_not_blank"),
        sa.CheckConstraint(
            "retired_at IS NULL OR retired_at >= created_at",
            name="reporting_category_retired_after_created",
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entity.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_reporting_category"),
        sa.UniqueConstraint("id", "entity_id", name="uq_reporting_category_id_entity"),
        sa.UniqueConstraint("entity_id", "code", name="uq_reporting_category_entity_code"),
    )
    op.create_table(
        "evidence_object",
        sa.Column(
            "evidence_ref", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("business_unit_id", UUID, nullable=False),
        sa.Column("media_type", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("plaintext_sha256", sa.LargeBinary(32), nullable=False),
        sa.Column("plaintext_size", sa.BigInteger(), nullable=False),
        sa.Column("audit_event_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "octet_length(plaintext_sha256) = 32", name="evidence_object_plaintext_sha256_length"
        ),
        sa.CheckConstraint(
            "plaintext_size BETWEEN 0 AND 134217728", name="evidence_object_plaintext_size_bounded"
        ),
        sa.CheckConstraint("btrim(media_type) <> ''", name="evidence_object_media_type_not_blank"),
        sa.CheckConstraint(
            "display_name IS NULL OR btrim(display_name) <> ''",
            name="evidence_object_display_name_not_blank",
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entity.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["entity_id", "business_unit_id"],
            ["business_unit.entity_id", "business_unit.id"],
            ondelete="RESTRICT",
            name="fk_evidence_object_scope",
        ),
        sa.ForeignKeyConstraint(["audit_event_id"], ["audit_event.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("evidence_ref", name="pk_evidence_object"),
        sa.UniqueConstraint(
            "entity_id", "business_unit_id", "evidence_ref", name="uq_evidence_object_scope"
        ),
        sa.UniqueConstraint("audit_event_id", name="uq_evidence_object_audit_event"),
    )
    op.create_table(
        "encrypted_blob_version",
        sa.Column("blob_ref", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("evidence_ref", UUID, nullable=False),
        sa.Column("predecessor_blob_ref", UUID, nullable=True),
        sa.Column("object_ref", sa.String(64), nullable=False),
        sa.Column("ciphertext_sha256", sa.LargeBinary(32), nullable=False),
        sa.Column("ciphertext_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(77), nullable=False),
        sa.Column("envelope_schema", sa.String(28), nullable=False),
        sa.Column("algorithm", sa.String(40), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False),
        sa.Column("stream_header", sa.LargeBinary(24), nullable=False),
        sa.Column("wrapped_key_generation", sa.String(128), nullable=False),
        sa.Column("wrapped_key_nonce", sa.LargeBinary(24), nullable=False),
        sa.Column("wrapped_key_ciphertext", sa.LargeBinary(48), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("audit_event_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("object_ref ~ '^[0-9a-f]{64}$'", name="encrypted_blob_object_ref_shape"),
        sa.CheckConstraint(
            "octet_length(ciphertext_sha256) = 32", name="encrypted_blob_ciphertext_sha256_length"
        ),
        sa.CheckConstraint(
            "ciphertext_size BETWEEN 0 AND 268435456", name="encrypted_blob_ciphertext_size_bounded"
        ),
        sa.CheckConstraint(
            "storage_key = 'sha256/' || substr(encode(ciphertext_sha256, 'hex'), 1, 2) || '/' || substr(encode(ciphertext_sha256, 'hex'), 3, 2) || '/' || encode(ciphertext_sha256, 'hex')",
            name="encrypted_blob_storage_key_matches_digest",
        ),
        sa.CheckConstraint(
            "envelope_schema = 'ledgerbridge.secretstream.v1'",
            name="encrypted_blob_envelope_schema_fixed",
        ),
        sa.CheckConstraint(
            "algorithm = 'xchacha20poly1305-secretstream'", name="encrypted_blob_algorithm_fixed"
        ),
        sa.CheckConstraint(
            "chunk_size BETWEEN 1 AND 1048576", name="encrypted_blob_chunk_size_bounded"
        ),
        sa.CheckConstraint(
            "octet_length(stream_header) = 24", name="encrypted_blob_stream_header_length"
        ),
        sa.CheckConstraint(
            "wrapped_key_generation ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'",
            name="encrypted_blob_generation_shape",
        ),
        sa.CheckConstraint(
            "octet_length(wrapped_key_nonce) = 24", name="encrypted_blob_wrapped_nonce_length"
        ),
        sa.CheckConstraint(
            "octet_length(wrapped_key_ciphertext) = 48",
            name="encrypted_blob_wrapped_ciphertext_length",
        ),
        sa.CheckConstraint(
            "purpose = 'ledgerbridge-artifact-v2'", name="encrypted_blob_purpose_fixed"
        ),
        sa.ForeignKeyConstraint(
            ["evidence_ref"], ["evidence_object.evidence_ref"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_blob_ref"], ["encrypted_blob_version.blob_ref"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["audit_event_id"], ["audit_event.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("blob_ref", name="pk_encrypted_blob_version"),
        sa.UniqueConstraint("object_ref", name="uq_encrypted_blob_object_ref"),
        sa.UniqueConstraint("ciphertext_sha256", name="uq_encrypted_blob_ciphertext_sha256"),
        sa.UniqueConstraint("storage_key", name="uq_encrypted_blob_storage_key"),
        sa.UniqueConstraint("audit_event_id", name="uq_encrypted_blob_audit_event"),
    )
    op.create_index(
        "ix_encrypted_blob_evidence_created",
        "encrypted_blob_version",
        ["evidence_ref", "created_at", "blob_ref"],
    )

    candidate_status = (
        "status IN ('INCOMPLETE','CONFLICTED','PENDING','CONFIRMED','IGNORED','SUPERSEDED')"
    )
    op.create_table(
        "candidate",
        sa.Column("id", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("short_id", sa.String(10), nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("contract_version", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_candidate_id", UUID, nullable=True),
        sa.CheckConstraint("short_id ~ '^C-[A-Z0-9]{4,8}$'", name="candidate_short_id_shape"),
        sa.CheckConstraint(
            "contract_version = 'ledgerbridge.candidate.v1'",
            name="candidate_contract_version_fixed",
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entity.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_candidate_id"], ["candidate.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_candidate"),
        sa.UniqueConstraint("short_id", name="uq_candidate_short_id"),
        sa.UniqueConstraint("id", "entity_id", name="uq_candidate_id_entity"),
    )
    op.create_index(
        "uq_candidate_supersedes",
        "candidate",
        ["supersedes_candidate_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_candidate_id IS NOT NULL"),
    )
    op.create_table(
        "candidate_source",
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("ingest_channel_id", sa.String(64), nullable=False),
        sa.Column("source_system_id", sa.String(64), nullable=False),
        sa.Column("source_event_ref", UUID, nullable=False),
        sa.Column("source_record_id", UUID, nullable=True),
        sa.Column("display_label", sa.String(100), nullable=False),
        sa.CheckConstraint(
            "btrim(display_label) <> ''", name="candidate_source_display_label_not_blank"
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ingest_channel_id"], ["ingest_channel.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_system_id"], ["source_system.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_record.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("candidate_id", name="pk_candidate_source"),
        sa.UniqueConstraint(
            "source_system_id", "source_event_ref", name="uq_candidate_source_event"
        ),
    )
    op.create_table(
        "candidate_revision",
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("business_unit_id", UUID, nullable=True),
        sa.Column("business_unit_ref_snapshot", sa.String(100), nullable=True),
        sa.Column("business_unit_label_snapshot", sa.String(200), nullable=True),
        sa.Column("category_id", UUID, nullable=True),
        sa.Column("category_code_snapshot", sa.String(100), nullable=True),
        sa.Column("category_label_snapshot", sa.String(200), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("accounting_month", sa.Date(), nullable=True),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("confidence_basis_points", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="candidate_revision_number_positive"),
        sa.CheckConstraint(candidate_status, name="candidate_revision_status_allowed"),
        sa.CheckConstraint(
            "amount_minor IS NULL OR amount_minor BETWEEN -9007199254740991 AND 9007199254740991",
            name="candidate_revision_amount_bounded",
        ),
        sa.CheckConstraint("currency = 'CNY'", name="candidate_revision_currency_fixed"),
        sa.CheckConstraint(
            "accounting_month IS NULL OR accounting_month = date_trunc('month', accounting_month)::date",
            name="candidate_revision_month_first_day",
        ),
        sa.CheckConstraint("btrim(summary) <> ''", name="candidate_revision_summary_not_blank"),
        sa.CheckConstraint(
            "confidence_basis_points BETWEEN 0 AND 10000",
            name="candidate_revision_confidence_bounded",
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name="candidate_revision_updated_after_created"
        ),
        sa.CheckConstraint(
            "(business_unit_id IS NULL) = (business_unit_ref_snapshot IS NULL AND business_unit_label_snapshot IS NULL)",
            name="candidate_revision_business_unit_snapshot_shape",
        ),
        sa.CheckConstraint(
            "(category_id IS NULL) = (category_code_snapshot IS NULL AND category_label_snapshot IS NULL)",
            name="candidate_revision_category_snapshot_shape",
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["business_unit_id"], ["business_unit.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["category_id"], ["reporting_category.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("candidate_id", "revision", name="pk_candidate_revision"),
    )
    op.create_index(
        "ix_candidate_revision_latest",
        "candidate_revision",
        ["candidate_id", sa.text("revision DESC")],
    )
    op.create_table(
        "candidate_blocker",
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("message", sa.String(300), nullable=False),
        sa.Column("field", sa.String(64), nullable=True),
        sa.Column("conflict_ref", UUID, nullable=True),
        sa.Column("evidence_ref", UUID, nullable=True),
        sa.CheckConstraint("ordinal >= 0", name="candidate_blocker_ordinal_nonnegative"),
        sa.CheckConstraint(
            "btrim(code) <> '' AND btrim(message) <> ''", name="candidate_blocker_text_not_blank"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "revision"],
            ["candidate_revision.candidate_id", "candidate_revision.revision"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_ref"], ["evidence_object.evidence_ref"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("candidate_id", "revision", "ordinal", name="pk_candidate_blocker"),
    )
    op.create_table(
        "candidate_event",
        sa.Column("event_ref", UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("command_fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("action", sa.String(32), nullable=True),
        sa.Column("from_revision", sa.Integer(), nullable=True),
        sa.Column("to_revision", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("actor_ref", sa.String(200), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("derived_candidate_id", UUID, nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("audit_event_id", UUID, nullable=False),
        sa.CheckConstraint(
            "octet_length(command_fingerprint) = 32", name="candidate_event_fingerprint_length"
        ),
        sa.CheckConstraint(
            "event_type IN ('CREATE','COMPLETE_FIELDS','RESOLVE_CONFLICT','CONFIRM','IGNORE','SUPERSEDE')",
            name="candidate_event_type_allowed",
        ),
        sa.CheckConstraint(
            "action IS NULL OR action IN ('COMPLETE_FIELDS','RESOLVE_CONFLICT','CONFIRM','IGNORE','SUPERSEDE')",
            name="candidate_event_action_allowed",
        ),
        sa.CheckConstraint(
            "to_revision >= 1 AND (from_revision IS NULL OR to_revision = from_revision + 1)",
            name="candidate_event_revision_sequence",
        ),
        sa.CheckConstraint(
            "btrim(actor_ref) <> '' AND btrim(reason) <> ''",
            name="candidate_event_actor_reason_not_blank",
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["derived_candidate_id"], ["candidate.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["audit_event_id"], ["audit_event.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_ref", name="pk_candidate_event"),
        sa.UniqueConstraint("operation_id", name="uq_candidate_event_operation"),
        sa.UniqueConstraint("audit_event_id", name="uq_candidate_event_audit_event"),
    )
    op.create_table(
        "candidate_field_change",
        sa.Column("event_ref", UUID, nullable=False),
        sa.Column("field", sa.String(64), nullable=False),
        sa.Column("previous_value", postgresql.JSONB, nullable=True),
        sa.Column("new_value", postgresql.JSONB, nullable=True),
        sa.CheckConstraint("btrim(field) <> ''", name="candidate_field_change_field_not_blank"),
        sa.ForeignKeyConstraint(["event_ref"], ["candidate_event.event_ref"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("event_ref", "field", name="pk_candidate_field_change"),
    )
    op.create_table(
        "candidate_conflict_resolution",
        sa.Column("event_ref", UUID, nullable=False),
        sa.Column("conflict_ref", UUID, nullable=False),
        sa.Column("resolution", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "btrim(resolution) <> ''", name="candidate_conflict_resolution_not_blank"
        ),
        sa.ForeignKeyConstraint(["event_ref"], ["candidate_event.event_ref"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint(
            "event_ref", "conflict_ref", name="pk_candidate_conflict_resolution"
        ),
    )
    op.create_table(
        "candidate_evidence",
        sa.Column("candidate_id", UUID, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("evidence_ref", UUID, nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("media_type_snapshot", sa.String(200), nullable=False),
        sa.Column("display_name_snapshot", sa.String(200), nullable=True),
        sa.Column("download_available", sa.Boolean(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="candidate_evidence_ordinal_nonnegative"),
        sa.CheckConstraint(
            "btrim(kind) <> '' AND btrim(media_type_snapshot) <> ''",
            name="candidate_evidence_text_not_blank",
        ),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidate.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["evidence_ref"], ["evidence_object.evidence_ref"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("candidate_id", "ordinal", name="pk_candidate_evidence"),
        sa.UniqueConstraint("candidate_id", "evidence_ref", name="uq_candidate_evidence_link"),
    )

    for table in (
        "business_unit",
        "reporting_category",
        "evidence_object",
        "encrypted_blob_version",
        "candidate",
        "candidate_source",
        "candidate_revision",
        "candidate_blocker",
        "candidate_event",
        "candidate_field_change",
        "candidate_conflict_resolution",
        "candidate_evidence",
    ):
        _append_only(table)

    op.execute(
        """
        CREATE FUNCTION public.r1_validate_candidate_scope()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            candidate_entity uuid;
            evidence_entity uuid;
            evidence_unit uuid;
            revision_unit uuid;
            candidate_unit uuid;
        BEGIN
            SELECT entity_id INTO candidate_entity FROM public.candidate WHERE id = NEW.candidate_id;
            SELECT entity_id, business_unit_id INTO evidence_entity, evidence_unit
              FROM public.evidence_object WHERE evidence_ref = NEW.evidence_ref;
            IF candidate_entity IS NULL OR evidence_entity IS NULL OR candidate_entity <> evidence_entity THEN
                RAISE EXCEPTION 'candidate and evidence must belong to the same entity'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT business_unit_id INTO revision_unit
              FROM public.candidate_revision WHERE candidate_id = NEW.candidate_id
              ORDER BY revision DESC LIMIT 1;
            IF revision_unit IS NOT NULL AND revision_unit <> evidence_unit THEN
                RAISE EXCEPTION 'assigned candidate evidence must share its business unit'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_candidate_evidence_scope
        AFTER INSERT ON public.candidate_evidence
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_candidate_scope();
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.r1_validate_revision_dimensions()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog
        AS $function$
        DECLARE
            entity_id uuid;
            unit_entity uuid;
            category_entity uuid;
            unit_ref text;
            unit_label text;
            category_code text;
            category_label text;
        BEGIN
            SELECT c.entity_id INTO entity_id FROM public.candidate AS c WHERE c.id = NEW.candidate_id;
            IF NEW.business_unit_id IS NOT NULL THEN
                SELECT b.entity_id, b.ref, b.label INTO unit_entity, unit_ref, unit_label
                  FROM public.business_unit AS b WHERE b.id = NEW.business_unit_id;
                IF unit_entity IS NULL OR unit_entity <> entity_id OR unit_ref <> NEW.business_unit_ref_snapshot OR unit_label <> NEW.business_unit_label_snapshot THEN
                    RAISE EXCEPTION 'candidate business unit scope or snapshot is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            IF NEW.category_id IS NOT NULL THEN
                SELECT r.entity_id, r.code, r.label INTO category_entity, category_code, category_label
                  FROM public.reporting_category AS r WHERE r.id = NEW.category_id;
                IF category_entity IS NULL OR category_entity <> entity_id OR category_code <> NEW.category_code_snapshot OR category_label <> NEW.category_label_snapshot THEN
                    RAISE EXCEPTION 'candidate reporting category scope or snapshot is invalid'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            RETURN NEW;
        END
        $function$;
        CREATE CONSTRAINT TRIGGER r1_candidate_revision_dimensions
        AFTER INSERT ON public.candidate_revision
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.r1_validate_revision_dimensions();
        """
    )

    # No runtime role receives access in Migration A.  The deployment may have
    # different roles present, so revoke conditionally without assuming them.
    op.execute(
        """
        DO $grant$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['ledgerbridge_app','ledgerbridge_api','ledgerbridge_worker','ledgerbridge_reader'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format('REVOKE ALL ON TABLE public.business_unit, public.reporting_category, public.evidence_object, public.encrypted_blob_version, public.candidate, public.candidate_source, public.candidate_revision, public.candidate_blocker, public.candidate_event, public.candidate_field_change, public.candidate_conflict_resolution, public.candidate_evidence FROM %I', role_name);
                END IF;
            END LOOP;
        END
        $grant$;
        REVOKE ALL ON TABLE public.business_unit, public.reporting_category, public.evidence_object, public.encrypted_blob_version, public.candidate, public.candidate_source, public.candidate_revision, public.candidate_blocker, public.candidate_event, public.candidate_field_change, public.candidate_conflict_resolution, public.candidate_evidence FROM PUBLIC;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = (
        "candidate_evidence",
        "candidate_conflict_resolution",
        "candidate_field_change",
        "candidate_event",
        "candidate_blocker",
        "candidate_revision",
        "candidate_source",
        "candidate",
        "encrypted_blob_version",
        "evidence_object",
        "reporting_category",
        "business_unit",
    )
    for table in tables:
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM public.{table})")).scalar_one():
            raise RuntimeError("R1 Candidate/evidence data prevents destructive downgrade")
    for table in tables:
        op.execute(f"DROP TRIGGER IF EXISTS r1_{table}_append_only_trigger ON public.{table}")
        op.execute(f"DROP FUNCTION IF EXISTS public.r1_{table}_append_only()")
    op.execute("DROP TRIGGER IF EXISTS r1_candidate_evidence_scope ON public.candidate_evidence")
    op.execute("DROP FUNCTION IF EXISTS public.r1_validate_candidate_scope()")
    op.execute(
        "DROP TRIGGER IF EXISTS r1_candidate_revision_dimensions ON public.candidate_revision"
    )
    op.execute("DROP FUNCTION IF EXISTS public.r1_validate_revision_dimensions()")
    op.drop_index("uq_candidate_supersedes", table_name="candidate")
    for table in tables:
        op.drop_table(table)
