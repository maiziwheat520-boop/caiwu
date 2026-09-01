"""Finalize one operator-owned MYbank cutover draft from a verified XLSX source."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Final

from ledgerbridge.mybank_cutover_command import (
    MYBANK_CUTOVER_PLAN_SCHEMA,
    MYBANK_EXISTING_ACCOUNT_PLAN_SCHEMA,
    load_private_mybank_cutover_plan,
)
from ledgerbridge.mybank_statement import MyBankEmptyStatementError, parse_mybank_xlsx

MYBANK_CUTOVER_DRAFT_SCHEMA: Final = "ledgerbridge.mybank-cutover-draft.v1"
MYBANK_EXISTING_ACCOUNT_DRAFT_SCHEMA: Final = "ledgerbridge.mybank-existing-account-draft.v1"
_MAX_PLAN_BYTES: Final = 1024 * 1024
_MAX_SOURCE_BYTES: Final = 50 * 1024 * 1024
_REGISTER_ACCOUNT_TOP_LEVEL_KEYS: Final = frozenset(
    {
        "schema_version",
        "target_revision",
        "source",
        "scope",
        "account",
        "principal",
        "audit",
        "safety",
    }
)
_EXISTING_ACCOUNT_TOP_LEVEL_KEYS: Final = frozenset(
    {
        "schema_version",
        "target_revision",
        "source",
        "scope",
        "account",
        "audit",
        "safety",
    }
)


class MyBankCutoverPlanBuildError(RuntimeError):
    """The private draft could not be bound to one verified statement source."""


class MyBankEmptyStatementSkipped(MyBankCutoverPlanBuildError):
    """A valid empty export was intentionally left without an executable plan."""


def finalize_private_mybank_cutover_plan(draft_path: Path, output_path: Path) -> Path:
    """Create a non-overwriting executable plan without printing private values."""

    created = False
    try:
        _require_new_private_json_path(output_path)
        draft = _read_private_json(draft_path)
        schema_version = draft.get("schema_version")
        if schema_version == MYBANK_CUTOVER_DRAFT_SCHEMA:
            expected_keys = _REGISTER_ACCOUNT_TOP_LEVEL_KEYS
            plan_schema = MYBANK_CUTOVER_PLAN_SCHEMA
        elif schema_version == MYBANK_EXISTING_ACCOUNT_DRAFT_SCHEMA:
            expected_keys = _EXISTING_ACCOUNT_TOP_LEVEL_KEYS
            plan_schema = MYBANK_EXISTING_ACCOUNT_PLAN_SCHEMA
        else:
            raise ValueError
        if set(draft) != expected_keys:
            raise ValueError
        source = _strict_mapping(draft["source"], {"path", "account_suffix"})
        source_path = _absolute_path(source["path"])
        account_suffix = _text(source["account_suffix"])
        source_bytes = _read_regular_file(source_path, maximum=_MAX_SOURCE_BYTES)
        digest = hashlib.sha256(source_bytes).hexdigest()
        statement = parse_mybank_xlsx(
            source_path,
            expected_sha256=digest,
            managed_account_suffix=account_suffix,
        )
        payload = dict(draft)
        payload["schema_version"] = plan_schema
        payload["source"] = {
            "path": str(source_path),
            "sha256": statement.source_sha256,
            "size": statement.source_size,
            "account_suffix": statement.account_suffix,
            "transaction_count": len(statement.transactions),
        }
        _write_new_private_json(output_path, payload)
        created = True
        load_private_mybank_cutover_plan(output_path)
        return output_path
    except MyBankEmptyStatementError:
        if created:
            with suppress(OSError):
                output_path.unlink()
        raise MyBankEmptyStatementSkipped("empty MYbank statement was skipped") from None
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if created:
            with suppress(OSError):
                output_path.unlink()
        raise MyBankCutoverPlanBuildError("private cutover plan could not be finalized") from None


def run_mybank_cutover_plan_builder(environ: Mapping[str, str] | None = None) -> int:
    """Finalize a plan from protected environment-bound paths."""

    values = os.environ if environ is None else environ
    try:
        draft = _absolute_path(values.get("LEDGERBRIDGE_MYBANK_PRIVATE_DRAFT"))
        output = _absolute_path(values.get("LEDGERBRIDGE_MYBANK_PRIVATE_PLAN"))
        finalize_private_mybank_cutover_plan(draft, output)
    except MyBankEmptyStatementSkipped:
        print("MYBANK_CUTOVER_EMPTY_STATEMENT_SKIPPED")
        return 0
    except (OSError, TypeError, ValueError, MyBankCutoverPlanBuildError):
        raise MyBankCutoverPlanBuildError("cutover plan builder environment is invalid") from None
    print("MYBANK_CUTOVER_PLAN_READY")
    return 0


def _read_private_json(path: Path) -> dict[str, Any]:
    raw = _read_regular_file(path, maximum=_MAX_PLAN_BYTES)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError
    return value


def _read_regular_file(path: Path, *, maximum: int) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise ValueError
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
        raise ValueError
    if (
        path.suffix.casefold() == ".json"
        and os.name != "nt"
        and stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb") as source:
            raw = source.read(maximum + 1)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    if len(raw) != metadata.st_size or len(raw) > maximum:
        raise ValueError
    return raw


def _write_new_private_json(path: Path, payload: dict[str, object]) -> None:
    _require_new_private_json_path(path)
    parent = path.parent
    descriptor, temporary_name = tempfile.mkstemp(prefix=".mybank-plan-", dir=parent)
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


def _require_new_private_json_path(path: Path) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError


def _strict_mapping(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError
    return value


def _absolute_path(value: object) -> Path:
    text = _text(value)
    path = Path(text)
    if not path.is_absolute():
        raise ValueError
    return path


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError
    return value
