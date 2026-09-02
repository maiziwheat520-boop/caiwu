"""Finalize and load strict registered-account bank-statement plans."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from ledgerbridge.bank_statement_contract import BankStatementParserProfile
from ledgerbridge.bank_statement_cutover_plan import (
    BankStatementExistingAccountPlan,
    ExistingStatementEvidenceMode,
)
from ledgerbridge.bank_statement_parsers import parse_bank_statement
from ledgerbridge.models import EntityType

BANK_STATEMENT_EXISTING_ACCOUNT_DRAFT_SCHEMA: Final = (
    "ledgerbridge.bank-statement-existing-account-draft.v1"
)
BANK_STATEMENT_EXISTING_ACCOUNT_PLAN_SCHEMA: Final = (
    "ledgerbridge.bank-statement-existing-account-plan.v1"
)
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_MAX_PLAN_BYTES = 1024 * 1024
_MAX_SOURCE_BYTES = 50 * 1024 * 1024
_TOP_LEVEL_KEYS = {
    "schema_version",
    "target_revision",
    "parser",
    "source",
    "scope",
    "account",
    "audit",
    "safety",
}


class BankStatementPlanBuildError(RuntimeError):
    """A private draft or plan failed strict source and scope validation."""


@dataclass(frozen=True, slots=True)
class LoadedBankStatementPlan:
    plan_sha256: str
    target_revision: str
    cutover: BankStatementExistingAccountPlan
    backup_directory: Path
    restore_report: Path
    key_file: Path
    artifact_root: Path


def finalize_private_bank_statement_plan(draft_path: Path, output_path: Path) -> Path:
    """Bind one operator draft to a real parser result without overwriting files."""

    created = False
    try:
        _require_new_private_json_path(output_path)
        draft = _read_private_json(draft_path)
        if (
            set(draft) != _TOP_LEVEL_KEYS
            or draft.get("schema_version") != BANK_STATEMENT_EXISTING_ACCOUNT_DRAFT_SCHEMA
        ):
            raise ValueError
        parser = _strict_mapping(draft["parser"], {"profile"})
        source = _strict_mapping(draft["source"], {"path", "account_suffix"})
        scope = _strict_mapping(
            draft["scope"],
            {
                "evidence_ref",
                "evidence_mode",
                "owner_entity_ref",
                "business_unit_ref",
                "owner_kind",
            },
        )
        account = _strict_mapping(draft["account"], {"managed_account_ref"})
        profile = BankStatementParserProfile(_text(parser["profile"]))
        source_path = _absolute_path(source["path"])
        account_suffix = _text(source["account_suffix"])
        source_bytes = _read_regular_file(source_path, maximum=_MAX_SOURCE_BYTES)
        digest = hashlib.sha256(source_bytes).hexdigest()
        statement = parse_bank_statement(
            profile,
            source_path,
            expected_sha256=digest,
            managed_account_suffix=account_suffix,
        )
        ExistingStatementEvidenceMode(_text(scope["evidence_mode"]))
        payload = dict(draft)
        payload["schema_version"] = BANK_STATEMENT_EXISTING_ACCOUNT_PLAN_SCHEMA
        payload["source"] = {
            "path": str(source_path),
            "sha256": statement.source_sha256,
            "size": statement.source_size,
            "account_suffix": statement.account_suffix,
            "period_start": statement.period_start.isoformat(),
            "period_end": statement.period_end.isoformat(),
            "transaction_count": len(statement.transactions),
            "transaction_set_sha256": statement.transaction_set_sha256,
            "parser_facts_sha256": statement.parser_facts_sha256,
            "monthly_transaction_counts": [
                {"month": month, "count": count}
                for month, count in statement.monthly_transaction_counts
            ],
        }
        payload["account"] = {
            "managed_account_ref": _text(account["managed_account_ref"]),
            "institution_code": statement.institution_code,
        }
        _write_new_private_json(output_path, payload)
        created = True
        loaded = load_private_bank_statement_plan(output_path)
        loaded.cutover.require_statement(statement)
        return output_path
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if created:
            with suppress(OSError):
                output_path.unlink()
        raise BankStatementPlanBuildError(
            "private bank statement plan could not be finalized"
        ) from None


def run_bank_statement_cutover_plan_builder(
    environ: Mapping[str, str] | None = None,
) -> int:
    """Finalize a plan from protected environment-bound paths."""

    values = os.environ if environ is None else environ
    try:
        draft = _absolute_path(values.get("LEDGERBRIDGE_BANK_STATEMENT_PRIVATE_DRAFT"))
        output = _absolute_path(values.get("LEDGERBRIDGE_BANK_STATEMENT_PRIVATE_PLAN"))
        finalize_private_bank_statement_plan(draft, output)
    except (OSError, TypeError, ValueError, BankStatementPlanBuildError):
        raise BankStatementPlanBuildError(
            "bank statement plan builder environment is invalid"
        ) from None
    print("BANK_STATEMENT_CUTOVER_PLAN_READY")
    return 0


def load_private_bank_statement_plan(path: Path) -> LoadedBankStatementPlan:
    """Load one strict plan without parsing or displaying private source fields."""

    try:
        payload = _read_private_json(path)
        if (
            set(payload) != _TOP_LEVEL_KEYS
            or payload.get("schema_version") != BANK_STATEMENT_EXISTING_ACCOUNT_PLAN_SCHEMA
        ):
            raise ValueError
        target_revision = _text(payload["target_revision"])
        if _REVISION.fullmatch(target_revision) is None:
            raise ValueError
        parser = _strict_mapping(payload["parser"], {"profile"})
        source = _strict_mapping(
            payload["source"],
            {
                "path",
                "sha256",
                "size",
                "account_suffix",
                "period_start",
                "period_end",
                "transaction_count",
                "transaction_set_sha256",
                "parser_facts_sha256",
                "monthly_transaction_counts",
            },
        )
        scope = _strict_mapping(
            payload["scope"],
            {
                "evidence_ref",
                "evidence_mode",
                "owner_entity_ref",
                "business_unit_ref",
                "owner_kind",
            },
        )
        account = _strict_mapping(payload["account"], {"managed_account_ref", "institution_code"})
        audit = _strict_mapping(payload["audit"], {"actor", "reason"})
        safety = _strict_mapping(
            payload["safety"],
            {"backup_directory", "restore_report", "key_file", "artifact_root"},
        )
        monthly_raw = source["monthly_transaction_counts"]
        if not isinstance(monthly_raw, list):
            raise ValueError
        monthly = tuple(
            (
                _text(_strict_mapping(value, {"month", "count"})["month"]),
                _integer(_strict_mapping(value, {"month", "count"})["count"]),
            )
            for value in monthly_raw
        )
        cutover = BankStatementExistingAccountPlan(
            source_path=_absolute_path(source["path"]),
            expected_sha256=_text(source["sha256"]),
            expected_size=_integer(source["size"]),
            parser_profile=BankStatementParserProfile(_text(parser["profile"])),
            evidence_ref=_uuid(scope["evidence_ref"]),
            evidence_mode=ExistingStatementEvidenceMode(_text(scope["evidence_mode"])),
            entity_ref=_uuid(scope["owner_entity_ref"]),
            business_unit_ref=_uuid(scope["business_unit_ref"]),
            managed_account_ref=_uuid(account["managed_account_ref"]),
            institution_code=_text(account["institution_code"]),
            account_suffix=_text(source["account_suffix"]),
            expected_owner_kind=EntityType(_text(scope["owner_kind"])),
            period_start=_date(source["period_start"]),
            period_end=_date(source["period_end"]),
            expected_transaction_count=_integer(source["transaction_count"]),
            expected_transaction_set_sha256=_text(source["transaction_set_sha256"]),
            expected_parser_facts_sha256=_text(source["parser_facts_sha256"]),
            expected_monthly_transaction_counts=monthly,
            actor=_text(audit["actor"]),
            reason=_text(audit["reason"]),
        )
        backup_directory = _absolute_path(safety["backup_directory"])
        restore_report = _absolute_path(safety["restore_report"])
        if restore_report.parent != backup_directory:
            raise ValueError
        return LoadedBankStatementPlan(
            plan_sha256=_plan_digest(payload),
            target_revision=target_revision,
            cutover=cutover,
            backup_directory=backup_directory,
            restore_report=restore_report,
            key_file=_absolute_path(safety["key_file"]),
            artifact_root=_absolute_path(safety["artifact_root"]),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise BankStatementPlanBuildError("private bank statement plan is invalid") from None


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
    descriptor, temporary_name = tempfile.mkstemp(prefix=".bank-plan-", dir=path.parent)
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
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise ValueError


def _strict_mapping(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError
    return value


def _uuid(value: object) -> UUID:
    return UUID(_text(value))


def _date(value: object) -> date:
    return date.fromisoformat(_text(value))


def _absolute_path(value: object) -> Path:
    path = Path(_text(value))
    if not path.is_absolute():
        raise ValueError
    return path


def _plan_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()
