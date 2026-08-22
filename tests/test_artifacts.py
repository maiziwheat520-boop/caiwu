from __future__ import annotations

import io
import os
import time
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ledgerbridge.artifacts import (
    ArtifactIntegrityError,
    ArtifactPublishedQuotaError,
    ArtifactQuotaStateError,
    ArtifactStagingQuotaError,
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


def _process_publish(
    root: str,
    content: bytes,
    start: Any,
    results: Any,
) -> None:
    store = ArtifactStore(
        Path(root),
        max_bytes=100,
        total_max_bytes=8,
        staging_max_bytes=100,
    )
    start.wait()
    try:
        results.put("created" if store.publish(io.BytesIO(content)).created else "duplicate")
    except ArtifactPublishedQuotaError:
        results.put("rejected")


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
    root = tmp_path / "store"
    store = ArtifactStore(root.resolve(), max_bytes=1_000)
    content = b"symlink evidence"
    digest = __import__("hashlib").sha256(content).digest()
    destination = root / storage_key_for_digest(digest)
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
    with pytest.raises(ArtifactQuotaStateError, match="symlink"):
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


def test_open_verified_keeps_the_verified_inode_when_the_path_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("Windows does not allow replacing an open file in this test")

    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000)
    original = b"AAAAAAAAAA"
    replacement = b"BBBBBBBBBB"
    artifact = store.publish(io.BytesIO(original))
    destination = tmp_path / artifact.storage_key
    replacement_path = tmp_path / "replacement"
    replacement_path.write_bytes(replacement)
    original_verify = store._verify_descriptor

    def verify_then_replace(descriptor: int, digest: bytes, byte_size: int) -> None:
        original_verify(descriptor, digest, byte_size)
        os.replace(replacement_path, destination)

    monkeypatch.setattr(store, "_verify_descriptor", verify_then_replace)
    with store.open_verified(artifact) as stream:
        assert stream.read() == original
    assert destination.read_bytes() == replacement


def test_open_verified_rejects_a_symlink_swap_at_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("O_NOFOLLOW is unavailable on Windows")

    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000)
    content = b"same bytes behind symlink"
    artifact = store.publish(io.BytesIO(content))
    destination = tmp_path / artifact.storage_key
    outside = tmp_path / "outside-same-bytes"
    outside.write_bytes(content)
    real_open = os.open
    armed = True

    def racing_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
        nonlocal armed
        if armed and Path(path) == destination:
            armed = False
            destination.unlink()
            destination.symlink_to(outside)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", racing_open)
    with (
        pytest.raises(ArtifactIntegrityError, match="regular file"),
        store.open_verified(artifact),
    ):
        pass
    assert outside.read_bytes() == content


def test_directory_fsync_uses_the_operating_system_primitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("directory fsync is a POSIX durability primitive")

    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", synced.append)

    ArtifactStore._fsync_directory(tmp_path)

    assert len(synced) == 1


