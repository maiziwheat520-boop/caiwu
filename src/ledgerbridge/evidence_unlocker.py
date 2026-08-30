"""One-shot encrypted archive processor for the dedicated unlocker process."""

from __future__ import annotations

import mimetypes
import stat
import threading
import zipfile
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from ledgerbridge.artifacts import ArtifactStoreError, PublishedArtifact
from ledgerbridge.encrypted_artifacts import (
    EncryptedArtifactError,
    EncryptedArtifactStore,
    EncryptedEnvelopeMetadata,
    EncryptedPublishedArtifact,
)
from ledgerbridge.evidence_unlocker_protocol import (
    MAX_ARCHIVE_BYTES,
    MAX_UNLOCKED_OUTPUTS,
    UnlockerOutputDescriptor,
    UnlockerRequest,
    UnlockerResponse,
    UnlockerStatus,
)
from ledgerbridge.keyring import KeyProviderError, WrappedKey

_MAX_COMPRESSION_RATIO = 1_000
_NESTED_ARCHIVE_SUFFIXES = {".7z", ".gz", ".rar", ".tar", ".tgz", ".zip"}
_SUPPORTED_COMPRESSION = {
    zipfile.ZIP_STORED,
    zipfile.ZIP_DEFLATED,
    zipfile.ZIP_BZIP2,
    zipfile.ZIP_LZMA,
}


class _ArchiveRejected(RuntimeError):
    pass


