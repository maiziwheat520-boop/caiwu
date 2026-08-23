from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from ledgerbridge.artifacts import ArtifactStore
from ledgerbridge.audit import append_audit_event
from ledgerbridge.dispatch import (
    MAX_ATTEMPTS,
    DispatchClaimLost,
    DispatchConflict,
    DispatchError,
    DispatchNotFound,
    DispatchRequest,
    DispatchService,
)
from ledgerbridge.models import (
    DispatchState,
    ImportJob,
    ImportJobStatus,
    RawArtifact,
)


def _run_alembic(database_url: str, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    if downgrade:
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)


@pytest.fixture(scope="session")
def database_url() -> str:
    value = os.environ.get("LEDGERBRIDGE_DATABASE_URL")
    if value is None:
        pytest.skip("PostgreSQL integration tests require LEDGERBRIDGE_DATABASE_URL")
    return value


@pytest.fixture(scope="session")
def migration_database_url() -> str:
    value = os.environ.get("LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    if value is None:
        pytest.skip("PostgreSQL integration tests require LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    return value


@pytest.fixture(scope="session")
def admin_engine(migration_database_url: str) -> Iterator[Engine]:
    _run_alembic(migration_database_url, "head")
    engine = create_engine(migration_database_url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def runtime_engine(database_url: str, admin_engine: Engine) -> Iterator[Engine]:
    del admin_engine
    engine = create_engine(database_url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def reset_database(admin_engine: Engine) -> Iterator[None]:
    with admin_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE evidence_import_dispatch, source_record, import_job, "
                "raw_artifact, posting, journal_entry, account, entity, audit_event "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def dispatch_context(
    runtime_engine: Engine,
    admin_engine: Engine,
    tmp_path: Path,
) -> tuple[DispatchService, UUID, sessionmaker[Session]]:
    sessions = sessionmaker(bind=runtime_engine, expire_on_commit=False)
    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000_000, chunk_size=127)
    published = store.publish(io.BytesIO(b"dispatch artifact"))
    with sessions() as session, session.begin():
        audit_event_id = append_audit_event(
            session,
            actor="pytest",
            action="artifact.ingest",
            reason="dispatch fixture",
            payload={
                "sha256": published.sha256_hex,
                "byte_size": published.byte_size,
                "storage_key": published.storage_key,
                "source": "synthetic_upload",
                "original_filename_sha256": hashlib.sha256(b"dispatch.csv").hexdigest(),
                "media_type": "text/csv",
            },
        )
        artifact = RawArtifact(
            sha256=published.sha256,
            source="synthetic_upload",
            original_filename="dispatch.csv",
            media_type="text/csv",
            byte_size=published.byte_size,
            storage_key=published.storage_key,
            audit_event_id=audit_event_id,
        )
        session.add(artifact)
        session.flush()
        artifact_id = artifact.id
    return DispatchService(sessions, lease_seconds=5, max_attempts=3), artifact_id, sessions


def _request(
    artifact_id: UUID, *, generation: str = "test-1", digest: bytes = b"d" * 32
) -> DispatchRequest:
    return DispatchRequest(
        artifact_id=artifact_id,
        ingest_channel="synthetic_upload",
        manifest_generation=generation,
        manifest_digest=digest,
        actor="pytest",
        reason="dispatch test",
    )


def _create_import_job(sessions: sessionmaker[Session], artifact_id: UUID) -> UUID:
    with sessions() as session, session.begin():
        job = ImportJob(
            artifact_id=artifact_id,
            connector_name="synthetic",
            connector_version="1",
            source_system="synthetic",
            status=ImportJobStatus.PENDING,
        )
        session.add(job)
        session.flush()
        return job.id


def test_enqueue_is_idempotent_and_binds_acceptance_audit(
    dispatch_context: tuple[DispatchService, UUID, sessionmaker[Session]],
) -> None:
    service, artifact_id, sessions = dispatch_context
    first = service.enqueue(_request(artifact_id))
    second = service.enqueue(_request(artifact_id))
    assert second == first
    with sessions() as session:
        assert session.scalar(text("SELECT count(*) FROM evidence_import_dispatch")) == 1
        assert (
            session.scalar(
                text("SELECT count(*) FROM audit_event WHERE action = 'import.dispatch.accepted'")
            )
            == 1
        )


def test_enqueue_rejects_manifest_digest_conflict(
    dispatch_context: tuple[DispatchService, UUID, sessionmaker[Session]],
) -> None:
    service, artifact_id, _sessions = dispatch_context
    service.enqueue(_request(artifact_id))
    with pytest.raises(DispatchConflict, match="manifest digest"):
        service.enqueue(_request(artifact_id, digest=b"e" * 32))


def test_concurrent_enqueue_converges_without_duplicate_acceptance(
    dispatch_context: tuple[DispatchService, UUID, sessionmaker[Session]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, artifact_id, _sessions = dispatch_context
    request = _request(artifact_id, generation="concurrent-enqueue")
    barrier = Barrier(2)

    def gated_append(
        session: Session,
        *,
        actor: str,
        action: str,
        reason: str,
        payload: Mapping[str, object] | None = None,
        rule_version: str | None = None,
    ) -> UUID:
        barrier.wait(timeout=10)
        return append_audit_event(
            session,
            actor=actor,
            action=action,
            reason=reason,
            payload=payload,
            rule_version=rule_version,
        )

    monkeypatch.setattr("ledgerbridge.dispatch.append_audit_event", gated_append)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(service.enqueue, (request, request)))
    assert results[0] == results[1]


def test_claim_renew_retry_reclaim_and_complete(
    dispatch_context: tuple[DispatchService, UUID, sessionmaker[Session]],
) -> None:
    service, artifact_id, sessions = dispatch_context
    operation = service.enqueue(_request(artifact_id))
    base = datetime.now(UTC) + timedelta(seconds=1)
    claim = service.claim_next("worker-a", now=base)
    assert claim is not None
    assert claim.operation_id == operation.operation_id
    renewed = service.renew_lease(
        operation.operation_id, "worker-a", now=base + timedelta(seconds=1)
    )
    assert renewed > claim.lease_until
    assert (
        service.mark_retry(
            operation.operation_id,
            "worker-a",
            available_at=base + timedelta(seconds=3),
            error_code="RUNNER_UNAVAILABLE",
            summary="runner unavailable",
            now=base + timedelta(seconds=2),
        )
        is DispatchState.RETRY_WAIT
    )
    second = service.claim_next("worker-b", now=base + timedelta(seconds=4))
    assert second is not None
    with sessions() as session, session.begin():
        job = ImportJob(
            artifact_id=artifact_id,
            connector_name="synthetic",
            connector_version="1",
            source_system="synthetic",
            status=ImportJobStatus.PENDING,
        )
        session.add(job)
        session.flush()
        job_id = job.id
    assert (
        service.complete(
            operation.operation_id,
            "worker-b",
            result_status=ImportJobStatus.NEEDS_REVIEW,
            import_job_id=job_id,
            now=base + timedelta(seconds=5),
        )
        is DispatchState.SUCCEEDED
    )
    result = service.get_for_actor(operation.operation_id, "pytest")
    assert result.state is DispatchState.SUCCEEDED
    assert result.result_status is ImportJobStatus.PENDING
    assert result.job_id == job_id


def test_expired_lease_is_recovered_and_exhaustion_is_terminal(
    dispatch_context: tuple[DispatchService, UUID, sessionmaker[Session]],
) -> None:
    service, artifact_id, _sessions = dispatch_context
    service.enqueue(_request(artifact_id))
    base = datetime.now(UTC) + timedelta(seconds=1)
    assert service.claim_next("worker-a", now=base) is not None
    assert service.recover_expired_leases(now=base + timedelta(seconds=7)) == 1
    assert service.claim_next("worker-b", now=base + timedelta(seconds=8)) is not None

    service_one_attempt = DispatchService(_sessions, lease_seconds=2, max_attempts=1)
    other = service_one_attempt.enqueue(_request(artifact_id, generation="test-2"))
    assert service_one_attempt.claim_next("worker-c", now=base) is not None
    assert service_one_attempt.recover_expired_leases(now=base + timedelta(seconds=3)) == 1
    terminal = service_one_attempt.get_for_actor(other.operation_id, "pytest")
    assert terminal.state is DispatchState.FAILED
    assert terminal.error_code == "DISPATCH_ATTEMPTS_EXHAUSTED"


def test_claim_owner_and_status_principal_are_enforced(
    dispatch_context: tuple[DispatchService, UUID, sessionmaker[Session]],
) -> None:
    service, artifact_id, _sessions = dispatch_context
    operation = service.enqueue(_request(artifact_id, generation="test-3"))
    base = datetime.now(UTC) + timedelta(seconds=1)
    assert service.claim_next("worker-a", now=base) is not None
    with pytest.raises(DispatchClaimLost):
        service.renew_lease(operation.operation_id, "worker-b", now=base + timedelta(seconds=1))
    with pytest.raises(DispatchClaimLost):
        service.fail(
            operation.operation_id,
            "worker-b",
            error_code="RUNNER_UNAVAILABLE",
            summary="runner unavailable",
            now=base + timedelta(seconds=1),
        )
    with pytest.raises(DispatchNotFound):
        service.get_for_actor(operation.operation_id, "another-principal")


def test_status_principal_and_success_terminal_path(
    dispatch_context: tuple[DispatchService, UUID, sessionmaker[Session]],
) -> None:
    service, artifact_id, sessions = dispatch_context
    operation = service.enqueue(_request(artifact_id, generation="terminal-success"))
    assert service.acceptance_principal(operation.operation_id).actor == "pytest"
    assert service.acceptance_principal(operation.operation_id).reason == "dispatch test"
    with pytest.raises(DispatchNotFound):
        service.acceptance_principal(uuid4())

    base = datetime.now(UTC) + timedelta(seconds=1)
    assert service.claim_next("worker-success", now=base) is not None
    job_id = _create_import_job(sessions, artifact_id)
    assert (
        service.complete(
            operation.operation_id,
            "worker-success",
            result_status=ImportJobStatus.SUCCEEDED,
            import_job_id=job_id,
            now=base + timedelta(seconds=1),
        )
        is DispatchState.SUCCEEDED
    )
    status = service.get_for_actor(operation.operation_id, "pytest")
    assert status.result_status is ImportJobStatus.PENDING
    assert service.claim_next("worker-empty", now=base + timedelta(seconds=2)) is None


def test_exhaustion_recovery_and_validation_boundaries(
    dispatch_context: tuple[DispatchService, UUID, sessionmaker[Session]],
) -> None:
    service, artifact_id, sessions = dispatch_context
    base = datetime.now(UTC) + timedelta(seconds=1)
    with pytest.raises(ValueError):
        service.recover_expired_leases(now=base, limit=0)
    with pytest.raises(ValueError):
        service.recover_expired_leases(now=base, limit=1001)
    with pytest.raises(ValueError, match="timezone-aware"):
        service.recover_expired_leases(now=datetime.now())

    one_attempt = DispatchService(sessions, lease_seconds=5, max_attempts=1)
    exhausted = one_attempt.enqueue(_request(artifact_id, generation="retry-exhausted"))
    assert one_attempt.claim_next("worker-exhausted", now=base) is not None
    assert (
        one_attempt.mark_retry(
            exhausted.operation_id,
            "worker-exhausted",
            available_at=base + timedelta(seconds=1),
            error_code="RUNNER_UNAVAILABLE",
            summary="runner unavailable",
            now=base + timedelta(seconds=1),
        )
        is DispatchState.FAILED
    )

    two_attempts = DispatchService(sessions, lease_seconds=5, max_attempts=2)
    claim_exhausted = two_attempts.enqueue(_request(artifact_id, generation="claim-exhausted"))
    assert two_attempts.claim_next("worker-claim", now=base + timedelta(seconds=2)) is not None
    assert (
        two_attempts.mark_retry(
            claim_exhausted.operation_id,
            "worker-claim",
            available_at=base + timedelta(seconds=3),
            error_code="RUNNER_UNAVAILABLE",
            summary="runner unavailable",
            now=base + timedelta(seconds=2),
        )
        is DispatchState.RETRY_WAIT
    )
    two_attempts._max_attempts = 1
    assert two_attempts.claim_next("worker-claim-2", now=base + timedelta(seconds=4)) is None
    assert (
        two_attempts.get_for_actor(claim_exhausted.operation_id, "pytest").state
        is DispatchState.FAILED
    )

    with pytest.raises(ValueError):
        service.enqueue(
            DispatchRequest(
                artifact_id=artifact_id,
                ingest_channel="",
                manifest_generation="valid",
                manifest_digest=b"d" * 32,
                actor="pytest",
                reason="valid",
            )
        )
    with pytest.raises(ValueError):
        service.enqueue(
            DispatchRequest(
                artifact_id=artifact_id,
                ingest_channel="synthetic_upload",
                manifest_generation="",
                manifest_digest=b"d" * 32,
                actor="pytest",
                reason="valid",
            )
        )
    with pytest.raises(ValueError):
        service.enqueue(
            DispatchRequest(
                artifact_id=artifact_id,
                ingest_channel="synthetic_upload",
                manifest_generation="valid",
                manifest_digest=b"d" * 32,
                actor="pytest",
                reason="",
            )
        )
    with pytest.raises(ValueError):
        service.get_for_actor(exhausted.operation_id, "")
    with pytest.raises(ValueError):
        service.claim_next("", now=base)
    with pytest.raises(ValueError):
        service.claim_next("worker", now=datetime.now())
    with pytest.raises(ValueError):
        service.fail(
            exhausted.operation_id,
            "worker",
            error_code="invalid-code",
            summary="summary",
            now=base,
        )
    with pytest.raises(ValueError):
        service.fail(
            exhausted.operation_id,
            "worker",
            error_code="VALID_CODE",
            summary="bad\x00summary",
            now=base,
        )


def test_failure_terminal_paths_and_argument_guards(
    dispatch_context: tuple[DispatchService, UUID, sessionmaker[Session]],
) -> None:
    service, artifact_id, sessions = dispatch_context
    base = datetime.now(UTC) + timedelta(seconds=1)

    failed = service.enqueue(_request(artifact_id, generation="terminal-failed"))
    assert service.claim_next("worker-failed", now=base) is not None
    job_id = _create_import_job(sessions, artifact_id)
    assert (
        service.complete(
            failed.operation_id,
            "worker-failed",
            result_status=ImportJobStatus.FAILED,
            import_job_id=job_id,
            error_code="CONNECTOR_CONTRACT",
            summary="connector contract failed",
            now=base + timedelta(seconds=1),
        )
        is DispatchState.FAILED
    )

    direct_failed = service.enqueue(_request(artifact_id, generation="direct-failed"))
    assert service.claim_next("worker-direct", now=base + timedelta(seconds=2)) is not None
    assert (
        service.fail(
            direct_failed.operation_id,
            "worker-direct",
            error_code="RUNNER_UNAVAILABLE",
            summary="runner unavailable",
            now=base + timedelta(seconds=3),
        )
        is DispatchState.FAILED
    )

    invalid = service.enqueue(_request(artifact_id, generation="invalid-complete"))
    assert service.claim_next("worker-invalid", now=base + timedelta(seconds=4)) is not None
    with pytest.raises(ValueError, match="terminal import status"):
        service.complete(
            invalid.operation_id,
            "worker-invalid",
            result_status=ImportJobStatus.PENDING,
            import_job_id=job_id,
            now=base + timedelta(seconds=5),
        )
    with pytest.raises(ValueError, match="successful dispatch"):
        service.complete(
            invalid.operation_id,
            "worker-invalid",
            result_status=ImportJobStatus.SUCCEEDED,
            import_job_id=job_id,
            error_code="RUNNER_UNAVAILABLE",
            summary="unexpected",
            now=base + timedelta(seconds=5),
        )
    with pytest.raises(ValueError, match="failed dispatch"):
        service.complete(
            invalid.operation_id,
            "worker-invalid",
            result_status=ImportJobStatus.FAILED,
            import_job_id=job_id,
            now=base + timedelta(seconds=5),
        )
    with pytest.raises(ValueError, match="review dispatch"):
        service.complete(
            invalid.operation_id,
            "worker-invalid",
            result_status=ImportJobStatus.NEEDS_REVIEW,
            import_job_id=job_id,
            error_code="REVIEW",
            summary="unexpected",
            now=base + timedelta(seconds=5),
        )
    with pytest.raises(ValueError, match="available_at"):
        service.mark_retry(
            invalid.operation_id,
            "worker-invalid",
            available_at=base,
            error_code="RUNNER_UNAVAILABLE",
            summary="runner unavailable",
            now=base + timedelta(seconds=6),
        )
    assert (
        service.fail(
            invalid.operation_id,
            "worker-invalid",
            error_code="CONNECTOR_CONTRACT",
            summary="invalid completion arguments",
            now=base + timedelta(seconds=7),
        )
        is DispatchState.FAILED
    )


def test_dispatch_input_bounds_and_missing_artifact(
    dispatch_context: tuple[DispatchService, UUID, sessionmaker[Session]],
) -> None:
    service, artifact_id, _sessions = dispatch_context
    with pytest.raises(ValueError):
        service.enqueue(_request(artifact_id, digest=b"short"))
    with pytest.raises(ValueError):
        service.enqueue(
            DispatchRequest(
                artifact_id=artifact_id,
                ingest_channel="internal",
                manifest_generation="test-4",
                manifest_digest=b"d" * 32,
                actor="bad\x00actor",
                reason="test",
            )
        )
    with pytest.raises(DispatchError, match="artifact"):
        service.enqueue(_request(uuid4(), generation="test-5"))
    with pytest.raises(DispatchError, match="ingest channel"):
        service.enqueue(
            DispatchRequest(
                artifact_id=artifact_id,
                ingest_channel="missing-channel",
                manifest_generation="test-6",
                manifest_digest=b"d" * 32,
                actor="pytest",
                reason="test",
            )
        )
    with pytest.raises(ValueError):
        DispatchService(cast(sessionmaker[Session], object()), lease_seconds=0)
    with pytest.raises(ValueError):
        DispatchService(cast(sessionmaker[Session], object()), max_attempts=MAX_ATTEMPTS + 1)