def test_existing_artifact_directories_are_tightened_to_owner_only(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX directory modes are unavailable on Windows")

    root = tmp_path / "store"
    root.mkdir(mode=0o755)
    store = ArtifactStore(root.resolve(), max_bytes=1_000)

    store.publish(io.BytesIO(b"private directory"))

    assert root.stat().st_mode & 0o777 == 0o700


def test_connector_stream_exposes_read_only_bytes_without_a_path(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=1_000)
    artifact = store.publish(io.BytesIO(b"read-only"))

    with store.open_verified(artifact) as stream:
        assert stream.read(4) == b"read"
        assert stream.read() == b"-only"
        assert not hasattr(stream, "write")
        assert not hasattr(stream, "name")
        assert not hasattr(stream, "fileno")
        assert not hasattr(stream, "_ReadOnlyArtifactStream__stream")
    with pytest.raises(ValueError, match="closed"):
        stream.read()


def test_published_quota_rejects_new_bytes_but_allows_deduplication(tmp_path: Path) -> None:
    store = ArtifactStore(
        tmp_path.resolve(),
        max_bytes=100,
        total_max_bytes=4,
        staging_max_bytes=100,
    )
    first = store.publish(io.BytesIO(b"1234"))

    duplicate = store.publish(io.BytesIO(b"1234"))
    assert duplicate.sha256 == first.sha256
    assert not duplicate.created

    with pytest.raises(ArtifactPublishedQuotaError) as rejected:
        store.publish(io.BytesIO(b"5"))
    assert rejected.value.limit == 4
    assert rejected.value.observed == 4
    assert rejected.value.requested == 1


def test_staging_quota_counts_existing_partial_bytes(tmp_path: Path) -> None:
    staging = tmp_path / ".staging"
    staging.mkdir()
    (staging / "artifact-existing").write_bytes(b"1234")
    store = ArtifactStore(
        tmp_path.resolve(),
        max_bytes=100,
        staging_max_bytes=5,
        staging_ttl_seconds=60,
    )

    with pytest.raises(ArtifactStagingQuotaError) as rejected:
        store.publish(io.BytesIO(b"12"))
    assert rejected.value.observed == 4
    assert rejected.value.requested == 2
    assert sorted(path.name for path in staging.iterdir()) == ["artifact-existing"]


def test_existing_staging_usage_above_limit_is_rejected_before_writing(tmp_path: Path) -> None:
    staging = tmp_path / ".staging"
    staging.mkdir()
    (staging / "artifact-existing").write_bytes(b"12")
    store = ArtifactStore(
        tmp_path.resolve(),
        max_bytes=100,
        staging_max_bytes=1,
        staging_ttl_seconds=60,
    )

    with pytest.raises(ArtifactStagingQuotaError) as rejected:
        store.publish(io.BytesIO(b""))

    assert rejected.value.observed == 2
    assert rejected.value.requested == 0


def test_stale_staging_files_are_removed_before_admission(tmp_path: Path) -> None:
    staging = tmp_path / ".staging"
    staging.mkdir()
    stale = staging / "artifact-stale"
    stale.write_bytes(b"stale")
    old = time.time() - 120
    os.utime(stale, (old, old))
    store = ArtifactStore(
        tmp_path.resolve(),
        max_bytes=100,
        staging_max_bytes=5,
        staging_ttl_seconds=60,
    )

    artifact = store.publish(io.BytesIO(b"fresh"))

    assert artifact.created
    assert not stale.exists()


def test_quota_snapshot_counts_orphans_and_rejects_unknown_state(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=100)
    artifact = store.publish(io.BytesIO(b"tracked"))
    orphan_content = b"orphan"
    orphan_digest = __import__("hashlib").sha256(orphan_content).digest()
    orphan = tmp_path / storage_key_for_digest(orphan_digest)
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(orphan_content)

    snapshot = store.quota_snapshot()
    assert snapshot.published_bytes == artifact.byte_size + len(orphan_content)
    assert snapshot.staging_bytes == 0

    (tmp_path / "unexpected").write_bytes(b"ambiguous")
    with pytest.raises(ArtifactQuotaStateError, match="unexpected entry"):
        store.quota_snapshot()


def test_parallel_publishes_cannot_oversubscribe_total_quota(tmp_path: Path) -> None:
    store = ArtifactStore(
        tmp_path.resolve(),
        max_bytes=100,
        total_max_bytes=8,
        staging_max_bytes=100,
    )

    def publish(content: bytes) -> str:
        try:
            return "created" if store.publish(io.BytesIO(content)).created else "duplicate"
        except ArtifactPublishedQuotaError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, (b"aaaaaaaa", b"bbbbbbbb")))

    assert sorted(results) == ["created", "rejected"]
    assert store.quota_snapshot().published_bytes == 8


