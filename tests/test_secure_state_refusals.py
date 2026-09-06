"""Every way the encrypted state store refuses to hand back a record.

The store's whole claim is that a caller who holds an opaque handle gets back
exactly the bytes that were sealed under that handle and that purpose, or
nothing at all. Each test below removes one of the guarantees that claim rests
on - the handle's shape, the lock, the atomic write, the file's stability
between the two stats, the record schema, the identity binding - and fixes
that the store fails closed rather than serving a partially trusted record.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ledgerbridge import secure_state
from ledgerbridge.crypto import CryptoError, SecretStreamCipher
from ledgerbridge.keyring import KeyProvider, SyntheticKeyProvider
from ledgerbridge.secure_state import (
    EncryptedStateStore,
    SecureStateError,
    StateConflictError,
    StateFormatError,
    StateHandle,
    StateLockError,
    StateMetadata,
    StateNotFoundError,
    StatePurpose,
    _encode_record,
    _StateRecord,
)
from tests.test_secure_state import _KEY_ONE, _cipher

HANDLE = StateHandle("ab" * 32)
OTHER_HANDLE = StateHandle("cd" * 32)
NOW_TEXT = "2026-09-06T00:00:00Z"
LATER_TEXT = "2026-09-07T00:00:00Z"
EARLIER_TEXT = "2026-09-05T00:00:00Z"


def _provider() -> SyntheticKeyProvider:
    return SyntheticKeyProvider({"synthetic-1": _KEY_ONE}, active_generation="synthetic-1")


class _DecryptsIntoForgedBytes(SecretStreamCipher):
    """A cipher whose authentication is defeated, so the reader's own checks show.

    Nothing else can reach the record schema: the real cipher binds purpose and
    handle as associated data, so a tampered record never decrypts at all.
    """

    def __init__(self, provider: KeyProvider, *, chunk_size: int = 64) -> None:
        super().__init__(provider, chunk_size=chunk_size)
        self.forged = b""

    def decrypt(self, envelope: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        return self.forged


class _RefusesToEncrypt(SecretStreamCipher):
    def encrypt(self, plaintext: bytes, *, purpose: str, aad: bytes = b"") -> bytes:
        raise CryptoError("the cipher is unavailable")


def _forgeable(
    tmp_path: Path, **kwargs: object
) -> tuple[EncryptedStateStore, _DecryptsIntoForgedBytes, StateHandle]:
    """Return a store holding one real object, plus the cipher that forges it back."""

    cipher = _DecryptsIntoForgedBytes(_provider())
    store = EncryptedStateStore(tmp_path, cipher, **kwargs)  # type: ignore[arg-type]
    created = store.create(StatePurpose.OUTBOX, b"payload")
    return store, cipher, created.handle


def _record(handle: StateHandle, /, **overrides: object) -> bytes:
    value: dict[str, object] = {
        "created_at": NOW_TEXT,
        "expires_at": None,
        "generation": 1,
        "handle": handle.value,
        "payload": base64.b64encode(b"payload").decode("ascii"),
        "purpose": StatePurpose.OUTBOX.value,
        "revoked_at": None,
        "updated_at": NOW_TEXT,
        "version": 1,
    }
    value.update(overrides)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


# --- the handle -------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "ab" * 31, "ab" * 33, "zz" * 32, "AB" * 32])
def test_a_handle_that_is_not_sixty_four_lowercase_hex_digits_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="state handle is invalid"):
        StateHandle(value)


# --- constructing the store -------------------------------------------------


@pytest.mark.parametrize("max_payload_bytes", [0, -1, True, 1.0, "4096"])
def test_a_store_must_be_given_a_positive_payload_limit(max_payload_bytes: object) -> None:
    with pytest.raises(ValueError, match="max_payload_bytes must be a positive integer"):
        EncryptedStateStore(Path("unused"), _cipher(), max_payload_bytes=max_payload_bytes)  # type: ignore[arg-type]


@pytest.mark.parametrize("lock_timeout_seconds", [0, -1.0, True, "2", float("inf"), float("nan")])
def test_a_store_must_be_given_a_finite_positive_lock_timeout(
    lock_timeout_seconds: object,
) -> None:
    with pytest.raises(ValueError, match="lock_timeout_seconds must be positive and finite"):
        EncryptedStateStore(
            Path("unused"),
            _cipher(),
            lock_timeout_seconds=lock_timeout_seconds,  # type: ignore[arg-type]
        )


def test_a_state_root_reached_through_a_symlink_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(SecureStateError, match="must be a real directory"):
        EncryptedStateStore(link, _cipher())


# --- argument contracts -----------------------------------------------------


def test_a_purpose_that_is_not_a_state_purpose_is_refused(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    with pytest.raises(TypeError, match="purpose must be a StatePurpose"):
        store.read("outbox", HANDLE)  # type: ignore[arg-type]


def test_a_handle_that_is_not_a_state_handle_is_refused(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    with pytest.raises(TypeError, match="handle must be a StateHandle"):
        store.read(StatePurpose.OUTBOX, "ab" * 32)  # type: ignore[arg-type]


@pytest.mark.parametrize("expected_generation", [0, -1, True, 1.0])
def test_a_compare_and_swap_needs_a_positive_integer_generation(
    tmp_path: Path, expected_generation: object
) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    with pytest.raises(ValueError, match="expected_generation must be a positive integer"):
        store.revoke(
            StatePurpose.OUTBOX,
            HANDLE,
            expected_generation=expected_generation,  # type: ignore[arg-type]
        )


def test_a_payload_that_is_not_bytes_is_refused(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    with pytest.raises(TypeError, match="payload must be bytes"):
        store.create(StatePurpose.OUTBOX, "text")  # type: ignore[arg-type]


def test_a_payload_beyond_the_configured_limit_is_refused(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher(), max_payload_bytes=8)
    with pytest.raises(ValueError, match="payload exceeds configured limit"):
        store.create(StatePurpose.OUTBOX, b"more than eight bytes")


def test_a_ttl_beyond_the_representable_range_is_refused(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    with pytest.raises(ValueError, match="ttl is out of range"):
        store.create(StatePurpose.OUTBOX, b"payload", ttl=timedelta.max)


def test_a_clock_that_does_not_return_an_aware_datetime_is_refused(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher(), clock=lambda: datetime(2026, 9, 6))
    with pytest.raises(SecureStateError, match="clock must return an aware datetime"):
        store.create(StatePurpose.OUTBOX, b"payload")


def test_a_revoke_against_a_stale_generation_is_refused(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    created = store.create(StatePurpose.OUTBOX, b"payload")
    with pytest.raises(StateConflictError, match="generation conflict"):
        store.revoke(StatePurpose.OUTBOX, created.handle, expected_generation=9)


# --- allocating a handle ----------------------------------------------------


def test_a_handle_that_is_already_taken_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight collisions in a row means the randomness is broken, not the caller."""

    store = EncryptedStateStore(tmp_path, _cipher())
    monkeypatch.setattr(secrets, "token_hex", lambda count: HANDLE.value)
    (tmp_path / f"{HANDLE.value}.state").write_bytes(b"occupied")
    with pytest.raises(SecureStateError, match="could not allocate an opaque state handle"):
        store.create(StatePurpose.OUTBOX, b"payload")
    assert (tmp_path / f"{HANDLE.value}.state").read_bytes() == b"occupied"


