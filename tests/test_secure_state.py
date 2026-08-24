from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from ledgerbridge.crypto import SecretStreamCipher
from ledgerbridge.keyring import SyntheticKeyProvider
from ledgerbridge.secure_state import (
    EncryptedStateStore,
    StateConflictError,
    StateDecryptionError,
    StateExpiredError,
    StateHandle,
    StatePurpose,
    StateRevokedError,
)

_KEY_ONE = bytes(range(32))
_KEY_TWO = bytes(reversed(range(32)))


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _cipher(
    *, keys: dict[str, bytes] | None = None, active: str = "synthetic-1"
) -> SecretStreamCipher:
    provider = SyntheticKeyProvider(keys or {"synthetic-1": _KEY_ONE}, active_generation=active)
    return SecretStreamCipher(provider, chunk_size=64)


@pytest.mark.parametrize("purpose", list(StatePurpose))
def test_state_round_trip_is_purpose_separated_and_filename_is_opaque(
    tmp_path: Path,
    purpose: StatePurpose,
) -> None:
    payload = (
        b'{"mailbox":"native-user@example.invalid",'
        b'"contact":"synthetic-contact-8842","token":"synthetic-token-canary"}'
    )
    store = EncryptedStateStore(tmp_path, _cipher())

    created = store.create(purpose, payload, ttl=timedelta(minutes=5))
    loaded = store.read(purpose, created.handle)

    assert loaded == created
    assert created.generation == 1
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert re.fullmatch(r"[0-9a-f]{64}\.state", files[0].name)
    assert b"native-user" not in files[0].read_bytes()
    assert b"synthetic-contact" not in files[0].read_bytes()
    assert b"synthetic-token-canary" not in files[0].read_bytes()
    assert all(marker not in files[0].name for marker in ("mailbox", "contact", "token"))


