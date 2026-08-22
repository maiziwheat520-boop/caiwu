"""Worker-side client for the bounded Connector runner protocol."""

from __future__ import annotations

import hashlib
import re
import socket
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from typing import BinaryIO
from uuid import uuid4

from ledgerbridge.connectors import (
    ArtifactMetadata,
    ConnectorContractError,
    DetectionResult,
    ParsedSourceRecord,
)
from ledgerbridge.runner_protocol import (
    MAX_RECORDS,
    MAX_REQUEST_SECONDS,
    MAX_RESPONSE_BYTES,
    FrameKind,
    RunnerOperation,
    RunnerProtocolError,
    RunnerRequest,
    RunnerStatus,
    RunnerTerminal,
    chunk_frames,
    encode_frame,
    health_control,
    parse_record_payload,
    parse_terminal_payload,
    read_frame,
)


class RunnerClientError(RuntimeError):
    """A bounded runner failure safe to map to an ImportJob error code."""

    def __init__(self, error_code: str, summary: str) -> None:
        normalized_code = (
            error_code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code) else "RUNNER_ERROR"
        )
        normalized_summary = summary.strip()[:500]
        if not normalized_summary or _contains_unstorable_text(normalized_summary):
            normalized_summary = "connector runner failed"
        super().__init__(normalized_summary)
        self.error_code = normalized_code
        self.summary = normalized_summary


@dataclass(frozen=True, slots=True)
class RunnerResult:
    terminal: RunnerTerminal
    records: tuple[ParsedSourceRecord, ...]


def _contains_unstorable_text(value: str) -> bool:
    return any(codepoint == 0 or 0xD800 <= codepoint <= 0xDFFF for codepoint in map(ord, value))


