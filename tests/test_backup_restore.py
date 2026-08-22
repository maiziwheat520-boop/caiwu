from __future__ import annotations

import io
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from scripts.backup_restore import (
    BACKUP_FORMAT_V1,
    BACKUP_FORMAT_V2,
    BackupError,
    CommonConfig,
    RestoreResources,
    Runner,
    SourceState,
    _artifact_archive_metadata,
    _assert_source_unchanged,
    _normalize_fingerprint,
    _replace_database_host,
    _safe_extract_tar,
    _validate_restored_database,
    _verify_payload_hashes,
    _write_payload_hashes,
    create_backup,
)

FINGERPRINT = "0123456789ABCDEF0123456789ABCDEF01234567"


def _source_state() -> SourceState:
    return SourceState(
        revision="a" * 40,
        postgres_container="postgres-id",
        api_container="api-id",
        worker_container="worker-id",
        api_image="ledgerbridge-app:abcdef0",
        artifact_volume="ledgerbridge_artifacts",
        database={"alembic_version": "20260821_0002"},
    )


def _database_metadata() -> dict[str, object]:
    return {
        "database_name": "ledgerbridge",
        "database_owner": "ledgerbridge",
        "alembic_version": "20260821_0002",
        "data_checksums": "on",
        "role_grant_count": 10,
        "runtime_role_valid": True,
        "audit_select_only": True,
        "schema_create_denied": True,
        "function_count": 2,
        "trigger_count": 5,
        "row_counts": {
            "entity": 0,
            "account": 0,
            "journal_entry": 0,
            "posting": 0,
            "audit_event": 0,
        },
    }


def test_fingerprint_normalization_and_validation() -> None:
    spaced = "0123 4567 89ab cdef 0123 4567 89ab cdef 0123 4567"
    assert _normalize_fingerprint(spaced) == FINGERPRINT

    with pytest.raises(BackupError, match="fingerprint"):
        _normalize_fingerprint("short")


def test_database_url_host_replacement_preserves_credentials_and_port() -> None:
    source = "postgresql+psycopg://ledgerbridge_app:p%40ss@postgres:5432/ledgerbridge"

    replaced = _replace_database_host(source, "ledgerbridge-restore-postgres-deadbeef")

    assert replaced == (
        "postgresql+psycopg://ledgerbridge_app:p%40ss@"
        "ledgerbridge-restore-postgres-deadbeef:5432/ledgerbridge"
    )


def test_restore_resource_names_are_exact_and_guarded() -> None:
    resources = RestoreResources.create("deadbeef")
    assert resources.container == "ledgerbridge-restore-postgres-deadbeef"
    assert resources.network == "ledgerbridge-restore-network-deadbeef"
    assert resources.database_volume == "ledgerbridge_restore_db_deadbeef"
    assert resources.artifact_volume == "ledgerbridge_restore_artifacts_deadbeef"

    with pytest.raises(BackupError, match="eight lowercase hex"):
        RestoreResources.create("../unsafe")


def test_source_state_comparison_detects_production_drift() -> None:
    before = _source_state()
    _assert_source_unchanged(before, before)

    after = replace(before, database={"alembic_version": "unexpected"})
    with pytest.raises(BackupError, match="database metadata"):
        _assert_source_unchanged(before, after)


def test_safe_extract_accepts_only_expected_regular_file(tmp_path: Path) -> None:
    archive = tmp_path / "payload.tar"
    contents = b"verified"
    with tarfile.open(archive, "w:") as bundle:
        member = tarfile.TarInfo("metadata.json")
        member.size = len(contents)
        member.mode = 0o600
        bundle.addfile(member, io.BytesIO(contents))

    destination = tmp_path / "payload"
    _safe_extract_tar(archive, destination, expected_files={"metadata.json"})

    assert (destination / "metadata.json").read_bytes() == contents


@pytest.mark.parametrize(("name", "link"), [("../escape", ""), ("link", "/etc/passwd")])
def test_safe_extract_rejects_traversal_and_symlink(tmp_path: Path, name: str, link: str) -> None:
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w:") as bundle:
        member = tarfile.TarInfo(name)
        if link:
            member.type = tarfile.SYMTYPE
            member.linkname = link
        bundle.addfile(member)

    with pytest.raises(BackupError, match="unsafe"):
        _safe_extract_tar(archive, tmp_path / "extract")


