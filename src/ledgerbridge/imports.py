"""Core-owned evidence ingestion and synthetic import orchestration."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ledgerbridge.artifacts import ArtifactIntegrityError, ArtifactStore, PublishedArtifact
from ledgerbridge.audit import append_audit_event
from ledgerbridge.connectors import (
    ArtifactMetadata,
    Connector,
    ConnectorContractError,
    DetectionResult,
    ParsedSourceRecord,
    validate_connector,
)
from ledgerbridge.models import ImportJob, ImportJobStatus, RawArtifact, SourceRecord

ROUTER_NAME = "ledgerbridge.router"
ROUTER_VERSION = "1"


class ImportIdentityConflict(RuntimeError):
    pass


class _ConcurrentArtifact(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IngestMetadata:
    source: str
    original_filename: str
    media_type: str

    def __post_init__(self) -> None:
        _validate_text("source", self.source, 200)
        _validate_text("original_filename", self.original_filename, 512)
        _validate_text("media_type", self.media_type, 200)


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    artifact_id: UUID
    job_id: UUID
    status: ImportJobStatus
    parsed_count: int
    created_count: int
    duplicate_count: int
    error_code: str | None
    artifact_created: bool


@dataclass(frozen=True, slots=True)
class _StoredArtifact:
    id: UUID
    source: str
    original_filename: str
    media_type: str
    published: PublishedArtifact
    created: bool


@dataclass(frozen=True, slots=True)
class _ConnectorBinding:
    connector: Connector
    name: str
    version: str


class EvidenceImporter:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        artifact_store: ArtifactStore,
        *,
        detection_prefix_bytes: int = 64 * 1024,
        max_records: int = 100_000,
    ) -> None:
        if detection_prefix_bytes <= 0 or max_records <= 0:
            raise ValueError("import limits must be positive")
        self._sessions = session_factory
        self._store = artifact_store
        self._detection_prefix_bytes = detection_prefix_bytes
        self._max_records = max_records

    def ingest_and_import(
        self,
        stream: BinaryIO,
        metadata: IngestMetadata,
        connectors: Sequence[Connector],
        *,
        actor: str,
        reason: str,
    ) -> ImportOutcome:
        published = self._store.publish(stream)
        artifact = self._ensure_artifact(published, metadata, actor=actor, reason=reason)
        try:
            connector_bindings = self._validate_connector_set(connectors)
            prefix = self._store.read_prefix(artifact.published, self._detection_prefix_bytes)
            artifact_metadata = ArtifactMetadata(
                source=artifact.source,
                original_filename=artifact.original_filename,
                media_type=artifact.media_type,
                byte_size=artifact.published.byte_size,
                sha256_hex=artifact.published.sha256_hex,
            )
            matches, ambiguous = self._detect(connector_bindings, artifact_metadata, prefix)
        except Exception:
            return self._route_terminal(
                artifact,
                ImportJobStatus.FAILED,
                "DETECTION_ERROR",
                "connector detection failed",
                actor=actor,
                reason=reason,
            )

        if ambiguous or len(matches) > 1:
            return self._route_terminal(
                artifact,
                ImportJobStatus.NEEDS_REVIEW,
                "AMBIGUOUS_CONNECTOR",
                "connector selection requires review",
                actor=actor,
                reason=reason,
            )
        if not matches:
            return self._route_terminal(
                artifact,
                ImportJobStatus.NEEDS_REVIEW,
                "NO_CONNECTOR",
                "no connector matched the evidence",
                actor=actor,
                reason=reason,
            )
        return self._run_connector(
            artifact,
            matches[0],
            actor=actor,
            reason=reason,
        )

    def _ensure_artifact(
        self,
        published: PublishedArtifact,
        metadata: IngestMetadata,
        *,
        actor: str,
        reason: str,
    ) -> _StoredArtifact:
        with self._sessions() as session:
            artifact = session.scalar(
                select(RawArtifact).where(RawArtifact.sha256 == published.sha256)
            )
            if artifact is not None:
                return self._stored_artifact(artifact, published, created=False)

        try:
            with self._sessions() as session, session.begin():
                audit_event_id = append_audit_event(
                    session,
                    actor=actor,
                    action="artifact.ingest",
                    reason=reason,
                    payload={
                        "sha256": published.sha256_hex,
                        "byte_size": published.byte_size,
                        "storage_key": published.storage_key,
                        "source": metadata.source,
                        "media_type": metadata.media_type,
                    },
                )
                artifact_id = session.execute(
                    postgresql_insert(RawArtifact)
                    .values(
                        sha256=published.sha256,
                        source=metadata.source,
                        original_filename=metadata.original_filename,
                        media_type=metadata.media_type,
                        byte_size=published.byte_size,
                        storage_key=published.storage_key,
                        audit_event_id=audit_event_id,
                    )
                    .on_conflict_do_nothing(index_elements=[RawArtifact.sha256])
                    .returning(RawArtifact.id)
                ).scalar_one_or_none()
                if artifact_id is None:
                    raise _ConcurrentArtifact
                artifact = session.get(RawArtifact, artifact_id)
                if artifact is None:
                    raise ArtifactIntegrityError("created artifact disappeared")
                return self._stored_artifact(artifact, published, created=True)
        except _ConcurrentArtifact:
            pass

        with self._sessions() as session:
            artifact = session.scalar(
                select(RawArtifact).where(RawArtifact.sha256 == published.sha256)
            )
            if artifact is None:
                raise ArtifactIntegrityError("artifact identity race did not converge")
            return self._stored_artifact(artifact, published, created=False)

    @staticmethod
    def _stored_artifact(
        artifact: RawArtifact,
        published: PublishedArtifact,
        *,
        created: bool,
    ) -> _StoredArtifact:
        if (
            artifact.sha256 != published.sha256
            or artifact.byte_size != published.byte_size
            or artifact.storage_key != published.storage_key
        ):
            raise ArtifactIntegrityError("artifact metadata conflicts with verified bytes")
        return _StoredArtifact(
            id=artifact.id,
            source=artifact.source,
            original_filename=artifact.original_filename,
            media_type=artifact.media_type,
            published=PublishedArtifact(
                sha256=artifact.sha256,
                byte_size=artifact.byte_size,
                storage_key=artifact.storage_key,
                created=published.created,
            ),
            created=created,
        )

    def _detect(
        self,
        connectors: Iterable[_ConnectorBinding],
        metadata: ArtifactMetadata,
        prefix: bytes,
    ) -> tuple[list[_ConnectorBinding], bool]:
        matches: list[_ConnectorBinding] = []
        ambiguous = False
        for binding in connectors:
            result = binding.connector.detect(metadata, prefix)
            if not isinstance(result, DetectionResult):
                raise ConnectorContractError("detect() must return DetectionResult")
            if result is DetectionResult.MATCH:
                matches.append(binding)
            elif result is DetectionResult.AMBIGUOUS:
                ambiguous = True
        return matches, ambiguous

    def _validate_connector_set(self, connectors: Sequence[Connector]) -> list[_ConnectorBinding]:
        identities: set[tuple[str, str]] = set()
        bindings: list[_ConnectorBinding] = []
        for connector in connectors:
            validate_connector(connector)
            identity = (connector.name, connector.version)
            if identity in identities:
                raise ConnectorContractError("connector identity must be unique")
            identities.add(identity)
            bindings.append(
                _ConnectorBinding(
                    connector=connector,
                    name=connector.name,
                    version=connector.version,
                )
            )
        return bindings

    def _route_terminal(
        self,
        artifact: _StoredArtifact,
        status: ImportJobStatus,
        error_code: str,
        summary: str,
        *,
        actor: str,
        reason: str,
    ) -> ImportOutcome:
        job_id = self._find_or_create_job(artifact.id, ROUTER_NAME, ROUTER_VERSION)
        return self._terminalize(
            artifact,
            job_id,
            status,
            error_code=error_code,
            summary=summary,
            parsed_count=0,
            created_count=0,
            duplicate_count=0,
            actor=actor,
            reason=reason,
        )

    def _run_connector(
        self,
        artifact: _StoredArtifact,
        binding: _ConnectorBinding,
        *,
        actor: str,
        reason: str,
    ) -> ImportOutcome:
        job_id = self._find_or_create_job(artifact.id, binding.name, binding.version)
        existing = self._job_outcome(artifact, job_id)
        if existing.status in {
            ImportJobStatus.SUCCEEDED,
            ImportJobStatus.FAILED,
            ImportJobStatus.NEEDS_REVIEW,
        }:
            return existing
        self._mark_running(job_id)
        try:
            with self._store.open_verified(artifact.published) as stream:
                parsed = self._validate_batch(
                    binding.name,
                    binding.version,
                    binding.connector.parse(stream),
                )
        except (ConnectorContractError, ArtifactIntegrityError, OSError):
            return self._terminalize(
                artifact,
                job_id,
                ImportJobStatus.FAILED,
                error_code="CONNECTOR_CONTRACT",
                summary="connector parse failed validation",
                parsed_count=0,
                created_count=0,
                duplicate_count=0,
                actor=actor,
                reason=reason,
            )
        except Exception:
            return self._terminalize(
                artifact,
                job_id,
                ImportJobStatus.FAILED,
                error_code="PARSE_ERROR",
                summary="connector parse failed",
                parsed_count=0,
                created_count=0,
                duplicate_count=0,
                actor=actor,
                reason=reason,
            )

        try:
            return self._publish_batch(
                artifact,
                job_id,
                parsed,
                actor=actor,
                reason=reason,
            )
        except (ImportIdentityConflict, IntegrityError):
            return self._terminalize(
                artifact,
                job_id,
                ImportJobStatus.NEEDS_REVIEW,
                error_code="IDENTITY_CONFLICT",
                summary="source identity requires review",
                parsed_count=len(parsed),
                created_count=0,
                duplicate_count=0,
                actor=actor,
                reason=reason,
            )

    def _validate_batch(
        self,
        connector_name: str,
        connector_version: str,
        values: Iterable[ParsedSourceRecord],
    ) -> list[ParsedSourceRecord]:
        parsed: list[ParsedSourceRecord] = []
        locators: set[str] = set()
        for value in values:
            if len(parsed) >= self._max_records:
                raise ConnectorContractError("connector exceeded the record limit")
            if not isinstance(value, ParsedSourceRecord):
                raise ConnectorContractError("parse() must yield ParsedSourceRecord")
            validated = ParsedSourceRecord(
                record_locator=value.record_locator,
                source=value.source,
                parser_version=value.parser_version,
                raw_fields=_detached_json_object("raw_fields", value.raw_fields),
                normalized_fields=_detached_json_object(
                    "normalized_fields", value.normalized_fields
                ),
                external_transaction_id=value.external_transaction_id,
            )
            if validated.source != connector_name or validated.parser_version != connector_version:
                raise ConnectorContractError("record provenance must match the connector")
            if validated.record_locator in locators:
                raise ConnectorContractError("batch record locators must be unique")
            locators.add(validated.record_locator)
            parsed.append(validated)
        return parsed

    def _find_or_create_job(self, artifact_id: UUID, name: str, version: str) -> UUID:
        with self._sessions() as session, session.begin():
            job_id = session.execute(
                postgresql_insert(ImportJob)
                .values(
                    artifact_id=artifact_id,
                    connector_name=name,
                    connector_version=version,
                    status=ImportJobStatus.PENDING,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        ImportJob.artifact_id,
                        ImportJob.connector_name,
                        ImportJob.connector_version,
                    ]
                )
                .returning(ImportJob.id)
            ).scalar_one_or_none()
            if job_id is None:
                job_id = session.scalar(
                    select(ImportJob.id).where(
                        ImportJob.artifact_id == artifact_id,
                        ImportJob.connector_name == name,
                        ImportJob.connector_version == version,
                    )
                )
            if job_id is None:
                raise RuntimeError("import job identity race did not converge")
            return job_id

    def _mark_running(self, job_id: UUID) -> None:
        with self._sessions() as session, session.begin():
            job = session.scalar(select(ImportJob).where(ImportJob.id == job_id).with_for_update())
            if job is None:
                raise RuntimeError("import job disappeared")
            if job.status is ImportJobStatus.PENDING:
                job.status = ImportJobStatus.RUNNING
                job.started_at = datetime.now(UTC)

    def _publish_batch(
        self,
        artifact: _StoredArtifact,
        job_id: UUID,
        parsed: Sequence[ParsedSourceRecord],
        *,
        actor: str,
        reason: str,
    ) -> ImportOutcome:
        created_count = 0
        duplicate_count = 0
        with self._sessions() as session, session.begin():
            job = session.scalar(select(ImportJob).where(ImportJob.id == job_id).with_for_update())
            if job is None:
                raise RuntimeError("import job disappeared")
            if job.status in {
                ImportJobStatus.SUCCEEDED,
                ImportJobStatus.FAILED,
                ImportJobStatus.NEEDS_REVIEW,
            }:
                return self._outcome(artifact, job)
            for value in parsed:
                existing = session.scalar(
                    select(SourceRecord).where(
                        SourceRecord.artifact_id == artifact.id,
                        SourceRecord.record_locator == value.record_locator,
                    )
                )
                if existing is not None:
                    self._assert_same_record(existing, value, job_id)
                    duplicate_count += 1
                    continue
                inserted = session.execute(
                    postgresql_insert(SourceRecord)
                    .values(
                        artifact_id=artifact.id,
                        import_job_id=job_id,
                        record_locator=value.record_locator,
                        source=value.source,
                        parser_version=value.parser_version,
                        raw_fields=dict(value.raw_fields),
                        normalized_fields=dict(value.normalized_fields),
                        external_transaction_id=value.external_transaction_id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[SourceRecord.artifact_id, SourceRecord.record_locator]
                    )
                    .returning(SourceRecord.id)
                ).scalar_one_or_none()
                if inserted is None:
                    existing = session.scalar(
                        select(SourceRecord).where(
                            SourceRecord.artifact_id == artifact.id,
                            SourceRecord.record_locator == value.record_locator,
                        )
                    )
                    if existing is None:
                        raise ImportIdentityConflict("source identity conflict")
                    self._assert_same_record(existing, value, job_id)
                    duplicate_count += 1
                else:
                    created_count += 1
            job.status = ImportJobStatus.SUCCEEDED
            job.completed_at = datetime.now(UTC)
            job.parsed_count = len(parsed)
            job.created_count = created_count
            job.duplicate_count = duplicate_count
            job.error_code = None
            job.diagnostic_summary = None
            append_audit_event(
                session,
                actor=actor,
                action="import.complete",
                reason=reason,
                payload=self._audit_payload(artifact, job),
            )
            session.flush()
            return self._outcome(artifact, job)

    def _terminalize(
        self,
        artifact: _StoredArtifact,
        job_id: UUID,
        status: ImportJobStatus,
        *,
        error_code: str,
        summary: str,
        parsed_count: int,
        created_count: int,
        duplicate_count: int,
        actor: str,
        reason: str,
    ) -> ImportOutcome:
        with self._sessions() as session, session.begin():
            job = session.scalar(select(ImportJob).where(ImportJob.id == job_id).with_for_update())
            if job is None:
                raise RuntimeError("import job disappeared")
            if job.status in {
                ImportJobStatus.SUCCEEDED,
                ImportJobStatus.FAILED,
                ImportJobStatus.NEEDS_REVIEW,
            }:
                return self._outcome(artifact, job)
            job.status = status
            job.completed_at = datetime.now(UTC)
            job.parsed_count = parsed_count
            job.created_count = created_count
            job.duplicate_count = duplicate_count
            job.error_code = error_code
            job.diagnostic_summary = summary
            append_audit_event(
                session,
                actor=actor,
                action="import.complete",
                reason=reason,
                payload=self._audit_payload(artifact, job),
            )
            session.flush()
            return self._outcome(artifact, job)

    def _job_outcome(self, artifact: _StoredArtifact, job_id: UUID) -> ImportOutcome:
        with self._sessions() as session:
            job = session.get(ImportJob, job_id)
            if job is None:
                raise RuntimeError("import job disappeared")
            return self._outcome(artifact, job)

    @staticmethod
    def _assert_same_record(
        existing: SourceRecord,
        value: ParsedSourceRecord,
        job_id: UUID,
    ) -> None:
        if (
            existing.import_job_id != job_id
            or existing.source != value.source
            or existing.parser_version != value.parser_version
            or existing.raw_fields != dict(value.raw_fields)
            or existing.normalized_fields != dict(value.normalized_fields)
            or existing.account_id is not None
            or existing.external_transaction_id != value.external_transaction_id
        ):
            raise ImportIdentityConflict("existing source identity has different provenance")

    @staticmethod
    def _audit_payload(artifact: _StoredArtifact, job: ImportJob) -> dict[str, object]:
        return {
            "artifact_id": str(artifact.id),
            "artifact_sha256": artifact.published.sha256_hex,
            "job_id": str(job.id),
            "connector_name": job.connector_name,
            "connector_version": job.connector_version,
            "status": job.status.value,
            "parsed_count": job.parsed_count,
            "created_count": job.created_count,
            "duplicate_count": job.duplicate_count,
            "error_code": job.error_code,
        }

    @staticmethod
    def _outcome(artifact: _StoredArtifact, job: ImportJob) -> ImportOutcome:
        return ImportOutcome(
            artifact_id=artifact.id,
            job_id=job.id,
            status=job.status,
            parsed_count=job.parsed_count,
            created_count=job.created_count,
            duplicate_count=job.duplicate_count,
            error_code=job.error_code,
            artifact_created=artifact.created,
        )


def _validate_text(field: str, value: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be non-blank and at most {maximum} characters")


def _detached_json_object(
    field: str,
    value: Mapping[str, object],
) -> dict[str, object]:
    try:
        encoded = json.dumps(
            dict(value),
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        decoded: object = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ConnectorContractError(f"{field} must contain JSON values only") from exc
    if not isinstance(decoded, dict):
        raise ConnectorContractError(f"{field} must be an object")
    return cast(dict[str, object], decoded)
