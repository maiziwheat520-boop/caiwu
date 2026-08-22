from __future__ import annotations

import hashlib
import json
import struct
from io import BytesIO
from typing import Any, cast
from uuid import uuid4

import pytest

from ledgerbridge.connectors import ArtifactMetadata, DetectionResult, ParsedSourceRecord
from ledgerbridge.runner_protocol import (
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_CHUNK_BYTES,
    MAX_ARTIFACT_CHUNK_PAYLOAD_BYTES,
    MAX_CHUNK_COUNT,
    MAX_CONTROL_FRAME_BYTES,
    MAX_RECORDS,
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
    frame_limit,
    health_control,
    is_health_control,
    parse_artifact_end_payload,
    parse_health_control,
    parse_record_payload,
    parse_request_control,
    parse_terminal_payload,
    record_payload,
    request_control,
    terminal_payload,
)


def _request(content: bytes = b"abc") -> RunnerRequest:
    digest = hashlib.sha256(content).hexdigest()
    return RunnerRequest(
        request_id=str(uuid4()),
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


def test_length_delimited_control_and_binary_frames_round_trip() -> None:
    request = _request(b"abcdef")
    frames = list(chunk_frames(request, BytesIO(b"abcdef"), chunk_size=2))
    assert len(frames) == 5
    kind, payload = decode_frame(frames[0])
    assert kind is FrameKind.CONTROL
    assert parse_request_control(payload) == request
    assert [decode_frame(frame)[0] for frame in frames[1:]] == [
        FrameKind.ARTIFACT_CHUNK,
        FrameKind.ARTIFACT_CHUNK,
        FrameKind.ARTIFACT_CHUNK,
        FrameKind.ARTIFACT_END,
    ]
    count, digest = parse_artifact_end_payload(decode_frame(frames[-1])[1])
    assert count == 6
    assert digest == request.verified_sha256_hex


def test_control_and_record_frames_have_independent_limits() -> None:
    with pytest.raises(RunnerProtocolError, match="type-specific"):
        encode_frame(FrameKind.CONTROL, b"x" * MAX_CONTROL_FRAME_BYTES)
    record = ParsedSourceRecord(
        record_locator="row:1",
        source="synthetic",
        parser_version="1",
        raw_fields={"amount": "1"},
        normalized_fields={},
    )
    request = _request()
    payload = record_payload(request.request_id, record)
    assert parse_record_payload(payload, request) == record
    with pytest.raises(RunnerProtocolError, match="request_id"):
        parse_record_payload(record_payload(str(uuid4()), record), request)


def test_chunk_stream_rejects_size_or_digest_mismatch_before_end_frame() -> None:
    request = _request(b"expected")
    with pytest.raises(RunnerProtocolError, match="verified digest"):
        list(chunk_frames(request, BytesIO(b"tampered")))


def test_default_chunk_size_accounts_for_frame_kind_byte() -> None:
    content = b"x" * (MAX_ARTIFACT_CHUNK_BYTES + 1)
    request = _request(content)
    frames = list(chunk_frames(request, BytesIO(content)))
    chunks = [
        decode_frame(frame)[1]
        for frame in frames
        if decode_frame(frame)[0] is FrameKind.ARTIFACT_CHUNK
    ]
    assert [len(chunk) for chunk in chunks] == [MAX_ARTIFACT_CHUNK_PAYLOAD_BYTES, 2]


def test_chunk_count_covers_the_full_artifact_limit() -> None:
    assert MAX_CHUNK_COUNT * MAX_ARTIFACT_CHUNK_PAYLOAD_BYTES >= MAX_ARTIFACT_BYTES


def test_default_chunk_size_streams_the_full_artifact_limit() -> None:
    content = b"x" * MAX_ARTIFACT_BYTES
    request = _request(content)
    frames = list(chunk_frames(request, BytesIO(content)))
    chunks = [
        decode_frame(frame)[1]
        for frame in frames
        if decode_frame(frame)[0] is FrameKind.ARTIFACT_CHUNK
    ]
    assert len(chunks) == MAX_CHUNK_COUNT
    assert sum(map(len, chunks)) == MAX_ARTIFACT_BYTES


@pytest.mark.parametrize("bad_text", ["\x00", "\ud800"])
def test_protocol_rejects_unstorable_terminal_and_record_text(bad_text: str) -> None:
    request = _request()
    terminal = RunnerTerminal(
        request_id=request.request_id,
        status=RunnerStatus.ERROR,
        operation=RunnerOperation.PARSE,
        error_code="RUNNER_ERROR",
        summary="safe",
        detection=None,
        parsed_count=0,
        byte_count=0,
        sha256_hex=None,
    )
    terminal_value = json.loads(terminal_payload(terminal))
    terminal_value["summary"] = bad_text
    with pytest.raises(RunnerProtocolError, match="summary"):
        parse_terminal_payload(json.dumps(terminal_value).encode())

    record = ParsedSourceRecord(
        record_locator="row:1",
        source="synthetic",
        parser_version="1",
        raw_fields={"memo": "safe"},
        normalized_fields={},
    )
    record_value = json.loads(record_payload(request.request_id, record))
    record_value["record_locator"] = bad_text
    with pytest.raises(RunnerProtocolError, match="record failed"):
        parse_record_payload(json.dumps(record_value).encode(), request)

    record_value["record_locator"] = "row:1"
    record_value["raw_fields"] = {"memo": bad_text}
    with pytest.raises(RunnerProtocolError, match="record failed"):
        parse_record_payload(json.dumps(record_value).encode(), request)


def test_artifact_end_payload_rejects_wrong_length() -> None:
    with pytest.raises(RunnerProtocolError, match="invalid length"):
        parse_artifact_end_payload(artifact_end_payload(0, "0" * 64)[:-1])


def test_control_rejects_duplicate_json_keys() -> None:
    request = _request()
    payload = (
        b'{"message_type":"request","message_type":"request",'
        b'"protocol_version":1,"request_id":"'
        + request.request_id.encode()
        + b'","operation":"parse","connector_name":"synthetic.csv",'
        b'"connector_version":"1","source_system":"synthetic",'
        b'"metadata":{"source":"manual_upload","original_filename":"fixture.csv",'
        b'"media_type":"text/csv","byte_size":3,"sha256_hex":"'
        + request.verified_sha256_hex.encode()
        + b'"},"declared_artifact_size":3,"verified_sha256_hex":"'
        + request.verified_sha256_hex.encode()
        + b'"}'
    )
    with pytest.raises(RunnerProtocolError, match="duplicate keys"):
        parse_request_control(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("request_id", "not-a-uuid", "request_id is invalid"),
        ("connector_name", "", "connector_name is invalid"),
        ("connector_version", "", "connector_version is invalid"),
        ("source_system", "Not Canonical", "source_system is not canonical"),
        ("declared_artifact_size", -1, "declared_artifact_size exceeds"),
        ("verified_sha256_hex", "bad", "sha256_hex is invalid"),
    ],
)
def test_request_rejects_invalid_fields(field: str, value: object, message: str) -> None:
    request = _request()
    fields = {
        "request_id": request.request_id,
        "connector_name": request.connector_name,
        "connector_version": request.connector_version,
        "source_system": request.source_system,
        "declared_artifact_size": request.declared_artifact_size,
        "verified_sha256_hex": request.verified_sha256_hex,
    }
    fields[field] = value
    with pytest.raises(RunnerProtocolError, match=message):
        RunnerRequest(
            request_id=fields["request_id"],  # type: ignore[arg-type]
            operation=request.operation,
            connector_name=fields["connector_name"],  # type: ignore[arg-type]
            connector_version=fields["connector_version"],  # type: ignore[arg-type]
            source_system=fields["source_system"],  # type: ignore[arg-type]
            metadata=request.metadata,
            declared_artifact_size=fields["declared_artifact_size"],  # type: ignore[arg-type]
            verified_sha256_hex=fields["verified_sha256_hex"],  # type: ignore[arg-type]
        )


