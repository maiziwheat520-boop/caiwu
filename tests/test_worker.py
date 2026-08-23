import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import gettempdir
from typing import Any, cast
from uuid import uuid4

import pytest

import ledgerbridge.worker as worker
from ledgerbridge.artifacts import PublishedArtifact, storage_key_for_digest
from ledgerbridge.dispatch import (
    DispatchClaim,
    DispatchClaimLost,
    DispatchPrincipal,
    DispatchService,
)
from ledgerbridge.imports import (
    EvidenceImporter,
    EvidenceIngestionError,
    ImportOutcome,
    IngestMetadata,
)
from ledgerbridge.models import DispatchState, ImportJobStatus
from ledgerbridge.worker import heartbeat_is_fresh, heartbeat_path, write_heartbeat


def test_worker_heartbeat_uses_ephemeral_runtime_path() -> None:
    assert heartbeat_path() == Path(gettempdir()) / "ledgerbridge-worker-heartbeat"


def test_worker_heartbeat_is_fresh_within_window(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    write_heartbeat(path, now=100.0)

    assert heartbeat_is_fresh(path, now=129.0, max_age_seconds=30.0)


def test_worker_heartbeat_rejects_missing_stale_future_and_malformed(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    assert not heartbeat_is_fresh(path, now=100.0)

    write_heartbeat(path, now=100.0)
    assert not heartbeat_is_fresh(path, now=131.0, max_age_seconds=30.0)
    assert not heartbeat_is_fresh(path, now=99.0, max_age_seconds=30.0)

    path.write_text("not-a-timestamp", encoding="ascii")
    assert not heartbeat_is_fresh(path, now=100.0)


def test_worker_main_writes_once_and_stops(monkeypatch: object) -> None:
    calls: list[str] = []
    importer_builds: list[str] = []

    def build_importer() -> None:
        importer_builds.append("build")

    class Settings:
        env = "test"
        enable_internal_async_dispatch = False
        dispatch_poll_seconds = 1.0

    monkeypatch.setattr(worker, "get_settings", lambda: Settings())  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "build_dispatch_service", lambda _settings: object())  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "build_worker_connectors", lambda: ())  # type: ignore[attr-defined]

    monkeypatch.setattr(worker.signal, "signal", lambda *_args: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        worker,
        "build_evidence_importer",
        build_importer,
    )
    monkeypatch.setattr(worker, "write_heartbeat", lambda: calls.append("write"))  # type: ignore[attr-defined]
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: worker._stop(0, None))  # type: ignore[attr-defined]

    worker.main()

    assert calls == ["write"]
    assert importer_builds == ["build"]


def test_worker_main_processes_enabled_async_profile(monkeypatch: object) -> None:
    calls: list[object] = []

    class Settings:
        env = "test"
        enable_internal_async_dispatch = True
        dispatch_poll_seconds = 1.0

    monkeypatch.setattr(worker, "get_settings", lambda: Settings())  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "build_evidence_importer", lambda: "importer")  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "build_dispatch_service", lambda _settings: "dispatch")  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "build_worker_manifest", lambda: ("generation", b"m" * 32))  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "build_worker_connectors", lambda: ("connector",))  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "worker_id", lambda: "worker")  # type: ignore[attr-defined]
    monkeypatch.setattr(worker.signal, "signal", lambda *_args: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "write_heartbeat", lambda: calls.append("heartbeat"))  # type: ignore[attr-defined]

    def process(*args: object, **kwargs: object) -> bool:
        calls.append((args, kwargs))
        worker._stop(0, None)
        return True

    monkeypatch.setattr(worker, "process_dispatch_once", process)  # type: ignore[attr-defined]
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)  # type: ignore[attr-defined]

    worker.main()

    assert calls[0] == "heartbeat"
    assert calls[1][0][0:4] == ("dispatch", "importer", ("connector",), "worker")  # type: ignore[index]
    assert calls[1][1]["expected_manifest"] == ("generation", b"m" * 32)  # type: ignore[index]