def test_each_object_has_distinct_random_handle_and_ciphertext(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    first = store.create(StatePurpose.OUTBOX, b"same synthetic payload")
    second = store.create(StatePurpose.OUTBOX, b"same synthetic payload")

    assert first.handle != second.handle
    assert (tmp_path / f"{first.handle.value}.state").read_bytes() != (
        tmp_path / f"{second.handle.value}.state"
    ).read_bytes()


def test_wrong_purpose_and_ciphertext_alias_fail_closed(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    created = store.create(StatePurpose.OAUTH_TOKEN_MAP, b"synthetic secret")

    with pytest.raises(StateDecryptionError):
        store.read(StatePurpose.IDENTITY_MAP, created.handle)

    alias = StateHandle("ab" * 32)
    (tmp_path / f"{alias.value}.state").write_bytes(
        (tmp_path / f"{created.handle.value}.state").read_bytes()
    )
    with pytest.raises(StateDecryptionError):
        store.read(StatePurpose.OAUTH_TOKEN_MAP, alias)


def test_compare_and_swap_advances_generation_and_rejects_stale_writer(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    created = store.create(StatePurpose.RETRY, b"attempt-1")

    updated = store.compare_and_swap(
        StatePurpose.RETRY,
        created.handle,
        expected_generation=1,
        payload=b"attempt-2",
        ttl=timedelta(seconds=30),
    )

    assert updated.generation == 2
    assert store.read(StatePurpose.RETRY, created.handle).payload == b"attempt-2"
    with pytest.raises(StateConflictError):
        store.compare_and_swap(
            StatePurpose.RETRY,
            created.handle,
            expected_generation=1,
            payload=b"stale attempt",
        )


def test_compare_and_swap_is_atomic_for_concurrent_writers(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher(), lock_timeout_seconds=5)
    created = store.create(StatePurpose.OUTBOX, b"queued")
    barrier = Barrier(2)

    def update(payload: bytes) -> str:
        barrier.wait()
        try:
            store.compare_and_swap(
                StatePurpose.OUTBOX,
                created.handle,
                expected_generation=1,
                payload=payload,
            )
        except StateConflictError:
            return "conflict"
        return "updated"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, (b"worker-a", b"worker-b")))

    assert sorted(results) == ["conflict", "updated"]
    current = store.read(StatePurpose.OUTBOX, created.handle)
    assert current.generation == 2
    assert current.payload in {b"worker-a", b"worker-b"}


def test_ttl_expiry_fails_closed_and_cannot_be_resurrected(tmp_path: Path) -> None:
    clock = MutableClock(datetime(2026, 8, 24, 6, 0, tzinfo=UTC))
    store = EncryptedStateStore(tmp_path, _cipher(), clock=clock)
    created = store.create(StatePurpose.RETRY, b"synthetic retry", ttl=timedelta(seconds=10))
    clock.value += timedelta(seconds=10)

    with pytest.raises(StateExpiredError):
        store.read(StatePurpose.RETRY, created.handle)
    with pytest.raises(StateExpiredError):
        store.compare_and_swap(
            StatePurpose.RETRY,
            created.handle,
            expected_generation=1,
            payload=b"resurrection is forbidden",
        )


def test_revoke_is_logical_and_keeps_an_authenticated_tombstone(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    created = store.create(StatePurpose.IDENTITY_MAP, b"synthetic identity")
    path = tmp_path / f"{created.handle.value}.state"

    metadata = store.revoke(
        StatePurpose.IDENTITY_MAP,
        created.handle,
        expected_generation=created.generation,
    )

    assert metadata.revoked
    assert metadata.generation == 2
    assert path.is_file()
    assert b"synthetic identity" not in path.read_bytes()
    with pytest.raises(StateRevokedError):
        store.read(StatePurpose.IDENTITY_MAP, created.handle)
    with pytest.raises(StateRevokedError):
        store.revoke(StatePurpose.IDENTITY_MAP, created.handle, expected_generation=2)


def test_missing_or_wrong_wrapping_key_fails_closed(tmp_path: Path) -> None:
    original = EncryptedStateStore(tmp_path, _cipher())
    created = original.create(StatePurpose.OAUTH_TOKEN_MAP, b"synthetic refresh secret")

    wrong_key = EncryptedStateStore(
        tmp_path,
        _cipher(keys={"synthetic-1": _KEY_TWO}),
    )
    with pytest.raises(StateDecryptionError):
        wrong_key.read(StatePurpose.OAUTH_TOKEN_MAP, created.handle)

    missing_generation = EncryptedStateStore(
        tmp_path,
        _cipher(keys={"synthetic-2": _KEY_TWO}, active="synthetic-2"),
    )
    with pytest.raises(StateDecryptionError):
        missing_generation.read(StatePurpose.OAUTH_TOKEN_MAP, created.handle)


def test_tampering_fails_closed_without_plaintext_fallback(tmp_path: Path) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    created = store.create(StatePurpose.OUTBOX, b"synthetic outbound body")
    path = tmp_path / f"{created.handle.value}.state"
    ciphertext = bytearray(path.read_bytes())
    ciphertext[-1] ^= 1
    path.write_bytes(ciphertext)

    with pytest.raises(StateDecryptionError):
        store.read(StatePurpose.OUTBOX, created.handle)


def test_write_fsyncs_before_publishing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    store = EncryptedStateStore(tmp_path, _cipher())

    created = store.create(StatePurpose.OUTBOX, b"synthetic body")

    assert calls
    assert (tmp_path / f"{created.handle.value}.state").is_file()


@pytest.mark.parametrize(
    "bad_ttl",
    [timedelta(0), timedelta(seconds=-1)],
)
def test_non_positive_ttl_is_rejected(tmp_path: Path, bad_ttl: timedelta) -> None:
    store = EncryptedStateStore(tmp_path, _cipher())
    with pytest.raises(ValueError, match="ttl"):
        store.create(StatePurpose.RETRY, b"synthetic", ttl=bad_ttl)
