"""Validate a captured host-storage encryption proof without probing the host.

The caller is responsible for collecting the snapshot.  This module deliberately
contains no subprocess, network, or filesystem inspection code: it only parses JSON
and applies fail-closed consistency checks.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Final

SNAPSHOT_VERSION: Final = "ledgerbridge-storage-encryption-snapshot-v1"
VERDICT_VERSION: Final = "ledgerbridge-storage-encryption-verdict-v1"
DEFAULT_MAX_AGE_SECONDS: Final = 300
REVISION_PATTERN: Final = re.compile(r"[0-9a-f]{40}")
UNKNOWN_VALUES: Final = frozenset({"", "unknown", "n/a", "none", "null", "unset"})
SECRET_FIELD_NAMES: Final = frozenset(
    {"key", "key_material", "passphrase", "recovery_key", "secret", "token"}
)

TOP_LEVEL_FIELDS: Final = frozenset(
    {
        "schema_version",
        "collected_at",
        "expires_at",
        "host_id",
        "boot_id",
        "revision",
        "approved_backings",
        "volume_inspect",
        "postgres",
        "findmnt",
        "lsblk",
        "crypt",
        "swap",
        "core",
        "key_custody",
        "deployment",
        "backup",
    }
)


class _Proof:
    def __init__(self) -> None:
        self.reasons: set[str] = set()
        self.checks: dict[str, bool] = {
            "schema": True,
            "binding": True,
            "freshness": True,
            "protected_storage": True,
            "swap": True,
            "core_dump": True,
            "key_separation": True,
        }

    def fail(self, check: str, reason: str) -> None:
        self.checks[check] = False
        self.reasons.add(reason)


def _object(value: object, proof: _Proof, reason: str) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        proof.fail("schema", reason)
        return None
    return value


def _objects(value: object, proof: _Proof, reason: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        proof.fail("schema", reason)
        return []
    result: list[Mapping[str, object]] = []
    for item in value:
        record = _object(item, proof, reason)
        if record is not None:
            result.append(record)
    return result


def _exact_fields(
    record: Mapping[str, object], expected: set[str] | frozenset[str], proof: _Proof, reason: str
) -> bool:
    if set(record) != set(expected):
        proof.fail("schema", reason)
        return False
    return True


def _known_string(value: object) -> str | None:
    if not isinstance(value, str) or value.strip() != value:
        return None
    if value.casefold() in UNKNOWN_VALUES:
        return None
    return value


def _absolute_path(value: object) -> str | None:
    path = _known_string(value)
    if path is None:
        return None
    pure = PurePosixPath(path)
    if not pure.is_absolute() or ".." in pure.parts or str(pure) != path:
        return None
    return path


def _aware_time(value: object) -> datetime | None:
    text = _known_string(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or key.casefold() in SECRET_FIELD_NAMES:
                return True
            if _contains_secret_field(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_field(item) for item in value)
    return False


def _path_contains(target: str, path: str) -> bool:
    target_parts = PurePosixPath(target).parts
    path_parts = PurePosixPath(path).parts
    return path_parts[: len(target_parts)] == target_parts


def _string_list(
    value: object, proof: _Proof, *, reason: str, allow_empty: bool = False
) -> list[str]:
    if not isinstance(value, list):
        proof.fail("schema", reason)
        return []
    paths: list[str] = []
    for item in value:
        path = _absolute_path(item)
        if path is None:
            proof.fail("schema", reason)
        else:
            paths.append(path)
    if not allow_empty and not paths:
        proof.fail("schema", reason)
    if len(paths) != len(set(paths)):
        proof.fail("schema", reason)
    return paths


def _parse_named_paths(snapshot: Mapping[str, object], name: str, proof: _Proof) -> list[str]:
    section = _object(snapshot.get(name), proof, f"{name}_section_invalid")
    if section is None:
        return []
    if not _exact_fields(section, {"paths", "complete"}, proof, f"{name}_section_invalid"):
        return []
    if section.get("complete") is not True:
        proof.fail("schema", f"{name}_inventory_incomplete")
    return _string_list(section.get("paths"), proof, reason=f"{name}_paths_invalid")


def evaluate_snapshot(
    snapshot: object,
    *,
    expected_host_id: str,
    expected_boot_id: str,
    expected_revision: str,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, object]:
    """Return a stable, non-secret PASS/FAIL verdict for a captured snapshot."""

    proof = _Proof()
    root = _object(snapshot, proof, "snapshot_not_an_object")
    binding = {
        "host_id": expected_host_id,
        "boot_id": expected_boot_id,
        "revision": expected_revision,
    }
    if root is None:
        return _verdict(proof, binding)

    if set(root) != TOP_LEVEL_FIELDS:
        proof.fail("schema", "snapshot_fields_invalid")
    if _contains_secret_field(root):
        proof.fail("schema", "secret_material_forbidden")
    if root.get("schema_version") != SNAPSHOT_VERSION:
        proof.fail("schema", "snapshot_version_invalid")

    expected_values = (expected_host_id, expected_boot_id, expected_revision)
    if any(_known_string(value) is None for value in expected_values):
        proof.fail("binding", "expected_binding_invalid")
    if (
        not isinstance(expected_revision, str)
        or REVISION_PATTERN.fullmatch(expected_revision) is None
    ):
        proof.fail("binding", "expected_revision_invalid")
    for field, expected in binding.items():
        actual = _known_string(root.get(field))
        if actual is None or actual != expected:
            proof.fail("binding", f"{field}_mismatch")

    supplied_now = now or datetime.now(UTC)
    if supplied_now.tzinfo is None or supplied_now.utcoffset() is None:
        proof.fail("freshness", "current_time_invalid")
        current = datetime.min.replace(tzinfo=UTC)
    else:
        current = supplied_now.astimezone(UTC)
    collected = _aware_time(root.get("collected_at"))
    expires = _aware_time(root.get("expires_at"))
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        proof.fail("freshness", "max_age_invalid")
    if collected is None or expires is None:
        proof.fail("freshness", "snapshot_time_invalid")
    elif (
        expires < collected
        or expires - collected > timedelta(seconds=max_age_seconds)
        or current < collected
        or current > expires
    ):
        proof.fail("freshness", "snapshot_expired_or_future")

    approved = _parse_approved(root.get("approved_backings"), proof)
    lsblk = _parse_lsblk(root.get("lsblk"), proof)
    crypt = _parse_crypt(root.get("crypt"), proof)
    observations = _parse_findmnt(root.get("findmnt"), proof)
    _validate_mount_identity_consistency(observations, proof)

    protected_paths = _parse_protected_paths(root, proof)
    deployment_paths = _parse_named_paths(root, "deployment", proof)
    backup_paths = _parse_named_paths(root, "backup", proof)
    custody_paths = _parse_named_paths(root, "key_custody", proof)

    for path in protected_paths:
        observation = _validated_observation(path, observations, proof, "protected_storage")
        if observation is not None:
            _validate_encrypted_source(observation, approved, lsblk, crypt, proof)

    _validate_swap(root.get("swap"), approved, lsblk, crypt, proof)
    _validate_core(root.get("core"), proof)
    _validate_key_separation(
        custody_paths,
        protected_paths + deployment_paths + backup_paths,
        observations,
        proof,
    )
    return _verdict(proof, binding)


def _parse_approved(value: object, proof: _Proof) -> dict[str, tuple[str, str]]:
    approved: dict[str, tuple[str, str]] = {}
    identities: set[tuple[str, str]] = set()
    for record in _objects(value, proof, "approved_backings_invalid"):
        if not _exact_fields(
            record,
            {"mapper", "backing_device", "luks_uuid"},
            proof,
            "approved_backing_fields_invalid",
        ):
            continue
        mapper = _absolute_path(record.get("mapper"))
        backing = _absolute_path(record.get("backing_device"))
        luks_uuid = _known_string(record.get("luks_uuid"))
        identity = (backing or "", luks_uuid or "")
        if (
            mapper is None
            or backing is None
            or luks_uuid is None
            or mapper in approved
            or identity in identities
        ):
            proof.fail("schema", "approved_backing_invalid")
            continue
        approved[mapper] = (backing, luks_uuid)
        identities.add(identity)
    if not approved:
        proof.fail("schema", "approved_backings_empty")
    return approved


def _parse_lsblk(value: object, proof: _Proof) -> dict[str, Mapping[str, object]]:
    devices: dict[str, Mapping[str, object]] = {}
    major_minors: set[str] = set()
    for record in _objects(value, proof, "lsblk_invalid"):
        if not _exact_fields(
            record, {"path", "major_minor", "type", "pkname"}, proof, "lsblk_fields_invalid"
        ):
            continue
        path = _absolute_path(record.get("path"))
        major_minor = _known_string(record.get("major_minor"))
        device_type = _known_string(record.get("type"))
        pkname = record.get("pkname")
        if (
            path is None
            or major_minor is None
            or major_minor in major_minors
            or device_type is None
            or not isinstance(pkname, str)
            or (pkname != "" and _absolute_path(pkname) is None)
            or path in devices
        ):
            proof.fail("schema", "lsblk_device_invalid")
            continue
        devices[path] = record
        major_minors.add(major_minor)
    if not devices:
        proof.fail("schema", "lsblk_empty")
    return devices


def _parse_crypt(value: object, proof: _Proof) -> dict[str, Mapping[str, object]]:
    mappings: dict[str, Mapping[str, object]] = {}
    for record in _objects(value, proof, "crypt_invalid"):
        if not _exact_fields(
            record,
            {"mapper", "backing_device", "luks_uuid", "luks_version", "active"},
            proof,
            "crypt_fields_invalid",
        ):
            continue
        mapper = _absolute_path(record.get("mapper"))
        backing = _absolute_path(record.get("backing_device"))
        luks_uuid = _known_string(record.get("luks_uuid"))
        if (
            mapper is None
            or backing is None
            or luks_uuid is None
            or record.get("luks_version") not in {"LUKS1", "LUKS2"}
            or type(record.get("active")) is not bool
            or mapper in mappings
        ):
            proof.fail("schema", "crypt_mapping_invalid")
            continue
        mappings[mapper] = record
    if not mappings:
        proof.fail("schema", "crypt_empty")
    return mappings


def _parse_findmnt(value: object, proof: _Proof) -> dict[str, Mapping[str, object]]:
    observations: dict[str, Mapping[str, object]] = {}
    fields = {
        "requested_path",
        "canonical_path",
        "target",
        "source",
        "major_minor",
        "filesystem_uuid",
        "fstype",
        "is_symlink",
    }
    for record in _objects(value, proof, "findmnt_invalid"):
        if not _exact_fields(record, fields, proof, "findmnt_fields_invalid"):
            continue
        path = _absolute_path(record.get("requested_path"))
        canonical = _absolute_path(record.get("canonical_path"))
        target = _absolute_path(record.get("target"))
        source = _absolute_path(record.get("source"))
        if (
            path is None
            or canonical is None
            or target is None
            or source is None
            or _known_string(record.get("major_minor")) is None
            or _known_string(record.get("filesystem_uuid")) is None
            or _known_string(record.get("fstype")) is None
            or type(record.get("is_symlink")) is not bool
            or path in observations
        ):
            proof.fail("schema", "findmnt_observation_invalid")
            continue
        observations[path] = record
    if not observations:
        proof.fail("schema", "findmnt_empty")
    return observations


def _validate_mount_identity_consistency(
    observations: Mapping[str, Mapping[str, object]], proof: _Proof
) -> None:
    """Reject contradictory source/device/filesystem identities in the snapshot."""

    by_source: dict[str, tuple[str, str]] = {}
    by_major_minor: dict[str, tuple[str, str]] = {}
    by_filesystem: dict[str, tuple[str, str]] = {}
    for observation in observations.values():
        source = _known_string(observation.get("source"))
        major_minor = _known_string(observation.get("major_minor"))
        filesystem_uuid = _known_string(observation.get("filesystem_uuid"))
        if source is None or major_minor is None or filesystem_uuid is None:
            continue
        indexes = (
            (by_source, source, (major_minor, filesystem_uuid)),
            (by_major_minor, major_minor, (source, filesystem_uuid)),
            (by_filesystem, filesystem_uuid, (source, major_minor)),
        )
        for index, key, identity in indexes:
            previous = index.setdefault(key, identity)
            if previous != identity:
                proof.fail("schema", "mount_device_identity_mismatch")


def _parse_protected_paths(snapshot: Mapping[str, object], proof: _Proof) -> list[str]:
    volume = _object(snapshot.get("volume_inspect"), proof, "volume_inspect_invalid")
    artifact_path: str | None = None
    if volume is not None and _exact_fields(
        volume, {"name", "driver", "mountpoint"}, proof, "volume_inspect_fields_invalid"
    ):
        if _known_string(volume.get("name")) is None or volume.get("driver") != "local":
            proof.fail("protected_storage", "artifact_volume_invalid")
        artifact_path = _absolute_path(volume.get("mountpoint"))
        if artifact_path is None:
            proof.fail("protected_storage", "artifact_mountpoint_invalid")

    postgres = _object(snapshot.get("postgres"), proof, "postgres_invalid")
    postgres_paths: list[str] = []
    postgres_fields = {
        "query_succeeded",
        "pgdata",
        "pg_wal",
        "temp_directories",
        "temp_directories_complete",
        "tablespaces",
        "tablespaces_complete",
    }
    if postgres is not None and _exact_fields(
        postgres, postgres_fields, proof, "postgres_fields_invalid"
    ):
        if postgres.get("query_succeeded") is not True:
            proof.fail("protected_storage", "postgres_query_failed")
        for field in ("pgdata", "pg_wal"):
            path = _absolute_path(postgres.get(field))
            if path is None:
                proof.fail("protected_storage", f"{field}_invalid")
            else:
                postgres_paths.append(path)
        for field in ("temp_directories", "tablespaces"):
            if postgres.get(f"{field}_complete") is not True:
                proof.fail("protected_storage", f"{field}_inventory_incomplete")
            postgres_paths.extend(
                _string_list(
                    postgres.get(field),
                    proof,
                    reason=f"{field}_invalid",
                    allow_empty=True,
                )
            )
    paths = ([artifact_path] if artifact_path is not None else []) + postgres_paths
    if len(paths) != len(set(paths)):
        proof.fail("protected_storage", "protected_paths_duplicate")
    return paths


def _validated_observation(
    path: str,
    observations: Mapping[str, Mapping[str, object]],
    proof: _Proof,
    check: str,
) -> Mapping[str, object] | None:
    observation = observations.get(path)
    if observation is None:
        proof.fail(check, "path_observation_missing")
        return None
    canonical = _absolute_path(observation.get("canonical_path"))
    target = _absolute_path(observation.get("target"))
    if observation.get("is_symlink") is not False or canonical != path:
        proof.fail(check, "path_symlink_or_canonical_mismatch")
    if target is None or not _path_contains(target, path):
        proof.fail(check, "mount_target_mismatch")
    for field in ("source", "major_minor", "filesystem_uuid", "fstype"):
        if _known_string(observation.get(field)) is None:
            proof.fail(check, "mount_identity_unknown")
    return observation


def _validate_encrypted_source(
    observation: Mapping[str, object],
    approved: Mapping[str, tuple[str, str]],
    lsblk: Mapping[str, Mapping[str, object]],
    crypt: Mapping[str, Mapping[str, object]],
    proof: _Proof,
) -> None:
    source = _absolute_path(observation.get("source"))
    if source is None or source not in approved:
        proof.fail("protected_storage", "source_not_approved")
        return
    device = lsblk.get(source)
    mapping = crypt.get(source)
    if device is None or mapping is None:
        proof.fail("protected_storage", "device_or_crypt_proof_missing")
        return
    backing, luks_uuid = approved[source]
    if (
        device.get("type") != "crypt"
        or device.get("major_minor") != observation.get("major_minor")
        or device.get("pkname") != backing
        or mapping.get("active") is not True
        or mapping.get("luks_version") not in {"LUKS1", "LUKS2"}
        or mapping.get("backing_device") != backing
        or mapping.get("luks_uuid") != luks_uuid
    ):
        proof.fail("protected_storage", "device_crypt_mismatch")
    backing_record = lsblk.get(backing)
    if backing_record is None or backing_record.get("type") not in {"disk", "part"}:
        proof.fail("protected_storage", "backing_device_invalid")


def _validate_swap(
    value: object,
    approved: Mapping[str, tuple[str, str]],
    lsblk: Mapping[str, Mapping[str, object]],
    crypt: Mapping[str, Mapping[str, object]],
    proof: _Proof,
) -> None:
    swap = _object(value, proof, "swap_invalid")
    if swap is None or not _exact_fields(
        swap, {"query_succeeded", "entries"}, proof, "swap_fields_invalid"
    ):
        proof.fail("swap", "swap_proof_invalid")
        return
    if swap.get("query_succeeded") is not True:
        proof.fail("swap", "swap_query_failed")
    entries = _objects(swap.get("entries"), proof, "swap_entries_invalid")
    for entry in entries:
        if not _exact_fields(
            entry, {"source", "mapper", "encrypted"}, proof, "swap_entry_fields_invalid"
        ):
            proof.fail("swap", "swap_entry_invalid")
            continue
        mapper = _absolute_path(entry.get("mapper"))
        source = _absolute_path(entry.get("source"))
        if (
            source is None
            or source != mapper
            or entry.get("encrypted") is not True
            or mapper is None
            or mapper not in approved
        ):
            proof.fail("swap", "swap_unencrypted_or_unknown")
            continue
        temporary = _Proof()
        _validate_encrypted_source(
            {
                "source": mapper,
                "major_minor": lsblk.get(mapper, {}).get("major_minor"),
            },
            approved,
            lsblk,
            crypt,
            temporary,
        )
        if not temporary.checks["protected_storage"]:
            proof.fail("swap", "swap_crypt_mismatch")


def _validate_core(value: object, proof: _Proof) -> None:
    core = _object(value, proof, "core_invalid")
    fields = {"query_succeeded", "rlimit_core_soft", "rlimit_core_hard", "storage", "suid_dumpable"}
    if core is None or not _exact_fields(core, fields, proof, "core_fields_invalid"):
        proof.fail("core_dump", "core_proof_invalid")
        return
    if (
        core.get("query_succeeded") is not True
        or type(core.get("rlimit_core_soft")) is not int
        or core.get("rlimit_core_soft") != 0
        or type(core.get("rlimit_core_hard")) is not int
        or core.get("rlimit_core_hard") != 0
        or core.get("storage") != "none"
        or type(core.get("suid_dumpable")) is not int
        or core.get("suid_dumpable") != 0
    ):
        proof.fail("core_dump", "core_dump_not_disabled")


def _validate_key_separation(
    custody_paths: Sequence[str],
    other_paths: Sequence[str],
    observations: Mapping[str, Mapping[str, object]],
    proof: _Proof,
) -> None:
    custody_identities: set[str] = set()
    other_identities: set[str] = set()
    for path, destination in [
        *((path, custody_identities) for path in custody_paths),
        *((path, other_identities) for path in other_paths),
    ]:
        observation = _validated_observation(path, observations, proof, "key_separation")
        if observation is not None:
            for field in ("source", "major_minor", "filesystem_uuid"):
                identity = _known_string(observation.get(field))
                if identity is not None:
                    destination.add(f"{field}:{identity}")
    if custody_identities & other_identities:
        proof.fail("key_separation", "key_custody_volume_not_separate")


def _verdict(proof: _Proof, binding: Mapping[str, str]) -> dict[str, object]:
    passed = not proof.reasons and all(proof.checks.values())
    return {
        "schema_version": VERDICT_VERSION,
        "verdict": "PASS" if passed else "FAIL",
        "bound": dict(binding),
        "checks": dict(sorted(proof.checks.items())),
        "reason_codes": sorted(proof.reasons),
    }


verify_snapshot = evaluate_snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--expected-host-id", required=True)
    parser.add_argument("--expected-boot-id", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--max-age-seconds", type=int, default=DEFAULT_MAX_AGE_SECONDS)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        snapshot: object = json.loads(args.snapshot.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        snapshot = None
    result = evaluate_snapshot(
        snapshot,
        expected_host_id=args.expected_host_id,
        expected_boot_id=args.expected_boot_id,
        expected_revision=args.expected_revision,
        max_age_seconds=args.max_age_seconds,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    raise SystemExit(0 if result["verdict"] == "PASS" else 2)


if __name__ == "__main__":
    main()
