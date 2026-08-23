"""Core-owned evidence ingestion and synthetic import orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO, NoReturn, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ledgerbridge.artifacts import (
    ArtifactIntegrityError,
    ArtifactPublishedQuotaError,
    ArtifactQuotaError,
    ArtifactQuotaStateError,
    ArtifactStagingQuotaError,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactTooLargeError,
    PublishedArtifact,
)
from ledgerbridge.audit import append_audit_event
from ledgerbridge.connectors import (
    CANONICAL_SOURCE_PATTERN,
    MAX_JSON_BYTES,
    ArtifactMetadata,
    Connector,
    ConnectorContractError,
    DetectionResult,
    ParsedSourceRecord,
    validate_connector,
)
from ledgerbridge.models import (
    ImportJob,
    ImportJobStatus,
    IngestChannel,
    RawArtifact,
    SourceRecord,
    SourceSystem,
)
from ledgerbridge.runner_client import RunnerClientError
from ledgerbridge.text import contains_unstorable_text

ROUTER_NAME = "ledgerbridge.router"
ROUTER_VERSION = "1"
PROVENANCE_NAME = "ledgerbridge.provenance"
PROVENANCE_VERSION = "1"
logger = logging.getLogger(__name__)


class EvidenceIngestionError(RuntimeError):
    def __init__(self, error_code: str, summary: str) -> None:
        super().__init__(summary)
        self.error_code = error_code


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
        if CANONICAL_SOURCE_PATTERN.fullmatch(self.source) is None:
            raise ValueError("source must be a lowercase canonical identifier")
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
    provenance_conflict: bool


@dataclass(frozen=True, slots=True)
class _ConnectorBinding:
    connector: Connector
    name: str
    version: str
    source_system: str


class EvidenceImporter:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        artifact_store: ArtifactStore,
        *,
        detection_prefix_bytes: int = 64 * 1024,
        max_records: int = 100_000,
        production: bool = False,
    ) -> None:
        if detection_prefix_bytes <= 0 or max_records <= 0:
            raise ValueError("import limits must be positive")
        self._sessions = session_factory
        self._store = artifact_store
        self._detection_prefix_bytes = detection_prefix_bytes
        self._max_records = max_records
        self._production = production

    def ingest_and_import(
        self,
        stream: BinaryIO,
        metadata: IngestMetadata,
        connectors: Sequence[Connector],
        *,
        actor: str,
        reason: str,
    ) -> ImportOutcome:
        try:
            return self._ingest_and_import(
                stream,
                metadata,
                connectors,
                actor=actor,
                reason=reason,
            )
        except EvidenceIngestionError:
            raise
        except ArtifactIntegrityError as exc:
            raise EvidenceIngestionError(
                "EVIDENCE_INTEGRITY",
                "evidence integrity validation failed",
            ) from exc
        except SQLAlchemyError as exc:
            raise EvidenceIngestionError(
                "IMPORT_DATABASE",
                "evidence import state could not be recorded",
            ) from exc

    def validate_ingest_channel(self, source: str) -> None:
        if not self._ingest_channel_is_registered(source):
            raise EvidenceIngestionError(
                "INGEST_CHANNEL_UNKNOWN",
                "evidence ingestion channel is not registered",
            )

    def ingest_published(
        self,
        published: PublishedArtifact,
        metadata: IngestMetadata,
        connectors: Sequence[Connector],
        *,
        actor: str,
        reason: str,
    ) -> ImportOutcome:
        """Continue import from an already committed ArtifactStore result."""

        try:
            return self._ingest_and_import(
                None,
                metadata,
                connectors,
                actor=actor,
                reason=reason,
                published=published,
            )
        except EvidenceIngestionError:
            raise
        except ArtifactIntegrityError as exc:
            raise EvidenceIngestionError(
                "EVIDENCE_INTEGRITY",
                "evidence integrity validation failed",
            ) from exc
        except SQLAlchemyError as exc:
            raise EvidenceIngestionError(
                "IMPORT_DATABASE",
                "evidence import state could not be recorded",
            ) from exc

    def _ingest_and_import(
        self,
        stream: BinaryIO | None,
        metadata: IngestMetadata,
        connectors: Sequence[Connector],
        *,
        actor: str,
        reason: str,
        published: PublishedArtifact | None = None,
    ) -> ImportOutcome:
        self.validate_ingest_channel(metadata.source)
        if published is None:
            if stream is None:
                raise ValueError("an evidence stream is required before publication")
            published = self._publish_stream(
                stream,
                metadata,
                actor=actor,
                reason=reason,
            )

        artifact = self._ensure_artifact(published, metadata, actor=actor, reason=reason)
        if artifact.provenance_conflict:
            return self._route_terminal(
                artifact,
                ImportJobStatus.NEEDS_REVIEW,
                "PROVENANCE_CONFLICT",
                "evidence provenance requires review",
                actor=actor,
                reason=reason,
                connector_name=PROVENANCE_NAME,
                connector_version=PROVENANCE_VERSION,
            )
        try:
            connector_bindings = self._validate_connector_set(
                connectors,
                production=self._production,
            )
            prefix = self._store.read_prefix(artifact.published, self._detection_prefix_bytes)
            artifact_metadata = ArtifactMetadata(
                source=artifact.source,
                original_filename=artifact.original_filename,
                media_type=artifact.media_type,
                byte_size=artifact.published.byte_size,
                sha256_hex=artifact.published.sha256_hex,
            )
            matches, ambiguous = self._detect(
                connector_bindings,
                artifact_metadata,
                prefix,
                verified_artifact=artifact.published,
            )
        except ArtifactIntegrityError:
            return self._route_terminal(
                artifact,
                ImportJobStatus.FAILED,
                "EVIDENCE_INTEGRITY",
                "evidence integrity validation failed",
                actor=actor,
                reason=reason,
            )
        except OSError:
            return self._route_terminal(
                artifact,
                ImportJobStatus.FAILED,
                "EVIDENCE_IO",
                "evidence could not be read",
                actor=actor,
                reason=reason,
            )
        except ConnectorContractError:
            return self._route_terminal(
                artifact,
                ImportJobStatus.FAILED,
                "CONNECTOR_CONTRACT",
                "connector contract validation failed",
                actor=actor,
                reason=reason,
            )
        except RunnerClientError as exc:
            return self._route_terminal(
                artifact,
                ImportJobStatus.FAILED,
                exc.error_code,
                exc.summary,
                actor=actor,
                reason=reason,
            )
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

    def _publish_stream(
        self,
        stream: BinaryIO,
        metadata: IngestMetadata,
        *,
        actor: str,
        reason: str,
    ) -> PublishedArtifact:
        try:
            return self._store.publish(stream)
        except ArtifactPublishedQuotaError as exc:
            self._raise_quota_rejection(
                exc,
                error_code="ARTIFACT_TOTAL_QUOTA",
                summary="published artifact capacity is exhausted",
                ingest_channel=metadata.source,
                actor=actor,
                reason=reason,
            )
        except ArtifactStagingQuotaError as exc:
            self._raise_quota_rejection(
                exc,
                error_code="ARTIFACT_STAGING_QUOTA",
                summary="artifact staging capacity is exhausted",
                ingest_channel=metadata.source,
                actor=actor,
                reason=reason,
            )
        except ArtifactQuotaStateError as exc:
            self._raise_quota_rejection(
                exc,
                error_code="ARTIFACT_QUOTA_STATE",
                summary="artifact capacity could not be measured safely",
                ingest_channel=metadata.source,
                actor=actor,
                reason=reason,
            )
        except ArtifactTooLargeError as exc:
            raise EvidenceIngestionError(
                "EVIDENCE_LIMIT",
                "evidence exceeds the configured ingestion limit",
            ) from exc
        except ArtifactIntegrityError as exc:
            raise EvidenceIngestionError(
                "EVIDENCE_INTEGRITY",
                "evidence storage integrity validation failed",
            ) from exc
        except (ArtifactStoreError, OSError) as exc:
            raise EvidenceIngestionError(
                "EVIDENCE_STORAGE",
                "evidence could not be durably published",
            ) from exc

    def _raise_quota_rejection(
        self,
        error: ArtifactQuotaError | ArtifactQuotaStateError,
        *,
        error_code: str,
        summary: str,
        ingest_channel: str,
        actor: str,
        reason: str,
    ) -> NoReturn:
        intake_id = str(uuid4())
        payload: dict[str, object] = {
            "intake_id": intake_id,
            "error_code": error_code,
            "quota_kind": error.quota_kind,
            "ingest_channel": ingest_channel,
            "limit_bytes": getattr(error, "limit", None),
            "observed_bytes": getattr(error, "observed", None),
            "requested_bytes": getattr(error, "requested", None),
        }
        audit_recorded = False
        try:
            with self._sessions() as session, session.begin():
                append_audit_event(
                    session,
                    actor=actor,
                    action="artifact.ingest_rejected",
                    reason=reason,
                    payload=payload,
                )
            audit_recorded = True
        except SQLAlchemyError:
            audit_recorded = False
        logger.error(
            "artifact ingestion rejected by quota control",
            extra=payload | {"audit_recorded": audit_recorded},
        )
        raise EvidenceIngestionError(error_code, summary) from error

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
                return self._stored_artifact(artifact, published, metadata, created=False)

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
                        "original_filename_sha256": hashlib.sha256(
                            metadata.original_filename.encode("utf-8")
                        ).hexdigest(),
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
                return self._stored_artifact(artifact, published, metadata, created=True)
        except _ConcurrentArtifact:
            pass

        with self._sessions() as session:
            artifact = session.scalar(
                select(RawArtifact).where(RawArtifact.sha256 == published.sha256)
            )
            if artifact is None:
                raise ArtifactIntegrityError("artifact identity race did not converge")
            return self._stored_artifact(artifact, published, metadata, created=False)

    @staticmethod
    def _stored_artifact(
        artifact: RawArtifact,
        published: PublishedArtifact,
        metadata: IngestMetadata,
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
            provenance_conflict=(
                artifact.source != metadata.source or artifact.media_type != metadata.media_type
            ),
        )

    def _detect(
        self,
        connectors: Iterable[_ConnectorBinding],
        metadata: ArtifactMetadata,
        prefix: bytes,
        *,
        verified_artifact: PublishedArtifact | None = None,
    ) -> tuple[list[_ConnectorBinding], bool]:
        matches: list[_ConnectorBinding] = []
        ambiguous = False
        for binding in connectors:
            detect_verified = getattr(binding.connector, "detect_verified", None)
            if callable(detect_verified):
                if verified_artifact is None:
                    raise ConnectorContractError("runner detection requires a verified artifact")
                with self._store.open_verified(verified_artifact) as verified_stream:
                    result = detect_verified(metadata, verified_stream)
            else:
                result = binding.connector.detect(metadata, prefix)
            if not isinstance(result, DetectionResult):
                raise ConnectorContractError("detect() must return DetectionResult")
            if result is DetectionResult.MATCH:
                matches.append(binding)
            elif result is DetectionResult.AMBIGUOUS:
                ambiguous = True
        return matches, ambiguous

    def _validate_connector_set(
        self,
        connectors: Sequence[Connector],
        *,
        production: bool | None = None,
    ) -> list[_ConnectorBinding]:
        identities: set[tuple[str, str]] = set()
        bindings: list[_ConnectorBinding] = []
        if production is None:
            production = self._production
        for connector in connectors:
            name, version, source_system = validate_connector(
                connector,
                production=production,
            )
            identity = (name, version)
            if identity in identities:
                raise ConnectorContractError("connector identity must be unique")
            identities.add(identity)
            bindings.append(
                _ConnectorBinding(
                    connector=connector,
                    name=name,
                    version=version,
                    source_system=source_system,
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
        connector_name: str = ROUTER_NAME,
        connector_version: str = ROUTER_VERSION,
    ) -> ImportOutcome:
        job_id = self._find_or_create_job(
            artifact.id,
            connector_name,
            connector_version,
        )
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
        # Do not persist an unregistered source_system: Phase 3 binds the
        # connector provenance to the immutable source_system registry via a
        # foreign key.  Route this contract failure through the internal
        # router job, whose identity intentionally has no external source
        # system, so the caller still receives a durable terminal outcome.
        if not self._source_system_is_registered(binding.source_system):
            return self._route_terminal(
                artifact,
                ImportJobStatus.FAILED,
                error_code="CONNECTOR_CONTRACT",
                summary="connector source system is not registered",
                actor=actor,
                reason=reason,
            )
        job_id = self._find_or_create_job(
            artifact.id,
            binding.name,
            binding.version,
            source_system=binding.source_system,
        )
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
                    binding.source_system,
                    binding.version,
                    binding.connector.parse(stream),
                )
        except ArtifactIntegrityError:
            return self._terminalize(
                artifact,
                job_id,
                ImportJobStatus.FAILED,
                error_code="EVIDENCE_INTEGRITY",
                summary="evidence integrity validation failed",
                parsed_count=0,
                created_count=0,
                duplicate_count=0,
                actor=actor,
                reason=reason,
            )
        except OSError:
            return self._terminalize(
                artifact,
                job_id,
                ImportJobStatus.FAILED,
                error_code="EVIDENCE_IO",
                summary="evidence could not be read",
                parsed_count=0,
                created_count=0,
                duplicate_count=0,
                actor=actor,
                reason=reason,
            )
        except ConnectorContractError:
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
        except RunnerClientError as exc:
            return self._terminalize(
                artifact,
                job_id,
                ImportJobStatus.FAILED,
                error_code=exc.error_code,
                summary=exc.summary,
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
        source_system: str,
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
            if validated.source != source_system or validated.parser_version != connector_version:
                raise ConnectorContractError("record provenance must match the connector")
            if validated.record_locator in locators:
                raise ConnectorContractError("batch record locators must be unique")
            locators.add(validated.record_locator)
            parsed.append(validated)
        return parsed

    def _source_system_is_registered(self, source_system: str) -> bool:
        with self._sessions() as session:
            return session.get(SourceSystem, source_system) is not None

    def _ingest_channel_is_registered(self, ingest_channel: str) -> bool:
        with self._sessions() as session:
            return session.get(IngestChannel, ingest_channel) is not None

    def _find_or_create_job(
        self,
        artifact_id: UUID,
        name: str,
        version: str,
        *,
        source_system: str | None = None,
    ) -> UUID:
        with self._sessions() as session, session.begin():
            job_id = session.execute(
                postgresql_insert(ImportJob)
                .values(
                    artifact_id=artifact_id,
                    connector_name=name,
                    connector_version=version,
                    source_system=source_system,
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
            audit_event_id = append_audit_event(
                session,
                actor=actor,
                action="import.complete",
                reason=reason,
                payload=self._audit_payload(artifact, job),
            )
            job.terminal_audit_event_id = audit_event_id
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
            audit_event_id = append_audit_event(
                session,
                actor=actor,
                action="import.complete",
                reason=reason,
                payload=self._audit_payload(artifact, job),
            )
            job.terminal_audit_event_id = audit_event_id
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
            "source_system": job.source_system,
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
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or contains_unstorable_text(value)
    ):
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
    except (RecursionError, TypeError, ValueError) as exc:
        raise ConnectorContractError(f"{field} must contain JSON values only") from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ConnectorContractError(f"{field} must serialize to at most {MAX_JSON_BYTES} bytes")
    if not isinstance(decoded, dict):
        raise ConnectorContractError(f"{field} must be an object")
    return cast(dict[str, object], decoded)
