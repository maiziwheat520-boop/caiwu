"""Encrypted, purpose-separated storage for small online-integration state.

This module deliberately knows nothing about Microsoft Graph, Hermes, mailbox
identifiers, contacts, or OAuth flows.  Callers receive a random opaque handle
and put any provider-native identifiers or secrets inside the encrypted payload.

``revoke`` is a logical state transition.  It does not promise physical erasure:
older ciphertext may remain in filesystem snapshots, backups, or storage media.
"""

from __future__ import annotations

import base64
import contextlib
import json
import math
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

from ledgerbridge.crypto import CryptoError, SecretStreamCipher
from ledgerbridge.keyring import KeyProviderError

STATE_PAYLOAD_VERSION: Final = 1
DEFAULT_MAX_PAYLOAD_BYTES: Final = 4 * 1024 * 1024
_MAX_CIPHERTEXT_OVERHEAD: Final = 1024 * 1024
_HANDLE_BYTES: Final = 32
_HANDLE_HEX_LENGTH: Final = _HANDLE_BYTES * 2
_STATE_SUFFIX: Final = ".state"
_LOCK_SUFFIX: Final = ".lock"
_PURPOSE_OAUTH: Final = "oauth-token-map"
_RECORD_FIELDS: Final = frozenset(
    {
        "created_at",
        "expires_at",
        "generation",
        "handle",
        "payload",
        "purpose",
        "revoked_at",
        "updated_at",
        "version",
    }
)


class SecureStateError(RuntimeError):
    """Base class for fail-closed secure-state failures."""


class StateNotFoundError(SecureStateError):
    """An opaque state handle has no stored object."""


class StateConflictError(SecureStateError):
    """The expected compare-and-swap generation is stale."""


class StateExpiredError(SecureStateError):
    """The state object has passed its encrypted expiry time."""


class StateRevokedError(SecureStateError):
    """The state object has been logically revoked."""


class StateDecryptionError(SecureStateError):
    """Ciphertext could not be authenticated or decrypted."""


class StateFormatError(SecureStateError):
    """Decrypted state did not satisfy the versioned record schema."""


class StateLockError(SecureStateError):
    """Exclusive access could not be acquired within the bounded wait."""


class StatePurpose(StrEnum):
    """Cryptographically separated online-state domains."""

    OUTBOX = "outbox"
    RETRY = "retry"
    OAUTH_TOKEN_MAP = _PURPOSE_OAUTH
    IDENTITY_MAP = "identity-map"


@dataclass(frozen=True, slots=True)
class StateHandle:
    """A random locator with no provider-native or user identity semantics."""

    value: str

    def __post_init__(self) -> None:
        if len(self.value) != _HANDLE_HEX_LENGTH:
            raise ValueError("state handle is invalid")
        try:
            decoded = bytes.fromhex(self.value)
        except ValueError as exc:
            raise ValueError("state handle is invalid") from exc
        if len(decoded) != _HANDLE_BYTES or self.value != self.value.lower():
            raise ValueError("state handle is invalid")


@dataclass(frozen=True, slots=True)
class StateMetadata:
    """Non-secret lifecycle metadata for one encrypted object."""

    handle: StateHandle
    purpose: StatePurpose
    generation: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """Authenticated plaintext plus its compare-and-swap metadata."""

    metadata: StateMetadata
    payload: bytes

    @property
    def handle(self) -> StateHandle:
        return self.metadata.handle

    @property
    def generation(self) -> int:
        return self.metadata.generation


@dataclass(frozen=True, slots=True)
class _StateRecord:
    metadata: StateMetadata
    payload: bytes


