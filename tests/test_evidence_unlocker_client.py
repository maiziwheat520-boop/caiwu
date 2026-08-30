from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from ledgerbridge.evidence_unlocker_client import (
    EvidenceUnlockerClient,
    EvidenceUnlockerClientError,
)
from ledgerbridge.evidence_unlocker_protocol import (
    UnlockerOutputDescriptor,
    UnlockerRequest,
    UnlockerResponse,
    UnlockerSourceDescriptor,
    UnlockerStatus,
    encode_unlocker_response,
)


class _MemoryPeer:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = bytearray()

    def __enter__(self) -> _MemoryPeer:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def send(self, payload: memoryview[bytes]) -> int:
        self.sent.extend(payload.tobytes())
        return len(payload)

    def recv(self, size: int) -> bytes:
        chunk, self.response = self.response[:size], self.response[size:]
        return chunk


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


def _request() -> UnlockerRequest:
    return UnlockerRequest(
        request_id=UUID("25000000-0000-4000-8000-000000000001"),
        operation_id=UUID("26000000-0000-4000-8000-000000000001"),
        request_nonce=UUID("27000000-0000-4000-8000-000000000001"),
        source=_source(),
        password="synthetic-one-request-password",
    )


def _response(request: UnlockerRequest) -> UnlockerResponse:
    return UnlockerResponse(
        request_id=request.request_id,
        operation_id=request.operation_id,
        request_nonce=request.request_nonce,
        source_ref=request.source.source_ref,
        status=UnlockerStatus.UNLOCKED,
        outputs=(
            UnlockerOutputDescriptor(
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
            ),
        ),
    )


def test_client_round_trip_binds_response_to_request_and_nonce(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    peer = _MemoryPeer(encode_unlocker_response(_response(request)))
    client = EvidenceUnlockerClient(tmp_path / "unlocker.sock", timeout_seconds=1)
    monkeypatch.setattr(client, "_connect", lambda: peer)

    response = client.process(request)

    assert response.status == UnlockerStatus.UNLOCKED
    assert response.request_nonce == request.request_nonce
    assert peer.sent
    assert "synthetic-one-request-password" not in repr(client)


def test_client_rejects_stale_response_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = _request()
    stale = _response(request).model_copy(
        update={"request_nonce": UUID("27000000-0000-4000-8000-000000000099")}
    )
    client = EvidenceUnlockerClient(tmp_path / "unlocker.sock", timeout_seconds=1)
    monkeypatch.setattr(client, "_connect", lambda: _MemoryPeer(encode_unlocker_response(stale)))

    with pytest.raises(EvidenceUnlockerClientError) as error:
        client.process(request)

    assert error.value.error_code == "UNLOCKER_PROTOCOL"
    assert "synthetic-one-request-password" not in str(error.value)