def test_request_rejects_metadata_mismatch() -> None:
    request = _request()
    with pytest.raises(RunnerProtocolError, match="metadata byte_size"):
        RunnerRequest(
            request_id=request.request_id,
            operation=request.operation,
            connector_name=request.connector_name,
            connector_version=request.connector_version,
            source_system=request.source_system,
            metadata=ArtifactMetadata(
                source=request.metadata.source,
                original_filename=request.metadata.original_filename,
                media_type=request.metadata.media_type,
                byte_size=request.metadata.byte_size + 1,
                sha256_hex=request.metadata.sha256_hex,
            ),
            declared_artifact_size=request.declared_artifact_size,
            verified_sha256_hex=request.verified_sha256_hex,
        )
    with pytest.raises(RunnerProtocolError, match="metadata digest"):
        RunnerRequest(
            request_id=request.request_id,
            operation=request.operation,
            connector_name=request.connector_name,
            connector_version=request.connector_version,
            source_system=request.source_system,
            metadata=ArtifactMetadata(
                source=request.metadata.source,
                original_filename=request.metadata.original_filename,
                media_type=request.metadata.media_type,
                byte_size=request.metadata.byte_size,
                sha256_hex="0" * 64,
            ),
            declared_artifact_size=request.declared_artifact_size,
            verified_sha256_hex=request.verified_sha256_hex,
        )


