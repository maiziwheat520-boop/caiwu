from __future__ import annotations

import base64
import io
import stat
from pathlib import Path
from uuid import UUID

from ledgerbridge.artifacts import ArtifactStore, PublishedArtifact
from ledgerbridge.crypto import SecretStreamCipher
from ledgerbridge.encrypted_artifacts import (
    EncryptedArtifactStore,
    EncryptedEnvelopeMetadata,
    EncryptedPublishedArtifact,
)
from ledgerbridge.evidence_unlocker import EvidenceArchiveUnlocker
from ledgerbridge.evidence_unlocker_protocol import (
    UnlockerRequest,
    UnlockerSourceDescriptor,
    UnlockerStatus,
)
from ledgerbridge.keyring import SyntheticKeyProvider, WrappedKey

_ARCHIVE = base64.b64decode(
    "UEsDBBQAAQAAAKJ7Hl1rG+cmKQAAAB0AAAANAAAAc3RhdGVtZW50LnBkZoIrO1mdiIJHcOazdYcQ5rWPwBREx/mZTlERwTK8vrfDICbg2+dJRbgGUEsBAj8AFAABAAAAonseXWsb5yYpAAAAHQAAAA0AJAAAAAAAAAAgAAAAAAAAAHN0YXRlbWVudC5wZGYKACAAAAAAAAEAGADEEOc7UTjdAQAAAAAAAAAAAAAAAAAAAABQSwUGAAAAAAEAAQBfAAAAVAAAAAAA"
)
_PLAINTEXT = b"synthetic statement evidence\n"


def _store(tmp_path: Path) -> EncryptedArtifactStore:
    provider = SyntheticKeyProvider({"test-v1": b"k" * 32}, active_generation="test-v1")
    return EncryptedArtifactStore(
        ArtifactStore(tmp_path.resolve(), max_bytes=2_000_000),
        SecretStreamCipher(provider, chunk_size=1024),
        max_plaintext_bytes=1_000_000,
    )


