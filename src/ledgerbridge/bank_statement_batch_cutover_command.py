"""Fail-closed command boundary for one private bank-statement batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final, Protocol
from uuid import UUID

from ledgerbridge.bank_statement_cutover import BankStatementCutoverReceipt
from ledgerbridge.bank_statement_cutover_plan_builder import (
    BankStatementPlanBuildError,
    LoadedBankStatementPlan,
    load_private_bank_statement_plan,
)

BANK_STATEMENT_PRIVATE_BATCH_SCHEMA: Final = "ledgerbridge.private-company-statement-batch.v1"
BANK_STATEMENT_PRIVATE_BATCH_SCHEMA_V2: Final = "ledgerbridge.private-company-statement-batch.v2"
BANK_STATEMENT_BATCH_PREFLIGHT_RECEIPT_SCHEMA: Final = (
    "ledgerbridge.bank-statement-batch-cutover-preflight.v1"
)
BANK_STATEMENT_BATCH_PRODUCTION_RECEIPT_SCHEMA: Final = (
    "ledgerbridge.bank-statement-batch-cutover-production.v1"
)
_BATCH_BINDING_SCHEMA: Final = "ledgerbridge.bank-statement-batch-binding.v1"
_MANIFEST_KEYS: Final = {
    "schema_version",
    "target_revision",
    "backup_directory",
    "restore_report",
    "item_count",
    "transaction_count",
    "items",
    "skipped_empty_count",
    "skipped_empty",
    "skipped_existing_count",
    "skipped_existing",
}
_ITEM_KEYS: Final = {
    "item_id",
    "source_group",
    "source_name",
    "source_sha256",
    "source_size",
    "account_suffix",
    "period",
    "transaction_count",
    "evidence_ref",
}
_ITEM_KEYS_V2: Final = (_ITEM_KEYS - {"period"}) | {
    "period_start",
    "period_end",
    "new_transaction_count",
}
_SKIPPED_EMPTY_KEYS: Final = {
    "source_group",
    "source_name",
    "source_sha256",
    "account_suffix",
    "reason",
}
_SKIPPED_EXISTING_KEYS: Final = _SKIPPED_EMPTY_KEYS | {"period"}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_ITEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_RECEIPT_BYTES = 4 * 1024 * 1024
_MAX_BATCH_ITEMS = 100


class BankStatementBatchCutoverCommandError(RuntimeError):
    """The private batch package or command gate is invalid."""


class BankStatementBatchCommittedReceiptError(BankStatementBatchCutoverCommandError):
    """The database committed, but the durable operator receipt was not written."""


class BankStatementBatchExecutor(Protocol):
    """Execute every loaded item under one caller-owned transaction boundary."""

    def __call__(
        self,
        plans: tuple[LoadedBankStatementPlan, ...],
        database_url: str,
        /,
        *,
        commit: bool,
    ) -> tuple[BankStatementCutoverReceipt, ...]: ...


@dataclass(frozen=True, slots=True)
class _ManifestItem:
    item_id: str
    source_sha256: str
    source_size: int
    account_suffix: str
    period_start: date
    period_end: date
    transaction_count: int
    new_transaction_count: int
    evidence_ref: UUID


@dataclass(frozen=True, slots=True)
class LoadedBankStatementBatch:
    """A strict manifest and its ordered, digest-bound private plans."""

    manifest_sha256: str
    batch_sha256: str
    target_revision: str
    transaction_count: int
    plans: tuple[LoadedBankStatementPlan, ...]
    item_ids: tuple[str, ...]


def run_bank_statement_batch_cutover_command(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    executor: BankStatementBatchExecutor | None = None,
) -> int:
    """Run an isolated batch preflight or an explicitly enabled production batch."""

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--execute-production", action="store_true")
    args = parser.parse_args(argv)
    values = os.environ if environ is None else environ
    try:
        manifest_path = _environment_path(values, "LEDGERBRIDGE_BANK_STATEMENT_PRIVATE_BATCH")
        receipt_path = _environment_path(
            values, "LEDGERBRIDGE_BANK_STATEMENT_BATCH_PREFLIGHT_RECEIPT"
        )
        database_url = _environment_text(values, "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_URL")
        database_target = _environment_text(values, "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET")
        deployed_revision = _environment_text(values, "LEDGERBRIDGE_DEPLOYED_REVISION")
        loaded = load_private_bank_statement_batch(manifest_path)
        if deployed_revision != loaded.target_revision:
            raise BankStatementBatchCutoverCommandError("deployed revision gate is not satisfied")
        if executor is None:
            raise BankStatementBatchCutoverCommandError("batch cutover executor is unavailable")

        if args.preflight_only:
            if values.get("LEDGERBRIDGE_ENV") == "production" or database_target != "isolated":
                raise BankStatementBatchCutoverCommandError(
                    "isolated batch preflight gate is not satisfied"
                )
            results = executor(loaded.plans, database_url, commit=False)
            validate_bank_statement_batch_receipts(
                results,
                loaded.plans,
                expected_transaction_count=loaded.transaction_count,
            )
            _write_private_json(receipt_path, _preflight_payload(loaded))
            print(
                "BANK_STATEMENT_BATCH_CUTOVER_PREFLIGHT_OK "
                f"items={len(loaded.plans)} transactions={loaded.transaction_count} "
                "candidates_added=0 replay_zero_delta=true conflict_rejected=true"
            )
            return 0

        if (
            values.get("LEDGERBRIDGE_ENV") != "production"
            or database_target != "production"
            or values.get("LEDGERBRIDGE_BANK_STATEMENT_PRODUCTION_EXECUTION")
            != "execute-reviewed-cutover-v1"
        ):
            raise BankStatementBatchCutoverCommandError(
                "production batch execution gate is not satisfied"
            )
        production_receipt_path = _environment_path(
            values, "LEDGERBRIDGE_BANK_STATEMENT_BATCH_PRODUCTION_RECEIPT"
        )
        _require_new_private_json_path(production_receipt_path)
        _validate_preflight_receipt(receipt_path, loaded)
        results = executor(loaded.plans, database_url, commit=True)
        validate_bank_statement_batch_receipts(
            results,
            loaded.plans,
            expected_transaction_count=loaded.transaction_count,
        )
        try:
            _write_private_json(
                production_receipt_path,
                _production_payload(loaded, results),
            )
        except (OSError, BankStatementBatchCutoverCommandError):
            raise BankStatementBatchCommittedReceiptError(
                "production batch committed but durable receipt persistence failed"
            ) from None
        print(
            "BANK_STATEMENT_BATCH_CUTOVER_PRODUCTION_OK "
            f"items={len(loaded.plans)} transactions={loaded.transaction_count} "
            "candidates_added=0 replay_zero_delta=true conflict_rejected=true"
        )
        return 0
    except BankStatementBatchCutoverCommandError:
        raise
    except BankStatementPlanBuildError:
        raise BankStatementBatchCutoverCommandError(
            "private batch plan is unavailable or invalid"
        ) from None
    except (OSError, TypeError, ValueError):
        raise BankStatementBatchCutoverCommandError(
            "batch cutover command gate is not satisfied"
        ) from None


def load_private_bank_statement_batch(path: Path) -> LoadedBankStatementBatch:
    """Load and bind the strict private manifest to ``items/<id>/plan.json``."""

    try:
        payload = _read_private_json(path, maximum=_MAX_MANIFEST_BYTES)
        schema_version = payload.get("schema_version")
        if set(payload) != _MANIFEST_KEYS or schema_version not in {
            BANK_STATEMENT_PRIVATE_BATCH_SCHEMA,
            BANK_STATEMENT_PRIVATE_BATCH_SCHEMA_V2,
        }:
            raise ValueError
        target_revision = _text(payload["target_revision"])
        if _REVISION.fullmatch(target_revision) is None:
            raise ValueError
        backup_directory = _absolute_path(payload["backup_directory"])
        restore_report = _absolute_path(payload["restore_report"])
        if restore_report.parent != backup_directory:
            raise ValueError
        item_count = _positive_integer(payload["item_count"])
        transaction_count = _positive_integer(payload["transaction_count"])
        items_raw = payload["items"]
        if (
            not isinstance(items_raw, list)
            or not 1 <= len(items_raw) <= _MAX_BATCH_ITEMS
            or len(items_raw) != item_count
        ):
            raise ValueError
        _validate_skipped_entries(payload)
        items = tuple(
            _load_manifest_item(value, schema_version=str(schema_version)) for value in items_raw
        )
        if (
            len({item.item_id for item in items}) != len(items)
            or len({item.source_sha256 for item in items}) != len(items)
            or len({item.evidence_ref for item in items}) != len(items)
            or sum(item.transaction_count for item in items) != transaction_count
        ):
            raise ValueError

        plans: list[LoadedBankStatementPlan] = []
        item_bindings: list[dict[str, object]] = []
        shared_key_file: Path | None = None
        shared_artifact_root: Path | None = None
        for item in items:
            plan_path = path.parent / "items" / item.item_id / "plan.json"
            _require_no_symlink_ancestors(plan_path.parent)
            loaded = load_private_bank_statement_plan(plan_path)
            _bind_item_plan(
                item,
                loaded,
                target_revision=target_revision,
                backup_directory=backup_directory,
                restore_report=restore_report,
            )
            if shared_key_file is None:
                shared_key_file = loaded.key_file
                shared_artifact_root = loaded.artifact_root
            elif loaded.key_file != shared_key_file or loaded.artifact_root != shared_artifact_root:
                raise ValueError
            plans.append(loaded)
            item_bindings.append(
                {
                    "item_id": item.item_id,
                    "plan_sha256": loaded.plan_sha256,
                }
            )

        manifest_sha256 = hashlib.sha256(_canonical_json(payload)).hexdigest()
        binding: dict[str, object] = {
            "schema_version": _BATCH_BINDING_SCHEMA,
            "manifest_sha256": manifest_sha256,
            "target_revision": target_revision,
            "backup_directory": str(backup_directory),
            "restore_report": str(restore_report),
            "key_file": str(shared_key_file),
            "artifact_root": str(shared_artifact_root),
            "items": item_bindings,
        }
        return LoadedBankStatementBatch(
            manifest_sha256=manifest_sha256,
            batch_sha256=hashlib.sha256(_canonical_json(binding)).hexdigest(),
            target_revision=target_revision,
            transaction_count=transaction_count,
            plans=tuple(plans),
            item_ids=tuple(item.item_id for item in items),
        )
    except BankStatementPlanBuildError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise BankStatementBatchCutoverCommandError(
            "private bank statement batch is invalid"
        ) from None


def _load_manifest_item(value: object, *, schema_version: str) -> _ManifestItem:
    is_v2 = schema_version == BANK_STATEMENT_PRIVATE_BATCH_SCHEMA_V2
    item = _strict_mapping(value, _ITEM_KEYS_V2 if is_v2 else _ITEM_KEYS)
    item_id = _text(item["item_id"])
    digest = _text(item["source_sha256"])
    suffix = _text(item["account_suffix"])
    if (
        _ITEM_ID.fullmatch(item_id) is None
        or _DIGEST.fullmatch(digest) is None
        or _ACCOUNT_SUFFIX.fullmatch(suffix) is None
    ):
        raise ValueError
    _text(item["source_group"])
    _text(item["source_name"])
    transaction_count = _positive_integer(item["transaction_count"])
    new_transaction_count = (
        _non_negative_integer(item["new_transaction_count"]) if is_v2 else transaction_count
    )
    if new_transaction_count > transaction_count:
        raise ValueError
    period_start = date.fromisoformat(_text(item["period_start"] if is_v2 else item["period"]))
    period_end = date.fromisoformat(_text(item["period_end"] if is_v2 else item["period"]))
    if period_start > period_end:
        raise ValueError
    return _ManifestItem(
        item_id=item_id,
        source_sha256=digest,
        source_size=_positive_integer(item["source_size"]),
        account_suffix=suffix,
        period_start=period_start,
        period_end=period_end,
        transaction_count=transaction_count,
        new_transaction_count=new_transaction_count,
        evidence_ref=UUID(_text(item["evidence_ref"])),
    )


def _validate_skipped_entries(payload: dict[str, Any]) -> None:
    for count_name, values_name, keys in (
        ("skipped_empty_count", "skipped_empty", _SKIPPED_EMPTY_KEYS),
        ("skipped_existing_count", "skipped_existing", _SKIPPED_EXISTING_KEYS),
    ):
        count = _non_negative_integer(payload[count_name])
        values = payload[values_name]
        if not isinstance(values, list) or len(values) != count:
            raise ValueError
        for value in values:
            entry = _strict_mapping(value, keys)
            digest = _text(entry["source_sha256"])
            suffix = _text(entry["account_suffix"])
            if _DIGEST.fullmatch(digest) is None or _ACCOUNT_SUFFIX.fullmatch(suffix) is None:
                raise ValueError
            _text(entry["source_group"])
            _text(entry["source_name"])
            _text(entry["reason"])
            if "period" in keys:
                date.fromisoformat(_text(entry["period"]))


def _bind_item_plan(
    item: _ManifestItem,
    loaded: LoadedBankStatementPlan,
    *,
    target_revision: str,
    backup_directory: Path,
    restore_report: Path,
) -> None:
    cutover = loaded.cutover
    if (
        loaded.target_revision != target_revision
        or loaded.backup_directory != backup_directory
        or loaded.restore_report != restore_report
        or cutover.expected_sha256 != item.source_sha256
        or cutover.expected_size != item.source_size
        or cutover.account_suffix != item.account_suffix
        or cutover.period_start != item.period_start
        or cutover.period_end != item.period_end
        or cutover.expected_transaction_count != item.transaction_count
        or (
            cutover.expected_new_transaction_count
            if cutover.expected_new_transaction_count is not None
            else cutover.expected_transaction_count
        )
        != item.new_transaction_count
        or cutover.evidence_ref != item.evidence_ref
    ):
        raise ValueError


def validate_bank_statement_batch_receipts(
    receipts: tuple[BankStatementCutoverReceipt, ...],
    plans: tuple[LoadedBankStatementPlan, ...],
    *,
    expected_transaction_count: int | None = None,
) -> None:
    """Bind ordered item receipts before a batch transaction may commit."""

    if not isinstance(receipts, tuple) or len(receipts) != len(plans):
        raise BankStatementBatchCutoverCommandError("batch cutover acceptance receipts conflict")
    transaction_count = 0
    previous: BankStatementCutoverReceipt | None = None
    for receipt, plan in zip(receipts, plans, strict=True):
        cutover = plan.cutover
        if (
            not isinstance(receipt, BankStatementCutoverReceipt)
            or receipt.evidence_ref != cutover.evidence_ref
            or receipt.managed_account_ref != cutover.managed_account_ref
            or receipt.transaction_count != cutover.expected_transaction_count
            or receipt.registry_created
            or receipt.registry_replay_created
            or receipt.replay_created
            or receipt.candidate_delta != 0
            or receipt.latest_pending_candidate_delta != 0
            or receipt.after_counts != receipt.replay_counts
            or receipt.after_counts.journal_entries != receipt.before_counts.journal_entries
            or receipt.after_counts.postings != receipt.before_counts.postings
            or not receipt.fact_conflict_rejected
            or (previous is not None and receipt.before_counts != previous.after_counts)
        ):
            raise BankStatementBatchCutoverCommandError(
                "batch cutover acceptance receipts conflict"
            )
        transaction_count += receipt.transaction_count
        previous = receipt
    expected = (
        sum(plan.cutover.expected_transaction_count for plan in plans)
        if expected_transaction_count is None
        else expected_transaction_count
    )
    if transaction_count != expected:
        raise BankStatementBatchCutoverCommandError("batch cutover acceptance receipts conflict")


def _preflight_payload(loaded: LoadedBankStatementBatch) -> dict[str, object]:
    return {
        "schema_version": BANK_STATEMENT_BATCH_PREFLIGHT_RECEIPT_SCHEMA,
        "manifest_sha256": loaded.manifest_sha256,
        "batch_sha256": loaded.batch_sha256,
        "target_revision": loaded.target_revision,
        "item_count": len(loaded.plans),
        "transaction_count": loaded.transaction_count,
        "candidate_delta": 0,
        "replay_zero_delta": True,
        "fact_conflict_rejected": True,
        "items": [
            {
                "item_id": item_id,
                "plan_sha256": plan.plan_sha256,
                "transaction_count": plan.cutover.expected_transaction_count,
            }
            for item_id, plan in zip(loaded.item_ids, loaded.plans, strict=True)
        ],
    }


def _production_payload(
    loaded: LoadedBankStatementBatch,
    receipts: tuple[BankStatementCutoverReceipt, ...],
) -> dict[str, object]:
    payload = _preflight_payload(loaded)
    payload["schema_version"] = BANK_STATEMENT_BATCH_PRODUCTION_RECEIPT_SCHEMA
    payload["items"] = [
        {
            "item_id": item_id,
            "plan_sha256": plan.plan_sha256,
            "transaction_count": plan.cutover.expected_transaction_count,
            "created": receipt.created,
        }
        for item_id, plan, receipt in zip(
            loaded.item_ids,
            loaded.plans,
            receipts,
            strict=True,
        )
    ]
    return payload


def _validate_preflight_receipt(
    path: Path,
    loaded: LoadedBankStatementBatch,
) -> None:
    try:
        receipt = _read_private_json(path, maximum=_MAX_RECEIPT_BYTES)
        if receipt != _preflight_payload(loaded):
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise BankStatementBatchCutoverCommandError(
            "production batch preflight receipt is invalid"
        ) from None


def _read_private_json(path: Path, *, maximum: int) -> dict[str, Any]:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise ValueError
    _require_no_symlink_ancestors(path.parent)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
        raise ValueError
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
        ):
            raise ValueError
        source = os.fdopen(descriptor, "rb")
        descriptor = -1
        with source:
            raw = source.read(maximum + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) != metadata.st_size or len(raw) > maximum:
        raise ValueError
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError
    return value


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    _require_new_private_json_path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".bank-statement-batch-preflight-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            json.dump(
                payload,
                target,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _require_new_private_json_path(path: Path) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise BankStatementBatchCutoverCommandError("private batch receipt is unavailable")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise BankStatementBatchCutoverCommandError("private batch receipt is unavailable")
    _require_no_symlink_ancestors(path.parent)


def _require_no_symlink_ancestors(path: Path) -> None:
    current = path
    while True:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError
        if current.parent == current:
            return
        current = current.parent


def _strict_mapping(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError
    return value


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError
    return value


def _non_negative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _absolute_path(value: object) -> Path:
    path = Path(_text(value))
    if not path.is_absolute():
        raise ValueError
    return path


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _environment_text(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not value or value != value.strip():
        raise BankStatementBatchCutoverCommandError(
            "batch cutover command environment is incomplete"
        )
    return value


def _environment_path(values: Mapping[str, str], name: str) -> Path:
    path = Path(_environment_text(values, name))
    if not path.is_absolute():
        raise BankStatementBatchCutoverCommandError(
            "batch cutover command environment is incomplete"
        )
    return path