class ConnectorRunnerClient:
    def __init__(self, socket_path: str, *, timeout_seconds: float = MAX_REQUEST_SECONDS) -> None:
        if timeout_seconds <= 0 or timeout_seconds > MAX_REQUEST_SECONDS:
            raise ValueError("runner timeout must be positive and at most 90 seconds")
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds

    def health_check(self) -> bool:
        request_id = str(uuid4())
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with self._connect() as connection:
                connection.sendall(encode_frame(FrameKind.CONTROL, health_control(request_id)))
                terminal = self._read_terminal(connection, request_id, deadline=deadline)
        except (OSError, RunnerProtocolError, RunnerClientError):
            return False
        return terminal.status is RunnerStatus.OK and terminal.summary == "runner ready"

    def detect(self, request: RunnerRequest, stream: BinaryIO) -> DetectionResult:
        if request.operation is not RunnerOperation.DETECT:
            raise ValueError("detect request must use detect operation")
        result = self._request(request, stream)
        if result.terminal.detection is None:
            raise RunnerClientError("RUNNER_PROTOCOL", "runner omitted detection result")
        return result.terminal.detection

    def parse(self, request: RunnerRequest, stream: BinaryIO) -> tuple[ParsedSourceRecord, ...]:
        if request.operation is not RunnerOperation.PARSE:
            raise ValueError("parse request must use parse operation")
        return self._request(request, stream).records

    def _request(self, request: RunnerRequest, stream: BinaryIO) -> RunnerResult:
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with self._connect() as connection:
                for frame in chunk_frames(request, stream):
                    connection.sendall(frame)
                return self._read_result(connection, request, deadline=deadline)
        except RunnerClientError:
            raise
        except RunnerProtocolError as exc:
            raise RunnerClientError(
                "RUNNER_PROTOCOL", "runner protocol rejected the request"
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise RunnerClientError("RUNNER_UNAVAILABLE", "connector runner unavailable") from exc

    def _connect(self) -> socket.socket:
        connection = socket.socket(getattr(socket, "AF_UNIX", 1), socket.SOCK_STREAM)
        connection.settimeout(self.timeout_seconds)
        try:
            connection.connect(self.socket_path)
        except OSError:
            connection.close()
            raise
        return connection

    def _read_result(
        self,
        connection: socket.socket,
        request: RunnerRequest,
        *,
        deadline: float | None = None,
    ) -> RunnerResult:
        records: list[ParsedSourceRecord] = []
        response_bytes = 0
        if deadline is None:
            deadline = time.monotonic() + self.timeout_seconds
        reader = _SocketReader(connection, deadline=deadline)
        while True:
            kind, payload = read_frame(reader)
            response_bytes += 4 + 1 + len(payload)
            if response_bytes > MAX_RESPONSE_BYTES:
                raise RunnerClientError("RESPONSE_LIMIT", "connector response exceeded the limit")
            if kind is FrameKind.RECORD:
                if request.operation is not RunnerOperation.PARSE:
                    raise RunnerClientError("RUNNER_PROTOCOL", "detect response contained records")
                if len(records) >= MAX_RECORDS:
                    raise RunnerClientError(
                        "RECORD_LIMIT", "connector response exceeded the record limit"
                    )
                records.append(parse_record_payload(payload, request))
                continue
            if kind is not FrameKind.TERMINAL:
                raise RunnerClientError(
                    "RUNNER_PROTOCOL", "runner response contained an unexpected frame"
                )
            terminal = parse_terminal_payload(payload)
            self._validate_terminal(terminal, request)
            if terminal.status is RunnerStatus.ERROR:
                raise RunnerClientError(
                    terminal.error_code or "RUNNER_ERROR",
                    terminal.summary,
                )
            if terminal.parsed_count != len(records):
                raise RunnerClientError(
                    "RUNNER_PROTOCOL", "runner record count did not match terminal"
                )
            return RunnerResult(terminal=terminal, records=tuple(records))

    def _read_terminal(
        self,
        connection: socket.socket,
        request_id: str,
        *,
        deadline: float | None = None,
    ) -> RunnerTerminal:
        if deadline is None:
            deadline = time.monotonic() + self.timeout_seconds
        kind, payload = read_frame(_SocketReader(connection, deadline=deadline))
        if kind is not FrameKind.TERMINAL:
            raise RunnerClientError("RUNNER_PROTOCOL", "health response was not terminal")
        terminal = parse_terminal_payload(payload)
        if terminal.request_id != request_id:
            raise RunnerClientError("STALE_RESPONSE", "runner response request_id did not match")
        return terminal

    @staticmethod
    def _validate_terminal(terminal: RunnerTerminal, request: RunnerRequest) -> None:
        if terminal.request_id != request.request_id:
            raise RunnerClientError("STALE_RESPONSE", "runner response request_id did not match")
        if terminal.operation is not request.operation:
            raise RunnerClientError("RUNNER_PROTOCOL", "runner response operation did not match")
        if terminal.status is RunnerStatus.ERROR:
            return
        if terminal.byte_count != request.declared_artifact_size:
            raise RunnerClientError(
                "ARTIFACT_SIZE_MISMATCH", "runner byte count did not match request"
            )
        if terminal.sha256_hex != request.verified_sha256_hex:
            raise RunnerClientError(
                "ARTIFACT_DIGEST_MISMATCH", "runner digest did not match request"
            )


class _SocketReader:
    def __init__(self, connection: socket.socket, *, deadline: float | None = None) -> None:
        self._connection = connection
        self._deadline = deadline

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = 64 * 1024
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("runner response deadline exceeded")
            self._connection.settimeout(remaining)
        return self._connection.recv(size)


class RunnerConnector:
    """Synthetic/test connector facade that routes calls through the runner.

    Production manifests must select ``execution_mode=runner`` and are enabled
    only by a later reviewed change.  The facade keeps the Phase 2 Connector
    shape so the existing importer can exercise the same output validation.
    """

    execution_mode = "runner"

    def __init__(
        self,
        name: str,
        version: str,
        source_system: str,
        client: ConnectorRunnerClient,
    ) -> None:
        self.name = name
        self.version = version
        self.source_system = source_system
        self._client = client
        self._pending_request: RunnerRequest | None = None
        self._lock = threading.Lock()

    def detect(self, metadata: ArtifactMetadata, bounded_prefix: bytes) -> DetectionResult:
        # Direct in-process calls only have a prefix.  Keep this helper useful
        # for synthetic fixtures by declaring and hashing exactly that bounded
        # prefix; production importer calls detect_verified below with the full
        # verified artifact stream.
        prefix_digest = hashlib.sha256(bounded_prefix).hexdigest()
        prefix_metadata = ArtifactMetadata(
            source=metadata.source,
            original_filename=metadata.original_filename,
            media_type=metadata.media_type,
            byte_size=len(bounded_prefix),
            sha256_hex=prefix_digest,
        )
        request = self._request(prefix_metadata, RunnerOperation.DETECT)
        return self._client.detect(request, BytesIO(bounded_prefix))

    def detect_verified(self, metadata: ArtifactMetadata, stream: BinaryIO) -> DetectionResult:
        request = self._request(metadata, RunnerOperation.DETECT)
        result = self._client.detect(request, stream)
        with self._lock:
            self._pending_request = request
        return result

    def parse(self, stream: BinaryIO) -> Iterable[ParsedSourceRecord]:
        with self._lock:
            request = self._pending_request
            self._pending_request = None
        if request is None:
            raise ConnectorContractError("runner parse requires a preceding verified detection")
        request = RunnerRequest(
            request_id=str(uuid4()),
            operation=RunnerOperation.PARSE,
            connector_name=request.connector_name,
            connector_version=request.connector_version,
            source_system=request.source_system,
            metadata=request.metadata,
            declared_artifact_size=request.declared_artifact_size,
            verified_sha256_hex=request.verified_sha256_hex,
        )
        return self._client.parse(request, stream)

    def _request(self, metadata: ArtifactMetadata, operation: RunnerOperation) -> RunnerRequest:
        return RunnerRequest(
            request_id=str(uuid4()),
            operation=operation,
            connector_name=self.name,
            connector_version=self.version,
            source_system=self.source_system,
            metadata=metadata,
            declared_artifact_size=metadata.byte_size,
            verified_sha256_hex=metadata.sha256_hex,
        )
