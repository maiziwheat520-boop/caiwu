from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import stat
import struct
import time
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO
from uuid import uuid4

import pytest

import ledgerbridge.connector_runner as runner_module
import ledgerbridge.runner_client as client_module
from ledgerbridge.connector_runner import ConnectorSupervisor, RunnerExecutionError
from ledgerbridge.connectors import (
    ArtifactMetadata,
    ConnectorContractError,
    DetectionResult,
    ParsedSourceRecord,
)
from ledgerbridge.runner_client import ConnectorRunnerClient, RunnerClientError, RunnerConnector
from ledgerbridge.runner_protocol import (
    MAX_RESPONSE_BYTES,
    FrameKind,
    RunnerOperation,
    RunnerProtocolError,
    RunnerRequest,
    RunnerStatus,
    RunnerTerminal,
    artifact_end_payload,
    chunk_frames,
    decode_frame,
    encode_frame,
    parse_terminal_payload,
    read_async_frame,
    read_frame,
    record_payload,
    request_control,
    terminal_payload,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix socket runner requires POSIX")


class SyntheticRunnerConnector:
    name = "synthetic.csv"
    version = "1"
    source_system = "synthetic"

    def detect(self, metadata: ArtifactMetadata, bounded_prefix: bytes) -> DetectionResult:
        return (
            DetectionResult.MATCH if bounded_prefix.startswith(b"ok") else DetectionResult.NO_MATCH
        )

    def parse(self, stream: BinaryIO) -> list[ParsedSourceRecord]:
        content = stream.read()
        return [
            ParsedSourceRecord(
                record_locator=f"row:{len(content)}",
                source="synthetic",
                parser_version="1",
                raw_fields={"size": len(content)},
                normalized_fields={},
            )
        ]


class SlowRunnerConnector(SyntheticRunnerConnector):
    def parse(self, stream: BinaryIO) -> list[ParsedSourceRecord]:
        import time

        time.sleep(0.2)
        return super().parse(stream)


class InvalidDetectionConnector(SyntheticRunnerConnector):
    def detect(self, metadata: ArtifactMetadata, bounded_prefix: bytes) -> DetectionResult:
        return "MATCH"  # type: ignore[return-value]


class RaisingDetectionConnector(SyntheticRunnerConnector):
    def detect(self, metadata: ArtifactMetadata, bounded_prefix: bytes) -> DetectionResult:
        raise ValueError("secret detector detail")


class InvalidRecordConnector(SyntheticRunnerConnector):
    def parse(self, stream: BinaryIO) -> list[ParsedSourceRecord]:
        return [object()]  # type: ignore[list-item]


class DuplicateRecordConnector(SyntheticRunnerConnector):
    def parse(self, stream: BinaryIO) -> list[ParsedSourceRecord]:
        value = ParsedSourceRecord(
            record_locator="same",
            source="synthetic",
            parser_version="1",
            raw_fields={},
            normalized_fields={},
        )
        return [value, value]


class WrongProvenanceConnector(SyntheticRunnerConnector):
    def parse(self, stream: BinaryIO) -> list[ParsedSourceRecord]:
        return [
            ParsedSourceRecord(
                record_locator="row:1",
                source="other",
                parser_version="1",
                raw_fields={},
                normalized_fields={},
            )
        ]


class WrongVersionConnector(SyntheticRunnerConnector):
    def parse(self, stream: BinaryIO) -> list[ParsedSourceRecord]:
        return [
            ParsedSourceRecord(
                record_locator="row:1",
                source="synthetic",
                parser_version="2",
                raw_fields={},
                normalized_fields={},
            )
        ]


class RaisingConnector(SyntheticRunnerConnector):
    def parse(self, stream: BinaryIO) -> list[ParsedSourceRecord]:
        raise ValueError("secret parser detail")


class ContractErrorConnector(SyntheticRunnerConnector):
    def parse(self, stream: BinaryIO) -> list[ParsedSourceRecord]:
        from ledgerbridge.connectors import ConnectorContractError

        raise ConnectorContractError("invalid output")


class MismatchedIdentityConnector(SyntheticRunnerConnector):
    name = "different.csv"


class MismatchedSourceConnector(SyntheticRunnerConnector):
    source_system = "other"


class InvalidMetadataConnector(SyntheticRunnerConnector):
    name = ""


class FailingSupervisor(ConnectorSupervisor):
    def __init__(self, failure: BaseException) -> None:
        super().__init__()
        self.failure = failure

    async def _receive_and_execute_async(
        self,
        reader: asyncio.StreamReader,
        request: RunnerRequest,
    ) -> runner_module._ExecutionResult:
        raise self.failure


class SlowResponseServer:
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def __call__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await read_async_frame(reader)
        for value in struct.pack("!I", 1025):
            writer.write(bytes([value]))
            await writer.drain()
            await asyncio.sleep(self.delay)
        writer.write(bytes([int(FrameKind.TERMINAL)]))
        await writer.drain()
        for _ in range(100):
            writer.write(b"\x00")
            await writer.drain()
            await asyncio.sleep(self.delay)
        writer.close()


class _MemoryWriter:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closed = False

    def write(self, value: bytes) -> None:
        self.frames.append(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _read_client_frames(
    client: ConnectorRunnerClient,
    request: RunnerRequest,
    frames: list[bytes],
) -> object:
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(b"".join(frames))
        sender.shutdown(socket.SHUT_WR)
        return client._read_result(receiver, request)
    finally:
        sender.close()
        receiver.close()


def _request(content: bytes, operation: RunnerOperation = RunnerOperation.PARSE) -> RunnerRequest:
    digest = hashlib.sha256(content).hexdigest()
    return RunnerRequest(
        request_id=str(uuid4()),
        operation=operation,
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


async def _serve(
    socket_path: str, connector: object, timeout: float = 1.0
) -> tuple[asyncio.Server, asyncio.Task[None]]:
    supervisor = ConnectorSupervisor(
        {("synthetic.csv", "1"): connector},  # type: ignore[dict-item]
        request_timeout_seconds=timeout,
    )
    start_unix_server = getattr(asyncio, "start_unix_server", None)
    if start_unix_server is None:
        raise RuntimeError("Unix socket tests require a POSIX asyncio implementation")
    server = await start_unix_server(
        supervisor.handle,
        path=socket_path,
    )
    task = asyncio.create_task(server.serve_forever())
    return server, task


@pytest.mark.asyncio
async def test_runner_health_detect_and_parse_round_trip() -> None:
    with TemporaryDirectory() as directory:
        socket_path = str(Path(directory) / "runner.sock")
        server, task = await _serve(socket_path, SyntheticRunnerConnector())
        try:
            client = ConnectorRunnerClient(socket_path)
            assert await asyncio.to_thread(client.health_check)
            content = b"ok,1\n"
            assert (
                await asyncio.to_thread(
                    client.detect,
                    _request(content, RunnerOperation.DETECT),
                    _bytes(content),
                )
                is DetectionResult.MATCH
            )
            records = await asyncio.to_thread(
                client.parse,
                _request(content, RunnerOperation.PARSE),
                _bytes(content),
            )
            assert len(records) == 1
            assert records[0].record_locator == "row:5"
        finally:
            server.close()
            await server.wait_closed()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_runner_client_enforces_overall_response_deadline() -> None:
    with TemporaryDirectory() as directory:
        socket_path = str(Path(directory) / "runner.sock")
        start_unix_server = getattr(asyncio, "start_unix_server", None)
        if start_unix_server is None:
            raise RuntimeError("Unix socket tests require a POSIX socket implementation")
        server = await start_unix_server(SlowResponseServer(0.02), path=socket_path)
        try:
            client = ConnectorRunnerClient(socket_path, timeout_seconds=0.2)
            started = time.monotonic()
            with pytest.raises(RunnerClientError, match="unavailable") as error:
                await asyncio.to_thread(
                    client.parse,
                    _request(b"ok", RunnerOperation.PARSE),
                    _bytes(b"ok"),
                )
            assert error.value.error_code == "RUNNER_UNAVAILABLE"
            assert time.monotonic() - started < 0.8
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_public_serve_sets_private_socket_mode() -> None:
    with TemporaryDirectory() as directory:
        socket_path = str(Path(directory) / "nested" / "runner.sock")
        task = asyncio.create_task(runner_module.serve(socket_path))
        try:
            for _ in range(100):
                if Path(socket_path).exists():
                    break
                await asyncio.sleep(0.01)
            socket_file = Path(socket_path)
            assert socket_file.exists()
            assert stat.S_IMODE(socket_file.stat().st_mode) == 0o600
            assert stat.S_IMODE(socket_file.parent.stat().st_mode) & 0o022 == 0
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_runner_maps_timeout_to_bounded_error() -> None:
    with TemporaryDirectory() as directory:
        socket_path = str(Path(directory) / "runner.sock")
        server, task = await _serve(socket_path, SlowRunnerConnector(), timeout=0.05)
        try:
            client = ConnectorRunnerClient(socket_path, timeout_seconds=1)
            with pytest.raises(RunnerClientError, match="timed out") as error:
                await asyncio.to_thread(
                    client.parse,
                    _request(b"ok", RunnerOperation.PARSE),
                    _bytes(b"ok"),
                )
            assert error.value.error_code == "TIMEOUT"
        finally:
            server.close()
            await server.wait_closed()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_stale_response_id_is_rejected() -> None:
    with TemporaryDirectory() as directory:
        socket_path = str(Path(directory) / "runner.sock")

        async def stale_handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            while True:
                kind, _payload = await read_async_frame(reader)
                if kind is FrameKind.ARTIFACT_END:
                    break
            request = _request(b"ok", RunnerOperation.PARSE)
            terminal = RunnerTerminal(
                request_id=str(uuid4()),
                status=RunnerStatus.OK,
                operation=RunnerOperation.PARSE,
                error_code=None,
                summary="connector completed",
                detection=None,
                parsed_count=0,
                byte_count=request.declared_artifact_size,
                sha256_hex=request.verified_sha256_hex,
            )
            writer.write(encode_frame(FrameKind.TERMINAL, terminal_payload(terminal)))
            await writer.drain()
            writer.close()

        start_unix_server = getattr(asyncio, "start_unix_server", None)
        if start_unix_server is None:
            raise RuntimeError("Unix socket tests require a POSIX asyncio implementation")
        server = await start_unix_server(
            stale_handler,
            path=socket_path,
        )
        try:
            client = ConnectorRunnerClient(socket_path)
            with pytest.raises(RunnerClientError, match="request_id") as error:
                await asyncio.to_thread(
                    client.parse,
                    _request(b"ok", RunnerOperation.PARSE),
                    _bytes(b"ok"),
                )
            assert error.value.error_code == "STALE_RESPONSE"
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_runner_rejects_digest_mismatch_without_records() -> None:
    with TemporaryDirectory() as directory:
        socket_path = str(Path(directory) / "runner.sock")
        server, task = await _serve(socket_path, SyntheticRunnerConnector())
        request = _request(b"expected", RunnerOperation.PARSE)

        def send_mismatch() -> str:
            unix_family = getattr(socket, "AF_UNIX", None)
            if unix_family is None:
                raise RuntimeError("Unix socket tests require a POSIX socket implementation")
            connection = socket.socket(
                unix_family,
                socket.SOCK_STREAM,
            )
            try:
                connection.connect(socket_path)
                connection.sendall(encode_frame(FrameKind.CONTROL, request_control(request)))
                connection.sendall(encode_frame(FrameKind.ARTIFACT_CHUNK, b"tampered"))
                connection.sendall(
                    encode_frame(
                        FrameKind.ARTIFACT_END,
                        artifact_end_payload(8, hashlib.sha256(b"tampered").hexdigest()),
                    )
                )
                kind, payload = read_frame(_SocketReader(connection))
                assert kind is FrameKind.TERMINAL
                terminal = parse_terminal_payload(payload)
                assert terminal.error_code == "ARTIFACT_DIGEST_MISMATCH"
                return terminal.error_code or ""
            finally:
                connection.close()

        try:
            assert await asyncio.to_thread(send_mismatch) == "ARTIFACT_DIGEST_MISMATCH"
        finally:
            server.close()
            await server.wait_closed()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


def test_supervisor_rejects_invalid_timeout_and_unknown_connector() -> None:
    with pytest.raises(ValueError, match="at most 90"):
        ConnectorSupervisor(request_timeout_seconds=91)
    with pytest.raises(ValueError, match="positive"):
        ConnectorSupervisor(request_timeout_seconds=0)
    supervisor = ConnectorSupervisor()
    with pytest.raises(RunnerExecutionError, match="not registered") as error:
        supervisor._execute_with_artifact(_request(b"ok", RunnerOperation.PARSE), BytesIO(b"ok"))
    assert error.value.error_code == "CONNECTOR_UNKNOWN"


@pytest.mark.parametrize(
    ("connector", "code"),
    [
        (InvalidDetectionConnector(), "CONNECTOR_CONTRACT"),
        (InvalidRecordConnector(), "CONNECTOR_CONTRACT"),
        (DuplicateRecordConnector(), "DUPLICATE_LOCATOR"),
        (WrongProvenanceConnector(), "PROVENANCE_MISMATCH"),
        (WrongVersionConnector(), "PROVENANCE_MISMATCH"),
        (RaisingConnector(), "PARSE_ERROR"),
        (ContractErrorConnector(), "CONNECTOR_CONTRACT"),
        (MismatchedIdentityConnector(), "CONNECTOR_IDENTITY"),
        (MismatchedSourceConnector(), "SOURCE_SYSTEM_MISMATCH"),
    ],
)
def test_supervisor_bounds_connector_contract_failures(connector: object, code: str) -> None:
    supervisor = ConnectorSupervisor({("synthetic.csv", "1"): connector})  # type: ignore[dict-item]
    operation = (
        RunnerOperation.DETECT
        if isinstance(connector, InvalidDetectionConnector)
        else RunnerOperation.PARSE
    )
    with pytest.raises(RunnerExecutionError) as error:
        supervisor._execute_with_artifact(_request(b"ok", operation), BytesIO(b"ok"))
    assert error.value.error_code == code
    assert "secret" not in error.value.summary


def test_supervisor_bounds_detection_and_record_execution_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detection = ConnectorSupervisor(
        {("synthetic.csv", "1"): RaisingDetectionConnector()}  # type: ignore[dict-item]
    )
    with pytest.raises(RunnerExecutionError, match="detection failed") as error:
        detection._execute_with_artifact(_request(b"ok", RunnerOperation.DETECT), BytesIO(b"ok"))
    assert error.value.error_code == "DETECTION_ERROR"

    metadata = ConnectorSupervisor(
        {("synthetic.csv", "1"): InvalidMetadataConnector()}  # type: ignore[dict-item]
    )
    with pytest.raises(RunnerExecutionError, match="metadata") as error:
        metadata._execute_with_artifact(_request(b"ok"), BytesIO(b"ok"))
    assert error.value.error_code == "CONNECTOR_CONTRACT"

    monkeypatch.setattr(runner_module, "MAX_RECORDS", 0)
    limited = ConnectorSupervisor(
        {("synthetic.csv", "1"): SyntheticRunnerConnector()}  # type: ignore[dict-item]
    )
    with pytest.raises(RunnerExecutionError, match="record limit") as error:
        limited._execute_with_artifact(_request(b"ok"), BytesIO(b"ok"))
    assert error.value.error_code == "RECORD_LIMIT"


@pytest.mark.asyncio
async def test_supervisor_bounds_artifact_stream_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(b"x")

    async def execute(frames: list[bytes]) -> None:
        reader = asyncio.StreamReader()
        for frame in frames:
            reader.feed_data(frame)
        reader.feed_eof()
        await ConnectorSupervisor()._receive_and_execute_async(reader, request)

    monkeypatch.setattr(runner_module, "MAX_CHUNK_COUNT", 0)
    with pytest.raises(RunnerExecutionError, match="chunk limit"):
        await execute([encode_frame(FrameKind.ARTIFACT_CHUNK, b"x")])

    monkeypatch.setattr(runner_module, "MAX_CHUNK_COUNT", 1000)
    monkeypatch.setattr(runner_module, "MAX_ARTIFACT_BYTES", 1)
    with pytest.raises(RunnerExecutionError, match="exceeds its declaration"):
        await execute([encode_frame(FrameKind.ARTIFACT_CHUNK, b"xx")])

    monkeypatch.setattr(runner_module, "MAX_ARTIFACT_BYTES", 50 * 1024 * 1024)
    with pytest.raises(RunnerProtocolError, match="unexpected frame"):
        await execute([encode_frame(FrameKind.CONTROL, b"x")])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "error_code"),
    [(ConnectionError("peer closed"), None), (ValueError("unexpected"), "RUNNER_INTERNAL")],
)
async def test_supervisor_handles_peer_and_internal_failures(
    failure: BaseException,
    error_code: str | None,
) -> None:
    request = _request(b"ok")
    reader = asyncio.StreamReader()
    reader.feed_data(encode_frame(FrameKind.CONTROL, request_control(request)))
    reader.feed_eof()
    writer = _MemoryWriter()
    await FailingSupervisor(failure).handle(reader, writer)  # type: ignore[arg-type]
    if error_code is None:
        assert writer.frames == []
    else:
        assert parse_terminal_payload(decode_frame(writer.frames[0])[1]).error_code == error_code


@pytest.mark.asyncio
async def test_supervisor_handles_truncated_and_malformed_requests() -> None:
    async def run(payload: bytes) -> _MemoryWriter:
        reader = asyncio.StreamReader()
        reader.feed_data(payload)
        reader.feed_eof()
        writer = _MemoryWriter()
        await ConnectorSupervisor().handle(reader, writer)  # type: ignore[arg-type]
        return writer

    malformed = await run(encode_frame(FrameKind.ARTIFACT_CHUNK, b"x"))
    kind, payload = decode_frame(malformed.frames[0])
    assert kind is FrameKind.TERMINAL
    assert parse_terminal_payload(payload).error_code == "MALFORMED_REQUEST"

    truncated = await run(encode_frame(FrameKind.CONTROL, request_control(_request(b"ok"))))
    kind, payload = decode_frame(truncated.frames[0])
    assert kind is FrameKind.TERMINAL
    assert parse_terminal_payload(payload).error_code == "TRUNCATED_STREAM"


@pytest.mark.asyncio
async def test_supervisor_unknown_connector_returns_terminal_without_records() -> None:
    request = _request(b"ok")
    reader = asyncio.StreamReader()
    for frame in chunk_frames(request, BytesIO(b"ok")):
        reader.feed_data(frame)
    reader.feed_eof()
    writer = _MemoryWriter()
    await ConnectorSupervisor().handle(reader, writer)  # type: ignore[arg-type]
    assert len(writer.frames) == 1
    kind, payload = decode_frame(writer.frames[0])
    assert kind is FrameKind.TERMINAL
    terminal = parse_terminal_payload(payload)
    assert terminal.error_code == "CONNECTOR_UNKNOWN"
    assert terminal.parsed_count == 0


@pytest.mark.asyncio
async def test_supervisor_send_success_enforces_response_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(b"ok")
    record = ParsedSourceRecord(
        record_locator="row:1",
        source="synthetic",
        parser_version="1",
        raw_fields={},
        normalized_fields={},
    )
    execution = runner_module._ExecutionResult(detection=None, records=(record,))
    writer = _MemoryWriter()
    monkeypatch.setattr(runner_module, "MAX_RESPONSE_BYTES", 10)
    await ConnectorSupervisor()._send_success(writer, request, execution)  # type: ignore[arg-type]
    assert parse_terminal_payload(decode_frame(writer.frames[-1])[1]).error_code == "RESPONSE_LIMIT"

    writer = _MemoryWriter()
    monkeypatch.setattr(runner_module, "MAX_RESPONSE_BYTES", 100)
    monkeypatch.setattr(runner_module, "record_payload", lambda request_id, record: b"x")
    monkeypatch.setattr(runner_module, "terminal_payload", lambda terminal: b"x" * 100)
    await ConnectorSupervisor()._send_success(writer, request, execution)  # type: ignore[arg-type]
    assert writer.frames


def test_runner_connector_facade_requires_verified_detection() -> None:
    class StubClient:
        def __init__(self) -> None:
            self.detected: RunnerRequest | None = None
            self.parsed: RunnerRequest | None = None

        def detect(self, request: RunnerRequest, stream: BinaryIO) -> DetectionResult:
            self.detected = request
            return DetectionResult.MATCH

        def parse(self, request: RunnerRequest, stream: BinaryIO) -> tuple[ParsedSourceRecord, ...]:
            self.parsed = request
            return ()

    client = StubClient()
    connector = RunnerConnector("synthetic.csv", "1", "synthetic", client)  # type: ignore[arg-type]
    metadata = _request(b"ok").metadata
    with pytest.raises(ConnectorContractError, match="preceding verified"):
        connector.parse(BytesIO(b"ok"))
    assert connector.detect(metadata, b"ok") is DetectionResult.MATCH
    assert client.detected is not None
    assert connector.detect_verified(metadata, BytesIO(b"ok")) is DetectionResult.MATCH
    assert tuple(connector.parse(BytesIO(b"ok"))) == ()
    assert client.parsed is not None and client.parsed.operation is RunnerOperation.PARSE


def test_runner_client_rejects_invalid_calls_and_unavailable_socket() -> None:
    request = _request(b"ok")
    with pytest.raises(ValueError, match="at most 90"):
        ConnectorRunnerClient("/tmp/unused", timeout_seconds=91)
    client = ConnectorRunnerClient("/tmp/does-not-exist-runner.sock", timeout_seconds=0.1)
    assert client.health_check() is False
    with pytest.raises(RunnerClientError, match="unavailable") as error:
        client.parse(request, BytesIO(b"ok"))
    assert error.value.error_code == "RUNNER_UNAVAILABLE"
    with pytest.raises(ValueError, match="detect request"):
        client.detect(request, BytesIO(b"ok"))
    detect_request = _request(b"ok")
    detect_request = RunnerRequest(
        request_id=detect_request.request_id,
        operation=RunnerOperation.DETECT,
        connector_name=detect_request.connector_name,
        connector_version=detect_request.connector_version,
        source_system=detect_request.source_system,
        metadata=detect_request.metadata,
        declared_artifact_size=detect_request.declared_artifact_size,
        verified_sha256_hex=detect_request.verified_sha256_hex,
    )
    with pytest.raises(ValueError, match="parse request"):
        client.parse(detect_request, BytesIO(b"ok"))


@pytest.mark.parametrize(
    ("error_code", "summary", "expected_summary"),
    [
        (
            "lower case; DROP--",
            "runner supplied an invalid code",
            "runner supplied an invalid code",
        ),
        ("A" * 100, "   ", "connector runner failed"),
        ("RUNNER_ERROR", "\x00", "connector runner failed"),
        ("RUNNER_ERROR", "\ud800", "connector runner failed"),
    ],
)
def test_runner_client_normalizes_untrusted_error_details(
    error_code: str,
    summary: str,
    expected_summary: str,
) -> None:
    error = RunnerClientError(error_code, summary)
    assert error.error_code == "RUNNER_ERROR"
    assert error.summary == expected_summary
    assert len(error.summary) <= 500


def test_runner_client_response_validation_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    client = ConnectorRunnerClient("unused")
    request = _request(b"ok")
    terminal = RunnerTerminal(
        request_id=request.request_id,
        status=RunnerStatus.OK,
        operation=RunnerOperation.PARSE,
        error_code=None,
        summary="connector completed",
        detection=None,
        parsed_count=0,
        byte_count=request.declared_artifact_size,
        sha256_hex=request.verified_sha256_hex,
    )
    record = ParsedSourceRecord(
        record_locator="row:1",
        source="synthetic",
        parser_version="1",
        raw_fields={},
        normalized_fields={},
    )

    def assert_error(frames: list[bytes], code: str) -> None:
        with pytest.raises(RunnerClientError) as error:
            _read_client_frames(client, request, frames)
        assert error.value.error_code == code

    assert_error(
        [
            encode_frame(
                FrameKind.TERMINAL,
                terminal_payload(
                    RunnerTerminal(
                        request_id=request.request_id,
                        status=RunnerStatus.ERROR,
                        operation=RunnerOperation.PARSE,
                        error_code="BOUNDED",
                        summary="failed",
                        detection=None,
                        parsed_count=0,
                        byte_count=0,
                        sha256_hex=None,
                    )
                ),
            )
        ],
        "BOUNDED",
    )
    assert_error(
        [
            encode_frame(
                FrameKind.TERMINAL,
                terminal_payload(
                    RunnerTerminal(
                        request_id=request.request_id,
                        status=RunnerStatus.ERROR,
                        operation=RunnerOperation.PARSE,
                        error_code="lower case; DROP--",
                        summary="runner rejected the artifact",
                        detection=None,
                        parsed_count=0,
                        byte_count=0,
                        sha256_hex=None,
                    )
                ),
            )
        ],
        "RUNNER_ERROR",
    )
    assert_error(
        [
            encode_frame(FrameKind.RECORD, record_payload(request.request_id, record)),
            encode_frame(FrameKind.TERMINAL, terminal_payload(terminal)),
        ],
        "RUNNER_PROTOCOL",
    )
    mismatch = RunnerTerminal(
        request_id=request.request_id,
        status=RunnerStatus.OK,
        operation=RunnerOperation.PARSE,
        error_code=None,
        summary="connector completed",
        detection=None,
        parsed_count=1,
        byte_count=request.declared_artifact_size,
        sha256_hex=request.verified_sha256_hex,
    )
    assert_error([encode_frame(FrameKind.TERMINAL, terminal_payload(mismatch))], "RUNNER_PROTOCOL")
    assert_error([encode_frame(FrameKind.CONTROL, b"x")], "RUNNER_PROTOCOL")

    wrong_operation = RunnerTerminal(
        request_id=request.request_id,
        status=RunnerStatus.OK,
        operation=RunnerOperation.DETECT,
        error_code=None,
        summary="connector completed",
        detection=None,
        parsed_count=0,
        byte_count=request.declared_artifact_size,
        sha256_hex=request.verified_sha256_hex,
    )
    assert_error(
        [encode_frame(FrameKind.TERMINAL, terminal_payload(wrong_operation))], "RUNNER_PROTOCOL"
    )
    for field, code in [
        ("byte_count", "ARTIFACT_SIZE_MISMATCH"),
        ("sha256_hex", "ARTIFACT_DIGEST_MISMATCH"),
    ]:
        byte_count = request.declared_artifact_size + 1
        sha256_hex = request.verified_sha256_hex
        if field == "sha256_hex":
            byte_count = request.declared_artifact_size
            sha256_hex = "0" * 64
        bad = RunnerTerminal(
            request_id=request.request_id,
            status=RunnerStatus.OK,
            operation=RunnerOperation.PARSE,
            error_code=None,
            summary="connector completed",
            detection=None,
            parsed_count=0,
            byte_count=byte_count,
            sha256_hex=sha256_hex,
        )
        assert_error([encode_frame(FrameKind.TERMINAL, terminal_payload(bad))], code)

    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 1)
    assert_error([encode_frame(FrameKind.TERMINAL, terminal_payload(terminal))], "RESPONSE_LIMIT")
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", MAX_RESPONSE_BYTES)
    monkeypatch.setattr(client_module, "MAX_RECORDS", 0)
    assert_error(
        [encode_frame(FrameKind.RECORD, record_payload(request.request_id, record))], "RECORD_LIMIT"
    )


