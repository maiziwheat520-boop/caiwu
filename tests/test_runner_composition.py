from __future__ import annotations

import asyncio
import hashlib
import threading
from io import BytesIO

import pytest

from ledgerbridge.connector_runner import ConnectorSupervisor, RunnerExecutionError
from ledgerbridge.connectors import ArtifactMetadata, ConnectorExecutionMode, DetectionResult
from ledgerbridge.runner_client import RunnerConnector
from ledgerbridge.runner_composition import (
    RUNNER_FACTORY_ID,
    RunnerCompositionError,
    RunnerConnectorSpec,
    VerifiedRunnerManifest,
    build_worker_runner_connectors,
)
from ledgerbridge.runner_protocol import RunnerOperation, RunnerRequest


def _spec(
    *,
    name: str = "synthetic.csv",
    version: str = "1",
    source_system: str = "synthetic",
) -> RunnerConnectorSpec:
    return RunnerConnectorSpec(
        factory_id=RUNNER_FACTORY_ID,
        name=name,
        version=version,
        source_system=source_system,
    )


def test_verified_manifest_has_stable_identity_and_builds_worker_facade() -> None:
    manifest = VerifiedRunnerManifest.from_connectors("test-generation", (_spec(),))

    assert manifest.digest == hashlib.sha256(manifest.canonical_bytes()).digest()
    assert manifest.identity == ("test-generation", manifest.digest)

    connectors = build_worker_runner_connectors(
        manifest,
        socket_path="/run/ledgerbridge-connector/runner.sock",
    )
    assert len(connectors) == 1
    assert connectors[0].name == "synthetic.csv"
    assert connectors[0].version == "1"
    assert connectors[0].source_system == "synthetic"
    assert connectors[0].execution_mode == ConnectorExecutionMode.RUNNER.value


def test_empty_manifest_is_safe_default() -> None:
    assert (
        build_worker_runner_connectors(
            None,
            socket_path="/run/ledgerbridge-connector/runner.sock",
        )
        == ()
    )


def test_manifest_rejects_digest_drift_and_duplicate_identity() -> None:
    spec = _spec()
    with pytest.raises(RunnerCompositionError, match="digest"):
        VerifiedRunnerManifest("generation", b"d" * 32, (spec,))

    with pytest.raises(RunnerCompositionError, match="duplicate"):
        VerifiedRunnerManifest.from_connectors("generation", (spec, spec))


@pytest.mark.parametrize("generation", ["", "-bad", "a" * 101, "bad\n"])
def test_manifest_rejects_invalid_generation(generation: str) -> None:
    with pytest.raises(RunnerCompositionError, match="generation"):
        VerifiedRunnerManifest.from_connectors(generation, ())


def test_manifest_rejects_invalid_digest_and_mutable_inputs() -> None:
    spec = _spec()
    with pytest.raises(RunnerCompositionError, match="32 bytes"):
        VerifiedRunnerManifest("generation", b"d" * 31, ())
    with pytest.raises(RunnerCompositionError, match="immutable bytes"):
        VerifiedRunnerManifest("generation", bytearray(b"d" * 32), ())  # type: ignore[arg-type]
    with pytest.raises(RunnerCompositionError, match="immutable tuple"):
        VerifiedRunnerManifest(
            "generation",
            hashlib.sha256(
                VerifiedRunnerManifest.from_connectors("generation", (spec,)).canonical_bytes()
            ).digest(),
            [spec],  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", "ledgerbridge.internal"),
        ("version", "x" * 101),
        ("source_system", "INVALID"),
        ("source_system", "synthetic\n"),
    ],
)
def test_manifest_rejects_unsafe_connector_fields(field: str, value: str) -> None:
    name = value if field == "name" else "synthetic.csv"
    version = value if field == "version" else "1"
    source_system = value if field == "source_system" else "synthetic"
    with pytest.raises(RunnerCompositionError):
        RunnerConnectorSpec(
            factory_id=RUNNER_FACTORY_ID,
            name=name,
            version=version,
            source_system=source_system,
        )


def test_manifest_builds_multiple_facades_over_one_runner_client() -> None:
    manifest = VerifiedRunnerManifest.from_connectors(
        "generation", (_spec(), _spec(name="synthetic.json"))
    )

    connectors = build_worker_runner_connectors(manifest, socket_path="/tmp/runner.sock")

    assert len(connectors) == 2
    assert connectors[0]._client is connectors[1]._client


