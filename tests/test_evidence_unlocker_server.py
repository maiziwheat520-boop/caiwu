from __future__ import annotations

import asyncio
import os
import stat
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
)
from ledgerbridge.evidence_unlocker_server import start_evidence_unlocker_server

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unix socket unlocker requires POSIX")


class _Processor:
    def process(self, request: UnlockerRequest) -> UnlockerResponse:
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


def _request() -> UnlockerRequest:
    return UnlockerRequest(
        request_id=UUID("25000000-0000-4000-8000-000000000001"),
        operation_id=UUID("26000000-0000-4000-8000-000000000001"),
        request_nonce=UUID("27000000-0000-4000-8000-000000000001"),
        source=UnlockerSourceDescriptor(
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
        ),
        password="synthetic-one-request-password",
    )


@pytest.mark.asyncio
async def test_server_uses_private_socket_and_round_trips_bound_request(tmp_path: Path) -> None:
    socket_path = tmp_path / "private" / "unlocker.sock"
    server = await start_evidence_unlocker_server(socket_path, _Processor())
    try:
        response = await asyncio.to_thread(
            EvidenceUnlockerClient(socket_path, timeout_seconds=1).process,
            _request(),
        )
    finally:
        server.close()
        await server.wait_closed()

    assert response.status == UnlockerStatus.UNLOCKED
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_server_rejects_unexpected_peer_uid_before_processing(tmp_path: Path) -> None:
    calls = 0

    class CountingProcessor(_Processor):
        def process(self, request: UnlockerRequest) -> UnlockerResponse:
            nonlocal calls
            calls += 1
            return super().process(request)

    socket_path = tmp_path / "private" / "unlocker.sock"
    server = await start_evidence_unlocker_server(
        socket_path,
        CountingProcessor(),
        allowed_uid=int(os.geteuid()) + 1,  # type: ignore[attr-defined]
    )
    try:
        with pytest.raises(EvidenceUnlockerClientError):
            await asyncio.to_thread(
                EvidenceUnlockerClient(socket_path, timeout_seconds=1).process,
                _request(),
            )
    finally:
        server.close()
        await server.wait_closed()

    assert calls == 0