def test_runner_client_health_response_validation() -> None:
    client = ConnectorRunnerClient("unused")
    request_id = str(uuid4())
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(encode_frame(FrameKind.RECORD, b"x"))
        sender.shutdown(socket.SHUT_WR)
        with pytest.raises(RunnerClientError, match="not terminal"):
            client._read_terminal(receiver, request_id)
    finally:
        sender.close()
        receiver.close()

    sender, receiver = socket.socketpair()
    try:
        terminal = RunnerTerminal(
            request_id=str(uuid4()),
            status=RunnerStatus.OK,
            operation=None,
            error_code=None,
            summary="runner ready",
            detection=None,
            parsed_count=0,
            byte_count=0,
            sha256_hex=None,
        )
        sender.sendall(encode_frame(FrameKind.TERMINAL, terminal_payload(terminal)))
        sender.shutdown(socket.SHUT_WR)
        with pytest.raises(RunnerClientError, match="request_id"):
            client._read_terminal(receiver, request_id)
    finally:
        sender.close()
        receiver.close()


def test_runner_client_detect_rejects_missing_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ConnectorRunnerClient("unused")
    request = _request(b"ok", RunnerOperation.DETECT)
    terminal = RunnerTerminal(
        request_id=request.request_id,
        status=RunnerStatus.OK,
        operation=RunnerOperation.DETECT,
        error_code=None,
        summary="connector completed",
        detection=None,
        parsed_count=0,
        byte_count=request.declared_artifact_size,
        sha256_hex=request.verified_sha256_hex,
    )
    monkeypatch.setattr(
        client,
        "_request",
        lambda _request, _stream: client_module.RunnerResult(terminal, ()),
    )
    with pytest.raises(RunnerClientError, match="omitted detection") as error:
        client.detect(request, BytesIO(b"ok"))
    assert error.value.error_code == "RUNNER_PROTOCOL"


def test_runner_client_socket_reader_default_size() -> None:
    sender, receiver = socket.socketpair()
    try:
        sender.sendall(b"x")
        assert client_module._SocketReader(receiver).read() == b"x"
    finally:
        sender.close()
        receiver.close()


def _bytes(content: bytes) -> BinaryIO:
    return BytesIO(content)


class _SocketReader:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection

    def read(self, size: int = -1) -> bytes:
        return self.connection.recv(size if size >= 0 else 65536)
