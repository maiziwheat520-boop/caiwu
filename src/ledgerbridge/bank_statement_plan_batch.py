"""Atomically materialize a private batch of independent bank-statement plans."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ledgerbridge.bank_statement_cutover_plan_builder import (
    finalize_private_bank_statement_plan,
    load_private_bank_statement_plan,
)

BANK_STATEMENT_PLAN_BATCH_SCHEMA: Final = "ledgerbridge.bank-statement-plan-batch.v1"
BANK_STATEMENT_PLAN_BATCH_INDEX_SCHEMA: Final = "ledgerbridge.bank-statement-plan-batch-index.v1"
_MANIFEST_KEYS: Final = {"schema_version", "expected_item_count", "items"}
_ITEM_KEYS: Final = {"draft_path"}
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_BATCH_ITEMS = 1_000


class BankStatementPlanBatchError(RuntimeError):
    """A private plan batch could not be validated or published completely."""


@dataclass(frozen=True, slots=True)
class MaterializedBankStatementPlanBatch:
    output_directory: Path
    plan_count: int
    index_sha256: str


def materialize_private_bank_statement_plan_batch(
    manifest_path: Path,
    output_directory: Path,
) -> MaterializedBankStatementPlanBatch:
    """Build every plan in a private staging directory, then publish it once."""

    staging_directory: Path | None = None
    published_directory: Path | None = None
    private_parent: Path | None = None
    try:
        draft_paths = _load_manifest(manifest_path)
        _require_new_private_directory(output_directory)
        parent = output_directory.parent
        private_parent = parent
        staging_directory = Path(tempfile.mkdtemp(prefix=".bank-statement-plan-batch-", dir=parent))
        os.chmod(staging_directory, 0o700)

        plan_index: list[dict[str, object]] = []
        source_digests: set[str] = set()
        evidence_refs: set[object] = set()
        target_revision: str | None = None
        for ordinal, draft_path in enumerate(draft_paths, start=1):
            plan_path = staging_directory / f"{ordinal:06d}.plan.json"
            finalize_private_bank_statement_plan(draft_path, plan_path)
            loaded = load_private_bank_statement_plan(plan_path)
            if _DIGEST.fullmatch(loaded.plan_sha256) is None:
                raise ValueError
            if loaded.cutover.expected_sha256 in source_digests:
                raise ValueError
            if loaded.cutover.evidence_ref in evidence_refs:
                raise ValueError
            source_digests.add(loaded.cutover.expected_sha256)
            evidence_refs.add(loaded.cutover.evidence_ref)
            if target_revision is None:
                target_revision = loaded.target_revision
            elif loaded.target_revision != target_revision:
                raise ValueError
            plan_index.append(
                {
                    "ordinal": ordinal,
                    "plan_sha256": loaded.plan_sha256,
                }
            )

        index_payload: dict[str, object] = {
            "schema_version": BANK_STATEMENT_PLAN_BATCH_INDEX_SCHEMA,
            "plan_count": len(plan_index),
            "plans": plan_index,
        }
        index_bytes = _canonical_json(index_payload)
        index_sha256 = hashlib.sha256(index_bytes).hexdigest()
        _write_new_private_file(staging_directory / "index.json", index_bytes)
        _sync_directory(staging_directory)

        if output_directory.exists() or output_directory.is_symlink():
            raise FileExistsError
        os.rename(staging_directory, output_directory)
        staging_directory = None
        published_directory = output_directory
        _sync_directory(parent)
        result = MaterializedBankStatementPlanBatch(
            output_directory=output_directory,
            plan_count=len(plan_index),
            index_sha256=index_sha256,
        )
        published_directory = None
        return result
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise BankStatementPlanBatchError(
            "private bank statement plan batch could not be materialized"
        ) from None
    finally:
        if private_parent is not None:
            if staging_directory is not None:
                _remove_created_directory(staging_directory, parent=private_parent)
            if published_directory is not None:
                _remove_created_directory(published_directory, parent=private_parent)


def run_bank_statement_plan_batch_builder(
    environ: Mapping[str, str] | None = None,
) -> int:
    """Materialize a batch from protected environment-bound paths."""

    values = os.environ if environ is None else environ
    try:
        manifest = _absolute_path(values.get("LEDGERBRIDGE_BANK_STATEMENT_PRIVATE_BATCH"))
        output = _absolute_path(values.get("LEDGERBRIDGE_BANK_STATEMENT_PRIVATE_PLAN_DIRECTORY"))
        result = materialize_private_bank_statement_plan_batch(manifest, output)
    except (TypeError, ValueError, BankStatementPlanBatchError):
        raise BankStatementPlanBatchError(
            "bank statement plan batch builder environment is invalid"
        ) from None
    print(
        "BANK_STATEMENT_CUTOVER_PLAN_BATCH_READY "
        f"count={result.plan_count} index_sha256={result.index_sha256}"
    )
    return 0


def _load_manifest(path: Path) -> tuple[Path, ...]:
    payload = _read_private_json(path)
    if (
        set(payload) != _MANIFEST_KEYS
        or payload.get("schema_version") != BANK_STATEMENT_PLAN_BATCH_SCHEMA
    ):
        raise ValueError
    expected_count = _positive_integer(payload["expected_item_count"])
    items = payload["items"]
    if (
        not isinstance(items, list)
        or not 1 <= len(items) <= _MAX_BATCH_ITEMS
        or len(items) != expected_count
    ):
        raise ValueError
    draft_paths = tuple(
        _absolute_path(_strict_mapping(item, _ITEM_KEYS)["draft_path"]) for item in items
    )
    if len(set(draft_paths)) != len(draft_paths):
        raise ValueError
    return draft_paths


def _read_private_json(path: Path) -> dict[str, Any]:
    raw = _read_regular_file(path, maximum=_MAX_MANIFEST_BYTES)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError
    return value


def _read_regular_file(path: Path, *, maximum: int) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise ValueError
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
    return raw


def _require_new_private_directory(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError
    if not path.name or path.exists() or path.is_symlink():
        raise ValueError
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError
    _require_no_symlink_ancestors(parent)


def _require_no_symlink_ancestors(path: Path) -> None:
    current = path
    while True:
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError
        if current.parent == current:
            return
        current = current.parent


def _write_new_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    os.chmod(path, 0o600)


def _remove_created_directory(path: Path, *, parent: Path) -> None:
    if path.parent != parent:
        return
    try:
        metadata = path.lstat()
    except OSError:
        return
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        shutil.rmtree(path, ignore_errors=True)


def _sync_directory(path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _strict_mapping(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError
    return value


def _positive_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError
    return value


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError
    path = Path(value)
    if not path.is_absolute():
        raise ValueError
    return path


def _canonical_json(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("ascii")