def test_manifest_rejects_non_runner_factory_and_mode() -> None:
    with pytest.raises(RunnerCompositionError, match="allowlisted"):
        RunnerConnectorSpec(
            factory_id="dynamic.import.path",
            name="synthetic.csv",
            version="1",
            source_system="synthetic",
        )

    with pytest.raises(RunnerCompositionError, match="execution_mode"):
        RunnerConnectorSpec(
            factory_id=RUNNER_FACTORY_ID,
            name="synthetic.csv",
            version="1",
            source_system="synthetic",
            execution_mode=ConnectorExecutionMode.IN_PROCESS,
        )


def test_runner_facade_keeps_verified_detection_context_per_thread() -> None:
    class StubClient:
        def __init__(self) -> None:
            self.parse_filenames: list[str] = []
            self.lock = threading.Lock()

        def detect(self, request: RunnerRequest, _stream: BytesIO) -> DetectionResult:
            return DetectionResult.MATCH

        def parse(self, request: RunnerRequest, _stream: BytesIO) -> tuple[object, ...]:
            with self.lock:
                self.parse_filenames.append(request.metadata.original_filename)
            return ()

    client = StubClient()
    connector = RunnerConnector("synthetic.csv", "1", "synthetic", client)  # type: ignore[arg-type]
    digest = hashlib.sha256(b"ok").hexdigest()
    metadata = {
        filename: ArtifactMetadata(
            source="manual_upload",
            original_filename=filename,
            media_type="text/csv",
            byte_size=2,
            sha256_hex=digest,
        )
        for filename in ("first.csv", "second.csv")
    }
    ready = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(filename: str) -> None:
        try:
            connector.detect_verified(metadata[filename], BytesIO(b"ok"))
            ready.wait(timeout=5)
            tuple(connector.parse(BytesIO(b"ok")))
        except BaseException as exc:  # pragma: no cover - assertion reports the thread error
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(filename,)) for filename in metadata]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(client.parse_filenames) == sorted(metadata)


@pytest.mark.asyncio
async def test_runner_timeout_keeps_execution_slot_until_connector_finishes() -> None:
    class BlockingConnector:
        name = "synthetic.csv"
        version = "1"
        source_system = "synthetic"

        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def detect(self, _metadata: ArtifactMetadata, _prefix: bytes) -> DetectionResult:
            return DetectionResult.MATCH

        def parse(self, _stream: BytesIO) -> tuple[object, ...]:
            self.started.set()
            try:
                self.release.wait(timeout=5)
                return ()
            finally:
                self.finished.set()

    content = b"ok"
    digest = hashlib.sha256(content).hexdigest()
    request = RunnerRequest(
        request_id="00000000-0000-4000-8000-000000000001",
        operation=RunnerOperation.PARSE,
        connector_name="synthetic.csv",
        connector_version="1",
        source_system="synthetic",
        metadata=ArtifactMetadata(
            source="manual_upload",
            original_filename="fixture.csv",
            media_type="text/csv",
            byte_size=len(content),
            sha256_hex=digest,
        ),
        declared_artifact_size=len(content),
        verified_sha256_hex=digest,
    )
    connector = BlockingConnector()
    supervisor = ConnectorSupervisor(
        {("synthetic.csv", "1"): connector},  # type: ignore[dict-item]
        max_execution_workers=1,
    )
    first = asyncio.create_task(
        supervisor._execute_with_artifact_bounded(request, BytesIO(content))
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(connector.started.wait, 2), timeout=1)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(first, timeout=0.01)
        assert first.cancelled()

        with pytest.raises(RunnerExecutionError, match="timed out") as error:
            await supervisor._execute_with_artifact_bounded(request, BytesIO(content))
        assert error.value.error_code == "TIMEOUT"
        assert supervisor._active_executions == 1

        connector.release.set()
        await asyncio.wait_for(asyncio.to_thread(connector.finished.wait, 2), timeout=1)
        await asyncio.sleep(0)
        assert supervisor._active_executions == 0
    finally:
        connector.release.set()
        await asyncio.gather(first, return_exceptions=True)
