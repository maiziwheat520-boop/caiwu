"""Versioned, bounded framing for the isolated Connector runner.

The protocol deliberately keeps control JSON separate from evidence bytes and
parsed-record frames.  Every frame has its own size budget so a malformed or
hostile peer cannot turn one generous global limit into an unbounded message.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, Protocol, cast
from uuid import UUID

from ledgerbridge.connectors import (
    CANONICAL_SOURCE_PATTERN,
    ArtifactMetadata,
    ConnectorContractError,
    DetectionResult,
    ParsedSourceRecord,
    _validate_json_object,
)
from ledgerbridge.text import contains_unstorable_text

PROTOCOL_VERSION = 1
MAX_REQUEST_SECONDS = 90
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_RECORDS = 10_000
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_CONTROL_FRAME_BYTES = 64 * 1024
MAX_ARTIFACT_CHUNK_BYTES = 256 * 1024
# Frame limits include the one-byte kind prefix, so this is the largest
# artifact payload that can fit in a single ARTIFACT_CHUNK frame.
MAX_ARTIFACT_CHUNK_PAYLOAD_BYTES = MAX_ARTIFACT_CHUNK_BYTES - 1
MAX_RECORD_FRAME_BYTES = 4 * 1024 * 1024
MAX_TERMINAL_FRAME_BYTES = 64 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_ARTIFACT_PREFIX_BYTES = 64 * 1024
MAX_CHUNK_COUNT = (
    MAX_ARTIFACT_BYTES + MAX_ARTIFACT_CHUNK_PAYLOAD_BYTES - 1
) // MAX_ARTIFACT_CHUNK_PAYLOAD_BYTES
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class RunnerProtocolError(ValueError):
    """A malformed, oversized, or semantically invalid runner message."""


class FrameKind(IntEnum):
    CONTROL = 1
    ARTIFACT_CHUNK = 2
    ARTIFACT_END = 3
    RECORD = 4
    TERMINAL = 5


class RunnerOperation(StrEnum):
    DETECT = "detect"
    PARSE = "parse"


class RunnerStatus(StrEnum):
    OK = "OK"
    ERROR = "ERROR"


class BinaryReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class RunnerRequest:
    request_id: str
    operation: RunnerOperation
    connector_name: str
    connector_version: str
    source_system: str
    metadata: ArtifactMetadata
    declared_artifact_size: int
    verified_sha256_hex: str

    def __post_init__(self) -> None:
        try:
            parsed_id = UUID(self.request_id)
        except (AttributeError, ValueError) as exc:
            raise RunnerProtocolError("request_id is invalid") from exc
        if str(parsed_id) != self.request_id.lower():
            raise RunnerProtocolError("request_id is invalid")
        _require_text("connector_name", self.connector_name, 100)
        _require_text("connector_version", self.connector_version, 100)
        _require_source("source_system", self.source_system)
        if not 0 <= self.declared_artifact_size <= MAX_ARTIFACT_BYTES:
            raise RunnerProtocolError("declared_artifact_size exceeds the runner limit")
        _require_sha256(self.verified_sha256_hex)
        if self.metadata.byte_size != self.declared_artifact_size:
            raise RunnerProtocolError("metadata byte_size differs from declared_artifact_size")
        if self.metadata.sha256_hex != self.verified_sha256_hex:
            raise RunnerProtocolError("metadata digest differs from verified_sha256_hex")


@dataclass(frozen=True, slots=True)
class RunnerTerminal:
    request_id: str
    status: RunnerStatus
    operation: RunnerOperation | None
    error_code: str | None
    summary: str
    detection: DetectionResult | None
    parsed_count: int
    byte_count: int
    sha256_hex: str | None

    def __post_init__(self) -> None:
        if self.request_id:
            try:
                UUID(self.request_id)
            except (AttributeError, ValueError) as exc:
                raise RunnerProtocolError("terminal request_id is invalid") from exc
        if len(self.summary) > 500:
            raise RunnerProtocolError("terminal summary is too long")
        if not 0 <= self.parsed_count <= MAX_RECORDS:
            raise RunnerProtocolError("terminal parsed_count is invalid")
        if not 0 <= self.byte_count <= MAX_ARTIFACT_BYTES:
            raise RunnerProtocolError("terminal byte_count is invalid")
        if self.sha256_hex is not None:
            _require_sha256(self.sha256_hex)
        if self.status is RunnerStatus.OK and self.error_code is not None:
            raise RunnerProtocolError("successful terminal response has an error code")
        if self.status is RunnerStatus.ERROR and not self.error_code:
            raise RunnerProtocolError("failed terminal response lacks an error code")


def frame_limit(kind: FrameKind) -> int:
    if kind is FrameKind.CONTROL:
        return MAX_CONTROL_FRAME_BYTES
    if kind is FrameKind.ARTIFACT_CHUNK:
        return MAX_ARTIFACT_CHUNK_BYTES
    if kind is FrameKind.ARTIFACT_END:
        return 80
    if kind is FrameKind.RECORD:
        return MAX_RECORD_FRAME_BYTES
    if kind is FrameKind.TERMINAL:
        return MAX_TERMINAL_FRAME_BYTES
    raise RunnerProtocolError("unknown frame kind")


def encode_frame(kind: FrameKind, payload: bytes) -> bytes:
    if not isinstance(payload, bytes):
        raise RunnerProtocolError("frame payload must be bytes")
    body_length = 1 + len(payload)
    limit = frame_limit(kind)
    if body_length > limit:
        raise RunnerProtocolError("frame exceeds its type-specific limit")
    return struct.pack("!I", body_length) + bytes([int(kind)]) + payload


def decode_frame(data: bytes) -> tuple[FrameKind, bytes]:
    if len(data) < 5:
        raise RunnerProtocolError("truncated frame")
    body_length = struct.unpack("!I", data[:4])[0]
    if body_length != len(data) - 4:
        raise RunnerProtocolError("frame length prefix does not match payload")
    try:
        kind = FrameKind(data[4])
    except ValueError as exc:
        raise RunnerProtocolError("unknown frame kind") from exc
    payload = data[5:]
    if body_length > frame_limit(kind):
        raise RunnerProtocolError("frame exceeds its type-specific limit")
    return kind, payload


def read_frame(stream: BinaryReader) -> tuple[FrameKind, bytes]:
    header = _read_exact(stream, 4)
    if len(header) != 4:
        raise RunnerProtocolError("truncated frame header")
    body_length = struct.unpack("!I", header)[0]
    if body_length < 1:
        raise RunnerProtocolError("empty frame")
    kind_byte = _read_exact(stream, 1)
    if len(kind_byte) != 1:
        raise RunnerProtocolError("truncated frame kind")
    try:
        kind = FrameKind(kind_byte[0])
    except ValueError as exc:
        raise RunnerProtocolError("unknown frame kind") from exc
    if body_length > frame_limit(kind):
        raise RunnerProtocolError("frame exceeds its type-specific limit")
    payload = _read_exact(stream, body_length - 1)
    if len(payload) != body_length - 1:
        raise RunnerProtocolError("truncated frame payload")
    return kind, payload


async def read_async_frame(reader: Any) -> tuple[FrameKind, bytes]:
    header = await reader.readexactly(4)
    body_length = struct.unpack("!I", header)[0]
    if body_length < 1:
        raise RunnerProtocolError("empty frame")
    kind_byte = await reader.readexactly(1)
    try:
        kind = FrameKind(kind_byte[0])
    except ValueError as exc:
        raise RunnerProtocolError("unknown frame kind") from exc
    if body_length > frame_limit(kind):
        raise RunnerProtocolError("frame exceeds its type-specific limit")
    return kind, await reader.readexactly(body_length - 1)


def request_control(request: RunnerRequest) -> bytes:
    payload = {
        "message_type": "request",
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request.request_id,
        "operation": request.operation.value,
        "connector_name": request.connector_name,
        "connector_version": request.connector_version,
        "source_system": request.source_system,
        "metadata": {
            "source": request.metadata.source,
            "original_filename": request.metadata.original_filename,
            "media_type": request.metadata.media_type,
            "byte_size": request.metadata.byte_size,
            "sha256_hex": request.metadata.sha256_hex,
        },
        "declared_artifact_size": request.declared_artifact_size,
        "verified_sha256_hex": request.verified_sha256_hex,
    }
    return _json_payload(payload, MAX_CONTROL_FRAME_BYTES - 1)


def parse_request_control(payload: bytes) -> RunnerRequest:
    value = _json_object(payload, MAX_CONTROL_FRAME_BYTES - 1)
    if value.get("message_type") != "request":
        raise RunnerProtocolError("control message is not a request")
    if value.get("protocol_version") != PROTOCOL_VERSION:
        raise RunnerProtocolError("unsupported protocol version")
    operation = _enum_value(RunnerOperation, value.get("operation"), "operation")
    metadata_value = value.get("metadata")
    if not isinstance(metadata_value, Mapping):
        raise RunnerProtocolError("metadata must be an object")
    metadata = ArtifactMetadata(
        source=_text_value(metadata_value, "source", 64),
        original_filename=_text_value(metadata_value, "original_filename", 512),
        media_type=_text_value(metadata_value, "media_type", 200),
        byte_size=_int_value(metadata_value, "byte_size", 0, MAX_ARTIFACT_BYTES),
        sha256_hex=_sha_value(metadata_value, "sha256_hex"),
    )
    request = RunnerRequest(
        request_id=_text_value(value, "request_id", 100),
        operation=operation,
        connector_name=_text_value(value, "connector_name", 100),
        connector_version=_text_value(value, "connector_version", 100),
        source_system=_text_value(value, "source_system", 64),
        metadata=metadata,
        declared_artifact_size=_int_value(value, "declared_artifact_size", 0, MAX_ARTIFACT_BYTES),
        verified_sha256_hex=_sha_value(value, "verified_sha256_hex"),
    )
    return request


def health_control(request_id: str) -> bytes:
    try:
        UUID(request_id)
    except (AttributeError, ValueError) as exc:
        raise RunnerProtocolError("health request_id is invalid") from exc
    return _json_payload(
        {
            "message_type": "health",
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
        },
        MAX_CONTROL_FRAME_BYTES - 1,
    )


def is_health_control(payload: bytes) -> bool:
    try:
        value = _json_object(payload, MAX_CONTROL_FRAME_BYTES - 1)
    except RunnerProtocolError:
        return False
    return (
        value.get("message_type") == "health" and value.get("protocol_version") == PROTOCOL_VERSION
    )


def parse_health_control(payload: bytes) -> str:
    value = _json_object(payload, MAX_CONTROL_FRAME_BYTES - 1)
    if value.get("message_type") != "health" or value.get("protocol_version") != PROTOCOL_VERSION:
        raise RunnerProtocolError("invalid health control message")
    return _text_value(value, "request_id", 100)


def artifact_end_payload(byte_count: int, sha256_hex: str) -> bytes:
    if not 0 <= byte_count <= MAX_ARTIFACT_BYTES:
        raise RunnerProtocolError("artifact byte count exceeds the runner limit")
    _require_sha256(sha256_hex)
    return struct.pack("!Q", byte_count) + sha256_hex.encode("ascii")


def parse_artifact_end_payload(payload: bytes) -> tuple[int, str]:
    if len(payload) != 8 + 64:
        raise RunnerProtocolError("artifact end frame has invalid length")
    byte_count = struct.unpack("!Q", payload[:8])[0]
    sha256_hex = payload[8:].decode("ascii")
    _require_sha256(sha256_hex)
    if byte_count > MAX_ARTIFACT_BYTES:
        raise RunnerProtocolError("artifact byte count exceeds the runner limit")
    return byte_count, sha256_hex


def record_payload(request_id: str, record: ParsedSourceRecord) -> bytes:
    payload = {
        "request_id": request_id,
        "record_locator": record.record_locator,
        "source": record.source,
        "parser_version": record.parser_version,
        "raw_fields": dict(record.raw_fields),
        "normalized_fields": dict(record.normalized_fields),
        "external_transaction_id": record.external_transaction_id,
    }
    return _json_payload(payload, MAX_RECORD_FRAME_BYTES - 1)


def parse_record_payload(payload: bytes, request: RunnerRequest) -> ParsedSourceRecord:
    value = _json_object(payload, MAX_RECORD_FRAME_BYTES - 1)
    if value.get("request_id") != request.request_id:
        raise RunnerProtocolError("record request_id does not match the request")
    raw_fields = value.get("raw_fields")
    normalized_fields = value.get("normalized_fields")
    if not isinstance(raw_fields, Mapping) or not isinstance(normalized_fields, Mapping):
        raise RunnerProtocolError("record fields must be objects")
    try:
        _validate_json_object("raw_fields", raw_fields, reject_floats=False)
        _validate_json_object("normalized_fields", normalized_fields, reject_floats=True)
        record = ParsedSourceRecord(
            record_locator=_text_value(value, "record_locator", 500),
            source=_text_value(value, "source", 64),
            parser_version=_text_value(value, "parser_version", 100),
            raw_fields=dict(raw_fields),
            normalized_fields=dict(normalized_fields),
            external_transaction_id=(
                None
                if value.get("external_transaction_id") is None
                else _text_value(value, "external_transaction_id", 300)
            ),
        )
    except (ConnectorContractError, TypeError, ValueError) as exc:
        raise RunnerProtocolError("record failed typed validation") from exc
    if record.source != request.source_system or record.parser_version != request.connector_version:
        raise RunnerProtocolError("record provenance does not match the request")
    return record


def terminal_payload(terminal: RunnerTerminal) -> bytes:
    payload = {
        "message_type": "terminal",
        "protocol_version": PROTOCOL_VERSION,
        "request_id": terminal.request_id,
        "status": terminal.status.value,
        "operation": terminal.operation.value if terminal.operation is not None else None,
        "error_code": terminal.error_code,
        "summary": terminal.summary,
        "detection": terminal.detection.value if terminal.detection is not None else None,
        "parsed_count": terminal.parsed_count,
        "byte_count": terminal.byte_count,
        "sha256_hex": terminal.sha256_hex,
    }
    return _json_payload(payload, MAX_TERMINAL_FRAME_BYTES - 1)


def parse_terminal_payload(payload: bytes) -> RunnerTerminal:
    value = _json_object(payload, MAX_TERMINAL_FRAME_BYTES - 1)
    if value.get("message_type") != "terminal":
        raise RunnerProtocolError("invalid terminal message")
    if value.get("protocol_version") != PROTOCOL_VERSION:
        raise RunnerProtocolError("unsupported protocol version")
    status = _enum_value(RunnerStatus, value.get("status"), "status")
    operation_value = value.get("operation")
    operation = (
        None
        if operation_value is None
        else _enum_value(RunnerOperation, operation_value, "operation")
    )
    detection_value = value.get("detection")
    detection = (
        None
        if detection_value is None
        else _enum_value(DetectionResult, detection_value, "detection")
    )
    error_code = cast(str | None, value.get("error_code"))
    if error_code is not None:
        _require_text("error_code", error_code, 100)
    summary = _text_value(value, "summary", 500)
    sha256_hex = cast(str | None, value.get("sha256_hex"))
    if sha256_hex is not None:
        _require_sha256(sha256_hex)
    request_id_value = value.get("request_id")
    request_id = "" if request_id_value == "" else _text_value(value, "request_id", 100)
    return RunnerTerminal(
        request_id=request_id,
        status=status,
        operation=operation,
        error_code=error_code,
        summary=summary,
        detection=detection,
        parsed_count=_int_value(value, "parsed_count", 0, MAX_RECORDS),
        byte_count=_int_value(value, "byte_count", 0, MAX_ARTIFACT_BYTES),
        sha256_hex=sha256_hex,
    )


def chunk_frames(
    request: RunnerRequest,
    stream: BinaryReader,
    chunk_size: int = MAX_ARTIFACT_CHUNK_PAYLOAD_BYTES,
) -> Iterable[bytes]:
    if not 0 < chunk_size < MAX_ARTIFACT_CHUNK_BYTES:
        raise RunnerProtocolError("invalid artifact chunk size")
    yield encode_frame(FrameKind.CONTROL, request_control(request))
    observed = 0
    digest = hashlib.sha256()
    chunks = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise RunnerProtocolError("artifact stream must yield bytes")
        chunks += 1
        if chunks > MAX_CHUNK_COUNT:
            raise RunnerProtocolError("artifact stream has too many chunks")
        observed += len(chunk)
        if observed > request.declared_artifact_size or observed > MAX_ARTIFACT_BYTES:
            raise RunnerProtocolError("artifact stream exceeds the declared size")
        digest.update(chunk)
        yield encode_frame(FrameKind.ARTIFACT_CHUNK, chunk)
    actual = digest.hexdigest()
    if observed != request.declared_artifact_size or actual != request.verified_sha256_hex:
        raise RunnerProtocolError("artifact stream does not match the verified digest")
    yield encode_frame(FrameKind.ARTIFACT_END, artifact_end_payload(observed, actual))


def _read_exact(stream: BinaryReader, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _json_payload(value: Mapping[str, object], limit: int) -> bytes:
    try:
        payload = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise RunnerProtocolError("message is not bounded JSON") from exc
    if len(payload) > limit:
        raise RunnerProtocolError("message exceeds its JSON limit")
    return payload


def _json_object(payload: bytes, limit: int) -> dict[str, object]:
    if len(payload) > limit:
        raise RunnerProtocolError("JSON frame exceeds its limit")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerProtocolError("message is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RunnerProtocolError("message must be a JSON object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise RunnerProtocolError("JSON object contains duplicate keys")
        value[key] = item
    return value


def _enum_value(enum_type: type[Any], value: object, field: str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise RunnerProtocolError(f"{field} is invalid") from exc


def _text_value(value: Mapping[str, object], field: str, maximum: int) -> str:
    item = value.get(field)
    _require_text(field, item, maximum)
    return cast(str, item)


def _require_text(field: str, value: object, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or contains_unstorable_text(value)
    ):
        raise RunnerProtocolError(f"{field} is invalid")


def _require_source(field: str, value: object) -> None:
    if not isinstance(value, str) or CANONICAL_SOURCE_PATTERN.fullmatch(value) is None:
        raise RunnerProtocolError(f"{field} is not canonical")


def _int_value(value: Mapping[str, object], field: str, minimum: int, maximum: int) -> int:
    item = value.get(field)
    if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
        raise RunnerProtocolError(f"{field} is invalid")
    return item


def _sha_value(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    _require_sha256(item)
    return cast(str, item)


def _require_sha256(value: object) -> None:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise RunnerProtocolError("sha256_hex is invalid")
