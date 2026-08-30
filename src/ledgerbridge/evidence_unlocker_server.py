"""Private Unix-socket host for the one-shot evidence archive unlocker."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import stat
import struct
import threading
from asyncio import Future
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, cast

from ledgerbridge.artifacts import ArtifactStore
from ledgerbridge.config import EvidenceUnlockerRuntimeSettings
from ledgerbridge.crypto import SecretStreamCipher
from ledgerbridge.encrypted_artifacts import EncryptedArtifactStore
from ledgerbridge.evidence_unlocker import EvidenceArchiveUnlocker
from ledgerbridge.evidence_unlocker_protocol import (
    MAX_UNLOCKER_REQUEST_BYTES,
    EvidenceUnlockerProtocolError,
    UnlockerRequest,
    UnlockerResponse,
    UnlockerStatus,
    decode_unlocker_request,
    encode_unlocker_response,
)
from ledgerbridge.file_key_provider import FileKeyProvider


class EvidenceUnlockProcessor(Protocol):
    def process(self, request: UnlockerRequest) -> UnlockerResponse: ...


class EvidenceUnlockerSupervisor:
    def __init__(
        self,
        processor: EvidenceUnlockProcessor,
        *,
        request_timeout_seconds: float = 30.0,
        concurrency: int = 2,
        allowed_uid: int | None = None,
    ) -> None:
        if not 0 < request_timeout_seconds <= 120:
            raise ValueError("unlocker request timeout is invalid")
        if not 1 <= concurrency <= 16:
            raise ValueError("unlocker concurrency is invalid")
        if allowed_uid is not None and allowed_uid < 0:
            raise ValueError("unlocker peer uid is invalid")
        self._processor = processor
        self._request_timeout_seconds = request_timeout_seconds
        self._admission = threading.BoundedSemaphore(concurrency)
        self._allowed_uid = allowed_uid if allowed_uid is not None else _current_uid()

    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request: UnlockerRequest | None = None
        processing: Future[UnlockerResponse] | None = None
        release_on_exit = False
        try:
            if not self._peer_is_allowed(writer):
                return
            if not self._admission.acquire(blocking=False):
                return
            release_on_exit = True
            try:
                async with asyncio.timeout(self._request_timeout_seconds):
                    header = await reader.readexactly(4)
                    declared = struct.unpack("!I", header)[0]
                    if declared > MAX_UNLOCKER_REQUEST_BYTES:
                        raise EvidenceUnlockerProtocolError(
                            "unlocker request exceeds its byte limit"
                        )
                    body = bytearray(await reader.readexactly(declared))
                    try:
                        request = decode_unlocker_request(header + bytes(body))
                    finally:
                        for index in range(len(body)):
                            body[index] = 0
                    processing = asyncio.ensure_future(
                        asyncio.to_thread(self._processor.process, request)
                    )
                    response = await asyncio.shield(processing)
                    encoded = encode_unlocker_response(response)
                    writer.write(encoded)
                    await writer.drain()
            except TimeoutError:
                if request is not None:
                    writer.write(encode_unlocker_response(_unavailable(request)))
                    await writer.drain()
                if processing is not None and not processing.done():
                    processing.add_done_callback(self._release_admission)
                    release_on_exit = False
            finally:
                if release_on_exit:
                    self._admission.release()
        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            EvidenceUnlockerProtocolError,
            OSError,
            RuntimeError,
            struct.error,
            ValueError,
        ):
            return
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def _release_admission(self, _future: Future[UnlockerResponse]) -> None:
        self._admission.release()

    def _peer_is_allowed(self, writer: asyncio.StreamWriter) -> bool:
        peer = writer.get_extra_info("socket")
        if peer is None:
            return False
        if hasattr(socket, "SO_PEERCRED"):
            try:
                credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                _pid, uid, _gid = struct.unpack("3i", credentials)
            except (AttributeError, OSError, struct.error):
                return False
            return int(uid) == self._allowed_uid
        getpeereid = getattr(peer, "getpeereid", None)
        if getpeereid is None:
            return False
        try:
            uid, _gid = getpeereid()
        except OSError:
            return False
        return int(uid) == self._allowed_uid


async def start_evidence_unlocker_server(
    socket_path: str | Path,
    processor: EvidenceUnlockProcessor,
    *,
    request_timeout_seconds: float = 30.0,
    concurrency: int = 2,
    allowed_uid: int | None = None,
) -> asyncio.Server:
    path = Path(socket_path)
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError("unlocker socket path must be absolute")
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_metadata = parent.lstat()
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise OSError("unlocker socket directory is invalid")
    if os.name != "nt":
        if parent_metadata.st_uid != _current_uid():
            raise OSError("unlocker socket directory owner is invalid")
        os.chmod(parent, 0o700)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != _current_uid():
            raise OSError("unlocker socket path is occupied")
        path.unlink()
    raw_starter = getattr(asyncio, "start_unix_server", None)
    if raw_starter is None:
        raise OSError("Unix sockets are unavailable")
    starter = cast(Callable[..., Awaitable[asyncio.Server]], raw_starter)
    supervisor = EvidenceUnlockerSupervisor(
        processor,
        request_timeout_seconds=request_timeout_seconds,
        concurrency=concurrency,
        allowed_uid=allowed_uid,
    )
    server = await starter(supervisor.handle, path=str(path))
    if os.name != "nt":
        os.chmod(path, 0o600)
    return server


async def serve_evidence_unlocker(settings: EvidenceUnlockerRuntimeSettings) -> None:
    provider = FileKeyProvider(settings.internal_read_evidence_key_file)
    provider.self_test()
    cipher = SecretStreamCipher(provider)
    cipher.self_test()
    ciphertext_limit = min(256 * 1024 * 1024, settings.artifact_max_bytes + 4 * 1024 * 1024)
    durable = ArtifactStore(
        settings.artifact_root,
        max_bytes=ciphertext_limit,
        total_max_bytes=settings.artifact_total_max_bytes,
        staging_max_bytes=settings.artifact_staging_max_bytes,
        staging_ttl_seconds=settings.artifact_staging_ttl_seconds,
    )
    source_store = EncryptedArtifactStore(
        durable,
        cipher,
        max_plaintext_bytes=settings.artifact_max_bytes,
    )
    output_store = EncryptedArtifactStore(
        durable,
        cipher,
        max_plaintext_bytes=settings.internal_evidence_unlock_max_output_bytes,
    )
    processor = EvidenceArchiveUnlocker(
        source_store,
        output_store=output_store,
        max_output_bytes=settings.internal_evidence_unlock_max_output_bytes,
        max_members=settings.internal_evidence_unlock_max_members,
    )
    server = await start_evidence_unlocker_server(
        settings.internal_evidence_unlock_socket_path,
        processor,
        request_timeout_seconds=settings.internal_evidence_unlock_timeout_seconds,
        concurrency=settings.internal_evidence_unlock_concurrency,
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    settings_factory = cast(
        Callable[[], EvidenceUnlockerRuntimeSettings],
        EvidenceUnlockerRuntimeSettings,
    )
    asyncio.run(serve_evidence_unlocker(settings_factory()))


def _unavailable(request: UnlockerRequest) -> UnlockerResponse:
    return UnlockerResponse(
        request_id=request.request_id,
        operation_id=request.operation_id,
        request_nonce=request.request_nonce,
        source_ref=request.source.source_ref,
        status=UnlockerStatus.ERROR,
        error_code="UNLOCKER_UNAVAILABLE",
    )


def _current_uid() -> int:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        raise OSError("Unix peer identity is unavailable")
    return int(geteuid())


if __name__ == "__main__":
    main()