def _source(
    store: EncryptedArtifactStore,
    archive: bytes = _ARCHIVE,
) -> UnlockerSourceDescriptor:
    published = store.publish(io.BytesIO(archive))
    envelope = store.envelope_metadata(published)
    return UnlockerSourceDescriptor(
        source_ref=UUID("21000000-0000-4000-8000-000000000001"),
        evidence_ref=UUID("20000000-0000-4000-8000-000000000001"),
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


def _request(source: UnlockerSourceDescriptor, *, password: str) -> UnlockerRequest:
    return UnlockerRequest(
        request_id=UUID("25000000-0000-4000-8000-000000000001"),
        operation_id=UUID("26000000-0000-4000-8000-000000000001"),
        request_nonce=UUID("27000000-0000-4000-8000-000000000001"),
        source=source,
        password=password,
    )


def test_unlocker_decrypts_archive_and_publishes_only_encrypted_output(tmp_path: Path) -> None:
    store = _store(tmp_path)
    response = EvidenceArchiveUnlocker(store).process(
        _request(_source(store), password="synthetic-archive-password")
    )

    assert response.status == UnlockerStatus.UNLOCKED
    assert response.error_code is None
    assert len(response.outputs) == 1
    output = response.outputs[0]
    assert output.display_name == "statement.pdf"
    assert output.media_type == "application/pdf"
    assert output.plaintext_sha256 != output.ciphertext_sha256
    artifact = EncryptedPublishedArtifact(
        object_ref=output.object_ref,
        plaintext_sha256=bytes.fromhex(output.plaintext_sha256),
        plaintext_size=output.plaintext_size,
        ciphertext=PublishedArtifact(
            sha256=bytes.fromhex(output.ciphertext_sha256),
            byte_size=output.ciphertext_size,
            storage_key=output.storage_key,
            created=False,
        ),
    )
    envelope = EncryptedEnvelopeMetadata(
        chunk_size=output.chunk_size,
        stream_header=bytes.fromhex(output.stream_header),
        wrapped_key=WrappedKey(
            generation=output.wrapped_key_generation,
            nonce=bytes.fromhex(output.wrapped_key_nonce),
            ciphertext=bytes.fromhex(output.wrapped_key_ciphertext),
        ),
    )
    with store.open_verified(artifact, envelope_metadata=envelope) as stream:
        assert stream.read() == _PLAINTEXT
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert _PLAINTEXT not in path.read_bytes()


def test_wrong_archive_password_returns_fixed_rejection_without_outputs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    response = EvidenceArchiveUnlocker(store).process(
        _request(_source(store), password="synthetic-wrong-password")
    )

    assert response.status == UnlockerStatus.REJECTED
    assert response.error_code == "UNLOCK_REJECTED"
    assert response.outputs == ()
    assert "synthetic-wrong-password" not in repr(response)


def test_source_descriptor_integrity_failure_is_unavailable_not_a_password_rejection(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    source = _source(store).model_copy(update={"plaintext_sha256": "0" * 64})

    response = EvidenceArchiveUnlocker(store).process(
        _request(source, password="synthetic-archive-password")
    )

    assert response.status == UnlockerStatus.ERROR
    assert response.error_code == "UNLOCKER_UNAVAILABLE"
    assert response.outputs == ()


def test_unlocker_replay_returns_same_encrypted_outputs_and_nonce_reuse_fails_closed(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    unlocker = EvidenceArchiveUnlocker(store)
    request = _request(_source(store), password="synthetic-archive-password")

    first = unlocker.process(request)
    replay = unlocker.process(request)
    mismatched = unlocker.process(
        request.model_copy(update={"request_id": UUID("25000000-0000-4000-8000-000000000099")})
    )
    operation_rebound = unlocker.process(
        request.model_copy(
            update={
                "request_id": UUID("25000000-0000-4000-8000-000000000098"),
                "request_nonce": UUID("27000000-0000-4000-8000-000000000099"),
            }
        )
    )
    nonce_rebound = unlocker.process(
        request.model_copy(
            update={
                "request_id": UUID("25000000-0000-4000-8000-000000000097"),
                "operation_id": UUID("26000000-0000-4000-8000-000000000099"),
            }
        )
    )

    assert first == replay
    assert first.outputs[0].evidence_ref == replay.outputs[0].evidence_ref
    assert mismatched.status == UnlockerStatus.ERROR
    assert mismatched.error_code == "UNLOCKER_UNAVAILABLE"
    assert operation_rebound.status == UnlockerStatus.ERROR
    assert nonce_rebound.status == UnlockerStatus.ERROR


def test_unlocker_rejects_decompressed_output_over_its_policy_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    response = EvidenceArchiveUnlocker(store, max_output_bytes=8).process(
        _request(_source(store), password="synthetic-archive-password")
    )

    assert response.status == UnlockerStatus.REJECTED
    assert response.error_code == "UNLOCK_REJECTED"
    assert response.outputs == ()


def test_unlocker_rejects_archive_path_traversal_and_nested_archives(tmp_path: Path) -> None:
    store = _store(tmp_path)
    unlocker = EvidenceArchiveUnlocker(store)
    traversal = _ARCHIVE.replace(b"statement.pdf", b"../report.pdf")
    nested = _ARCHIVE.replace(b"statement.pdf", b"payload12.zip")

    traversal_response = unlocker.process(
        _request(_source(store, traversal), password="synthetic-archive-password")
    )
    nested_response = unlocker.process(
        _request(_source(store, nested), password="synthetic-archive-password").model_copy(
            update={
                "request_id": UUID("25000000-0000-4000-8000-000000000002"),
                "operation_id": UUID("26000000-0000-4000-8000-000000000002"),
                "request_nonce": UUID("27000000-0000-4000-8000-000000000002"),
            }
        )
    )

    assert traversal_response.status == UnlockerStatus.REJECTED
    assert nested_response.status == UnlockerStatus.REJECTED


def test_unlocker_rejects_symlink_archive_members(tmp_path: Path) -> None:
    store = _store(tmp_path)
    symlink_archive = bytearray(_ARCHIVE)
    central = symlink_archive.index(b"PK\x01\x02")
    symlink_mode = (stat.S_IFLNK | 0o777) << 16
    symlink_archive[central + 38 : central + 42] = symlink_mode.to_bytes(4, "little")

    response = EvidenceArchiveUnlocker(store).process(
        _request(
            _source(store, bytes(symlink_archive)),
            password="synthetic-archive-password",
        )
    )

    assert response.status == UnlockerStatus.REJECTED
    assert response.outputs == ()