class EvidenceArchiveUnlocker:
    """Authenticate one reviewed ZIP and publish each member as encrypted evidence."""

    def __init__(
        self,
        source_store: EncryptedArtifactStore,
        *,
        output_store: EncryptedArtifactStore | None = None,
        max_output_bytes: int = MAX_ARCHIVE_BYTES,
        max_members: int = MAX_UNLOCKED_OUTPUTS,
        max_cached_requests: int = 1024,
    ) -> None:
        if not 1 <= max_output_bytes <= MAX_ARCHIVE_BYTES:
            raise ValueError("unlocker output byte limit is invalid")
        if not 1 <= max_members <= MAX_UNLOCKED_OUTPUTS:
            raise ValueError("unlocker member limit is invalid")
        if not 1 <= max_cached_requests <= 65_536:
            raise ValueError("unlocker replay cache limit is invalid")
        self._source_store = source_store
        self._output_store = output_store or source_store
        self._max_output_bytes = max_output_bytes
        self._max_members = max_members
        self._max_cached_requests = max_cached_requests
        self._responses: dict[tuple[UUID, UUID], UnlockerResponse] = {}
        self._in_flight: set[tuple[UUID, UUID]] = set()
        self._operation_nonces: dict[UUID, UUID] = {}
        self._nonce_operations: dict[UUID, UUID] = {}
        self._lock = threading.Lock()

    def process(self, request: UnlockerRequest) -> UnlockerResponse:
        if type(request) is not UnlockerRequest:
            raise TypeError("unlocker request type is invalid")
        cache_key = (request.operation_id, request.request_nonce)
        with self._lock:
            if (
                self._operation_nonces.get(request.operation_id, request.request_nonce)
                != request.request_nonce
                or self._nonce_operations.get(request.request_nonce, request.operation_id)
                != request.operation_id
            ):
                return self._failure(request, UnlockerStatus.ERROR, "UNLOCKER_UNAVAILABLE")
            previous = self._responses.get(cache_key)
            if previous is not None:
                if (
                    previous.request_id != request.request_id
                    or previous.source_ref != request.source.source_ref
                ):
                    return self._failure(request, UnlockerStatus.ERROR, "UNLOCKER_UNAVAILABLE")
                return previous
            if cache_key in self._in_flight or len(self._responses) >= self._max_cached_requests:
                return self._failure(request, UnlockerStatus.ERROR, "UNLOCKER_UNAVAILABLE")
            self._operation_nonces[request.operation_id] = request.request_nonce
            self._nonce_operations[request.request_nonce] = request.operation_id
            self._in_flight.add(cache_key)
        response: UnlockerResponse | None = None
        try:
            try:
                response = self._process_once(request)
            except (
                _ArchiveRejected,
                NotImplementedError,
                zipfile.BadZipFile,
                zipfile.LargeZipFile,
            ):
                response = self._failure(
                    request,
                    UnlockerStatus.REJECTED,
                    "UNLOCK_REJECTED",
                )
            except (ArtifactStoreError, EncryptedArtifactError, KeyProviderError, OSError):
                response = self._failure(
                    request,
                    UnlockerStatus.ERROR,
                    "UNLOCKER_UNAVAILABLE",
                )
        finally:
            with self._lock:
                self._in_flight.discard(cache_key)
                if response is not None:
                    self._responses[cache_key] = response
        if response is None:
            raise RuntimeError("evidence unlocker failed without a bounded response")
        return response

    def _process_once(self, request: UnlockerRequest) -> UnlockerResponse:
        source = request.source
        artifact = EncryptedPublishedArtifact(
            object_ref=source.object_ref,
            plaintext_sha256=bytes.fromhex(source.plaintext_sha256),
            plaintext_size=source.plaintext_size,
            ciphertext=PublishedArtifact(
                sha256=bytes.fromhex(source.ciphertext_sha256),
                byte_size=source.ciphertext_size,
                storage_key=source.storage_key,
                created=False,
            ),
        )
        envelope = EncryptedEnvelopeMetadata(
            chunk_size=source.chunk_size,
            stream_header=bytes.fromhex(source.stream_header),
            wrapped_key=WrappedKey(
                generation=source.wrapped_key_generation,
                nonce=bytes.fromhex(source.wrapped_key_nonce),
                ciphertext=bytes.fromhex(source.wrapped_key_ciphertext),
            ),
        )
        with (
            self._source_store.open_verified(
                artifact,
                envelope_metadata=envelope,
            ) as archive_stream,
            zipfile.ZipFile(archive_stream, mode="r", allowZip64=False) as archive,
        ):
            members = self._validated_members(archive)
            password_bytes = request.password.encode("utf-8")
            outputs: list[UnlockerOutputDescriptor] = []
            for member in members:
                try:
                    with archive.open(member, mode="r", pwd=password_bytes) as extracted:
                        published = self._output_store.publish(extracted)
                except (RuntimeError, NotImplementedError, zipfile.BadZipFile):
                    raise _ArchiveRejected("archive member could not be authenticated") from None
                if published.plaintext_size != member.file_size:
                    raise _ArchiveRejected("archive member size is inconsistent")
                outputs.append(self._output_descriptor(member, published))
        return UnlockerResponse(
            request_id=request.request_id,
            operation_id=request.operation_id,
            request_nonce=request.request_nonce,
            source_ref=source.source_ref,
            status=UnlockerStatus.UNLOCKED,
            outputs=tuple(outputs),
        )

    def _validated_members(self, archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
        members = tuple(item for item in archive.infolist() if not item.is_dir())
        if not members or len(members) > self._max_members:
            raise _ArchiveRejected("archive member count is invalid")
        total = 0
        observed_names: set[str] = set()
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            unix_mode = (member.external_attr >> 16) & 0xFFFF
            if (
                not name
                or name in {".", ".."}
                or path.name != name
                or "\\" in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or stat.S_ISLNK(unix_mode)
                or name.casefold() in observed_names
            ):
                raise _ArchiveRejected("archive member path is invalid")
            observed_names.add(name.casefold())
            if path.suffix.lower() in _NESTED_ARCHIVE_SUFFIXES:
                raise _ArchiveRejected("nested archives are unavailable")
            if member.compress_type not in _SUPPORTED_COMPRESSION or not (member.flag_bits & 0x1):
                raise _ArchiveRejected("archive member encryption is unavailable")
            if member.file_size <= 0 or member.file_size > self._max_output_bytes:
                raise _ArchiveRejected("archive member size is invalid")
            if member.compress_size <= 0 or member.file_size > (
                member.compress_size * _MAX_COMPRESSION_RATIO
            ):
                raise _ArchiveRejected("archive compression ratio is invalid")
            total += member.file_size
            if total > self._max_output_bytes:
                raise _ArchiveRejected("archive output exceeds its byte limit")
        return members

    def _output_descriptor(
        self,
        member: zipfile.ZipInfo,
        published: EncryptedPublishedArtifact,
    ) -> UnlockerOutputDescriptor:
        envelope = self._output_store.envelope_metadata(published)
        media_type = mimetypes.guess_type(member.filename, strict=False)[0]
        return UnlockerOutputDescriptor(
            evidence_ref=uuid4(),
            media_type=media_type or "application/octet-stream",
            display_name=member.filename,
            object_ref=published.object_ref,
            plaintext_sha256=published.plaintext_sha256.hex(),
            plaintext_size=published.plaintext_size,
            ciphertext_sha256=published.ciphertext.sha256.hex(),
            ciphertext_size=published.ciphertext.byte_size,
            storage_key=published.ciphertext.storage_key,
            chunk_size=envelope.chunk_size,
            stream_header=envelope.stream_header.hex(),
            wrapped_key_generation=envelope.wrapped_key.generation,
            wrapped_key_nonce=envelope.wrapped_key.nonce.hex(),
            wrapped_key_ciphertext=envelope.wrapped_key.ciphertext.hex(),
        )

    @staticmethod
    def _failure(
        request: UnlockerRequest,
        status: UnlockerStatus,
        error_code: str,
    ) -> UnlockerResponse:
        return UnlockerResponse(
            request_id=request.request_id,
            operation_id=request.operation_id,
            request_nonce=request.request_nonce,
            source_ref=request.source.source_ref,
            status=status,
            error_code=error_code,  # type: ignore[arg-type]
        )