def test_payload_hash_manifest_detects_tampering(tmp_path: Path) -> None:
    for name in (
        "database.dump",
        "roles.sql",
        "artifacts.tar",
        "deployment-tree.tar",
        "metadata.json",
    ):
        (tmp_path / name).write_bytes(name.encode())
    _write_payload_hashes(tmp_path)
    _verify_payload_hashes(tmp_path)

    (tmp_path / "roles.sql").write_text("tampered", encoding="utf-8")
    with pytest.raises(BackupError, match="hash mismatch"):
        _verify_payload_hashes(tmp_path)


def test_restored_database_requires_nonempty_runtime_grants() -> None:
    expected = _database_metadata()
    _validate_restored_database(expected, expected.copy())

    invalid = expected | {"role_grant_count": 0}
    with pytest.raises(BackupError, match="no restored table grants"):
        _validate_restored_database(invalid, invalid.copy())


def test_v1_database_metadata_compares_only_legacy_source_fields() -> None:
    expected = _database_metadata()
    actual = expected | {
        "metadata_version": 2,
        "security_functions": [{"name": "legacy", "proconfig": []}],
    }

    compared = _validate_restored_database(expected, actual)

    assert compared == sorted(expected)
    assert BACKUP_FORMAT_V1 != BACKUP_FORMAT_V2


def test_v2_database_metadata_requires_exact_rich_comparison() -> None:
    expected = _database_metadata() | {"metadata_version": 2}
    actual = expected | {"unexpected": True}

    with pytest.raises(BackupError, match="metadata differs"):
        _validate_restored_database(expected, actual)


def test_artifact_archive_metadata_counts_published_and_staging_bytes(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "artifacts.tar"
    digest = "aabb" + "0" * 60
    with tarfile.open(archive, "w:") as bundle:
        for directory in (".", "./.staging", "./sha256", "./sha256/aa", "./sha256/aa/bb"):
            member = tarfile.TarInfo(directory)
            member.type = tarfile.DIRTYPE
            bundle.addfile(member)
        for name, contents in (
            (f"./sha256/aa/bb/{digest}", b"published"),
            ("./.staging/artifact-partial", b"stage"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(contents)
            bundle.addfile(member, io.BytesIO(contents))
    quota = {
        "per_artifact_max_bytes": 100,
        "published_max_bytes": 100,
        "staging_max_bytes": 100,
        "staging_ttl_seconds": 60,
    }

    observed = _artifact_archive_metadata(archive, quota)

    assert observed == {
        "published_bytes": 9,
        "staging_bytes": 5,
        "unsafe_entries": 0,
        "quota": quota,
    }


@pytest.mark.parametrize("field", ["function_count", "trigger_count"])
def test_restored_database_requires_schema_objects(field: str) -> None:
    invalid = _database_metadata() | {field: 0}

    with pytest.raises(BackupError, match="lacks required objects"):
        _validate_restored_database(invalid, invalid.copy())


class _InterruptingRunner:
    def run(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt


def test_backup_interrupt_restarts_services_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    backup_root = tmp_path / "backups"
    work_root = tmp_path / "work"
    gpg_home = tmp_path / "gpg"
    for directory in (project, backup_root, work_root, gpg_home):
        directory.mkdir()
    config = CommonConfig(
        project_dir=project,
        backup_root=backup_root,
        work_root=work_root,
        gpg_home=gpg_home,
        fingerprint=FINGERPRINT,
    )
    state = _source_state()
    restarts: list[SourceState] = []
    monkeypatch.setattr(
        "scripts.backup_restore._validated_config",
        lambda value, runner: value,
    )
    monkeypatch.setattr(
        "scripts.backup_restore._collect_source_state",
        lambda value, runner: state,
    )
    monkeypatch.setattr(
        "scripts.backup_restore._assert_tree_has_no_symlinks",
        lambda value: None,
    )
    monkeypatch.setattr(
        "scripts.backup_restore._restart_application",
        lambda runner, value: restarts.append(value),
    )

    with pytest.raises(KeyboardInterrupt):
        create_backup(config, cast(Runner, _InterruptingRunner()))

    assert restarts == [state]
    assert list(backup_root.iterdir()) == []
    assert list(work_root.iterdir()) == []
