from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from scripts import storage_encryption_preflight
from scripts.storage_encryption_preflight import SNAPSHOT_VERSION, evaluate_snapshot

HOST = "synthetic-host-01"
BOOT = "11111111-2222-3333-4444-555555555555"
REVISION = "a" * 40
NOW = datetime(2026, 8, 24, 8, 2, tzinfo=UTC)
DATA_MAPPER = "/dev/mapper/synthetic-data"
DATA_BACKING = "/dev/synthetic-disk1"
SWAP_MAPPER = "/dev/mapper/synthetic-swap"
SWAP_BACKING = "/dev/synthetic-disk2"


def _observation(
    path: str,
    *,
    target: str,
    source: str,
    major_minor: str,
    filesystem_uuid: str,
) -> dict[str, object]:
    return {
        "requested_path": path,
        "canonical_path": path,
        "target": target,
        "source": source,
        "major_minor": major_minor,
        "filesystem_uuid": filesystem_uuid,
        "fstype": "ext4",
        "is_symlink": False,
    }


def _snapshot() -> dict[str, object]:
    paths = {
        "/srv/ledgerbridge/artifacts": ("/srv/ledgerbridge", DATA_MAPPER, "253:0", "data-fs"),
        "/srv/ledgerbridge/postgres": ("/srv/ledgerbridge", DATA_MAPPER, "253:0", "data-fs"),
        "/srv/ledgerbridge/postgres/wal": (
            "/srv/ledgerbridge",
            DATA_MAPPER,
            "253:0",
            "data-fs",
        ),
        "/srv/ledgerbridge/postgres/tmp": (
            "/srv/ledgerbridge",
            DATA_MAPPER,
            "253:0",
            "data-fs",
        ),
        "/srv/ledgerbridge/postgres/tablespace-one": (
            "/srv/ledgerbridge",
            DATA_MAPPER,
            "253:0",
            "data-fs",
        ),
        "/opt/ledgerbridge": ("/", "/dev/root", "8:1", "root-fs"),
        "/mnt/ledgerbridge-backups": (
            "/mnt/ledgerbridge-backups",
            "/dev/backup",
            "8:17",
            "backup-fs",
        ),
        "/mnt/key-custody/ledgerbridge": (
            "/mnt/key-custody",
            "/dev/custody",
            "8:33",
            "custody-fs",
        ),
    }
    findmnt = [
        _observation(
            path,
            target=details[0],
            source=details[1],
            major_minor=details[2],
            filesystem_uuid=details[3],
        )
        for path, details in paths.items()
    ]
    return {
        "schema_version": SNAPSHOT_VERSION,
        "collected_at": "2026-08-24T08:00:00Z",
        "expires_at": "2026-08-24T08:05:00Z",
        "host_id": HOST,
        "boot_id": BOOT,
        "revision": REVISION,
        "approved_backings": [
            {
                "mapper": DATA_MAPPER,
                "backing_device": DATA_BACKING,
                "luks_uuid": "synthetic-luks-data",
            },
            {
                "mapper": SWAP_MAPPER,
                "backing_device": SWAP_BACKING,
                "luks_uuid": "synthetic-luks-swap",
            },
        ],
        "volume_inspect": {
            "name": "ledgerbridge_artifacts",
            "driver": "local",
            "mountpoint": "/srv/ledgerbridge/artifacts",
        },
        "postgres": {
            "query_succeeded": True,
            "pgdata": "/srv/ledgerbridge/postgres",
            "pg_wal": "/srv/ledgerbridge/postgres/wal",
            "temp_directories": ["/srv/ledgerbridge/postgres/tmp"],
            "temp_directories_complete": True,
            "tablespaces": ["/srv/ledgerbridge/postgres/tablespace-one"],
            "tablespaces_complete": True,
        },
        "findmnt": findmnt,
        "lsblk": [
            {
                "path": DATA_MAPPER,
                "major_minor": "253:0",
                "type": "crypt",
                "pkname": DATA_BACKING,
            },
            {
                "path": DATA_BACKING,
                "major_minor": "8:1",
                "type": "part",
                "pkname": "/dev/synthetic-disk",
            },
            {
                "path": SWAP_MAPPER,
                "major_minor": "253:1",
                "type": "crypt",
                "pkname": SWAP_BACKING,
            },
            {
                "path": SWAP_BACKING,
                "major_minor": "8:2",
                "type": "part",
                "pkname": "/dev/synthetic-disk",
            },
        ],
        "crypt": [
            {
                "mapper": DATA_MAPPER,
                "backing_device": DATA_BACKING,
                "luks_uuid": "synthetic-luks-data",
                "luks_version": "LUKS2",
                "active": True,
            },
            {
                "mapper": SWAP_MAPPER,
                "backing_device": SWAP_BACKING,
                "luks_uuid": "synthetic-luks-swap",
                "luks_version": "LUKS2",
                "active": True,
            },
        ],
        "swap": {"query_succeeded": True, "entries": []},
        "core": {
            "query_succeeded": True,
            "rlimit_core_soft": 0,
            "rlimit_core_hard": 0,
            "storage": "none",
            "suid_dumpable": 0,
        },
        "key_custody": {"paths": ["/mnt/key-custody/ledgerbridge"], "complete": True},
        "deployment": {"paths": ["/opt/ledgerbridge"], "complete": True},
        "backup": {"paths": ["/mnt/ledgerbridge-backups"], "complete": True},
    }


