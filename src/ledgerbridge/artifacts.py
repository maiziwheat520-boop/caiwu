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
from typing import Literal, Protocol


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


HandoffState = Literal["open", "sealed", "committed", "aborted"]


class ArtifactHandoff:
    """One bounded, store-owned upload staging session.

    The caller writes validated file bytes and must pass an explicit parser
    completion signal to :meth:`complete`.  The session never exposes its
    temporary path or descriptor; ArtifactStore remains the only publication
    authority.
    """

    __slots__ = (
        "__byte_size",
        "__descriptor",
        "__hasher",
        "__state",
        "__store",
        "__temporary",
        "__temporary_identity",
    )

    def __init__(
        self,
        store: ArtifactStore,
        descriptor: int,
        temporary: Path,
        temporary_identity: tuple[int, int],
    ) -> None:
        self.__store = store
        self.__descriptor: int | None = descriptor
        self.__temporary = temporary
        self.__temporary_identity = temporary_identity
        self.__hasher = hashlib.sha256()
        self.__byte_size = 0
        self.__state: HandoffState = "open"

    @property
    def state(self) -> HandoffState:
        return self.__state

    @property
    def byte_size(self) -> int:
        return self.__byte_size

    def write(self, chunk: bytes) -> None:
        self.__require_state("open")
        if not isinstance(chunk, bytes):
            raise ArtifactStoreError("artifact handoff chunks must be bytes")
        if not chunk:
            return
        if self.__byte_size + len(chunk) > self.__store.max_bytes:
            raise ArtifactTooLargeError("artifact exceeds configured byte limit")
        try:
            with self.__store._quota_lock(
                timeout_seconds=self.__store.handoff_lock_timeout_seconds
            ):
                self.__store._validate_root_entries()
                staging_bytes = self.__store._staging_usage(clean_stale=False)
                if staging_bytes + len(chunk) > self.__store.staging_max_bytes:
                    raise ArtifactStagingQuotaError(
                        "artifact staging area would exceed its configured byte limit",
                        limit=self.__store.staging_max_bytes,
                        observed=staging_bytes,
                        requested=len(chunk),
                    )
                descriptor = self.__require_descriptor()
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_size != self.__byte_size:
                    raise ArtifactIntegrityError("artifact handoff descriptor changed")
                self.__write_all(descriptor, chunk)
                after = os.fstat(descriptor)
                if after.st_size != self.__byte_size + len(chunk):
                    raise ArtifactIntegrityError("artifact handoff size changed")
        except BaseException:
            self.abort()
            raise
        self.__hasher.update(chunk)
        self.__byte_size += len(chunk)

    def complete(self, *, parser_complete: bool) -> PublishedArtifact:
        self.__require_state("open")
        if not parser_complete:
            raise ArtifactStoreError("artifact handoff requires parser completion")
        try:
            with self.__store._quota_lock(
                timeout_seconds=self.__store.handoff_lock_timeout_seconds
            ):
                self.__store._validate_root_entries()
                descriptor = self.__require_descriptor()
                os.fsync(descriptor)
                if os.name != "nt":
                    fchmod = getattr(os, "fchmod", None)
                    if fchmod is None:
                        raise ArtifactIntegrityError("artifact handoff cannot set private mode")
                    fchmod(descriptor, stat.S_IRUSR | stat.S_IRGRP)
                digest = self.__hasher.digest()
                self.__store._verify_descriptor(descriptor, digest, self.__byte_size)
                self.__verify_temporary_path()
                storage_key = storage_key_for_digest(digest)
                destination = self.__store._destination(digest)
                self.__store._ensure_private_directory(destination.parent)
                try:
                    destination_metadata = destination.lstat()
                except FileNotFoundError:
                    published_bytes = self.__store._published_usage()
                    if published_bytes + self.__byte_size > self.__store.total_max_bytes:
                        raise ArtifactPublishedQuotaError(
                            "published artifacts would exceed their configured byte limit",
                            limit=self.__store.total_max_bytes,
                            observed=published_bytes,
                            requested=self.__byte_size,
                        ) from None
                    try:
                        os.link(self.__temporary, destination, follow_symlinks=False)
                    except FileExistsError:
                        self.__store._verify_path(destination, digest, self.__byte_size)
                        created = False
                    else:
                        linked_metadata = destination.lstat()
                        try:
                            self.__store._verify_path(destination, digest, self.__byte_size)
                        except BaseException:
                            self.__unlink_owned_destination(
                                destination,
                                (linked_metadata.st_dev, linked_metadata.st_ino),
                            )
                            raise
                        self.__store._fsync_directory(destination.parent)
                        created = True
                else:
                    if not stat.S_ISREG(destination_metadata.st_mode):
                        raise ArtifactIntegrityError("published artifact must be a regular file")
                    self.__store._verify_path(destination, digest, self.__byte_size)
                    created = False
                self.__state = "committed"
                result = PublishedArtifact(
                    sha256=digest,
                    byte_size=self.__byte_size,
                    storage_key=storage_key,
                    created=created,
                )
            self.__close_descriptor()
            with suppress(FileNotFoundError):
                self.__temporary.unlink()
            return result
        except BaseException:
            self.abort()
            raise

    def abort(self) -> None:
        if self.__state in {"committed", "aborted"}:
            return
        try:
            try:
                with self.__store._quota_lock(
                    timeout_seconds=self.__store.handoff_lock_timeout_seconds
                ):
                    self.__cleanup()
            except ArtifactQuotaStateError:
                # Cleanup must still be attempted if a lock holder is wedged;
                # the private handoff inode is safe to unlink by identity.
                self.__cleanup()
        finally:
            self.__state = "aborted"

    def __enter__(self) -> ArtifactHandoff:
        self.__require_state("open")
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self.__state in {"open", "sealed"}:
            self.abort()

    def __require_state(self, expected: HandoffState) -> None:
        if self.__state != expected:
            raise ArtifactStoreError(f"artifact handoff is {self.__state}; expected {expected}")

    def __require_descriptor(self) -> int:
        if self.__descriptor is None:
            raise ArtifactIntegrityError("artifact handoff descriptor is closed")
        return self.__descriptor

    @staticmethod
    def __write_all(descriptor: int, chunk: bytes) -> None:
        view = memoryview(chunk)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ArtifactStoreError("artifact handoff write made no progress")
            view = view[written:]

    def __close_descriptor(self) -> None:
        if self.__descriptor is not None:
            os.close(self.__descriptor)
            self.__descriptor = None

    def __cleanup(self) -> None:
        self.__close_descriptor()
        if self.__temporary_matches_identity():
            with suppress(FileNotFoundError):
                self.__temporary.unlink()
        self.__store._fsync_directory(self.__temporary.parent)

    def __temporary_matches_identity(self) -> bool:
        try:
            metadata = self.__temporary.lstat()
        except FileNotFoundError:
            return False
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_dev == self.__temporary_identity[0]
            and metadata.st_ino == self.__temporary_identity[1]
        )

    def __verify_temporary_path(self) -> None:
        if not self.__temporary_matches_identity():
            raise ArtifactIntegrityError("artifact handoff staging path was replaced")

    @staticmethod
    def __unlink_owned_destination(destination: Path, expected_identity: tuple[int, int]) -> None:
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            return
        if metadata.st_dev == expected_identity[0] and metadata.st_ino == expected_identity[1]:
            with suppress(FileNotFoundError):
                destination.unlink()


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
        handoff_lock_timeout_seconds: float = 5.0,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("artifact root must be absolute")
        if (
            max_bytes <= 0
            or total_max_bytes <= 0
            or staging_max_bytes <= 0
            or staging_ttl_seconds <= 0
            or chunk_size <= 0
            or handoff_lock_timeout_seconds <= 0
        ):
            raise ValueError("artifact limits must be positive")
        self.root = root
        self.max_bytes = max_bytes
        self.total_max_bytes = total_max_bytes
        self.staging_max_bytes = staging_max_bytes
        self.staging_ttl_seconds = staging_ttl_seconds
        self.chunk_size = chunk_size
        self.handoff_lock_timeout_seconds = handoff_lock_timeout_seconds

    def begin_handoff(self) -> ArtifactHandoff:
        """Create one bounded, store-owned staging session."""

        self._ensure_private_directory(self.root)
        staging_root = self.root / ".staging"
        self._ensure_private_directory(staging_root)
        with self._quota_lock(timeout_seconds=self.handoff_lock_timeout_seconds):
            self._validate_root_entries()
            staging_bytes = self._staging_usage(clean_stale=True)
            if staging_bytes > self.staging_max_bytes:
                raise ArtifactStagingQuotaError(
                    "artifact staging area exceeds its configured byte limit",
                    limit=self.staging_max_bytes,
                    observed=staging_bytes,
                    requested=0,
                )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="artifact-handoff-", dir=staging_root
            )
        temporary = Path(temporary_name)
        metadata = os.fstat(descriptor)
        return ArtifactHandoff(
            self,
            descriptor,
            temporary,
            (metadata.st_dev, metadata.st_ino),
        )

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
    def _quota_lock(self, *, timeout_seconds: float | None = None) -> Iterator[None]:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("quota lock timeout must be positive")
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
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ArtifactQuotaStateError("artifact quota lock must be a regular file")
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                locking = getattr(msvcrt, "lock" + "ing")
                lock_mode = getattr(
                    msvcrt,
                    "LK_" + ("LOCK" if timeout_seconds is None else "NBLCK"),
                )
                while True:
                    try:
                        locking(descriptor, lock_mode, 1)
                        break
                    except OSError as exc:
                        if deadline is None or time.monotonic() >= deadline:
                            raise ArtifactQuotaStateError(
                                "artifact quota lock acquisition timed out"
                            ) from exc
                        time.sleep(min(0.01, max(0.001, deadline - time.monotonic())))
            else:
                import fcntl

                flock = getattr(fcntl, "fl" + "ock")
                lock_ex = getattr(fcntl, "LOCK_" + "EX")
                lock_mode = (
                    lock_ex if timeout_seconds is None else lock_ex | getattr(fcntl, "LOCK_" + "NB")
                )
                while True:
                    try:
                        flock(descriptor, lock_mode)
                        break
                    except BlockingIOError as exc:
                        if deadline is None or time.monotonic() >= deadline:
                            raise ArtifactQuotaStateError(
                                "artifact quota lock acquisition timed out"
                            ) from exc
                        time.sleep(min(0.01, max(0.001, deadline - time.monotonic())))
            try:
                yield
            finally:
                if os.name == "nt":
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    locking = getattr(msvcrt, "lock" + "ing")
                    lock_unlck = getattr(msvcrt, "LK_" + "UNLCK")
                    locking(descriptor, lock_unlck, 1)
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
                metadata = path.lstat()
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