def test_frame_readers_reject_truncation_and_unknown_kinds() -> None:
    with pytest.raises(RunnerProtocolError, match="truncated"):
        decode_frame(b"\x00")
    with pytest.raises(RunnerProtocolError, match="length prefix"):
        decode_frame(struct.pack("!I", 2) + b"\x01")
    with pytest.raises(RunnerProtocolError, match="unknown frame"):
        decode_frame(struct.pack("!I", 1) + b"\xff")
    with pytest.raises(RunnerProtocolError, match="truncated frame header"):
        from ledgerbridge.runner_protocol import read_frame

        read_frame(BytesIO(b"\x00"))
    with pytest.raises(RunnerProtocolError, match="empty frame"):
        from ledgerbridge.runner_protocol import read_frame

        read_frame(BytesIO(struct.pack("!I", 0)))
    with pytest.raises(RunnerProtocolError, match="unknown frame"):
        read_frame(BytesIO(struct.pack("!I", 1) + b"\xff"))


def test_health_and_terminal_payloads_round_trip_and_validate() -> None:
    request_id = str(uuid4())
    health = health_control(request_id)
    assert is_health_control(health)
    assert parse_health_control(health) == request_id
    assert not is_health_control(b"not-json")
    with pytest.raises(RunnerProtocolError, match="health request_id"):
        health_control("bad")
    with pytest.raises(RunnerProtocolError, match="invalid health"):
        parse_health_control(request_control(_request()))

    terminal = RunnerTerminal(
        request_id=request_id,
        status=RunnerStatus.OK,
        operation=RunnerOperation.DETECT,
        error_code=None,
        summary="connector completed",
        detection=DetectionResult.MATCH,
        parsed_count=0,
        byte_count=3,
        sha256_hex=_request().verified_sha256_hex,
    )
    assert parse_terminal_payload(terminal_payload(terminal)) == terminal
    with pytest.raises(RunnerProtocolError, match="unsupported protocol"):
        parse_terminal_payload(
            json.dumps({"message_type": "terminal", "protocol_version": 99}).encode()
        )


def test_terminal_constructor_error_paths() -> None:
    request_id = str(uuid4())
    base = dict(
        request_id=request_id,
        status=RunnerStatus.OK,
        operation=None,
        error_code=None,
        summary="ok",
        detection=None,
        parsed_count=0,
        byte_count=0,
        sha256_hex=None,
    )
    for key, value, message in [
        ("summary", "x" * 501, "summary is too long"),
        ("parsed_count", MAX_RECORDS + 1, "parsed_count is invalid"),
        ("byte_count", MAX_ARTIFACT_BYTES + 1, "byte_count is invalid"),
        ("sha256_hex", "bad", "sha256_hex is invalid"),
    ]:
        fields = {**base, key: value}
        with pytest.raises(RunnerProtocolError, match=message):
            RunnerTerminal(**cast(Any, fields))
    with pytest.raises(RunnerProtocolError, match="failed terminal"):
        RunnerTerminal(**cast(Any, {**base, "status": RunnerStatus.ERROR}))
    with pytest.raises(RunnerProtocolError, match="terminal request_id"):
        RunnerTerminal(**cast(Any, {**base, "request_id": "bad"}))
    with pytest.raises(RunnerProtocolError, match="successful terminal"):
        RunnerTerminal(**cast(Any, {**base, "error_code": "ERR"}))


