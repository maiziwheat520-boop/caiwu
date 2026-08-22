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

from ledgerbridge.connector_runner import ConnectorSupervisor
from ledgerbridge.connectors import ArtifactMetadata, DetectionResult, ParsedSourceRecord
from ledgerbridge.runner_client import ConnectorRunnerClient, RunnerClientError
from ledgerbridge.runner_protocol import (
    FrameKind,
    RunnerOperation,
    RunnerRequest,
    RunnerStatus,
    RunnerTerminal,
    artifact_end_payload,
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


def _request(content: bytes, operation: RunnerOperation) -> RunnerRequest:
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


def _bytes(content: bytes) -> BinaryIO:
    return BytesIO(content)


class _SocketReader:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection

    def read(self, size: int = -1) -> bytes:
        return self.connection.recv(size if size >= 0 else 65536)