def test_worker_composition_enables_production_connector_boundary(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class Settings:
        env = "production"
        database_url = "postgresql+psycopg://runtime"
        artifact_root = tmp_path
        artifact_max_bytes = 100
        artifact_total_max_bytes = 200
        artifact_staging_max_bytes = 300
        artifact_staging_ttl_seconds = 400

    monkeypatch.setattr(worker, "get_settings", lambda: Settings())  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        worker,
        "get_session_factory",
        lambda database_url: captured.update(database_url=database_url) or "sessions",
    )
    monkeypatch.setattr(worker, "ArtifactStore", lambda *args, **kwargs: (args, kwargs))  # type: ignore[attr-defined]

    def fake_importer(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(worker, "EvidenceImporter", fake_importer)  # type: ignore[attr-defined]

    worker.build_evidence_importer()

    assert captured["database_url"] == "postgresql+psycopg://runtime"
    assert captured["production"] is True


def test_worker_defaults_are_empty_and_dispatch_service_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Settings:
        database_url = "postgresql+psycopg://runtime"
        dispatch_lease_seconds = 7
        dispatch_max_attempts = 4

    monkeypatch.setattr(
        worker, "get_session_factory", lambda value: captured.update(url=value) or "sessions"
    )

    service = worker.build_dispatch_service(Settings())  # type: ignore[arg-type]

    assert isinstance(service, DispatchService)
    assert captured["url"] == "postgresql+psycopg://runtime"
    assert worker.build_worker_connectors() == ()
    assert worker.build_worker_manifest() is None


@pytest.mark.parametrize("renew_error", [DispatchClaimLost("lost"), RuntimeError("transient")])
def test_worker_lease_renewal_handles_lost_and_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
    renew_error: BaseException,
) -> None:
    class Event:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, _interval: float) -> bool:
            self.calls += 1
            return self.calls > 1

        def set(self) -> None:
            return None

    event = Event()

    class Thread:
        def __init__(self, target: Any, **_kwargs: object) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

        def join(self, **_kwargs: object) -> None:
            return None

    class Dispatch:
        def renew_lease(self, _operation_id: object, _owner: str) -> None:
            raise renew_error

    monkeypatch.setattr(worker.threading, "Event", lambda: event)  # type: ignore[attr-defined]
    monkeypatch.setattr(worker.threading, "Thread", Thread)  # type: ignore[attr-defined]

    with worker.renew_dispatch_lease(
        cast(DispatchService, Dispatch()), uuid4(), "owner", lease_seconds=3
    ):
        pass


def test_process_dispatch_once_covers_guards_manifest_and_failure_paths() -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)
    digest = hashlib.sha256(b"evidence").digest()

    class Dispatch:
        def __init__(
            self,
            error: BaseException | None = None,
            claim: DispatchClaim | None = None,
            principal_error: BaseException | None = None,
        ) -> None:
            self.claim = claim
            self.error = error
            self.principal_error = principal_error
            self.failed: list[dict[str, object]] = []
            self.retried: list[dict[str, object]] = []
            self.completed = False

        def recover_expired_leases(self, **_kwargs: object) -> int:
            return 0

        def claim_next(self, _owner: str, **_kwargs: object) -> DispatchClaim | None:
            if self.error is not None:
                raise self.error
            return self.claim

        def acceptance_principal(self, _operation_id: object) -> DispatchPrincipal:
            if self.principal_error is not None:
                raise self.principal_error
            return DispatchPrincipal(actor="actor", reason="reason")

        def fail(self, *_args: object, **kwargs: object) -> DispatchState:
            self.failed.append(kwargs)
            return DispatchState.FAILED

        def mark_retry(self, *_args: object, **kwargs: object) -> DispatchState:
            self.retried.append(kwargs)
            return DispatchState.RETRY_WAIT

        def complete(self, *_args: object, **_kwargs: object) -> DispatchState:
            self.completed = True
            return DispatchState.SUCCEEDED

    claim = DispatchClaim(
        operation_id=uuid4(),
        artifact_id=uuid4(),
        ingest_channel="manual_upload",
        manifest_generation="generation-a",
        manifest_digest=b"m" * 32,
        byte_size=8,
        storage_key=storage_key_for_digest(digest),
        original_filename="statement.csv",
        media_type="text/csv",
        attempt_count=2,
        lease_owner="owner",
        lease_until=now + timedelta(seconds=30),
    )

    assert not worker.process_dispatch_once(
        cast(DispatchService, Dispatch()),
        cast(EvidenceImporter, object()),
        (),
        "owner",
        now=now,
    )
    assert not worker.process_dispatch_once(
        cast(DispatchService, Dispatch(claim=None)),
        cast(EvidenceImporter, object()),
        cast(Any, (object(),)),
        "owner",
        now=now,
    )

    drift = Dispatch(claim=claim)
    assert worker.process_dispatch_once(
        cast(DispatchService, drift),
        cast(EvidenceImporter, object()),
        cast(Any, (object(),)),
        "owner",
        now=now,
        expected_manifest=("generation-b", b"m" * 32),
    )
    assert drift.failed[0]["error_code"] == "MANIFEST_DRIFT"

    failed = Dispatch(claim=claim)

    class FailedImporter:
        def ingest_published(self, *_args: object, **_kwargs: object) -> ImportOutcome:
            return ImportOutcome(
                artifact_id=uuid4(),
                job_id=uuid4(),
                status=ImportJobStatus.FAILED,
                parsed_count=0,
                created_count=0,
                duplicate_count=0,
                error_code=None,
                artifact_created=False,
            )

    assert worker.process_dispatch_once(
        cast(DispatchService, failed),
        cast(EvidenceImporter, FailedImporter()),
        cast(Any, (object(),)),
        "owner",
        now=now,
    )
    assert failed.failed[0]["error_code"] == "IMPORT_FAILED"

    retry = Dispatch(claim=claim)

    class RetryImporter:
        def ingest_published(self, *_args: object, **_kwargs: object) -> ImportOutcome:
            raise EvidenceIngestionError("RUNNER_UNAVAILABLE", "bounded")

    assert worker.process_dispatch_once(
        cast(DispatchService, retry),
        cast(EvidenceImporter, RetryImporter()),
        cast(Any, (object(),)),
        "owner",
        now=now,
    )
    assert retry.retried[0]["error_code"] == "RUNNER_UNAVAILABLE"

    internal = Dispatch(claim=claim)

    class InternalImporter:
        def ingest_published(self, *_args: object, **_kwargs: object) -> ImportOutcome:
            raise RuntimeError("bounded")

    assert worker.process_dispatch_once(
        cast(DispatchService, internal),
        cast(EvidenceImporter, InternalImporter()),
        cast(Any, (object(),)),
        "owner",
        now=now,
    )
    assert internal.retried[0]["error_code"] == "WORKER_INTERNAL"

    lost = Dispatch(claim=claim, principal_error=DispatchClaimLost("lost"))
    assert worker.process_dispatch_once(
        cast(DispatchService, lost),
        cast(EvidenceImporter, object()),
        cast(Any, (object(),)),
        "owner",
        now=now,
    )

    terminal = Dispatch(claim=claim)
    worker._handle_dispatch_error(
        cast(DispatchService, terminal),
        claim,
        "owner",
        "CONNECTOR_CONTRACT",
        "bounded",
        now,
    )
    assert terminal.failed[0]["error_code"] == "CONNECTOR_CONTRACT"

    class LostRecorder(Dispatch):
        def mark_retry(self, *_args: object, **_kwargs: object) -> DispatchState:
            raise DispatchClaimLost("lost while recording")

    worker._handle_dispatch_error(
        cast(DispatchService, LostRecorder(claim=claim)),
        claim,
        "owner",
        "RUNNER_UNAVAILABLE",
        "bounded",
        now,
    )