def test_cross_process_lock_prevents_total_quota_oversubscription(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX process-lock behavior is exercised on Linux CI")
    context = get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_publish,
            args=(str(tmp_path.resolve()), content, start, results),
        )
        for content in (b"aaaaaaaa", b"bbbbbbbb")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sorted([results.get(timeout=2), results.get(timeout=2)]) == [
        "created",
        "rejected",
    ]
    assert (
        ArtifactStore(
            tmp_path.resolve(),
            max_bytes=100,
            total_max_bytes=8,
            staging_max_bytes=100,
        )
        .quota_snapshot()
        .published_bytes
        == 8
    )


def test_quota_tree_rejects_unexpected_directories_files_and_root_types(
    tmp_path: Path,
) -> None:
    nested_root = tmp_path / "nested"
    (nested_root / ".staging" / "directory").mkdir(parents=True)
    with pytest.raises(ArtifactQuotaStateError, match="unexpected directory"):
        ArtifactStore(nested_root.resolve(), max_bytes=100).quota_snapshot()

    unknown_root = tmp_path / "unknown"
    (unknown_root / ".staging").mkdir(parents=True)
    (unknown_root / ".staging" / "not-a-partial").write_bytes(b"x")
    with pytest.raises(ArtifactQuotaStateError, match="unexpected entry"):
        ArtifactStore(unknown_root.resolve(), max_bytes=100).quota_snapshot()

    malformed_root = tmp_path / "malformed"
    (malformed_root / ".staging").mkdir(parents=True)
    malformed_blob = malformed_root / "sha256" / "zz" / "zz" / ("z" * 64)
    malformed_blob.parent.mkdir(parents=True)
    malformed_blob.write_bytes(b"x")
    with pytest.raises(ArtifactQuotaStateError, match="unexpected entry"):
        ArtifactStore(malformed_root.resolve(), max_bytes=100).quota_snapshot()

    wrong_type_root = tmp_path / "wrong-type"
    wrong_type_root.mkdir()
    (wrong_type_root / ".staging").write_bytes(b"not a directory")
    with pytest.raises(ArtifactQuotaStateError, match="unexpected type"):
        ArtifactStore(wrong_type_root.resolve(), max_bytes=100)._validate_root_entries()


def test_low_level_quota_and_path_guards_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "guards").resolve()
    root.mkdir()
    store = ArtifactStore(root, max_bytes=100)
    blocked_directory = root / "blocked"
    blocked_directory.write_bytes(b"file")
    with pytest.raises(ArtifactIntegrityError, match="real directories"):
        store._ensure_private_directory(blocked_directory)

    read_descriptor, write_descriptor = os.pipe()
    try:
        with pytest.raises(ArtifactIntegrityError, match="regular file"):
            store._verify_descriptor(read_descriptor, b"0" * 32, 0)
    finally:
        os.close(read_descriptor)
        os.close(write_descriptor)

    missing = root / "missing"
    with pytest.raises(ArtifactQuotaStateError, match="unreadable"):
        store._open_quota_regular(missing)

    real_storage_key = storage_key_for_digest
    monkeypatch.setattr("ledgerbridge.artifacts.storage_key_for_digest", lambda _digest: "../x")
    with pytest.raises(ArtifactIntegrityError, match="escaped"):
        store._destination(b"0" * 32)
    monkeypatch.setattr("ledgerbridge.artifacts.storage_key_for_digest", real_storage_key)