# --- writing ----------------------------------------------------------------


def test_a_cipher_that_cannot_encrypt_is_reported_without_writing(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _RefusesToEncrypt(_provider(), chunk_size=64))
    with pytest.raises(secure_state.StateDecryptionError, match="encryption failed"):
        store.create(StatePurpose.OUTBOX, b"payload")
    assert list(tmp_path.iterdir()) == []


def test_ciphertext_beyond_the_configured_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secure_state, "_MAX_CIPHERTEXT_OVERHEAD", 0)
    store = EncryptedStateStore(tmp_path, _cipher(), max_payload_bytes=8)
    with pytest.raises(StateFormatError, match="encrypted secure state exceeds configured limit"):
        store.create(StatePurpose.OUTBOX, b"payload")


def test_an_atomic_write_that_fails_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())

    def refuse_fdopen(*args: object, **kwargs: object) -> object:
        raise OSError("no file object")

    monkeypatch.setattr(os, "fdopen", refuse_fdopen)
    with pytest.raises(SecureStateError, match="atomic write failed"):
        store.create(StatePurpose.OUTBOX, b"payload")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot fsync a directory handle")
def test_a_directory_that_cannot_be_fsynced_fails_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On POSIX the rename is only durable once the directory entry is synced."""

    store = EncryptedStateStore(tmp_path, _cipher())
    root = tmp_path.resolve(strict=True)
    real_open = os.open

    def refuse_directory(path: str, flags: int, *args: int) -> int:
        if Path(path) == root:
            raise PermissionError("no directory handle")
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", refuse_directory)
    with pytest.raises(SecureStateError, match="atomic write failed"):
        store.create(StatePurpose.OUTBOX, b"payload")


# --- the exclusive lock -----------------------------------------------------


def test_a_lock_held_past_the_bounded_wait_is_reported(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher(), lock_timeout_seconds=0.01)
    created = store.create(StatePurpose.OUTBOX, b"payload")
    (tmp_path / f"{created.handle.value}.lock").write_bytes(b"")
    with pytest.raises(StateLockError, match="lock is unavailable"):
        store.revoke(StatePurpose.OUTBOX, created.handle, expected_generation=1)


def test_a_lock_that_cannot_be_created_at_all_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())

    def refuse_open(*args: object, **kwargs: object) -> int:
        raise PermissionError("no lock")

    monkeypatch.setattr(os, "open", refuse_open)
    with pytest.raises(StateLockError, match="lock is unavailable"):
        store.create(StatePurpose.OUTBOX, b"payload")


# --- reading the file -------------------------------------------------------


def test_a_state_file_that_is_not_there_is_refused(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    with pytest.raises(StateNotFoundError, match="secure state was not found"):
        store.read(StatePurpose.OUTBOX, HANDLE)


def test_a_state_file_larger_than_the_ciphertext_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    created = store.create(StatePurpose.OUTBOX, b"payload")
    monkeypatch.setattr(secure_state, "_MAX_CIPHERTEXT_OVERHEAD", 0)
    narrowed = EncryptedStateStore(tmp_path, _cipher(), max_payload_bytes=8)
    with pytest.raises(StateFormatError, match="file is invalid or too large"):
        narrowed.read(StatePurpose.OUTBOX, created.handle)


def test_a_state_file_that_cannot_be_opened_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    created = store.create(StatePurpose.OUTBOX, b"payload")

    def refuse_open(*args: object, **kwargs: object) -> int:
        raise PermissionError("no descriptor")

    monkeypatch.setattr(os, "open", refuse_open)
    with pytest.raises(SecureStateError, match="secure state is unavailable"):
        store.read(StatePurpose.OUTBOX, created.handle)


def test_a_descriptor_that_never_became_a_file_object_is_still_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    created = store.create(StatePurpose.OUTBOX, b"payload")
    closed: list[int] = []
    real_close = os.close

    def refuse_fdopen(*args: object, **kwargs: object) -> object:
        raise OSError("no file object")

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "fdopen", refuse_fdopen)
    monkeypatch.setattr(os, "close", record_close)
    with pytest.raises(SecureStateError, match="secure state is unavailable"):
        store.read(StatePurpose.OUTBOX, created.handle)
    assert closed


def test_a_state_file_that_changes_between_the_two_stats_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    created = store.create(StatePurpose.OUTBOX, b"payload")
    counter = itertools.count()
    monkeypatch.setattr(secure_state, "_file_identity", lambda value: (next(counter),))
    with pytest.raises(StateFormatError, match="file changed while reading"):
        store.read(StatePurpose.OUTBOX, created.handle)


# --- the decrypted record ---------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"\xff\xfe{}", b"[]", b'{"version":1,"version":1}'],
)
def test_a_record_that_is_not_a_json_object_is_refused(tmp_path: Path, raw: bytes) -> None:
    store, cipher, handle = _forgeable(tmp_path)
    cipher.forged = raw
    with pytest.raises(StateFormatError, match=r"payload (is invalid JSON|fields are invalid)"):
        store.read(StatePurpose.OUTBOX, handle)


def test_a_record_without_exactly_the_expected_fields_is_refused(tmp_path: Path) -> None:
    store, cipher, handle = _forgeable(tmp_path)
    cipher.forged = b'{"version":1}'
    with pytest.raises(StateFormatError, match="payload fields are invalid"):
        store.read(StatePurpose.OUTBOX, handle)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"version": 2}, "payload version is unsupported"),
        ({"generation": 0}, "payload values are invalid"),
        ({"generation": "1"}, "payload values are invalid"),
        ({"payload": 1}, "payload values are invalid"),
        ({"payload": "not base64!"}, "payload values are invalid"),
        ({"purpose": "not-a-purpose"}, "payload values are invalid"),
        ({"handle": "zz" * 32}, "payload values are invalid"),
        ({"created_at": None}, "payload values are invalid"),
        ({"created_at": "2026-09-06T00:00:00+00:00"}, "payload values are invalid"),
        ({"updated_at": 1}, "payload values are invalid"),
        ({"updated_at": EARLIER_TEXT}, "timestamps are invalid"),
        ({"expires_at": NOW_TEXT}, "expiry is invalid"),
        ({"expires_at": EARLIER_TEXT}, "expiry is invalid"),
        ({"revoked_at": LATER_TEXT}, "revocation is invalid"),
        ({"revoked_at": EARLIER_TEXT}, "revocation is invalid"),
        (
            {"revoked_at": NOW_TEXT, "payload": base64.b64encode(b"x").decode("ascii")},
            "must not expose a payload",
        ),
    ],
)
def test_a_record_whose_values_do_not_hold_together_is_refused(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    store, cipher, handle = _forgeable(tmp_path)
    cipher.forged = _record(handle, **overrides)
    with pytest.raises(StateFormatError, match=message):
        store.read(StatePurpose.OUTBOX, handle)


@pytest.mark.parametrize(
    "overrides",
    [{"handle": OTHER_HANDLE.value}, {"purpose": StatePurpose.RETRY.value}],
)
def test_a_record_sealed_for_another_handle_or_purpose_is_refused(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    """The identity binding is re-checked after decryption, not only through the AAD."""

    store, cipher, handle = _forgeable(tmp_path)
    cipher.forged = _record(handle, **overrides)
    with pytest.raises(StateFormatError, match="identity binding is invalid"):
        store.read(StatePurpose.OUTBOX, handle)


def test_a_record_whose_payload_outgrew_the_configured_limit_is_refused(
    tmp_path: Path,
) -> None:
    store, cipher, handle = _forgeable(tmp_path, max_payload_bytes=8)
    cipher.forged = _record(
        handle, payload=base64.b64encode(b"more than eight bytes").decode("ascii")
    )
    with pytest.raises(StateFormatError, match="payload exceeds configured limit"):
        store.read(StatePurpose.OUTBOX, handle)


def test_a_naive_timestamp_can_never_be_encoded() -> None:
    """Every stored timestamp is UTC, so an unaware one is a bug, not a default."""

    naive = datetime(2026, 9, 6, 0, 0)
    metadata = StateMetadata(
        handle=HANDLE,
        purpose=StatePurpose.OUTBOX,
        generation=1,
        created_at=naive,
        updated_at=datetime(2026, 9, 6, tzinfo=UTC),
        expires_at=None,
        revoked_at=None,
    )
    with pytest.raises(StateFormatError, match="timestamp must be timezone-aware"):
        _encode_record(_StateRecord(metadata, b""))
