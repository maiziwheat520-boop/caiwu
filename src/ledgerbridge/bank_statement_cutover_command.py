"""Command boundary for one profile-bound existing-account statement cutover."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path

from ledgerbridge.bank_statement_cutover import BankStatementCutoverReceipt
from ledgerbridge.bank_statement_cutover_plan_builder import (
    BankStatementPlanBuildError,
    LoadedBankStatementPlan,
    load_private_bank_statement_plan,
)

BANK_STATEMENT_PREFLIGHT_RECEIPT_SCHEMA = (
    "ledgerbridge.bank-statement-cutover-preflight.v1"
)
_MAX_RECEIPT_BYTES = 1024 * 1024


class BankStatementCutoverCommandError(RuntimeError):
    """The private execution package or command gate is invalid."""


def run_bank_statement_cutover_command(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    executor: Callable[..., BankStatementCutoverReceipt] | None = None,
) -> int:
    """Run an isolated preflight or an explicitly enabled production cutover."""

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--execute-production", action="store_true")
    args = parser.parse_args(argv)
    values = os.environ if environ is None else environ
    try:
        plan_path = _environment_path(values, "LEDGERBRIDGE_BANK_STATEMENT_PRIVATE_PLAN")
        receipt_path = _environment_path(
            values, "LEDGERBRIDGE_BANK_STATEMENT_PREFLIGHT_RECEIPT"
        )
        database_url = _environment_text(
            values, "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_URL"
        )
        database_target = _environment_text(
            values, "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET"
        )
        deployed_revision = _environment_text(values, "LEDGERBRIDGE_DEPLOYED_REVISION")
        loaded = load_private_bank_statement_plan(plan_path)
        if deployed_revision != loaded.target_revision:
            raise BankStatementCutoverCommandError(
                "deployed revision gate is not satisfied"
            )
        if executor is None:
            raise BankStatementCutoverCommandError("cutover executor is unavailable")

        if args.preflight_only:
            if values.get("LEDGERBRIDGE_ENV") == "production" or database_target != "isolated":
                raise BankStatementCutoverCommandError(
                    "isolated preflight gate is not satisfied"
                )
            result = executor(loaded, database_url, commit=False)
            _validate_receipt(result, loaded)
            _write_private_json(
                receipt_path,
                {
                    "schema_version": BANK_STATEMENT_PREFLIGHT_RECEIPT_SCHEMA,
                    "plan_sha256": loaded.plan_sha256,
                    "target_revision": loaded.target_revision,
                    "transaction_count": result.transaction_count,
                    "candidate_delta": result.candidate_delta,
                    "replay_zero_delta": result.after_counts == result.replay_counts,
                    "fact_conflict_rejected": result.fact_conflict_rejected,
                },
            )
            print(
                "BANK_STATEMENT_CUTOVER_PREFLIGHT_OK "
                f"transactions={result.transaction_count} candidates_added=0 "
                "replay_zero_delta=true conflict_rejected=true"
            )
            return 0

        if (
            values.get("LEDGERBRIDGE_ENV") != "production"
            or database_target != "production"
            or values.get("LEDGERBRIDGE_BANK_STATEMENT_PRODUCTION_EXECUTION")
            != "execute-reviewed-cutover-v1"
        ):
            raise BankStatementCutoverCommandError(
                "production execution gate is not satisfied"
            )
        _validate_preflight_receipt(receipt_path, loaded)
        result = executor(loaded, database_url, commit=True)
        _validate_receipt(result, loaded)
        print(
            "BANK_STATEMENT_CUTOVER_PRODUCTION_OK "
            f"transactions={result.transaction_count} candidates_added=0 "
            "replay_zero_delta=true conflict_rejected=true"
        )
        return 0
    except BankStatementCutoverCommandError:
        raise
    except BankStatementPlanBuildError:
        raise BankStatementCutoverCommandError("private plan is unavailable or invalid") from None
    except (OSError, TypeError, ValueError):
        raise BankStatementCutoverCommandError("cutover command gate is not satisfied") from None


def _validate_receipt(
    receipt: BankStatementCutoverReceipt,
    loaded: LoadedBankStatementPlan,
) -> None:
    if (
        receipt.transaction_count != loaded.cutover.expected_transaction_count
        or receipt.registry_created
        or receipt.registry_replay_created
        or receipt.candidate_delta != 0
        or receipt.latest_pending_candidate_delta != 0
        or receipt.replay_created
        or receipt.after_counts != receipt.replay_counts
        or not receipt.fact_conflict_rejected
    ):
        raise BankStatementCutoverCommandError("cutover acceptance receipt conflicts")


def _validate_preflight_receipt(
    path: Path,
    loaded: LoadedBankStatementPlan,
) -> None:
    try:
        receipt = _read_private_json(path)
        if set(receipt) != {
            "schema_version",
            "plan_sha256",
            "target_revision",
            "transaction_count",
            "candidate_delta",
            "replay_zero_delta",
            "fact_conflict_rejected",
        } or (
            receipt["schema_version"] != BANK_STATEMENT_PREFLIGHT_RECEIPT_SCHEMA
            or receipt["plan_sha256"] != loaded.plan_sha256
            or receipt["target_revision"] != loaded.target_revision
            or receipt["transaction_count"] != loaded.cutover.expected_transaction_count
            or receipt["candidate_delta"] != 0
            or receipt["replay_zero_delta"] is not True
            or receipt["fact_conflict_rejected"] is not True
        ):
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise BankStatementCutoverCommandError(
            "production preflight receipt is invalid"
        ) from None


def _read_private_json(path: Path) -> dict[str, object]:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_RECEIPT_BYTES:
        raise ValueError
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError
    return value


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise BankStatementCutoverCommandError(
            "private preflight receipt is unavailable"
        )
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise BankStatementCutoverCommandError(
            "private preflight receipt is unavailable"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".bank-statement-preflight-", dir=path.parent
    )
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


def _environment_text(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not value or value != value.strip():
        raise BankStatementCutoverCommandError("cutover command environment is incomplete")
    return value


def _environment_path(values: Mapping[str, str], name: str) -> Path:
    path = Path(_environment_text(values, name))
    if not path.is_absolute():
        raise BankStatementCutoverCommandError("cutover command environment is incomplete")
    return path
