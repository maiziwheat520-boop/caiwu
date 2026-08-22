from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO
from uuid import uuid4

import pytest

import ledgerbridge.connector_runner as runner_module
from ledgerbridge.connector_runner import ConnectorSupervisor, RunnerExecutionError
from ledgerbridge.connectors import (
    ArtifactMetadata,
    ConnectorContractError,
    DetectionResult,
    ParsedSourceRecord,
)
from ledgerbridge.runner_client import ConnectorRunnerClient, RunnerClientError, RunnerConnector
from ledgerbridge.runner_protocol import (
    FrameKind,
    RunnerOperation,
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
            await read_async_frame(reader)
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


def _bytes(content: bytes) -> BinaryIO:
    return BytesIO(content)


class _SocketReader:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection

    def read(self, size: int = -1) -> bytes:
        return self.connection.recv(size if size >= 0 else 65536)