def _evaluate(snapshot: object) -> dict[str, object]:
    return evaluate_snapshot(
        snapshot,
        expected_host_id=HOST,
        expected_boot_id=BOOT,
        expected_revision=REVISION,
        now=NOW,
    )


def _reasons(result: dict[str, object]) -> list[str]:
    return cast(list[str], result["reason_codes"])


def test_valid_disabled_swap_snapshot_passes_without_echoing_proof() -> None:
    snapshot = _snapshot()

    result = _evaluate(snapshot)

    assert result["verdict"] == "PASS"
    assert result["reason_codes"] == []
    encoded = json.dumps(result)
    assert "approved_backings" not in encoded
    assert "luks_uuid" not in encoded
    assert "/srv/ledgerbridge" not in encoded


def test_valid_encrypted_swap_passes() -> None:
    snapshot = _snapshot()
    cast(dict[str, object], snapshot["swap"])["entries"] = [
        {"source": SWAP_MAPPER, "mapper": SWAP_MAPPER, "encrypted": True}
    ]

    assert _evaluate(snapshot)["verdict"] == "PASS"


@pytest.mark.parametrize("field", ["host_id", "boot_id", "revision"])
def test_binding_mismatch_fails(field: str) -> None:
    snapshot = _snapshot()
    snapshot[field] = "b" * 40 if field == "revision" else "different"

    result = _evaluate(snapshot)

    assert result["verdict"] == "FAIL"
    assert f"{field}_mismatch" in _reasons(result)


@pytest.mark.parametrize(
    ("collected", "expires"),
    [
        ("2026-08-24T07:50:00Z", "2026-08-24T07:55:00Z"),
        ("2026-08-24T08:03:00Z", "2026-08-24T08:05:00Z"),
        ("2026-08-24T08:00:00", "2026-08-24T08:05:00Z"),
        ("2026-08-24T08:00:00Z", "2026-08-24T08:06:00Z"),
    ],
)
def test_stale_future_naive_or_overlong_proof_fails(collected: str, expires: str) -> None:
    snapshot = _snapshot()
    snapshot["collected_at"] = collected
    snapshot["expires_at"] = expires

    result = _evaluate(snapshot)

    assert result["verdict"] == "FAIL"
    assert any(reason.startswith("snapshot_") for reason in _reasons(result))


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("unapproved", "source_not_approved"),
        ("device", "device_crypt_mismatch"),
        ("crypt", "device_crypt_mismatch"),
        ("backing", "backing_device_invalid"),
    ],
)
def test_storage_device_chain_mismatch_fails(mutation: str, reason: str) -> None:
    snapshot = _snapshot()
    observations = cast(list[dict[str, object]], snapshot["findmnt"])
    if mutation == "unapproved":
        observations[0]["source"] = "/dev/mapper/unapproved"
    elif mutation == "device":
        cast(list[dict[str, object]], snapshot["lsblk"])[0]["major_minor"] = "253:99"
    elif mutation == "crypt":
        cast(list[dict[str, object]], snapshot["crypt"])[0]["luks_uuid"] = "wrong"
    else:
        cast(list[dict[str, object]], snapshot["lsblk"])[1]["type"] = "loop"

    result = _evaluate(snapshot)

    assert result["verdict"] == "FAIL"
    assert reason in _reasons(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("canonical_path", "/different"),
        ("is_symlink", True),
        ("source", "unknown"),
        ("target", "/unrelated"),
    ],
)
def test_path_symlink_unknown_and_mount_mismatch_fail(field: str, value: object) -> None:
    snapshot = _snapshot()
    cast(list[dict[str, object]], snapshot["findmnt"])[0][field] = value

    result = _evaluate(snapshot)

    assert result["verdict"] == "FAIL"
    assert cast(dict[str, bool], result["checks"])["protected_storage"] is False


@pytest.mark.parametrize("inventory", ["temp_directories", "tablespaces"])
def test_postgres_inventory_must_be_complete(inventory: str) -> None:
    snapshot = _snapshot()
    cast(dict[str, object], snapshot["postgres"])[f"{inventory}_complete"] = False

    result = _evaluate(snapshot)

    assert result["verdict"] == "FAIL"
    assert f"{inventory}_inventory_incomplete" in _reasons(result)


