"""Isolated Connector supervisor for the Phase 3 Unix-socket boundary."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from ledgerbridge.connectors import (
    Connector,
    ConnectorContractError,
    DetectionResult,
    ParsedSourceRecord,
    validate_connector,
)
from ledgerbridge.runner_protocol import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_PREFIX_BYTES,
    MAX_CHUNK_COUNT,
    MAX_RECORDS,
    MAX_RESPONSE_BYTES,
    MAX_TERMINAL_FRAME_BYTES,
    FrameKind,
    RunnerOperation,
    RunnerProtocolError,
    RunnerRequest,
    RunnerStatus,
    RunnerTerminal,
    encode_frame,
    is_health_control,
    parse_artifact_end_payload,
    parse_health_control,
    parse_request_control,
    read_async_frame,
    record_payload,
    terminal_payload,
)

DEFAULT_SOCKET_PATH = "/run/ledgerbridge-connector/runner.sock"
# Four in-flight connector calls bound the runner's synchronous work under its
# 128 MiB container limit; callers may raise this only within the hard cap.
DEFAULT_EXECUTION_WORKERS = 4
MAX_EXECUTION_WORKERS = 8
# Admission applies before request bodies are received.  The aggregate spool
# reservation is based on the validated control frame's declared artifact size, so
# a peer cannot create unbounded 4 MiB in-memory spools or fill the runner's
# private /tmp with many near-limit artifacts before execution slots apply.
DEFAULT_MAX_CONNECTIONS = 16
MAX_CONNECTIONS = 64
SPOOL_MEMORY_BYTES = 4 * 1024 * 1024
DEFAULT_SPOOL_BUDGET_BYTES = MAX_ARTIFACT_BYTES
MAX_SPOOL_BUDGET_BYTES = 64 * 1024 * 1024
logger = logging.getLogger(__name__)


class RunnerExecutionError(RuntimeError):
    """A bounded, machine-readable Connector execution failure."""

    def __init__(self, error_code: str, summary: str) -> None:
        super().__init__(summary)
        self.error_code = error_code
        self.summary = summary


@dataclass(frozen=True, slots=True)
class _ExecutionResult:
    detection: DetectionResult | None
    records: tuple[ParsedSourceRecord, ...]


class ConnectorSupervisor:
    """Serve one bounded request per Unix connection.

    The production runner starts with an empty connector registry.  Tests and
    explicitly synthetic fixtures inject a registry; no production manifest is
    enabled by this foundation slice.
    """

    def __init__(
        self,
        connectors: Mapping[tuple[str, str], Connector] | None = None,
        *,
        request_timeout_seconds: float = 90.0,
        max_execution_workers: int = DEFAULT_EXECUTION_WORKERS,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        spool_budget_bytes: int = DEFAULT_SPOOL_BUDGET_BYTES,
    ) -> None:
        if request_timeout_seconds <= 0 or request_timeout_seconds > 90:
            raise ValueError("runner timeout must be positive and at most 90 seconds")
        if max_execution_workers <= 0 or max_execution_workers > MAX_EXECUTION_WORKERS:
            raise ValueError(
                f"runner execution workers must be between 1 and {MAX_EXECUTION_WORKERS}"
            )
        if max_connections <= 0 or max_connections > MAX_CONNECTIONS:
            raise ValueError(f"runner connections must be between 1 and {MAX_CONNECTIONS}")
        if spool_budget_bytes < MAX_ARTIFACT_BYTES:
            raise ValueError("runner spool budget must admit at least one maximum-size artifact")
        if spool_budget_bytes > MAX_SPOOL_BUDGET_BYTES:
            raise ValueError(f"runner spool budget must be at most {MAX_SPOOL_BUDGET_BYTES} bytes")
        self._connectors = dict(connectors or {})
        self._request_timeout_seconds = request_timeout_seconds
        self._execution_workers = max_execution_workers
        self._active_executions = 0
        self._connection_slots = asyncio.BoundedSemaphore(max_connections)
        self._spool_budget_bytes = spool_budget_bytes
        self._reserved_spool_bytes = 0
        self._executor = ThreadPoolExecutor(
            max_workers=max_execution_workers,
            thread_name_prefix="ledgerbridge-connector",
        )

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_id = ""
        operation: RunnerOperation | None = None
        connection_admitted = False
        try:
            if self._connection_slots.locked():
                # No control frame has been read, so there is no trustworthy
                # request_id to echo.  A transport close maps to the existing
                # retryable RUNNER_UNAVAILABLE client contract.
                return
            await self._connection_slots.acquire()
            connection_admitted = True
            first_kind, first_payload = await asyncio.wait_for(
                read_async_frame(reader), timeout=self._request_timeout_seconds
            )
            if first_kind is not FrameKind.CONTROL:
                raise RunnerProtocolError("request must begin with a control frame")
            if is_health_control(first_payload):
                request_id = parse_health_control(first_payload)
                await self._send_terminal(
                    writer,
                    RunnerTerminal(
                        request_id=request_id,
                        status=RunnerStatus.OK,
                        operation=None,
                        error_code=None,
                        summary="runner ready",
                        detection=None,
                        parsed_count=0,
                        byte_count=0,
                        sha256_hex=None,
                    ),
                )
                return

            request = parse_request_control(first_payload)
            request_id = request.request_id
            operation = request.operation
            execution = await asyncio.wait_for(
                self._receive_and_execute_async(reader, request),
                timeout=self._request_timeout_seconds,
            )
            await self._send_success(writer, request, execution)
        except TimeoutError:
            await self._send_error(writer, request_id, operation, "TIMEOUT", "connector timed out")
        except (RunnerProtocolError, RunnerExecutionError) as exc:
            error_code = getattr(exc, "error_code", "MALFORMED_REQUEST")
            summary = getattr(exc, "summary", "connector request was rejected")
            await self._send_error(writer, request_id, operation, error_code, summary)
        except asyncio.IncompleteReadError:
            await self._send_error(
                writer,
                request_id,
                operation,
                "TRUNCATED_STREAM",
                "connector stream was truncated",
            )
        except (ConnectionError, OSError):
            # A dropped socket is itself the bounded failure signal.  There is
            # no safe way to send a response after the peer has disappeared.
            logger.info("connector runner peer disconnected", extra={"request_id": request_id})
        except Exception:
            logger.exception("connector runner internal failure")
            await self._send_error(
                writer,
                request_id,
                operation,
                "RUNNER_INTERNAL",
                "connector runner failed",
            )
        finally:
            if connection_admitted:
                self._connection_slots.release()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _receive_and_execute_async(
        self,
        reader: asyncio.StreamReader,
        request: RunnerRequest,
    ) -> _ExecutionResult:
        reserved_spool_bytes = request.declared_artifact_size
        self._reserve_spool(reserved_spool_bytes)
        reservation_owned = True
        artifact: BinaryIO | None = None
        try:
            artifact_file = cast(
                BinaryIO,
                tempfile.SpooledTemporaryFile(  # noqa: SIM115 - ownership crosses timeout cancellation
                    max_size=SPOOL_MEMORY_BYTES,
                    mode="w+b",
                ),
            )
            artifact = artifact_file
            observed = 0
            chunks = 0
            digest = hashlib.sha256()
            ended = False
            while not ended:
                kind, payload = await read_async_frame(reader)
                if kind is FrameKind.ARTIFACT_CHUNK:
                    chunks += 1
                    if chunks > MAX_CHUNK_COUNT:
                        raise RunnerExecutionError(
                            "ARTIFACT_LIMIT", "artifact chunk limit exceeded"
                        )
                    observed += len(payload)
                    if observed > request.declared_artifact_size or observed > MAX_ARTIFACT_BYTES:
                        raise RunnerExecutionError(
                            "ARTIFACT_SIZE_MISMATCH", "artifact stream exceeds its declaration"
                        )
                    artifact_file.write(payload)
                    digest.update(payload)
                    continue
                if kind is FrameKind.ARTIFACT_END:
                    declared_count, declared_digest = parse_artifact_end_payload(payload)
                    if (
                        declared_count != observed
                        or declared_digest != digest.hexdigest()
                        or observed != request.declared_artifact_size
                        or declared_digest != request.verified_sha256_hex
                    ):
                        raise RunnerExecutionError(
                            "ARTIFACT_DIGEST_MISMATCH", "artifact digest or size mismatch"
                        )
                    ended = True
                    continue
                raise RunnerProtocolError("artifact stream contains an unexpected frame")
            artifact_file.seek(0)
        except BaseException:
            if artifact is not None:
                artifact.close()
            if reservation_owned:
                self._release_spool(reserved_spool_bytes)
            raise
        reservation_owned = False
        return await self._execute_with_artifact_bounded(
            request,
            artifact,
            reserved_spool_bytes=reserved_spool_bytes,
        )

    def _reserve_spool(self, byte_count: int) -> None:
        if self._reserved_spool_bytes + byte_count > self._spool_budget_bytes:
            raise RunnerExecutionError(
                "RUNNER_UNAVAILABLE",
                "runner artifact capacity is unavailable",
            )
        self._reserved_spool_bytes += byte_count

    def _release_spool(self, byte_count: int) -> None:
        self._reserved_spool_bytes -= byte_count
        if self._reserved_spool_bytes < 0:
            logger.error("runner spool reservation accounting underflow")
            self._reserved_spool_bytes = 0

    async def _execute_with_artifact_bounded(
        self,
        request: RunnerRequest,
        artifact: BinaryIO,
        *,
        reserved_spool_bytes: int = 0,
    ) -> _ExecutionResult:
        """Run connector code without growing an unbounded executor queue.

        A timeout cancels the asyncio wrapper, not the underlying synchronous
        connector call.  The execution slot therefore remains occupied until
        the concurrent future really finishes; otherwise every timeout could
        release a slot while leaving another thread alive.
        """

        if self._active_executions >= self._execution_workers:
            artifact.close()
            self._release_spool(reserved_spool_bytes)
            raise RunnerExecutionError("TIMEOUT", "connector timed out")
        self._active_executions += 1
        loop = asyncio.get_running_loop()
        try:
            future = self._executor.submit(
                self._execute_with_artifact_and_close,
                request,
                artifact,
            )
        except BaseException:
            self._active_executions -= 1
            artifact.close()
            self._release_spool(reserved_spool_bytes)
            raise

        def release_slot(_future: object) -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(
                    self._release_execution_slot,
                    reserved_spool_bytes,
                )

        future.add_done_callback(release_slot)
        return await asyncio.wrap_future(future)

    def _release_execution_slot(self, reserved_spool_bytes: int = 0) -> None:
        self._active_executions -= 1
        self._release_spool(reserved_spool_bytes)

    def _execute_with_artifact_and_close(
        self,
        request: RunnerRequest,
        artifact: BinaryIO,
    ) -> _ExecutionResult:
        try:
            return self._execute_with_artifact(request, artifact)
        finally:
            artifact.close()

    def _execute_with_artifact(
        self,
        request: RunnerRequest,
        artifact: BinaryIO,
    ) -> _ExecutionResult:
        connector = self._connectors.get((request.connector_name, request.connector_version))
        if connector is None:
            raise RunnerExecutionError("CONNECTOR_UNKNOWN", "connector is not registered")
        try:
            name, version, source_system = validate_connector(connector)
        except ConnectorContractError as exc:
            raise RunnerExecutionError(
                "CONNECTOR_CONTRACT", "connector metadata is invalid"
            ) from exc
        if (name, version) != (request.connector_name, request.connector_version):
            raise RunnerExecutionError("CONNECTOR_IDENTITY", "connector identity mismatch")
        if source_system != request.source_system:
            raise RunnerExecutionError("SOURCE_SYSTEM_MISMATCH", "connector source system mismatch")
        metadata = request.metadata
        if request.operation is RunnerOperation.DETECT:
            prefix = artifact.read(MAX_ARTIFACT_PREFIX_BYTES)
            try:
                result = connector.detect(metadata, prefix)
            except Exception as exc:
                raise RunnerExecutionError("DETECTION_ERROR", "connector detection failed") from exc
            if not isinstance(result, DetectionResult):
                raise RunnerExecutionError("CONNECTOR_CONTRACT", "connector detection was invalid")
            return _ExecutionResult(detection=result, records=())

        try:
            values = connector.parse(artifact)
            records: list[ParsedSourceRecord] = []
            locators: set[str] = set()
            response_bytes = 0
            terminal_frame_budget = 4 + MAX_TERMINAL_FRAME_BYTES
            for value in values:
                if len(records) >= MAX_RECORDS:
                    raise RunnerExecutionError(
                        "RECORD_LIMIT", "connector exceeded the record limit"
                    )
                if not isinstance(value, ParsedSourceRecord):
                    raise RunnerExecutionError(
                        "CONNECTOR_CONTRACT", "connector returned an invalid record"
                    )
                validated = ParsedSourceRecord(
                    record_locator=value.record_locator,
                    source=value.source,
                    parser_version=value.parser_version,
                    raw_fields=dict(value.raw_fields),
                    normalized_fields=dict(value.normalized_fields),
                    external_transaction_id=value.external_transaction_id,
                )
                if validated.source != request.source_system:
                    raise RunnerExecutionError(
                        "PROVENANCE_MISMATCH", "record source system mismatch"
                    )
                if validated.parser_version != request.connector_version:
                    raise RunnerExecutionError(
                        "PROVENANCE_MISMATCH", "record parser version mismatch"
                    )
                if validated.record_locator in locators:
                    raise RunnerExecutionError(
                        "DUPLICATE_LOCATOR", "record locators must be unique"
                    )
                record_frame = encode_frame(
                    FrameKind.RECORD,
                    record_payload(request.request_id, validated),
                )
                if response_bytes + len(record_frame) + terminal_frame_budget > MAX_RESPONSE_BYTES:
                    raise RunnerExecutionError(
                        "RESPONSE_LIMIT", "connector response exceeded the limit"
                    )
                response_bytes += len(record_frame)
                locators.add(validated.record_locator)
                records.append(validated)
            return _ExecutionResult(detection=None, records=tuple(records))
        except RunnerExecutionError:
            raise
        except ConnectorContractError as exc:
            raise RunnerExecutionError(
                "CONNECTOR_CONTRACT", "connector parse failed validation"
            ) from exc
        except Exception as exc:
            raise RunnerExecutionError("PARSE_ERROR", "connector parse failed") from exc

    async def _send_success(
        self,
        writer: asyncio.StreamWriter,
        request: RunnerRequest,
        execution: _ExecutionResult,
    ) -> None:
        frames: list[bytes] = []
        for record in execution.records:
            frames.append(
                encode_frame(FrameKind.RECORD, record_payload(request.request_id, record))
            )
        response_bytes = sum(len(frame) for frame in frames)
        if response_bytes > MAX_RESPONSE_BYTES:
            await self._send_error(
                writer,
                request.request_id,
                request.operation,
                "RESPONSE_LIMIT",
                "connector response exceeded the limit",
            )
            return
        terminal = RunnerTerminal(
            request_id=request.request_id,
            status=RunnerStatus.OK,
            operation=request.operation,
            error_code=None,
            summary="connector completed",
            detection=execution.detection,
            parsed_count=len(execution.records),
            byte_count=request.declared_artifact_size,
            sha256_hex=request.verified_sha256_hex,
        )
        terminal_frame = encode_frame(FrameKind.TERMINAL, terminal_payload(terminal))
        if response_bytes + len(terminal_frame) > MAX_RESPONSE_BYTES:
            await self._send_error(
                writer,
                request.request_id,
                request.operation,
                "RESPONSE_LIMIT",
                "connector response exceeded the limit",
            )
            return
        for frame in frames:
            writer.write(frame)
        writer.write(terminal_frame)
        await writer.drain()

    async def _send_terminal(self, writer: asyncio.StreamWriter, terminal: RunnerTerminal) -> None:
        writer.write(encode_frame(FrameKind.TERMINAL, terminal_payload(terminal)))
        await writer.drain()

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        request_id: str,
        operation: RunnerOperation | None,
        error_code: str,
        summary: str,
    ) -> None:
        with contextlib.suppress(ConnectionError, OSError):
            await self._send_terminal(
                writer,
                RunnerTerminal(
                    request_id=request_id,
                    status=RunnerStatus.ERROR,
                    operation=operation,
                    error_code=error_code,
                    summary=summary,
                    detection=None,
                    parsed_count=0,
                    byte_count=0,
                    sha256_hex=None,
                ),
            )


async def serve(socket_path: str = DEFAULT_SOCKET_PATH) -> None:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    supervisor = ConnectorSupervisor()
    start_unix_server = getattr(asyncio, "start_unix_server", None)
    if start_unix_server is None:
        raise RuntimeError("Unix socket runner requires a POSIX asyncio implementation")
    server = await start_unix_server(
        supervisor.handle,
        path=str(path),
    )
    os.chmod(path, 0o600)
    logger.info("connector runner listening", extra={"socket": str(path)})
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="LedgerBridge isolated Connector runner")
    parser.add_argument("--socket", default=DEFAULT_SOCKET_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.socket))


if __name__ == "__main__":
    main()