class EncryptedStateStore:
    """Filesystem state store backed by authenticated envelope encryption.

    The injected ``SecretStreamCipher`` creates an independent data-encryption
    key for every ``encrypt`` call.  The object purpose and random handle are
    authenticated as associated data, so copying ciphertext to another handle
    or opening it through another purpose fails closed.

    Successful writes fsync the temporary file before atomic replacement and,
    where the platform supports opening directories, fsync the containing
    directory.  A small exclusive lock file serializes generation checks across
    processes.  Lock and state names contain only random opaque handles.
    """

    def __init__(
        self,
        root: Path,
        cipher: SecretStreamCipher,
        *,
        clock: Callable[[], datetime] | None = None,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        lock_timeout_seconds: float = 2.0,
    ) -> None:
        if type(max_payload_bytes) is not int or max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be a positive integer")
        if (
            isinstance(lock_timeout_seconds, bool)
            or not isinstance(lock_timeout_seconds, (int, float))
            or not math.isfinite(lock_timeout_seconds)
            or lock_timeout_seconds <= 0
        ):
            raise ValueError("lock_timeout_seconds must be positive and finite")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise SecureStateError("secure-state root must be a real directory")
        self._root = root.resolve(strict=True)
        self._cipher = cipher
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_payload_bytes = max_payload_bytes
        self._max_ciphertext_bytes = max_payload_bytes + _MAX_CIPHERTEXT_OVERHEAD
        self._lock_timeout_seconds = float(lock_timeout_seconds)

    def create(
        self,
        purpose: StatePurpose,
        payload: bytes,
        *,
        ttl: timedelta | None = None,
    ) -> StateSnapshot:
        """Create a new object with generation one and a random opaque handle."""

        purpose = _require_purpose(purpose)
        payload = self._validate_payload(payload)
        now = self._now()
        expires_at = _expiry_from_ttl(now, ttl)
        for _ in range(8):
            handle = StateHandle(secrets.token_hex(_HANDLE_BYTES))
            path = self._state_path(handle)
            with self._exclusive_lock(handle):
                if path.exists():
                    continue
                metadata = StateMetadata(
                    handle=handle,
                    purpose=purpose,
                    generation=1,
                    created_at=now,
                    updated_at=now,
                    expires_at=expires_at,
                    revoked_at=None,
                )
                self._write_record(path, _StateRecord(metadata, payload))
                return StateSnapshot(metadata, payload)
        raise SecureStateError("could not allocate an opaque state handle")

    def read(self, purpose: StatePurpose, handle: StateHandle) -> StateSnapshot:
        """Read an authenticated live object or fail closed."""

        purpose = _require_purpose(purpose)
        handle = _require_handle(handle)
        record = self._read_record(self._state_path(handle), purpose, handle)
        self._require_live(record.metadata)
        return StateSnapshot(record.metadata, record.payload)

    def compare_and_swap(
        self,
        purpose: StatePurpose,
        handle: StateHandle,
        *,
        expected_generation: int,
        payload: bytes,
        ttl: timedelta | None = None,
    ) -> StateSnapshot:
        """Replace live state only when its encrypted generation matches."""

        purpose = _require_purpose(purpose)
        handle = _require_handle(handle)
        expected_generation = _require_generation(expected_generation)
        payload = self._validate_payload(payload)
        path = self._state_path(handle)
        with self._exclusive_lock(handle):
            current = self._read_record(path, purpose, handle)
            self._require_live(current.metadata)
            if current.metadata.generation != expected_generation:
                raise StateConflictError("secure-state generation conflict")
            now = self._now()
            metadata = StateMetadata(
                handle=handle,
                purpose=purpose,
                generation=expected_generation + 1,
                created_at=current.metadata.created_at,
                updated_at=now,
                expires_at=_expiry_from_ttl(now, ttl),
                revoked_at=None,
            )
            self._write_record(path, _StateRecord(metadata, payload))
        return StateSnapshot(metadata, payload)

    def revoke(
        self,
        purpose: StatePurpose,
        handle: StateHandle,
        *,
        expected_generation: int,
    ) -> StateMetadata:
        """Logically revoke an object without claiming physical data erasure.

        The active ciphertext is replaced by a record with an empty payload and
        a revocation timestamp.  Prior ciphertext may still exist in snapshots,
        backups, journals, or media remanence and is outside this API's claim.
        """

        purpose = _require_purpose(purpose)
        handle = _require_handle(handle)
        expected_generation = _require_generation(expected_generation)
        path = self._state_path(handle)
        with self._exclusive_lock(handle):
            current = self._read_record(path, purpose, handle)
            if current.metadata.revoked:
                raise StateRevokedError("secure state is revoked")
            if current.metadata.generation != expected_generation:
                raise StateConflictError("secure-state generation conflict")
            now = self._now()
            metadata = StateMetadata(
                handle=handle,
                purpose=purpose,
                generation=expected_generation + 1,
                created_at=current.metadata.created_at,
                updated_at=now,
                expires_at=current.metadata.expires_at,
                revoked_at=now,
            )
            self._write_record(path, _StateRecord(metadata, b""))
        return metadata

    def _validate_payload(self, payload: bytes) -> bytes:
        if type(payload) is not bytes:
            raise TypeError("secure-state payload must be bytes")
        if len(payload) > self._max_payload_bytes:
            raise ValueError("secure-state payload exceeds configured limit")
        return payload

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise SecureStateError("secure-state clock must return an aware datetime")
        return value.astimezone(UTC)

    def _require_live(self, metadata: StateMetadata) -> None:
        if metadata.revoked:
            raise StateRevokedError("secure state is revoked")
        if metadata.expires_at is not None and self._now() >= metadata.expires_at:
            raise StateExpiredError("secure state is expired")

    def _state_path(self, handle: StateHandle) -> Path:
        return self._root / f"{handle.value}{_STATE_SUFFIX}"

    def _lock_path(self, handle: StateHandle) -> Path:
        return self._root / f"{handle.value}{_LOCK_SUFFIX}"

    def _write_record(self, path: Path, record: _StateRecord) -> None:
        plaintext = _encode_record(record)
        purpose = _crypto_purpose(record.metadata.purpose)
        aad = _associated_data(record.metadata.purpose, record.metadata.handle)
        try:
            ciphertext = self._cipher.encrypt(plaintext, purpose=purpose, aad=aad)
        except (CryptoError, KeyProviderError) as exc:
            raise StateDecryptionError("secure-state encryption failed") from exc
        if len(ciphertext) > self._max_ciphertext_bytes:
            raise StateFormatError("encrypted secure state exceeds configured limit")
        self._atomic_write(path, ciphertext)

    def _read_record(
        self,
        path: Path,
        purpose: StatePurpose,
        handle: StateHandle,
    ) -> _StateRecord:
        ciphertext = self._read_stable(path)
        try:
            plaintext = self._cipher.decrypt(
                ciphertext,
                purpose=_crypto_purpose(purpose),
                aad=_associated_data(purpose, handle),
            )
        except (CryptoError, KeyProviderError) as exc:
            raise StateDecryptionError("secure-state authentication failed") from exc
        record = _decode_record(plaintext)
        if record.metadata.handle != handle or record.metadata.purpose is not purpose:
            raise StateFormatError("secure-state identity binding is invalid")
        if len(record.payload) > self._max_payload_bytes:
            raise StateFormatError("secure-state payload exceeds configured limit")
        return record

    def _read_stable(self, path: Path) -> bytes:
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = None
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > self._max_ciphertext_bytes:
                    raise StateFormatError("secure-state file is invalid or too large")
                ciphertext = handle.read(self._max_ciphertext_bytes + 1)
                after = os.fstat(handle.fileno())
        except FileNotFoundError as exc:
            raise StateNotFoundError("secure state was not found") from exc
        except (StateFormatError, StateNotFoundError):
            raise
        except OSError as exc:
            raise SecureStateError("secure state is unavailable") from exc
        finally:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
        if len(ciphertext) > self._max_ciphertext_bytes or _file_identity(before) != _file_identity(
            after
        ):
            raise StateFormatError("secure-state file changed while reading")
        return ciphertext

    def _atomic_write(self, target: Path, ciphertext: bytes) -> None:
        temporary = self._root / f".{secrets.token_hex(_HANDLE_BYTES)}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(ciphertext)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            _fsync_directory(self._root)
        except OSError as exc:
            raise SecureStateError("secure-state atomic write failed") from exc
        finally:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    @contextmanager
    def _exclusive_lock(self, handle: StateHandle) -> Iterator[None]:
        path = self._lock_path(handle)
        deadline = time.monotonic() + self._lock_timeout_seconds
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                if time.monotonic() >= deadline:
                    raise StateLockError("secure-state lock is unavailable") from exc
                time.sleep(0.005)
            except OSError as exc:
                raise StateLockError("secure-state lock is unavailable") from exc
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def _require_purpose(value: StatePurpose) -> StatePurpose:
    if not isinstance(value, StatePurpose):
        raise TypeError("purpose must be a StatePurpose")
    return value