def test_parse_record_rejects_bad_fields_and_provenance() -> None:
    request = _request()
    payload = json.loads(
        record_payload(
            request.request_id,
            ParsedSourceRecord(
                record_locator="row:1",
                source="synthetic",
                parser_version="1",
                raw_fields={"amount": "1"},
                normalized_fields={},
            ),
        )
    )
    for key, value, message in [
        ("raw_fields", [], "record fields"),
        ("record_locator", "", "record failed"),
        ("source", "other", "provenance"),
        ("parser_version", "2", "provenance"),
    ]:
        altered = {**payload, key: value}
        with pytest.raises(RunnerProtocolError, match=message):
            parse_record_payload(json.dumps(altered).encode(), request)


def test_chunk_and_artifact_end_limits() -> None:
    request = _request()
    with pytest.raises(RunnerProtocolError, match="chunk size"):
        list(chunk_frames(request, BytesIO(b"abc"), chunk_size=0))
    with pytest.raises(RunnerProtocolError, match="byte count"):
        artifact_end_payload(MAX_ARTIFACT_BYTES + 1, "0" * 64)
    with pytest.raises(RunnerProtocolError, match="byte count"):
        parse_artifact_end_payload(struct.pack("!Q", MAX_ARTIFACT_BYTES + 1) + b"0" * 64)


def test_protocol_helper_error_branches() -> None:
    request = _request()
    with pytest.raises(RunnerProtocolError, match="request_id"):
        RunnerRequest(
            request_id=request.request_id.replace("-", ""),
            operation=request.operation,
            connector_name=request.connector_name,
            connector_version=request.connector_version,
            source_system=request.source_system,
            metadata=request.metadata,
            declared_artifact_size=request.declared_artifact_size,
            verified_sha256_hex=request.verified_sha256_hex,
        )
    with pytest.raises(RunnerProtocolError, match="unknown frame"):
        frame_limit(99)  # type: ignore[arg-type]
    with pytest.raises(RunnerProtocolError, match="payload must"):
        encode_frame(FrameKind.CONTROL, bytearray(b"x"))  # type: ignore[arg-type]
    with pytest.raises(RunnerProtocolError, match="metadata"):
        parse_request_control(
            b'{"message_type":"request","protocol_version":1,"operation":"parse","metadata":[]}'
        )
    with pytest.raises(RunnerProtocolError, match="control message"):
        parse_request_control(b'{"message_type":"other","protocol_version":1}')
    with pytest.raises(RunnerProtocolError, match="unsupported protocol"):
        parse_request_control(b'{"message_type":"request","protocol_version":99}')
    with pytest.raises(RunnerProtocolError, match="invalid"):
        parse_request_control(b'{"message_type":"request","protocol_version":1,"operation":"bad"}')
    with pytest.raises(RunnerProtocolError, match="must be a JSON object"):
        parse_request_control(b"[]")
    with pytest.raises(RunnerProtocolError, match="valid UTF"):
        parse_request_control(b"\xff")


@pytest.mark.asyncio
async def test_async_frame_reader_rejects_invalid_frames() -> None:
    from ledgerbridge.runner_protocol import read_async_frame

    class Reader:
        def __init__(self, data: bytes) -> None:
            self.data = BytesIO(data)

        async def readexactly(self, size: int) -> bytes:
            value = self.data.read(size)
            if len(value) != size:
                raise EOFError
            return value

    with pytest.raises(RunnerProtocolError, match="empty frame"):
        await read_async_frame(Reader(struct.pack("!I", 0)))
    with pytest.raises(RunnerProtocolError, match="unknown frame"):
        await read_async_frame(Reader(struct.pack("!I", 1) + b"\xff"))
    with pytest.raises(RunnerProtocolError, match="type-specific"):
        await read_async_frame(Reader(struct.pack("!I", MAX_CONTROL_FRAME_BYTES + 1) + b"\x01"))


def test_record_and_terminal_parsers_reject_invalid_json_shapes() -> None:
    request = _request()
    with pytest.raises(RunnerProtocolError, match="record fields"):
        parse_record_payload(
            json.dumps(
                {"request_id": request.request_id, "raw_fields": [], "normalized_fields": {}}
            ).encode(),
            request,
        )
    with pytest.raises(RunnerProtocolError, match="invalid terminal"):
        parse_terminal_payload(b'{"message_type":"other"}')
    with pytest.raises(RunnerProtocolError, match="unsupported protocol"):
        parse_terminal_payload(b'{"message_type":"terminal","protocol_version":99}')
    with pytest.raises(RunnerProtocolError, match="status"):
        parse_terminal_payload(b'{"message_type":"terminal","protocol_version":1,"status":"bad"}')
