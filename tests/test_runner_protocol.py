from __future__ import annotations

import hashlib
from io import BytesIO
from uuid import uuid4

import pytest

from ledgerbridge.connectors import ArtifactMetadata, ParsedSourceRecord
from ledgerbridge.runner_protocol import (
    MAX_CONTROL_FRAME_BYTES,
    FrameKind,
    RunnerOperation,
    RunnerProtocolError,
    RunnerRequest,
    artifact_end_payload,
    chunk_frames,
    decode_frame,
    encode_frame,
    parse_artifact_end_payload,
    parse_record_payload,
    parse_request_control,
    record_payload,
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


def test_artifact_end_payload_rejects_wrong_length() -> None:
    with pytest.raises(RunnerProtocolError, match="invalid length"):
        parse_artifact_end_payload(artifact_end_payload(0, "0" * 64)[:-1])
