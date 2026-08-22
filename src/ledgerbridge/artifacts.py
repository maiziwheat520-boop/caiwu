"""Content-addressed, fail-closed raw evidence storage."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class BinarySource(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class ArtifactStoreError(RuntimeError):
    """Base class for artifact publication and verification failures."""


class ArtifactTooLargeError(ArtifactStoreError):
    pass


class ArtifactIntegrityError(ArtifactStoreError):
    pass


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    sha256: bytes
    byte_size: int
    storage_key: str
    created: bool

    @property
    def sha256_hex(self) -> str:
        return self.sha256.hex()


class ReadOnlyArtifactStream:
    """Minimal connector-facing reader over one already-verified descriptor."""

    __slots__ = ("__closed", "__descriptor")

    def __init__(self, descriptor: int) -> None:
        self.__descriptor = descriptor
        self.__closed = False

    def read(self, size: int = -1) -> bytes:
        if self.__closed:
            raise ValueError("I/O operation on closed evidence stream")
        if size >= 0:
            return os.read(self.__descriptor, size)
        chunks: list[bytes] = []
        while chunk := os.read(self.__descriptor, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)

    def _close(self) -> None:
        if not self.__closed:
            os.close(self.__descriptor)
            self.__closed = True


def storage_key_for_digest(digest: bytes) -> str:
    if len(digest) != hashlib.sha256().digest_size:
        raise ValueError("SHA-256 digest must be exactly 32 bytes")
    value = digest.hex()
    return f"sha256/{value[:2]}/{value[2:4]}/{value}"


class ArtifactStore:
    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int,
        chunk_size: int = 64 * 1024,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("artifact root must be absolute")
        if max_bytes <= 0 or chunk_size <= 0:
            raise ValueError("artifact limits must be positive")
        self.root = root
        self.max_bytes = max_bytes
        self.chunk_size = chunk_size

    def publish(self, stream: BinarySource) -> PublishedArtifact:
        self._ensure_private_directory(self.root)
        staging_root = self.root / ".staging"
        self._ensure_private_directory(staging_root)
        descriptor, temporary_name = tempfile.mkstemp(prefix="artifact-", dir=staging_root)
        temporary = Path(temporary_name)
        hasher = hashlib.sha256()
        byte_size = 0
        try:
            with os.fdopen(descriptor, "wb") as target:
                while True:
                    chunk = stream.read(self.chunk_size)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise ArtifactStoreError("artifact stream must return bytes")
                    byte_size += len(chunk)
                    if byte_size > self.max_bytes:
                        raise ArtifactTooLargeError("artifact exceeds configured byte limit")
                    hasher.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())

            if os.name != "nt":
                os.chmod(temporary, stat.S_IRUSR | stat.S_IRGRP)
                metadata_descriptor = self._open_regular(temporary)
                try:
                    os.fsync(metadata_descriptor)
                finally:
                    os.close(metadata_descriptor)

            digest = hasher.digest()
            storage_key = storage_key_for_digest(digest)
            destination = self._destination(digest)
            self._ensure_private_directory(destination.parent)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                self._verify_path(destination, digest, byte_size)
                created = False
            else:
                temporary.unlink()
                if os.name == "nt":
                    os.chmod(destination, stat.S_IRUSR | stat.S_IRGRP)
                self._fsync_directory(destination.parent)
                created = True
            return PublishedArtifact(
                sha256=digest,
                byte_size=byte_size,
                storage_key=storage_key,
                created=created,
            )
        finally:
            with suppress(FileNotFoundError):
                temporary.unlink()

    def read_prefix(self, artifact: PublishedArtifact, limit: int) -> bytes:
        if limit < 0:
            raise ValueError("prefix limit must be non-negative")
        with self.open_verified(artifact) as stream:
            return stream.read(limit)

    @contextmanager
    def open_verified(self, artifact: PublishedArtifact) -> Iterator[ReadOnlyArtifactStream]:
        destination = self._destination(artifact.sha256)
        descriptor = self._open_regular(destination)
        try:
            self._verify_descriptor(descriptor, artifact.sha256, artifact.byte_size)
        except BaseException:
            os.close(descriptor)
            raise
        stream = ReadOnlyArtifactStream(descriptor)
        try:
            yield stream
        finally:
            stream._close()

    def _destination(self, digest: bytes) -> Path:
        relative = Path(storage_key_for_digest(digest))
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 4:
            raise ArtifactIntegrityError("derived artifact path escaped its root")
        return self.root.joinpath(relative)

    def _ensure_private_directory(self, directory: Path) -> None:
        try:
            relative = directory.relative_to(self.root)
        except ValueError as exc:
            raise ArtifactIntegrityError("artifact directory escaped its root") from exc
        current = self.root
        components = (None, *relative.parts)
        for component in components:
            parent = current.parent if component is None else current
            candidate = current if component is None else current / component
            created = False
            try:
                candidate.mkdir(mode=stat.S_IRWXU)
                created = True
            except FileExistsError:
                pass
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ArtifactIntegrityError(
                    "artifact directories must be real directories, not symbolic links"
                )
            if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != stat.S_IRWXU:
                os.chmod(candidate, stat.S_IRWXU)
                self._fsync_directory(candidate)
            if created:
                self._fsync_directory(parent)
            current = candidate

    def _verify_path(self, path: Path, digest: bytes, byte_size: int) -> None:
        descriptor = self._open_regular(path)
        try:
            self._verify_descriptor(descriptor, digest, byte_size)
        finally:
            os.close(descriptor)

    @staticmethod
    def _open_regular(path: Path) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            return os.open(path, flags)
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError("published artifact is missing") from exc
        except OSError as exc:
            raise ArtifactIntegrityError("published artifact must be a regular file") from exc

    def _verify_descriptor(self, descriptor: int, digest: bytes, byte_size: int) -> None:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ArtifactIntegrityError("published artifact must be a regular file")
        if metadata.st_size != byte_size:
            raise ArtifactIntegrityError("published artifact size mismatch")
        hasher = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        while chunk := os.read(descriptor, self.chunk_size):
            hasher.update(chunk)
        if hasher.digest() != digest:
            raise ArtifactIntegrityError("published artifact digest mismatch")
        os.lseek(descriptor, 0, os.SEEK_SET)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
