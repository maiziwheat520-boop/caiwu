"""Command gate for one private account-registry intake plan."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path

from ledgerbridge.account_registry_intake import (
    AccountRegistryIntakeError,
    AccountRegistryIntakeReceipt,
    LoadedAccountRegistryIntake,
    load_private_account_registry_intake,
)

ACCOUNT_REGISTRY_INTAKE_PREFLIGHT_SCHEMA = "ledgerbridge.account-registry-intake-preflight.v1"
_MAX_RECEIPT_BYTES = 1024 * 1024
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class AccountRegistryIntakeCommandError(RuntimeError):
    """A private command gate or acceptance receipt is invalid."""


IntakeExecutor = Callable[
    [LoadedAccountRegistryIntake, str],
    AccountRegistryIntakeReceipt,
]


def run_account_registry_intake_command(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    executor: Callable[..., AccountRegistryIntakeReceipt] | None = None,
) -> int:
    """Run a production rollback preflight or an explicitly enabled production intake."""

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--execute-production", action="store_true")
    args = parser.parse_args(argv)
    values = os.environ if environ is None else environ
    try:
        plan_path = _environment_path(values, "LEDGERBRIDGE_ACCOUNT_INTAKE_PRIVATE_PLAN")
        receipt_path = _environment_path(
            values,
            "LEDGERBRIDGE_ACCOUNT_INTAKE_PREFLIGHT_RECEIPT",
        )
        database_url = _environment_text(values, "LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_URL")
        database_target = _environment_text(
            values,
            "LEDGERBRIDGE_ACCOUNT_INTAKE_DATABASE_TARGET",
        )
        deployed_revision = _read_deployed_revision(Path.cwd() / "DEPLOYED_REVISION")
        loaded = load_private_account_registry_intake(plan_path)
        if deployed_revision != loaded.plan.target_revision:
            raise AccountRegistryIntakeCommandError("deployed revision gate is not satisfied")
        if executor is None:
            raise AccountRegistryIntakeCommandError("account intake executor is unavailable")

        if args.preflight_only:
            if (
                values.get("LEDGERBRIDGE_ENV") != "production"
                or database_target != "production-rollback-only"
            ):
                raise AccountRegistryIntakeCommandError(
                    "production rollback preflight gate is not satisfied"
                )
            receipt = executor(loaded, database_url, commit=False)
            _validate_execution_receipt(receipt, loaded)
            _write_private_json(
                receipt_path,
                {
                    "schema_version": ACCOUNT_REGISTRY_INTAKE_PREFLIGHT_SCHEMA,
                    "plan_sha256": loaded.plan_sha256,
                    "target_revision": loaded.plan.target_revision,
                    "database_mode": "production-rollback-only",
                    "registry_revision": receipt.registry_revision,
                    "lifecycle_revision": receipt.lifecycle_revision,
                    "lifecycle_status": receipt.lifecycle_status,
                },
            )
            print(
                "ACCOUNT_REGISTRY_INTAKE_PRODUCTION_ROLLBACK_PREFLIGHT_OK "
                f"lifecycle={receipt.lifecycle_status} exact_idempotence=true"
            )
            return 0

        if (
            values.get("LEDGERBRIDGE_ENV") != "production"
            or database_target != "production"
            or values.get("LEDGERBRIDGE_ACCOUNT_INTAKE_PRODUCTION_EXECUTION")
            != "execute-reviewed-account-intake-v1"
        ):
            raise AccountRegistryIntakeCommandError("production execution gate is not satisfied")
        _validate_preflight_receipt(receipt_path, loaded)
        receipt = executor(loaded, database_url, commit=True)
        _validate_execution_receipt(receipt, loaded)
        print(
            "ACCOUNT_REGISTRY_INTAKE_PRODUCTION_OK "
            f"lifecycle={receipt.lifecycle_status} exact_idempotence=true"
        )
        return 0
    except AccountRegistryIntakeCommandError:
        raise
    except AccountRegistryIntakeError as exc:
        raise AccountRegistryIntakeCommandError("account intake command failed closed") from exc
    except (OSError, TypeError, ValueError):
        raise AccountRegistryIntakeCommandError("account intake command gate is invalid") from None


def _validate_execution_receipt(
    receipt: AccountRegistryIntakeReceipt,
    loaded: LoadedAccountRegistryIntake,
) -> None:
    plan = loaded.plan
    expected_lifecycle_revision = 1 if plan.account.initial_lifecycle == "ACTIVE" else 2
    if (
        receipt.plan_sha256 != loaded.plan_sha256
        or receipt.operation_id != plan.account.operation_id
        or receipt.owner_entity_ref != plan.entity.entity_ref
        or receipt.business_unit_ref != plan.business_unit.business_unit_ref
        or receipt.evidence_ref != plan.evidence.evidence_ref
        or receipt.managed_account_ref != plan.account.managed_account_ref
        or receipt.registry_revision != plan.account.expected_registry_revision + 1
        or receipt.lifecycle_revision != expected_lifecycle_revision
        or receipt.lifecycle_status != plan.account.initial_lifecycle
    ):
        raise AccountRegistryIntakeCommandError("account intake acceptance receipt conflicts")


def _validate_preflight_receipt(
    path: Path,
    loaded: LoadedAccountRegistryIntake,
) -> None:
    try:
        value = json.loads(_read_private_file(path, maximum=_MAX_RECEIPT_BYTES))
        expected_lifecycle_revision = 1 if loaded.plan.account.initial_lifecycle == "ACTIVE" else 2
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "plan_sha256",
            "target_revision",
            "database_mode",
            "registry_revision",
            "lifecycle_revision",
            "lifecycle_status",
        }:
            raise ValueError
        if (
            value["schema_version"] != ACCOUNT_REGISTRY_INTAKE_PREFLIGHT_SCHEMA
            or value["plan_sha256"] != loaded.plan_sha256
            or value["target_revision"] != loaded.plan.target_revision
            or value["database_mode"] != "production-rollback-only"
            or value["registry_revision"] != loaded.plan.account.expected_registry_revision + 1
            or value["lifecycle_revision"] != expected_lifecycle_revision
            or value["lifecycle_status"] != loaded.plan.account.initial_lifecycle
        ):
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise AccountRegistryIntakeCommandError("production preflight receipt is invalid") from None


def _environment_text(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not value or value != value.strip():
        raise AccountRegistryIntakeCommandError("account intake environment is incomplete")
    return value


def _environment_path(values: Mapping[str, str], name: str) -> Path:
    path = Path(_environment_text(values, name))
    if not path.is_absolute():
        raise AccountRegistryIntakeCommandError("account intake environment is incomplete")
    return path


def _read_deployed_revision(path: Path) -> str:
    try:
        if not path.is_absolute() or path.is_symlink():
            raise ValueError
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 100:
            raise ValueError
        revision = path.read_text(encoding="ascii").strip()
        if _REVISION.fullmatch(revision) is None:
            raise ValueError
        return revision
    except (OSError, UnicodeError, ValueError):
        raise AccountRegistryIntakeCommandError("deployed revision file is invalid") from None


def _read_private_file(path: Path, *, maximum: int) -> str:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
        raise ValueError
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError
    return path.read_text(encoding="utf-8")


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise AccountRegistryIntakeCommandError("private preflight receipt is unavailable")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise AccountRegistryIntakeCommandError("private preflight receipt is unavailable")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".account-intake-", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            json.dump(payload, target, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()