def test_every_declared_postgres_path_requires_observation() -> None:
    snapshot = _snapshot()
    cast(list[str], cast(dict[str, object], snapshot["postgres"])["tablespaces"]).append(
        "/srv/ledgerbridge/postgres/missing"
    )

    result = _evaluate(snapshot)

    assert result["verdict"] == "FAIL"
    assert "path_observation_missing" in _reasons(result)


@pytest.mark.parametrize(
    "swap",
    [
        {"query_succeeded": False, "entries": []},
        {
            "query_succeeded": True,
            "entries": [{"source": "/swapfile", "mapper": DATA_MAPPER, "encrypted": False}],
        },
        {
            "query_succeeded": True,
            "entries": [
                {"source": "/dev/dm-9", "mapper": "/dev/mapper/unknown", "encrypted": True}
            ],
        },
    ],
)
def test_swap_unknown_or_unencrypted_fails(swap: dict[str, object]) -> None:
    snapshot = _snapshot()
    snapshot["swap"] = swap

    result = _evaluate(snapshot)

    assert result["verdict"] == "FAIL"
    assert cast(dict[str, bool], result["checks"])["swap"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("query_succeeded", False),
        ("rlimit_core_soft", 1),
        ("rlimit_core_hard", -1),
        ("storage", "external"),
        ("suid_dumpable", 1),
    ],
)
def test_core_dump_must_be_completely_disabled(field: str, value: object) -> None:
    snapshot = _snapshot()
    cast(dict[str, object], snapshot["core"])[field] = value

    result = _evaluate(snapshot)

    assert result["verdict"] == "FAIL"
    assert "core_dump_not_disabled" in _reasons(result)


@pytest.mark.parametrize("shared_path", ["/opt/ledgerbridge", "/mnt/ledgerbridge-backups"])
def test_key_custody_cannot_share_deployment_or_backup_volume(shared_path: str) -> None:
    snapshot = _snapshot()
    observations = cast(list[dict[str, object]], snapshot["findmnt"])
    source = next(item for item in observations if item["requested_path"] == shared_path)
    custody = next(
        item for item in observations if item["requested_path"] == "/mnt/key-custody/ledgerbridge"
    )
    custody["source"] = source["source"]
    custody["major_minor"] = source["major_minor"]
    custody["filesystem_uuid"] = source["filesystem_uuid"]

    result = _evaluate(snapshot)

    assert result["verdict"] == "FAIL"
    assert "key_custody_volume_not_separate" in _reasons(result)


def test_key_custody_cannot_share_data_volume() -> None:
    snapshot = _snapshot()
    observations = cast(list[dict[str, object]], snapshot["findmnt"])
    custody = next(
        item for item in observations if item["requested_path"] == "/mnt/key-custody/ledgerbridge"
    )
    custody.update({"source": DATA_MAPPER, "major_minor": "253:0", "filesystem_uuid": "data-fs"})

    assert "key_custody_volume_not_separate" in _reasons(_evaluate(snapshot))


def test_unknown_fields_and_secret_material_fail_without_echo() -> None:
    snapshot = _snapshot()
    snapshot["key_material"] = "synthetic-do-not-echo"

    result = _evaluate(snapshot)

    assert result["verdict"] == "FAIL"
    assert "snapshot_fields_invalid" in _reasons(result)
    assert "secret_material_forbidden" in _reasons(result)
    assert "synthetic-do-not-echo" not in json.dumps(result)


@pytest.mark.parametrize("snapshot", [None, [], {"schema_version": SNAPSHOT_VERSION}])
def test_malformed_or_incomplete_snapshot_fails_closed(snapshot: object) -> None:
    result = _evaluate(snapshot)

    assert result["verdict"] == "FAIL"
    assert result["reason_codes"]


def test_cli_prints_json_and_uses_pass_fail_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(_snapshot()), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "storage_encryption_preflight",
            "--snapshot",
            str(path),
            "--expected-host-id",
            HOST,
            "--expected-boot-id",
            BOOT,
            "--expected-revision",
            REVISION,
        ],
    )
    monkeypatch.setattr(storage_encryption_preflight, "datetime", _FixedDateTime)

    with pytest.raises(SystemExit, match="0"):
        storage_encryption_preflight.main()
    assert json.loads(capsys.readouterr().out)["verdict"] == "PASS"

    invalid = deepcopy(_snapshot())
    invalid["host_id"] = "wrong"
    path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(SystemExit, match="2"):
        storage_encryption_preflight.main()
    assert json.loads(capsys.readouterr().out)["verdict"] == "FAIL"


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz: object = None) -> _FixedDateTime:
        del tz
        return cls(2026, 8, 24, 8, 2, tzinfo=UTC)
