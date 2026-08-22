from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ledgerbridge.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactTooLargeError,
    PublishedArtifact,
    storage_key_for_digest,
)


class ChunkedStream:
    def __init__(self, content: bytes, boundaries: list[int]) -> None:
        self._content = content
        self._boundaries = iter(boundaries)
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._content):
            return b""
        requested = next(self._boundaries, size)
        count = min(max(requested, 1), size, len(self._content) - self._offset)
        result = self._content[self._offset : self._offset + count]
        self._offset += count
        return result


class FailingStream:
    def __init__(self) -> None:
        self._reads = 0

    def read(self, _size: int = -1) -> bytes:
        self._reads += 1
        if self._reads == 1:
            return b"partial"
        raise OSError("synthetic read failure with secret-looking row 12345")


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    content=st.binary(max_size=8_192),
    boundaries=st.lists(st.integers(min_value=1, max_value=257), max_size=80),
)
def test_chunk_boundaries_do_not_change_content_identity(
    tmp_path: Path,
    content: bytes,
    boundaries: list[int],
) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=10_000, chunk_size=509)
    first = store.publish(ChunkedStream(content, boundaries))
    second = store.publish(io.BytesIO(content))

    assert first.sha256 == second.sha256
    assert first.storage_key == second.storage_key

    assert not second.created
    assert len(list((tmp_path / "sha256").rglob(first.sha256_hex))) == 1


def test_storage_key_is_digest_only_and_rejects_wrong_length(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000)
    artifact = store.publish(io.BytesIO(b"same bytes"))

    assert artifact.storage_key == storage_key_for_digest(artifact.sha256)
    assert (
        artifact.storage_key
        == f"sha256/{artifact.sha256_hex[:2]}/{artifact.sha256_hex[2:4]}/{artifact.sha256_hex}"
    )
    assert "statement.csv" not in artifact.storage_key
    with pytest.raises(ValueError, match="32 bytes"):
        storage_key_for_digest(b"short")


def test_concurrent_identical_publication_converges(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000_000, chunk_size=113)
    content = b"synthetic evidence" * 2_000

    def publish() -> tuple[bytes, bool]:
        result = store.publish(io.BytesIO(content))
        return result.sha256, result.created

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _index: publish(), range(16)))

    assert len({digest for digest, _created in results}) == 1
    assert sum(created for _digest, created in results) == 1


def test_new_directory_entries_are_fsynced_through_the_shard_hierarchy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced: list[Path] = []
    root = (tmp_path / "store").resolve()
    store = ArtifactStore(root, max_bytes=1_000)
    monkeypatch.setattr(store, "_fsync_directory", synced.append)

    artifact = store.publish(io.BytesIO(b"durable hierarchy"))
    digest = artifact.sha256_hex

    assert synced == [
        tmp_path,
        root,
        root,
        root / "sha256",
        root / "sha256" / digest[:2],
        root / "sha256" / digest[:2] / digest[2:4],
    ]


def test_existing_destination_mismatch_fails_without_overwrite(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000)
    content = b"correct evidence"
    digest = __import__("hashlib").sha256(content).digest()
    destination = tmp_path / storage_key_for_digest(digest)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"wrong---evidence")
    original = destination.read_bytes()

    with pytest.raises(ArtifactIntegrityError, match="digest mismatch"):
        store.publish(io.BytesIO(content))
    assert destination.read_bytes() == original


def test_symlink_destination_is_rejected(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000)
    content = b"symlink evidence"
    digest = __import__("hashlib").sha256(content).digest()
    destination = tmp_path / storage_key_for_digest(digest)
    destination.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(content)
    try:
        destination.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable in this test environment")

    with pytest.raises(ArtifactIntegrityError, match="regular file"):
        store.publish(io.BytesIO(content))
    assert outside.read_bytes() == content


def test_intermediate_symlink_fails_before_creating_outside_directories(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "sha256").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable in this test environment")

    store = ArtifactStore(root.resolve(), max_bytes=1_000)
    with pytest.raises(ArtifactIntegrityError, match="real directories"):
        store.publish(io.BytesIO(b"must not escape"))
    assert not list(outside.iterdir())
    assert not (outside / ".staging").exists()


def test_stream_and_fsync_failures_leave_no_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000)
    with pytest.raises(OSError, match="synthetic read failure"):
        store.publish(FailingStream())
    assert not list((tmp_path / ".staging").iterdir())

    monkeypatch.setattr(os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError("fsync")))
    with pytest.raises(OSError, match="fsync"):
        store.publish(io.BytesIO(b"fsync failure"))
    assert not list((tmp_path / ".staging").iterdir())
    assert not list((tmp_path / "sha256").rglob("*")) if (tmp_path / "sha256").exists() else True


def test_size_limit_and_non_bytes_stream_fail_closed(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=4)
    with pytest.raises(ArtifactTooLargeError):
        store.publish(io.BytesIO(b"12345"))
    with pytest.raises(ArtifactStoreError, match="return bytes"):
        store.publish(io.StringIO("text"))  # type: ignore[arg-type]
    assert not list((tmp_path / ".staging").iterdir())


def test_invalid_store_configuration_and_verification_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        ArtifactStore(Path("relative"), max_bytes=1)
    with pytest.raises(ValueError, match="positive"):
        ArtifactStore(tmp_path.resolve(), max_bytes=0)
    with pytest.raises(ValueError, match="positive"):
        ArtifactStore(tmp_path.resolve(), max_bytes=1, chunk_size=0)

    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000)
    artifact = store.publish(io.BytesIO(b"verify paths"))
    with pytest.raises(ValueError, match="non-negative"):
        store.read_prefix(artifact, -1)
    with pytest.raises(ArtifactIntegrityError, match="size mismatch"):
        store.read_prefix(
            PublishedArtifact(
                sha256=artifact.sha256,
                byte_size=artifact.byte_size + 1,
                storage_key=artifact.storage_key,
                created=False,
            ),
            1,
        )
    destination = tmp_path / artifact.storage_key
    destination.chmod(0o600)
    destination.unlink()
    with pytest.raises(ArtifactIntegrityError, match="missing"):
        store.open_verified(artifact).__enter__()
    with pytest.raises(ArtifactIntegrityError, match="escaped"):
        store._ensure_private_directory(tmp_path.parent)


def test_connector_stream_exposes_read_only_bytes_without_a_path(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000)
    artifact = store.publish(io.BytesIO(b"read-only"))

    with store.open_verified(artifact) as stream:
        assert stream.read() == b"read-only"
        assert not hasattr(stream, "write")
        assert not hasattr(stream, "name")
        assert not hasattr(stream, "fileno")