def test_quota_lock_rejects_symlink_or_nonregular_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "lock").resolve()
    root.mkdir()
    store = ArtifactStore(root, max_bytes=100)
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"x")
    try:
        (root / ".quota.lock").symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable in this test environment")
    with pytest.raises(ArtifactQuotaStateError, match="lock is unavailable"):
        store.quota_snapshot()
    (root / ".quota.lock").unlink()

    real_open = os.open
    pipe_descriptors: list[int] = []

    def open_pipe_for_lock(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
        if Path(path) == root / ".quota.lock":
            read_descriptor, write_descriptor = os.pipe()
            pipe_descriptors.append(write_descriptor)
            return read_descriptor
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", open_pipe_for_lock)
    with pytest.raises(ArtifactQuotaStateError, match="regular file"), store._quota_lock():
        pass
    for descriptor in pipe_descriptors:
        os.close(descriptor)


def test_link_race_converges_on_the_verified_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=100)
    real_link = os.link

    def link_then_report_race(
        source: os.PathLike[str] | str, target: os.PathLike[str] | str
    ) -> None:
        real_link(source, target)
        raise FileExistsError

    monkeypatch.setattr(os, "link", link_then_report_race)
    artifact = store.publish(io.BytesIO(b"race"))

    assert not artifact.created
    assert (tmp_path / artifact.storage_key).read_bytes() == b"race"


def test_quota_scanner_rejects_missing_non_directory_and_nonregular_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ArtifactStore(tmp_path.resolve(), max_bytes=100)
    with pytest.raises(ArtifactQuotaStateError, match="directory is unreadable"):
        store._scan_tree(tmp_path / "missing", published=False, clean_stale=False)

    not_directory = tmp_path / "not-directory"
    not_directory.write_bytes(b"x")
    with pytest.raises(ArtifactQuotaStateError, match="real directory"):
        store._scan_tree(not_directory, published=False, clean_stale=False)
    assert not store._valid_published_relative(Path("bad"))

    real_open = os.open
    write_descriptors: list[int] = []

    def open_pipe(_path: os.PathLike[str] | str, _flags: int) -> int:
        read_descriptor, write_descriptor = os.pipe()
        write_descriptors.append(write_descriptor)
        return read_descriptor

    monkeypatch.setattr(os, "open", open_pipe)
    with pytest.raises(ArtifactQuotaStateError, match="regular file"):
        store._open_quota_regular(tmp_path / "ignored")
    monkeypatch.setattr(os, "open", real_open)
    for descriptor in write_descriptors:
        os.close(descriptor)


def test_quota_scanner_rejects_scan_errors_symlinks_devices_and_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "store").resolve()
    staging = root / ".staging"
    staging.mkdir(parents=True)
    store = ArtifactStore(root, max_bytes=100)
    real_scandir = os.scandir

    def fail_staging_scan(path: os.PathLike[str] | str) -> Any:
        if Path(path) == staging:
            raise OSError("synthetic scan failure")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fail_staging_scan)
    with pytest.raises(ArtifactQuotaStateError, match="directory is unreadable"):
        store._scan_tree(staging, published=False, clean_stale=False)
    monkeypatch.setattr(os, "scandir", real_scandir)

    def fail_root_scan(path: os.PathLike[str] | str) -> Any:
        if Path(path) == root:
            raise OSError("synthetic root scan failure")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fail_root_scan)
    with pytest.raises(ArtifactQuotaStateError, match="root is unreadable"):
        store._validate_root_entries()
    monkeypatch.setattr(os, "scandir", real_scandir)

    outside = tmp_path / "outside"
    outside.write_bytes(b"x")
    try:
        (staging / "artifact-link").symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable in this test environment")
    with pytest.raises(ArtifactQuotaStateError, match="symlinks"):
        store.quota_snapshot()
    (staging / "artifact-link").unlink()

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    fifo = staging / "artifact-fifo"
    os.mkfifo(fifo)
    with pytest.raises(ArtifactQuotaStateError, match="regular files only"):
        store.quota_snapshot()
    fifo.unlink()

    expected = staging / "artifact-expected"
    expected.write_bytes(b"x")
    alternate = tmp_path / "alternate"
    alternate.write_bytes(b"different-size")
    monkeypatch.setattr(store, "_open_quota_regular", lambda _path: os.open(alternate, os.O_RDONLY))
    with pytest.raises(ArtifactQuotaStateError, match="changed during measurement"):
        store.quota_snapshot()