def test_process_dispatch_once_imports_and_terminalizes(monkeypatch: object) -> None:
    now = datetime(2026, 8, 23, tzinfo=UTC)
    digest = hashlib.sha256(b"evidence").digest()
    operation_id = uuid4()

    class Dispatch:
        def __init__(self) -> None:
            self.claim = DispatchClaim(
                operation_id=operation_id,
                artifact_id=uuid4(),
                ingest_channel="manual_upload",
                manifest_generation="synthetic-test",
                manifest_digest=b"m" * 32,
                byte_size=8,
                storage_key=storage_key_for_digest(digest),
                original_filename="statement.csv",
                media_type="text/csv",
                attempt_count=1,
                lease_owner="worker-1",
                lease_until=now + timedelta(seconds=30),
            )
            self.completed: tuple[object, ...] | None = None

        def recover_expired_leases(self, **_kwargs: object) -> int:
            return 0

        def claim_next(self, _owner: str, **_kwargs: object) -> DispatchClaim:
            return self.claim

        def acceptance_principal(self, _operation_id: object) -> DispatchPrincipal:
            return DispatchPrincipal(actor="actor-1", reason="accepted")

        def renew_lease(self, _operation_id: object, _owner: str) -> datetime:
            return now + timedelta(seconds=30)

        def complete(self, *args: object, **kwargs: object) -> DispatchState:
            self.completed = (*args, kwargs)
            return DispatchState.SUCCEEDED

    class Importer:
        def ingest_published(
            self,
            published: PublishedArtifact,
            metadata: IngestMetadata,
            connectors: object,
            **kwargs: object,
        ) -> ImportOutcome:
            assert published.sha256 == digest
            assert metadata.source == "manual_upload"
            assert connectors
            assert kwargs == {"actor": "actor-1", "reason": "accepted"}
            return ImportOutcome(
                artifact_id=uuid4(),
                job_id=uuid4(),
                status=ImportJobStatus.SUCCEEDED,
                parsed_count=1,
                created_count=1,
                duplicate_count=0,
                error_code=None,
                artifact_created=False,
            )

    dispatch = Dispatch()
    assert worker.process_dispatch_once(
        cast(DispatchService, dispatch),
        cast(EvidenceImporter, Importer()),
        (object(),),  # type: ignore[arg-type]
        "worker-1",
        now=now,
    )
    assert dispatch.completed is not None
