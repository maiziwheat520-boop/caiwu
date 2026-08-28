from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from ledgerbridge.artifacts import ArtifactStore
from ledgerbridge.crypto import SecretStreamCipher
from ledgerbridge.encrypted_artifacts import (
    EncryptedArtifactError,
    EncryptedArtifactIntegrityError,
    EncryptedArtifactStore,
    EncryptedEnvelopeMetadata,
    EncryptedPublishedArtifact,
)
from ledgerbridge.keyring import SyntheticKeyProvider, WrappedKey

KEY = b"\x91" * 32
OTHER_KEY = b"\x92" * 32


def _store(root: Path, *, key: bytes = KEY) -> EncryptedArtifactStore:
    durable = ArtifactStore(
        root.resolve(),
        max_bytes=2_000_000,
        total_max_bytes=8_000_000,
        staging_max_bytes=4_000_000,
    )
    cipher = SecretStreamCipher(
        SyntheticKeyProvider({"synthetic-1": key}, active_generation="synthetic-1"),
        chunk_size=7,
    )
    return EncryptedArtifactStore(durable, cipher, max_plaintext_bytes=1_000_000)


def _durable_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file() and path.name != ".quota.lock"]


def test_publish_persists_only_randomized_ciphertext_and_round_trips(tmp_path: Path) -> None:
    marker = b"S1-ARTIFACT-CANARY-must-not-be-on-disk"
    store = _store(tmp_path)

    first = store.publish(io.BytesIO(marker))
    second = store.publish(io.BytesIO(marker))

    assert first.plaintext_sha256 == hashlib.sha256(marker).digest()
    assert first.plaintext_size == len(marker)
    assert first.object_ref != second.object_ref
    assert first.storage_key != second.storage_key
    assert first.storage_key != f"sha256/{first.plaintext_sha256.hex()}"
    assert all(marker not in path.read_bytes() for path in _durable_files(tmp_path))
    with store.open_verified(first) as stream:
        assert stream.read() == marker
    assert store.read_prefix(first, 2) == marker[:2]


def test_encrypted_handoff_never_places_plaintext_in_durable_staging(tmp_path: Path) -> None:
    marker = b"S1-HANDOFF-CANARY"
    store = _store(tmp_path)
    handoff = store.begin_handoff()
    handoff.write(marker[:5])
    handoff.write(marker[5:])

    assert all(marker not in path.read_bytes() for path in _durable_files(tmp_path))
    artifact = handoff.complete(parser_complete=True)
    assert handoff.state == "committed"
    assert all(marker not in path.read_bytes() for path in _durable_files(tmp_path))
    with store.open_verified(artifact) as stream:
        assert stream.read() == marker


def test_encrypted_artifact_binds_object_reference_digest_size_and_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = store.publish(io.BytesIO(b"bound evidence"))

    wrong_ref = EncryptedPublishedArtifact(
        object_ref="0" * 64,
        plaintext_sha256=artifact.plaintext_sha256,
        plaintext_size=artifact.plaintext_size,
        ciphertext=artifact.ciphertext,
    )
    wrong_digest = EncryptedPublishedArtifact(
        object_ref=artifact.object_ref,
        plaintext_sha256=b"x" * 32,
        plaintext_size=artifact.plaintext_size,
        ciphertext=artifact.ciphertext,
    )
    wrong_size = EncryptedPublishedArtifact(
        object_ref=artifact.object_ref,
        plaintext_sha256=artifact.plaintext_sha256,
        plaintext_size=artifact.plaintext_size + 1,
        ciphertext=artifact.ciphertext,
    )
    for invalid in (wrong_ref, wrong_digest, wrong_size):
        with pytest.raises(EncryptedArtifactIntegrityError), store.open_verified(invalid):
            pass
    wrong_key_store = _store(tmp_path, key=OTHER_KEY)
    with pytest.raises(EncryptedArtifactIntegrityError), wrong_key_store.open_verified(artifact):
        pass


def test_encrypted_artifact_descriptor_metadata_is_verified(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = store.publish(io.BytesIO(b"descriptor evidence"))
    metadata = store.envelope_metadata(artifact)
    with store.open_verified(artifact, envelope_metadata=metadata) as stream:
        assert stream.read() == b"descriptor evidence"

    drifted = EncryptedEnvelopeMetadata(
        chunk_size=metadata.chunk_size + 1,
        stream_header=metadata.stream_header,
        wrapped_key=metadata.wrapped_key,
    )
    with (
        pytest.raises(EncryptedArtifactIntegrityError, match="authentication"),
        store.open_verified(artifact, envelope_metadata=drifted),
    ):
        pass


def test_encrypted_envelope_metadata_rejects_invalid_shape() -> None:
    with pytest.raises(ValueError, match="chunk size"):
        EncryptedEnvelopeMetadata(
            chunk_size=0,
            stream_header=b"h" * 24,
            wrapped_key=WrappedKey("synthetic-1", b"n" * 24, b"w" * 48),
        )
    with pytest.raises(ValueError, match="stream header"):
        EncryptedEnvelopeMetadata(
            chunk_size=7,
            stream_header=b"short",
            wrapped_key=WrappedKey("synthetic-1", b"n" * 24, b"w" * 48),
        )


def test_encrypted_handoff_abort_and_limits_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    handoff = store.begin_handoff()
    handoff.write(b"partial")
    with pytest.raises(EncryptedArtifactError, match="parser completion"):
        handoff.complete(parser_complete=False)
    handoff.abort()
    assert handoff.state == "aborted"

    too_large = EncryptedArtifactStore(
        ArtifactStore(tmp_path.resolve(), max_bytes=1000),
        SecretStreamCipher(
            SyntheticKeyProvider({"synthetic-1": KEY}, active_generation="synthetic-1")
        ),
        max_plaintext_bytes=2,
    )
    with pytest.raises(EncryptedArtifactError, match="plaintext limit"):
        too_large.publish(io.BytesIO(b"abc"))
