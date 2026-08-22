"""Content-addressed, fail-closed raw evidence storage."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import time
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


class ArtifactQuotaError(ArtifactStoreError):
    """Base class for capacity rejections with log-safe diagnostics."""

    quota_kind: str

    def __init__(self, message: str, *, limit: int, observed: int, requested: int) -> None:
        super().__init__(message)
        self.limit = limit
        self.observed = observed
        self.requested = requested


class ArtifactPublishedQuotaError(ArtifactQuotaError):
    quota_kind = "published"


class ArtifactStagingQuotaError(ArtifactQuotaError):
    quota_kind = "staging"


class ArtifactQuotaStateError(ArtifactStoreError):
    """Raised when artifact usage cannot be measured without ambiguity."""

    quota_kind = "state"


@dataclass(frozen=True, slots=True)
class ArtifactQuotaSnapshot:
    published_bytes: int
    published_limit_bytes: int
    staging_bytes: int
    staging_limit_bytes: int


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
        total_max_bytes: int = 10 * 1024 * 1024 * 1024,
        staging_max_bytes: int = 512 * 1024 * 1024,
        staging_ttl_seconds: int = 60 * 60,
        chunk_size: int = 64 * 1024,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("artifact root must be absolute")
        if (
            max_bytes <= 0
            or total_max_bytes <= 0
            or staging_max_bytes <= 0
            or staging_ttl_seconds <= 0
            or chunk_size <= 0
        ):
            raise ValueError("artifact limits must be positive")
        self.root = root
        self.max_bytes = max_bytes
        self.total_max_bytes = total_max_bytes
        self.staging_max_bytes = staging_max_bytes
        self.staging_ttl_seconds = staging_ttl_seconds
        self.chunk_size = chunk_size

    def publish(self, stream: BinarySource) -> PublishedArtifact:
        self._ensure_private_directory(self.root)
        staging_root = self.root / ".staging"
        self._ensure_private_directory(staging_root)
        with self._quota_lock():
            self._validate_root_entries()
            staging_bytes = self._staging_usage(clean_stale=True)
            if staging_bytes > self.staging_max_bytes:
                raise ArtifactStagingQuotaError(
                    "artifact staging area exceeds its configured byte limit",
                    limit=self.staging_max_bytes,
                    observed=staging_bytes,
                    requested=0,
                )
            descriptor, temporary_name = tempfile.mkstemp(prefix="artifact-", dir=staging_root)
            temporary = Path(temporary_name)
            hasher = hashlib.sha256()
            byte_size = 0
            try:
                with os.fdopen(descriptor, "wb", buffering=0) as target:
                    while True:
                        chunk = stream.read(self.chunk_size)
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise ArtifactStoreError("artifact stream must return bytes")
                        next_size = byte_size + len(chunk)
                        if next_size > self.max_bytes:
                            raise ArtifactTooLargeError("artifact exceeds configured byte limit")
                        if staging_bytes + next_size > self.staging_max_bytes:
                            raise ArtifactStagingQuotaError(
                                "artifact staging area would exceed its configured byte limit",
                                limit=self.staging_max_bytes,
                                observed=staging_bytes + byte_size,
                                requested=len(chunk),
                            )
                        byte_size = next_size
                        hasher.update(chunk)
                        target.write(chunk)
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
                    destination_metadata = destination.lstat()
                except FileNotFoundError:
                    published_bytes = self._published_usage()
                    if published_bytes + byte_size > self.total_max_bytes:
                        raise ArtifactPublishedQuotaError(
                            "published artifacts would exceed their configured byte limit",
                            limit=self.total_max_bytes,
                            observed=published_bytes,
                            requested=byte_size,
                        ) from None
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
                else:
                    if not stat.S_ISREG(destination_metadata.st_mode):
                        raise ArtifactIntegrityError("published artifact must be a regular file")
                    self._verify_path(destination, digest, byte_size)
                    created = False
                return PublishedArtifact(
                    sha256=digest,
                    byte_size=byte_size,
                    storage_key=storage_key,
                    created=created,
                )
            finally:
                with suppress(FileNotFoundError):
                    temporary.unlink()

    def quota_snapshot(self) -> ArtifactQuotaSnapshot:
        """Return one lock-consistent view of published and staging capacity."""

        self._ensure_private_directory(self.root)
        self._ensure_private_directory(self.root / ".staging")
        with self._quota_lock():
            self._validate_root_entries()
            return ArtifactQuotaSnapshot(
                published_bytes=self._published_usage(),
                published_limit_bytes=self.total_max_bytes,
                staging_bytes=self._staging_usage(clean_stale=True),
                staging_limit_bytes=self.staging_max_bytes,
            )

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

    @contextmanager
    def _quota_lock(self) -> Iterator[None]:
        path = self.root / ".quota.lock"
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            raise ArtifactQuotaStateError("artifact quota lock is unavailable") from exc
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ArtifactQuotaStateError("artifact quota lock must be a regular file")
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                flock = getattr(fcntl, "fl" + "ock")
                lock_ex = getattr(fcntl, "LOCK_" + "EX")
                flock(descriptor, lock_ex)
            try:
                yield
            finally:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    flock = getattr(fcntl, "fl" + "ock")
                    lock_un = getattr(fcntl, "LOCK_" + "UN")
                    flock(descriptor, lock_un)
        finally:
            os.close(descriptor)

    def _published_usage(self) -> int:
        published_root = self.root / "sha256"
        try:
            published_root.lstat()
        except FileNotFoundError:
            return 0
        return self._scan_tree(published_root, published=True, clean_stale=False)

    def _staging_usage(self, *, clean_stale: bool) -> int:
        return self._scan_tree(
            self.root / ".staging",
            published=False,
            clean_stale=clean_stale,
        )

    def _scan_tree(self, root: Path, *, published: bool, clean_stale: bool) -> int:
        try:
            root_metadata = root.lstat()
        except OSError as exc:
            raise ArtifactQuotaStateError("artifact quota directory is unreadable") from exc
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise ArtifactQuotaStateError("artifact quota directory must be a real directory")

        total = 0
        try:
            entries = list(os.scandir(root))
        except OSError as exc:
            raise ArtifactQuotaStateError("artifact quota directory is unreadable") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ArtifactQuotaStateError("artifact quota entry is unreadable") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise ArtifactQuotaStateError("artifact quota trees cannot contain symlinks")
            if stat.S_ISDIR(metadata.st_mode):
                if not published:
                    raise ArtifactQuotaStateError(
                        "artifact staging area contains an unexpected directory"
                    )
                total += self._scan_tree(path, published=True, clean_stale=False)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ArtifactQuotaStateError("artifact quota trees may contain regular files only")
            if published:
                relative = path.relative_to(self.root / "sha256")
                if not self._valid_published_relative(relative):
                    raise ArtifactQuotaStateError(
                        "published artifact tree contains an unexpected entry"
                    )
            elif not entry.name.startswith("artifact-"):
                raise ArtifactQuotaStateError("artifact staging area contains an unexpected entry")
            descriptor = self._open_quota_regular(path)
            descriptor_metadata = os.fstat(descriptor)
            os.close(descriptor)
            if descriptor_metadata.st_size != metadata.st_size or (
                os.name != "nt"
                and (
                    descriptor_metadata.st_dev != metadata.st_dev
                    or descriptor_metadata.st_ino != metadata.st_ino
                )
            ):
                raise ArtifactQuotaStateError("artifact quota entry changed during measurement")
            if clean_stale and time.time() - metadata.st_mtime >= self.staging_ttl_seconds:
                try:
                    current = path.lstat()
                    if (
                        current.st_size != metadata.st_size
                        or not stat.S_ISREG(current.st_mode)
                        or (
                            os.name != "nt"
                            and (
                                current.st_dev != metadata.st_dev
                                or current.st_ino != metadata.st_ino
                            )
                        )
                    ):
                        raise ArtifactQuotaStateError(
                            "stale artifact staging entry changed during cleanup"
                        )
                    path.unlink()
                    self._fsync_directory(root)
                except ArtifactQuotaStateError:
                    raise
                except OSError as exc:
                    raise ArtifactQuotaStateError(
                        "stale artifact staging entry could not be removed"
                    ) from exc
                continue
            total += descriptor_metadata.st_size
        return total

    def _validate_root_entries(self) -> None:
        allowed = {".quota.lock", ".staging", "sha256"}
        try:
            entries = list(os.scandir(self.root))
        except OSError as exc:
            raise ArtifactQuotaStateError("artifact root is unreadable") from exc
        for entry in entries:
            if entry.name not in allowed:
                raise ArtifactQuotaStateError("artifact root contains an unexpected entry")
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ArtifactQuotaStateError("artifact root entry is unreadable") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise ArtifactQuotaStateError("artifact root cannot contain symlinks")
            if entry.name == ".quota.lock":
                expected = stat.S_ISREG(metadata.st_mode)
            else:
                expected = stat.S_ISDIR(metadata.st_mode)
            if not expected:
                raise ArtifactQuotaStateError("artifact root entry has an unexpected type")

    @staticmethod
    def _valid_published_relative(relative: Path) -> bool:
        if len(relative.parts) != 3:
            return False
        first, second, digest = relative.parts
        hexdigits = set("0123456789abcdef")
        return (
            len(first) == 2
            and len(second) == 2
            and len(digest) == 64
            and set(first + second + digest) <= hexdigits
            and digest.startswith(first + second)
        )

    @staticmethod
    def _open_quota_regular(path: Path) -> int:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ArtifactQuotaStateError("artifact quota entry is unreadable") from exc
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ArtifactQuotaStateError("artifact quota entry must be a regular file")
        return descriptor

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
