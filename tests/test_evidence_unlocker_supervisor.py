from __future__ import annotations

import asyncio
import threading
from uuid import UUID

import pytest

from ledgerbridge.evidence_unlocker_protocol import (
    UnlockerRequest,
    UnlockerResponse,
    UnlockerSourceDescriptor,
    UnlockerStatus,
    encode_unlocker_request,
)
from ledgerbridge.evidence_unlocker_server import EvidenceUnlockerSupervisor


class _Writer:
    def __init__(self) -> None:
        self.output = bytearray()

    def get_extra_info(self, _name: str) -> object:
        return object()

    def write(self, data: bytes) -> None:
        self.output.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


class _TrustedSupervisor(EvidenceUnlockerSupervisor):
    def _peer_is_allowed(self, _writer: asyncio.StreamWriter) -> bool:
        return True


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


def _reader(request: UnlockerRequest) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(encode_unlocker_request(request))
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_timed_out_processor_keeps_its_admission_slot_until_thread_finishes() -> None:
    release = threading.Event()
    calls = 0

    class SlowProcessor:
        def process(self, request: UnlockerRequest) -> UnlockerResponse:
            nonlocal calls
            calls += 1
            release.wait(timeout=1)
            return UnlockerResponse(
                request_id=request.request_id,
                operation_id=request.operation_id,
                request_nonce=request.request_nonce,
                source_ref=request.source.source_ref,
                status=UnlockerStatus.ERROR,
                error_code="UNLOCKER_UNAVAILABLE",
            )

    supervisor = _TrustedSupervisor(
        SlowProcessor(),
        request_timeout_seconds=0.01,
        concurrency=1,
        allowed_uid=1,
    )
    first_writer = _Writer()
    second_writer = _Writer()
    try:
        await supervisor.handle(_reader(_request()), first_writer)  # type: ignore[arg-type]
        await supervisor.handle(_reader(_request()), second_writer)  # type: ignore[arg-type]

        assert first_writer.output
        assert not second_writer.output
        assert calls == 1
    finally:
        release.set()
        await asyncio.sleep(0.05)
