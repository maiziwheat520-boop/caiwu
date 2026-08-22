"""Immutable evidence provenance and import-job models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from ledgerbridge.db import Base


class ImportJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class RawArtifact(Base):
    __tablename__ = "raw_artifact"
    __table_args__ = (
        CheckConstraint("octet_length(sha256) = 32", name="raw_artifact_sha256_length"),
        CheckConstraint("btrim(source) <> ''", name="raw_artifact_source_not_blank"),
        CheckConstraint(
            "btrim(original_filename) <> ''",
            name="raw_artifact_original_filename_not_blank",
        ),
        CheckConstraint("btrim(media_type) <> ''", name="raw_artifact_media_type_not_blank"),
        CheckConstraint("byte_size >= 0", name="raw_artifact_byte_size_nonnegative"),
        CheckConstraint(
            "storage_key ~ '^sha256/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$'",
            name="raw_artifact_storage_key_content_addressed",
        ),
        CheckConstraint(
            "storage_key = 'sha256/' || substr(encode(sha256, 'hex'), 1, 2) || '/' "
            "|| substr(encode(sha256, 'hex'), 3, 2) || '/' || encode(sha256, 'hex')",
            name="raw_artifact_storage_key_matches_sha256",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(200), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    storage_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    audit_event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("audit_event.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )


class ImportJob(Base):
    __tablename__ = "import_job"
    __table_args__ = (
        CheckConstraint("btrim(connector_name) <> ''", name="import_job_connector_not_blank"),
        CheckConstraint(
            "btrim(connector_version) <> ''", name="import_job_connector_version_not_blank"
        ),
        CheckConstraint(
            "parsed_count >= 0 AND created_count >= 0 AND duplicate_count >= 0",
            name="import_job_counts_nonnegative",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'",
            name="import_job_error_code_bounded",
        ),
        CheckConstraint(
            "diagnostic_summary IS NULL OR btrim(diagnostic_summary) <> ''",
            name="import_job_diagnostic_not_blank",
        ),
        CheckConstraint(
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
            name="import_job_state_timestamps",
        ),
        UniqueConstraint(
            "artifact_id",
            "connector_name",
            "connector_version",
            name="uq_import_job_artifact_connector_version",
        ),
        UniqueConstraint("id", "artifact_id", name="uq_import_job_id_artifact"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    artifact_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("raw_artifact.id", ondelete="RESTRICT"),
        nullable=False,
    )
    connector_name: Mapped[str] = mapped_column(String(100), nullable=False)
    connector_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[ImportJobStatus] = mapped_column(
        Enum(ImportJobStatus, name="import_job_status"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parsed_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    duplicate_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    diagnostic_summary: Mapped[str | None] = mapped_column(String(500))


class SourceRecord(Base):
    __tablename__ = "source_record"
    __table_args__ = (
        CheckConstraint("btrim(record_locator) <> ''", name="source_record_locator_not_blank"),
        CheckConstraint("btrim(source) <> ''", name="source_record_source_not_blank"),
        CheckConstraint(
            "btrim(parser_version) <> ''", name="source_record_parser_version_not_blank"
        ),
        CheckConstraint(
            "jsonb_typeof(raw_fields) = 'object'", name="source_record_raw_fields_object"
        ),
        CheckConstraint(
            "jsonb_typeof(normalized_fields) = 'object'",
            name="source_record_normalized_fields_object",
        ),
        CheckConstraint(
            "external_transaction_id IS NULL OR btrim(external_transaction_id) <> ''",
            name="source_record_external_id_not_blank",
        ),
        ForeignKeyConstraint(
            ["import_job_id", "artifact_id"],
            ["import_job.id", "import_job.artifact_id"],
            name="fk_source_record_job_artifact",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("artifact_id", "record_locator", name="uq_source_record_artifact_locator"),
        Index(
            "uq_source_record_external_identity",
            "account_id",
            "source",
            "external_transaction_id",
            unique=True,
            postgresql_where=text("account_id IS NOT NULL AND external_transaction_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    artifact_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("raw_artifact.id", ondelete="RESTRICT"),
        nullable=False,
    )
    import_job_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    record_locator: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    normalized_fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    account_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("account.id", ondelete="RESTRICT")
    )
    external_transaction_id: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