def _require_handle(value: StateHandle) -> StateHandle:
    if not isinstance(value, StateHandle):
        raise TypeError("handle must be a StateHandle")
    return value


def _require_generation(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("expected_generation must be a positive integer")
    return value


def _expiry_from_ttl(now: datetime, ttl: timedelta | None) -> datetime | None:
    if ttl is None:
        return None
    if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
        raise ValueError("ttl must be a positive timedelta")
    try:
        expiry = now + ttl
    except OverflowError as exc:
        raise ValueError("ttl is out of range") from exc
    return expiry.astimezone(UTC)


def _crypto_purpose(purpose: StatePurpose) -> str:
    return f"ledgerbridge.secure-state.v{STATE_PAYLOAD_VERSION}/{purpose.value}"


def _associated_data(purpose: StatePurpose, handle: StateHandle) -> bytes:
    return json.dumps(
        {
            "handle": handle.value,
            "purpose": purpose.value,
            "version": STATE_PAYLOAD_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _encode_record(record: _StateRecord) -> bytes:
    metadata = record.metadata
    value: dict[str, object] = {
        "created_at": _format_datetime(metadata.created_at),
        "expires_at": _format_datetime(metadata.expires_at),
        "generation": metadata.generation,
        "handle": metadata.handle.value,
        "payload": base64.b64encode(record.payload).decode("ascii"),
        "purpose": metadata.purpose.value,
        "revoked_at": _format_datetime(metadata.revoked_at),
        "updated_at": _format_datetime(metadata.updated_at),
        "version": STATE_PAYLOAD_VERSION,
    }
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _decode_record(plaintext: bytes) -> _StateRecord:
    try:
        value = json.loads(plaintext.decode("ascii"), object_pairs_hook=_reject_duplicate_fields)
    except (UnicodeDecodeError, json.JSONDecodeError, StateFormatError) as exc:
        raise StateFormatError("secure-state payload is invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != _RECORD_FIELDS:
        raise StateFormatError("secure-state payload fields are invalid")
    if value["version"] != STATE_PAYLOAD_VERSION:
        raise StateFormatError("secure-state payload version is unsupported")
    try:
        handle = StateHandle(value["handle"])
        purpose = StatePurpose(value["purpose"])
        generation = value["generation"]
        payload_text = value["payload"]
        if type(generation) is not int or generation <= 0 or not isinstance(payload_text, str):
            raise ValueError
        payload = base64.b64decode(payload_text.encode("ascii"), validate=True)
        created_at = _parse_datetime(value["created_at"], required=True)
        updated_at = _parse_datetime(value["updated_at"], required=True)
        expires_at = _parse_datetime(value["expires_at"], required=False)
        revoked_at = _parse_datetime(value["revoked_at"], required=False)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise StateFormatError("secure-state payload values are invalid") from exc
    if created_at is None or updated_at is None:
        raise StateFormatError("secure-state required timestamps are missing")
    if updated_at < created_at:
        raise StateFormatError("secure-state timestamps are invalid")
    if expires_at is not None and expires_at <= created_at:
        raise StateFormatError("secure-state expiry is invalid")
    if revoked_at is not None and (revoked_at < created_at or revoked_at != updated_at):
        raise StateFormatError("secure-state revocation is invalid")
    if revoked_at is not None and payload:
        raise StateFormatError("revoked secure state must not expose a payload")
    metadata = StateMetadata(
        handle=handle,
        purpose=purpose,
        generation=generation,
        created_at=created_at,
        updated_at=updated_at,
        expires_at=expires_at,
        revoked_at=revoked_at,
    )
    return _StateRecord(metadata, payload)


def _reject_duplicate_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StateFormatError("secure-state payload contains duplicate fields")
        result[key] = value
    return result


def _format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise StateFormatError("secure-state timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object, *, required: bool) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError
    parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _fsync_directory(path: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        # Windows cannot open/fsync directory handles through this API.  The
        # state file itself has already been fsynced before atomic replacement.
        if os.name != "nt":
            raise
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
