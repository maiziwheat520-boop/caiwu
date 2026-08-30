from __future__ import annotations

import struct
from uuid import UUID

import pytest

from ledgerbridge.evidence_unlocker_protocol import (
    MAX_UNLOCKER_REQUEST_BYTES,
    EvidenceUnlockerProtocolError,
    UnlockerOutputDescriptor,
    UnlockerRequest,
    UnlockerResponse,
    UnlockerSourceDescriptor,
    UnlockerStatus,
    decode_unlocker_request,
    decode_unlocker_response,
    encode_unlocker_request,
    encode_unlocker_response,
)


def _source() -> UnlockerSourceDescriptor:
    return UnlockerSourceDescriptor(
        source_ref=UUID("21000000-0000-4000-8000-000000000001"),
        evidence_ref=UUID("20000000-0000-4000-8000-000000000001"),
        object_ref="a" * 64,
        plaintext_sha256="b" * 64,
        plaintext_size=4096,
        ciphertext_sha256="c" * 64,
        ciphertext_size=4608,
        storage_key=f"sha256/cc/cc/{'c' * 64}",
        chunk_size=65536,
        stream_header="d" * 48,
        wrapped_key_generation="test-v1",
        wrapped_key_nonce="e" * 48,
        wrapped_key_ciphertext="f" * 96,
    )


def test_unlocker_request_round_trip_is_bounded_and_secret_safe_to_represent() -> None:
    request = UnlockerRequest(
        request_id=UUID("25000000-0000-4000-8000-000000000001"),
        operation_id=UUID("26000000-0000-4000-8000-000000000001"),
        request_nonce=UUID("27000000-0000-4000-8000-000000000001"),
        source=_source(),
        password="synthetic-one-request-password",
    )

    encoded = encode_unlocker_request(request)
    decoded = decode_unlocker_request(encoded)

    assert len(encoded) <= MAX_UNLOCKER_REQUEST_BYTES
    assert decoded == request
    assert decoded.password == "synthetic-one-request-password"
    assert "synthetic-one-request-password" not in repr(request)
    assert "synthetic-one-request-password" not in repr(decoded)


def test_unlocker_request_rejects_duplicate_keys_and_oversized_frames_without_secret_echo() -> None:
    marker = "synthetic-protocol-secret"
    duplicate = (
        b'{"contract_version":"ledgerbridge.evidence-unlocker.v1",'
        b'"request_id":"25000000-0000-4000-8000-000000000001",'
        b'"request_id":"25000000-0000-4000-8000-000000000002",'
        + f'"password":"{marker}"'.encode()
        + b"}"
    )
    frame = struct.pack("!I", len(duplicate)) + duplicate

    with pytest.raises(EvidenceUnlockerProtocolError) as duplicate_error:
        decode_unlocker_request(frame)
    with pytest.raises(EvidenceUnlockerProtocolError) as oversized_error:
        decode_unlocker_request(struct.pack("!I", MAX_UNLOCKER_REQUEST_BYTES + 1))

    assert marker not in str(duplicate_error.value)
    assert marker not in str(oversized_error.value)


def test_unlocker_success_response_returns_only_encrypted_output_descriptors() -> None:
    output = UnlockerOutputDescriptor(
        evidence_ref=UUID("22000000-0000-4000-8000-000000000001"),
        media_type="application/pdf",
        display_name="statement.pdf",
        object_ref="1" * 64,
        plaintext_sha256="2" * 64,
        plaintext_size=2048,
        ciphertext_sha256="3" * 64,
        ciphertext_size=2560,
        storage_key=f"sha256/33/33/{'3' * 64}",
        chunk_size=65536,
        stream_header="4" * 48,
        wrapped_key_generation="test-v1",
        wrapped_key_nonce="5" * 48,
        wrapped_key_ciphertext="6" * 96,
    )
    response = UnlockerResponse(
        request_id=UUID("25000000-0000-4000-8000-000000000001"),
        operation_id=UUID("26000000-0000-4000-8000-000000000001"),
        request_nonce=UUID("27000000-0000-4000-8000-000000000001"),
        source_ref=UUID("21000000-0000-4000-8000-000000000001"),
        status=UnlockerStatus.UNLOCKED,
        outputs=(output,),
    )

    decoded = decode_unlocker_response(encode_unlocker_response(response))

    assert decoded == response
    assert decoded.outputs == (output,)
    assert decoded.error_code is None
