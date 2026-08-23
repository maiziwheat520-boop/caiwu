"""Durable worker-owned evidence import dispatch operations."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ledgerbridge.artifacts import PublishedArtifact
from ledgerbridge.audit import append_audit_event
from ledgerbridge.models import (
    AuditEvent,
    DispatchState,
    ImportDispatch,
    ImportJob,
    ImportJobStatus,
    IngestChannel,
    RawArtifact,
)
from ledgerbridge.text import contains_unstorable_text

MAX_MANIFEST_GENERATION: Final = 100
MAX_WORKER_ID: Final = 128
MAX_ATTEMPTS: Final = 16
MAX_ERROR_CODE: Final = 64
MAX_SUMMARY: Final = 500
_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class DispatchError(RuntimeError):
    """Base class for durable dispatch failures."""


class DispatchConflict(DispatchError):
    """The requested operation conflicts with an immutable existing identity."""


class DispatchNotFound(DispatchError):
    """The operation is absent or not owned by the supplied principal."""


class DispatchClaimLost(DispatchError):
    """A worker update lost its lease to another worker or a terminal state."""


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    artifact_id: UUID
    ingest_channel: str
    manifest_generation: str
    manifest_digest: bytes
    actor: str
    reason: str


@dataclass(frozen=True, slots=True)
class DispatchSnapshot:
    operation_id: UUID
    artifact_id: UUID
    state: DispatchState
    job_id: UUID | None
    result_status: ImportJobStatus | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class DispatchClaim:
    operation_id: UUID
    artifact_id: UUID
    ingest_channel: str
    manifest_generation: str
    manifest_digest: bytes
    byte_size: int
    storage_key: str
    original_filename: str
    media_type: str
    attempt_count: int
    lease_owner: str
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class DispatchPrincipal:
    actor: str
    reason: str


class DispatchService:
    """Own enqueue, claim and terminal state transitions for import dispatch."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: int = 120,
        max_attempts: int = 5,
    ) -> None:
        if lease_seconds <= 0 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        if max_attempts <= 0 or max_attempts > MAX_ATTEMPTS:
            raise ValueError(f"max_attempts must be between 1 and {MAX_ATTEMPTS}")
        self._sessions = session_factory
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts

    def enqueue(self, request: DispatchRequest) -> DispatchSnapshot:
        _validate_request(request)
        with self._sessions() as session, session.begin():
            artifact = session.get(RawArtifact, request.artifact_id)
            if artifact is None:
                raise DispatchError("artifact does not exist")
            if session.get(IngestChannel, request.ingest_channel) is None:
                raise DispatchError("ingest channel is not registered")

            existing = session.scalar(
                select(ImportDispatch)
                .where(
                    ImportDispatch.artifact_id == request.artifact_id,
                    ImportDispatch.ingest_channel == request.ingest_channel,
                    ImportDispatch.manifest_generation == request.manifest_generation,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.manifest_digest != request.manifest_digest:
                    raise DispatchConflict("manifest digest conflicts with existing dispatch")
                return self._snapshot(session, existing)

            operation_id = uuid4()
            dispatch: ImportDispatch | None = None
            try:
                with session.begin_nested():
                    audit_event_id = append_audit_event(
                        session,
                        actor=request.actor,
                        action="import.dispatch.accepted",
                        reason=request.reason,
                        payload={
                            "operation_id": str(operation_id),
                            "artifact_id": str(request.artifact_id),
                            "ingest_channel": request.ingest_channel,
                            "manifest_generation": request.manifest_generation,
                            "manifest_digest": request.manifest_digest.hex(),
                        },
                    )
                    dispatch = ImportDispatch(
                        id=operation_id,
                        artifact_id=request.artifact_id,
                        ingest_channel=request.ingest_channel,
                        accepted_audit_event_id=audit_event_id,
                        manifest_generation=request.manifest_generation,
                        manifest_digest=request.manifest_digest,
                        state=DispatchState.PENDING,
                    )
                    session.add(dispatch)
                    session.flush()
            except IntegrityError as exc:
                existing = session.scalar(
                    select(ImportDispatch).where(
                        ImportDispatch.artifact_id == request.artifact_id,
                        ImportDispatch.ingest_channel == request.ingest_channel,
                        ImportDispatch.manifest_generation == request.manifest_generation,
                    )
                )
                if existing is None:
                    raise DispatchError("dispatch identity race did not converge") from exc
                if existing.manifest_digest != request.manifest_digest:
                    raise DispatchConflict(
                        "manifest digest conflicts with existing dispatch"
                    ) from exc
                return self._snapshot(session, existing)
            return self._snapshot(session, dispatch)

    def enqueue_published(
        self,
        published: PublishedArtifact,
        *,
        ingest_channel: str,
        original_filename: str,
        media_type: str,
        manifest_generation: str,
        manifest_digest: bytes,
        actor: str,
        reason: str,
    ) -> DispatchSnapshot:
        """Bind a verified artifact and enqueue it in one database transaction."""

        _validate_published_metadata(
            published,
            ingest_channel=ingest_channel,
            original_filename=original_filename,
            media_type=media_type,
            manifest_generation=manifest_generation,
            manifest_digest=manifest_digest,
            actor=actor,
            reason=reason,
        )
        with self._sessions() as session, session.begin():
            if session.get(IngestChannel, ingest_channel) is None:
                raise DispatchError("ingest channel is not registered")
            artifact = session.scalar(
                select(RawArtifact).where(RawArtifact.sha256 == published.sha256)
            )
            if artifact is None:
                try:
                    with session.begin_nested():
                        artifact_audit_id = append_audit_event(
                            session,
                            actor=actor,
                            action="artifact.ingest",
                            reason=reason,
                            payload={
                                "sha256": published.sha256_hex,
                                "byte_size": published.byte_size,
                                "storage_key": published.storage_key,
                                "source": ingest_channel,
                                "original_filename_sha256": hashlib.sha256(
                                    original_filename.encode("utf-8")
                                ).hexdigest(),
                                "media_type": media_type,
                            },
                        )
                        artifact_id = session.execute(
                            postgresql_insert(RawArtifact)
                            .values(
                                sha256=published.sha256,
                                source=ingest_channel,
                                original_filename=original_filename,
                                media_type=media_type,
                                byte_size=published.byte_size,
                                storage_key=published.storage_key,
                                audit_event_id=artifact_audit_id,
                            )
                            .on_conflict_do_nothing(index_elements=[RawArtifact.sha256])
                            .returning(RawArtifact.id)
                        ).scalar_one_or_none()
                        if artifact_id is None:
                            raise DispatchConflict("artifact identity race")
                        artifact = session.get(RawArtifact, artifact_id)
                        if artifact is None:
                            raise DispatchError("created artifact disappeared")
                except DispatchConflict:
                    artifact = session.scalar(
                        select(RawArtifact).where(RawArtifact.sha256 == published.sha256)
                    )
                    if artifact is None:
                        raise DispatchError("artifact identity race did not converge") from None
            if (
                artifact.sha256 != published.sha256
                or artifact.byte_size != published.byte_size
                or artifact.storage_key != published.storage_key
            ):
                raise DispatchConflict("artifact metadata conflicts with verified bytes")
            if artifact.source != ingest_channel or artifact.media_type != media_type:
                raise DispatchConflict("artifact provenance conflicts with existing bytes")

            existing = session.scalar(
                select(ImportDispatch)
                .where(
                    ImportDispatch.artifact_id == artifact.id,
                    ImportDispatch.ingest_channel == ingest_channel,
                    ImportDispatch.manifest_generation == manifest_generation,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.manifest_digest != manifest_digest:
                    raise DispatchConflict("manifest digest conflicts with existing dispatch")
                return self._snapshot(session, existing)

            operation_id = uuid4()
            try:
                with session.begin_nested():
                    accepted_audit_id = append_audit_event(
                        session,
                        actor=actor,
                        action="import.dispatch.accepted",
                        reason=reason,
                        payload={
                            "operation_id": str(operation_id),
                            "artifact_id": str(artifact.id),
                            "ingest_channel": ingest_channel,
                            "manifest_generation": manifest_generation,
                            "manifest_digest": manifest_digest.hex(),
                        },
                    )
                    dispatch = ImportDispatch(
                        id=operation_id,
                        artifact_id=artifact.id,
                        ingest_channel=ingest_channel,
                        accepted_audit_event_id=accepted_audit_id,
                        manifest_generation=manifest_generation,
                        manifest_digest=manifest_digest,
                        state=DispatchState.PENDING,
                    )
                    session.add(dispatch)
                    session.flush()
            except IntegrityError as exc:
                existing = session.scalar(
                    select(ImportDispatch).where(
                        ImportDispatch.artifact_id == artifact.id,
                        ImportDispatch.ingest_channel == ingest_channel,
                        ImportDispatch.manifest_generation == manifest_generation,
                    )
                )
                if existing is None:
                    raise DispatchError("dispatch identity race did not converge") from exc
                if existing.manifest_digest != manifest_digest:
                    raise DispatchConflict(
                        "manifest digest conflicts with existing dispatch"
                    ) from exc
                return self._snapshot(session, existing)
            if dispatch is None:
                raise DispatchError("dispatch creation failed")
            return self._snapshot(session, dispatch)

    def get_for_actor(self, operation_id: UUID, actor: str) -> DispatchSnapshot:
        _validate_actor(actor)
        with self._sessions() as session:
            row = session.execute(
                select(ImportDispatch, AuditEvent.actor, ImportJob.status)
                .join(
                    AuditEvent,
                    AuditEvent.id == ImportDispatch.accepted_audit_event_id,
                )
                .outerjoin(
                    ImportJob,
                    (ImportJob.id == ImportDispatch.import_job_id)
                    & (ImportJob.artifact_id == ImportDispatch.artifact_id),
                )
                .where(ImportDispatch.id == operation_id)
            ).one_or_none()
            if row is None or row[1] != actor:
                raise DispatchNotFound("dispatch operation was not found")
            dispatch, _stored_actor, result_status = row
            return DispatchSnapshot(
                operation_id=dispatch.id,
                artifact_id=dispatch.artifact_id,
                state=dispatch.state,
                job_id=dispatch.import_job_id,
                result_status=result_status,
                error_code=dispatch.error_code,
            )

    def validate_ingest_channel(self, ingest_channel: str) -> None:
        _validate_channel(ingest_channel)
        with self._sessions() as session:
            if session.get(IngestChannel, ingest_channel) is None:
                raise DispatchError("ingest channel is not registered")

    def acceptance_principal(self, operation_id: UUID) -> DispatchPrincipal:
        with self._sessions() as session:
            row = session.execute(
                select(AuditEvent.actor, AuditEvent.reason)
                .join(
                    ImportDispatch,
                    ImportDispatch.accepted_audit_event_id == AuditEvent.id,
                )
                .where(ImportDispatch.id == operation_id)
            ).one_or_none()
            if row is None:
                raise DispatchNotFound("dispatch operation was not found")
            return DispatchPrincipal(actor=row[0], reason=row[1])

    def recover_expired_leases(self, *, now: datetime | None = None, limit: int = 100) -> int:
        current = _utc_now(now)
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        recovered = 0
        with self._sessions() as session, session.begin():
            rows: Sequence[ImportDispatch] = session.scalars(
                select(ImportDispatch)
                .where(
                    ImportDispatch.state == DispatchState.RUNNING,
                    ImportDispatch.lease_until <= current,
                )
                .order_by(ImportDispatch.lease_until, ImportDispatch.id)
                .with_for_update(of=ImportDispatch, skip_locked=True)
                .limit(limit)
            ).all()
            for dispatch in rows:
                if dispatch.attempt_count >= self._max_attempts:
                    dispatch.state = DispatchState.FAILED
                    dispatch.completed_at = current
                    dispatch.error_code = "DISPATCH_ATTEMPTS_EXHAUSTED"
                    dispatch.diagnostic_summary = "dispatch lease attempts exhausted"
                else:
                    dispatch.state = DispatchState.RETRY_WAIT
                    dispatch.available_at = current
                    dispatch.error_code = "LEASE_EXPIRED"
                    dispatch.diagnostic_summary = "dispatch lease expired"
                dispatch.lease_owner = None
                dispatch.lease_until = None
                recovered += 1
            session.flush()
        return recovered

    def claim_next(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> DispatchClaim | None:
        _validate_worker_id(worker_id)
        current = _utc_now(now)
        lease_until = current + timedelta(seconds=self._lease_seconds)
        with self._sessions() as session, session.begin():
            row = session.execute(
                select(ImportDispatch)
                .join(RawArtifact, RawArtifact.id == ImportDispatch.artifact_id)
                .where(
                    ImportDispatch.state.in_((DispatchState.PENDING, DispatchState.RETRY_WAIT)),
                    ImportDispatch.available_at <= current,
                )
                .order_by(ImportDispatch.available_at, ImportDispatch.created_at, ImportDispatch.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).one_or_none()
            if row is None:
                return None
            dispatch = row[0]
            artifact = session.get(RawArtifact, dispatch.artifact_id)
            if artifact is None:
                raise DispatchError("dispatch artifact disappeared")
            if dispatch.attempt_count >= self._max_attempts:
                dispatch.state = DispatchState.FAILED
                dispatch.completed_at = current
                dispatch.error_code = "DISPATCH_ATTEMPTS_EXHAUSTED"
                dispatch.diagnostic_summary = "dispatch attempts exhausted"
                session.flush()
                return None
            dispatch.state = DispatchState.RUNNING
            dispatch.attempt_count += 1
            dispatch.lease_owner = worker_id
            dispatch.lease_until = lease_until
            dispatch.error_code = None
            dispatch.diagnostic_summary = None
            if dispatch.started_at is None:
                dispatch.started_at = current
            session.flush()
            return DispatchClaim(
                operation_id=dispatch.id,
                artifact_id=dispatch.artifact_id,
                ingest_channel=dispatch.ingest_channel,
                manifest_generation=dispatch.manifest_generation,
                manifest_digest=dispatch.manifest_digest,
                byte_size=artifact.byte_size,
                storage_key=artifact.storage_key,
                original_filename=artifact.original_filename,
                media_type=artifact.media_type,
                attempt_count=dispatch.attempt_count,
                lease_owner=worker_id,
                lease_until=lease_until,
            )

    def renew_lease(
        self,
        operation_id: UUID,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> datetime:
        _validate_worker_id(worker_id)
        current = _utc_now(now)
        lease_until = current + timedelta(seconds=self._lease_seconds)
        with self._sessions() as session, session.begin():
            dispatch = session.scalar(
                select(ImportDispatch).where(ImportDispatch.id == operation_id).with_for_update()
            )
            if (
                dispatch is None
                or dispatch.state is not DispatchState.RUNNING
                or dispatch.lease_owner != worker_id
                or dispatch.lease_until is None
                or dispatch.lease_until <= current
            ):
                raise DispatchClaimLost("dispatch lease is no longer owned")
            dispatch.lease_until = lease_until
            session.flush()
        return lease_until

    def mark_retry(
        self,
        operation_id: UUID,
        worker_id: str,
        *,
        available_at: datetime,
        error_code: str,
        summary: str,
        now: datetime | None = None,
    ) -> DispatchState:
        _validate_worker_id(worker_id)
        _validate_error(error_code, summary)
        current = _utc_now(now)
        if available_at < current:
            raise ValueError("available_at cannot be in the past")
        with self._sessions() as session, session.begin():
            dispatch = self._owned_running(session, operation_id, worker_id, current)
            if dispatch.attempt_count >= self._max_attempts:
                dispatch.state = DispatchState.FAILED
                dispatch.completed_at = current
                dispatch.error_code = "DISPATCH_ATTEMPTS_EXHAUSTED"
                dispatch.diagnostic_summary = "dispatch attempts exhausted"
                dispatch.lease_owner = None
                dispatch.lease_until = None
                session.flush()
                return dispatch.state
            dispatch.state = DispatchState.RETRY_WAIT
            dispatch.available_at = available_at
            dispatch.lease_owner = None
            dispatch.lease_until = None
            dispatch.error_code = error_code
            dispatch.diagnostic_summary = summary
            session.flush()
            return dispatch.state

    def complete(
        self,
        operation_id: UUID,
        worker_id: str,
        *,
        result_status: ImportJobStatus,
        import_job_id: UUID,
        now: datetime | None = None,
        error_code: str | None = None,
        summary: str | None = None,
    ) -> DispatchState:
        _validate_worker_id(worker_id)
        if result_status is ImportJobStatus.SUCCEEDED:
            if error_code is not None or summary is not None:
                raise ValueError("successful dispatch cannot carry an error")
            target = DispatchState.SUCCEEDED
        elif result_status in {ImportJobStatus.FAILED, ImportJobStatus.NEEDS_REVIEW}:
            if result_status is ImportJobStatus.FAILED:
                if error_code is None or summary is None:
                    raise ValueError("failed dispatch requires an error")
                _validate_error(error_code, summary)
                target = DispatchState.FAILED
            elif error_code is not None or summary is not None:
                raise ValueError("review dispatch does not carry a dispatch error")
            else:
                target = DispatchState.SUCCEEDED
        else:
            raise ValueError("dispatch completion requires a terminal import status")
        current = _utc_now(now)
        with self._sessions() as session, session.begin():
            dispatch = self._owned_running(session, operation_id, worker_id, current)
            dispatch.state = target
            dispatch.completed_at = current
            dispatch.import_job_id = import_job_id
            dispatch.error_code = error_code
            dispatch.diagnostic_summary = summary
            dispatch.lease_owner = None
            dispatch.lease_until = None
            session.flush()
            return dispatch.state

    def fail(
        self,
        operation_id: UUID,
        worker_id: str,
        *,
        error_code: str,
        summary: str,
        now: datetime | None = None,
        import_job_id: UUID | None = None,
    ) -> DispatchState:
        _validate_worker_id(worker_id)
        _validate_error(error_code, summary)
        current = _utc_now(now)
        with self._sessions() as session, session.begin():
            dispatch = self._owned_running(session, operation_id, worker_id, current)
            dispatch.state = DispatchState.FAILED
            dispatch.completed_at = current
            dispatch.import_job_id = import_job_id
            dispatch.error_code = error_code
            dispatch.diagnostic_summary = summary
            dispatch.lease_owner = None
            dispatch.lease_until = None
            session.flush()
            return dispatch.state

    @staticmethod
    def _owned_running(
        session: Session,
        operation_id: UUID,
        worker_id: str,
        now: datetime,
    ) -> ImportDispatch:
        dispatch = session.scalar(
            select(ImportDispatch).where(ImportDispatch.id == operation_id).with_for_update()
        )
        if (
            dispatch is None
            or dispatch.state is not DispatchState.RUNNING
            or dispatch.lease_owner != worker_id
            or dispatch.lease_until is None
            or dispatch.lease_until <= now
        ):
            raise DispatchClaimLost("dispatch lease is no longer owned")
        return dispatch

    @staticmethod
    def _snapshot(session: Session, dispatch: ImportDispatch) -> DispatchSnapshot:
        result_status = None
        if dispatch.import_job_id is not None:
            result_status = session.scalar(
                select(ImportJob.status).where(
                    ImportJob.id == dispatch.import_job_id,
                    ImportJob.artifact_id == dispatch.artifact_id,
                )
            )
        return DispatchSnapshot(
            operation_id=dispatch.id,
            artifact_id=dispatch.artifact_id,
            state=dispatch.state,
            job_id=dispatch.import_job_id,
            result_status=result_status,
            error_code=dispatch.error_code,
        )


def _validate_request(request: DispatchRequest) -> None:
    if not isinstance(request.artifact_id, UUID):
        raise ValueError("artifact_id must be a UUID")
    _validate_channel(request.ingest_channel)
    if (
        not request.manifest_generation
        or len(request.manifest_generation) > MAX_MANIFEST_GENERATION
    ):
        raise ValueError("manifest_generation is invalid")
    if len(request.manifest_digest) != 32:
        raise ValueError("manifest_digest must contain 32 bytes")
    _validate_actor(request.actor)
    if not request.reason or len(request.reason) > 500 or contains_unstorable_text(request.reason):
        raise ValueError("reason is invalid")


def _validate_published_metadata(
    published: PublishedArtifact,
    *,
    ingest_channel: str,
    original_filename: str,
    media_type: str,
    manifest_generation: str,
    manifest_digest: bytes,
    actor: str,
    reason: str,
) -> None:
    if len(published.sha256) != 32 or published.byte_size < 0 or not published.storage_key:
        raise ValueError("published artifact identity is invalid")
    _validate_channel(ingest_channel)
    if (
        not original_filename
        or len(original_filename) > 512
        or contains_unstorable_text(original_filename)
    ):
        raise ValueError("original_filename is invalid")
    if not media_type or len(media_type) > 200 or contains_unstorable_text(media_type):
        raise ValueError("media_type is invalid")
    if not manifest_generation or len(manifest_generation) > MAX_MANIFEST_GENERATION:
        raise ValueError("manifest_generation is invalid")
    if len(manifest_digest) != 32:
        raise ValueError("manifest_digest must contain 32 bytes")
    _validate_actor(actor)
    if not reason or len(reason) > 500 or contains_unstorable_text(reason):
        raise ValueError("reason is invalid")


def _validate_actor(actor: str) -> None:
    if not actor or len(actor) > 200 or contains_unstorable_text(actor):
        raise ValueError("actor is invalid")


def _validate_channel(channel: str) -> None:
    if not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", channel):
        raise ValueError("ingest_channel is invalid")


def _validate_worker_id(worker_id: str) -> None:
    if not worker_id or len(worker_id) > MAX_WORKER_ID or contains_unstorable_text(worker_id):
        raise ValueError("worker_id is invalid")


def _validate_error(error_code: str, summary: str) -> None:
    if not _CODE_PATTERN.fullmatch(error_code) or len(error_code) > MAX_ERROR_CODE:
        raise ValueError("error_code is invalid")
    if not summary or len(summary) > MAX_SUMMARY or contains_unstorable_text(summary):
        raise ValueError("summary is invalid")


def _utc_now(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if current.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return current.astimezone(UTC)
