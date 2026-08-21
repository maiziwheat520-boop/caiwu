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
from typing import BinaryIO, Protocol


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
    """Minimal connector-facing stream that does not expose a host path or write API."""

    __slots__ = ("__stream",)

    def __init__(self, stream: BinaryIO) -> None:
        self.__stream = stream

    def read(self, size: int = -1) -> bytes:
        return self.__stream.read(size)


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

            digest = hasher.digest()
            storage_key = storage_key_for_digest(digest)
            destination = self._destination(digest)
            self._ensure_private_directory(destination.parent)
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                self._verify_path(destination, digest, byte_size)
                created = False
            else:
                temporary.unlink()
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
        self._verify_path(destination, artifact.sha256, artifact.byte_size)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination, flags)
        stream = os.fdopen(descriptor, "rb")
        try:
            yield ReadOnlyArtifactStream(stream)
        finally:
            stream.close()

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
            if component is not None:
                current = current / component
            with suppress(FileExistsError):
                current.mkdir(mode=stat.S_IRWXU)
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ArtifactIntegrityError(
                    "artifact directories must be real directories, not symbolic links"
                )

    def _verify_path(self, path: Path, digest: bytes, byte_size: int) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise ArtifactIntegrityError("published artifact is missing") from exc
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ArtifactIntegrityError("published artifact must be a regular file")
        if metadata.st_size != byte_size:
            raise ArtifactIntegrityError("published artifact size mismatch")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        hasher = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "rb") as source:
                while chunk := source.read(self.chunk_size):
                    hasher.update(chunk)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            raise
        if hasher.digest() != digest:
            raise ArtifactIntegrityError("published artifact digest mismatch")

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
