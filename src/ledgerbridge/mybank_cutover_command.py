"""Private-plan command boundary for one MYbank whole-statement cutover."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from ledgerbridge.account_registry import (
    AccountAliasRegistration,
    AccountRegistryPlan,
    ManagedAccountRegistration,
)
from ledgerbridge.internal_read_contract import (
    Capability,
    EntityGrant,
    WorkloadPrincipal,
)
from ledgerbridge.models import EntityType
from ledgerbridge.mybank_statement_cutover import (
    MyBankCutoverSafetyProof,
    MyBankEvidenceMode,
    MyBankExistingAccountStatementPlan,
    MyBankStatementCutoverPlan,
    MyBankStatementCutoverReceipt,
)

MYBANK_CUTOVER_PLAN_SCHEMA = "ledgerbridge.mybank-cutover-plan.v1"
MYBANK_EXISTING_ACCOUNT_PLAN_SCHEMA = "ledgerbridge.mybank-existing-account-plan.v1"
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_MAX_PLAN_BYTES = 1024 * 1024


class MyBankCutoverCommandError(RuntimeError):
    """The private execution package or command gate is invalid."""


@dataclass(frozen=True, slots=True)
class LoadedMyBankCutoverPlan:
    plan_sha256: str
    target_revision: str
    cutover: MyBankStatementCutoverPlan | MyBankExistingAccountStatementPlan
    principal: WorkloadPrincipal | None
    safety_proof: MyBankCutoverSafetyProof
    key_file: Path
    artifact_root: Path


def load_private_mybank_cutover_plan(path: Path) -> LoadedMyBankCutoverPlan:
    """Load one strict owner-confirmed plan without disclosing rejected values."""

    try:
        payload = _read_private_json(path)
        schema_version = payload.get("schema_version")
        if schema_version == MYBANK_CUTOVER_PLAN_SCHEMA:
            _require_keys(
                payload,
                {
                    "schema_version",
                    "target_revision",
                    "source",
                    "scope",
                    "account",
                    "principal",
                    "audit",
                    "safety",
                },
            )
        elif schema_version == MYBANK_EXISTING_ACCOUNT_PLAN_SCHEMA:
            _require_keys(
                payload,
                {
                    "schema_version",
                    "target_revision",
                    "source",
                    "scope",
                    "account",
                    "audit",
                    "safety",
                },
            )
        else:
            raise ValueError
        target_revision = _text(payload["target_revision"])
        if _REVISION.fullmatch(target_revision) is None:
            raise ValueError

        source = _mapping(
            payload["source"],
            {"path", "sha256", "size", "account_suffix", "transaction_count"},
        )
        scope_keys = {"evidence_ref", "owner_entity_ref", "business_unit_ref", "owner_kind"}
        if schema_version == MYBANK_EXISTING_ACCOUNT_PLAN_SCHEMA:
            scope_keys.add("evidence_mode")
        scope = _mapping(payload["scope"], scope_keys)
        audit = _mapping(payload["audit"], {"actor", "reason"})
        safety = _mapping(
            payload["safety"],
            {"backup_directory", "restore_report", "key_file", "artifact_root"},
        )

        entity_ref = _uuid(scope["owner_entity_ref"])
        business_unit_ref = _uuid(scope["business_unit_ref"])
        evidence_ref = _uuid(scope["evidence_ref"])
        account_suffix = _text(source["account_suffix"])
        actor = _text(audit["actor"])
        reason = _text(audit["reason"])
        owner_kind = EntityType(_text(scope["owner_kind"]))
        if schema_version == MYBANK_CUTOVER_PLAN_SCHEMA:
            account = _mapping(
                payload["account"],
                {
                    "operation_id",
                    "expected_registry_revision",
                    "managed_account_ref",
                    "account_key",
                    "account_kind",
                    "aliases",
                    "business_unit_assignment",
                },
            )
            principal = _mapping(
                payload["principal"],
                {"principal_ref", "san_uri", "policy_generation"},
            )
            aliases_raw = account["aliases"]
            if not isinstance(aliases_raw, list):
                raise ValueError
            aliases = tuple(_alias(value) for value in aliases_raw)
            if account["business_unit_assignment"] is not None:
                raise ValueError
            registry_plan = AccountRegistryPlan(
                operation_id=_uuid(account["operation_id"]),
                owner_entity_ref=entity_ref,
                expected_owner_kind=owner_kind,
                expected_registry_revision=_integer(account["expected_registry_revision"]),
                actor_ref=actor,
                reason=reason,
                accounts=(
                    ManagedAccountRegistration(
                        managed_account_ref=_uuid(account["managed_account_ref"]),
                        admission_evidence_ref=evidence_ref,
                        account_key=_text(account["account_key"]),
                        institution_code="mybank",
                        account_suffix=account_suffix,
                        account_kind=_text(account["account_kind"]),
                        aliases=aliases,
                    ),
                ),
            )
            cutover: MyBankStatementCutoverPlan | MyBankExistingAccountStatementPlan = (
                MyBankStatementCutoverPlan(
                    source_path=_absolute_path(source["path"]),
                    expected_sha256=_text(source["sha256"]),
                    expected_size=_integer(source["size"]),
                    evidence_ref=evidence_ref,
                    entity_ref=entity_ref,
                    business_unit_ref=business_unit_ref,
                    registry_plan=registry_plan,
                    account_suffix=account_suffix,
                    expected_transaction_count=_integer(source["transaction_count"]),
                    actor=actor,
                    reason=reason,
                )
            )
            workload: WorkloadPrincipal | None = WorkloadPrincipal(
                principal_ref=_text(principal["principal_ref"]),
                san_uri=_text(principal["san_uri"]),
                policy_generation=_integer(principal["policy_generation"]),
                capabilities=frozenset({Capability.ACCOUNT_REGISTRY_WRITE}),
                grants=(EntityGrant(entity_ref=entity_ref, allow_account_registry=True),),
            )
        else:
            account = _mapping(payload["account"], {"managed_account_ref"})
            cutover = MyBankExistingAccountStatementPlan(
                source_path=_absolute_path(source["path"]),
                expected_sha256=_text(source["sha256"]),
                expected_size=_integer(source["size"]),
                evidence_ref=evidence_ref,
                evidence_mode=MyBankEvidenceMode(_text(scope["evidence_mode"])),
                entity_ref=entity_ref,
                business_unit_ref=business_unit_ref,
                managed_account_ref=_uuid(account["managed_account_ref"]),
                account_suffix=account_suffix,
                expected_transaction_count=_integer(source["transaction_count"]),
                expected_owner_kind=owner_kind,
                actor=actor,
                reason=reason,
            )
            workload = None
        backup_directory = _absolute_path(safety["backup_directory"])
        proof = MyBankCutoverSafetyProof(
            backup_directory=backup_directory,
            restore_report=_absolute_path(safety["restore_report"]),
        )
        if proof.restore_report.parent != backup_directory:
            raise ValueError
        return LoadedMyBankCutoverPlan(
            plan_sha256=_plan_digest(payload),
            target_revision=target_revision,
            cutover=cutover,
            principal=workload,
            safety_proof=proof,
            key_file=_absolute_path(safety["key_file"]),
            artifact_root=_absolute_path(safety["artifact_root"]),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise MyBankCutoverCommandError("private plan is unavailable or invalid") from None


CutoverExecutor = Callable[
    [LoadedMyBankCutoverPlan, str],
    MyBankStatementCutoverReceipt,
]


def run_mybank_cutover_command(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    executor: Callable[..., MyBankStatementCutoverReceipt] | None = None,
) -> int:
    """Run an isolated preflight or an explicitly enabled production cutover."""

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--execute-production", action="store_true")
    args = parser.parse_args(argv)
    values = os.environ if environ is None else environ
    try:
        plan_path = _environment_path(values, "LEDGERBRIDGE_MYBANK_PRIVATE_PLAN")
        receipt_path = _environment_path(values, "LEDGERBRIDGE_MYBANK_PREFLIGHT_RECEIPT")
        database_url = _environment_text(values, "LEDGERBRIDGE_MYBANK_DATABASE_URL")
        database_target = _environment_text(values, "LEDGERBRIDGE_MYBANK_DATABASE_TARGET")
        deployed_revision = _environment_text(values, "LEDGERBRIDGE_DEPLOYED_REVISION")
        loaded = load_private_mybank_cutover_plan(plan_path)
        if deployed_revision != loaded.target_revision:
            raise MyBankCutoverCommandError("deployed revision gate is not satisfied")
        if executor is None:
            raise MyBankCutoverCommandError("cutover executor is unavailable")

        if args.preflight_only:
            if values.get("LEDGERBRIDGE_ENV") == "production" or database_target != "isolated":
                raise MyBankCutoverCommandError("isolated preflight gate is not satisfied")
            result = executor(loaded, database_url, commit=False)
            _validate_receipt(result, loaded)
            _write_private_json(
                receipt_path,
                {
                    "schema_version": "ledgerbridge.mybank-cutover-preflight.v1",
                    "plan_sha256": loaded.plan_sha256,
                    "target_revision": loaded.target_revision,
                    "transaction_count": result.transaction_count,
                    "candidate_delta": result.candidate_delta,
                    "replay_zero_delta": result.after_counts == result.replay_counts,
                    "fact_conflict_rejected": result.fact_conflict_rejected,
                },
            )
            print(
                "MYBANK_CUTOVER_PREFLIGHT_OK "
                f"transactions={result.transaction_count} candidates_added=0 "
                "replay_zero_delta=true conflict_rejected=true"
            )
            return 0

        if (
            values.get("LEDGERBRIDGE_ENV") != "production"
            or database_target != "production"
            or values.get("LEDGERBRIDGE_MYBANK_PRODUCTION_EXECUTION")
            != "execute-reviewed-cutover-v1"
        ):
            raise MyBankCutoverCommandError("production execution gate is not satisfied")
        _validate_preflight_receipt(receipt_path, loaded)
        result = executor(loaded, database_url, commit=True)
        _validate_receipt(result, loaded)
        print(
            "MYBANK_CUTOVER_PRODUCTION_OK "
            f"transactions={result.transaction_count} candidates_added=0 "
            "replay_zero_delta=true conflict_rejected=true"
        )
        return 0
    except MyBankCutoverCommandError:
        raise
    except (OSError, TypeError, ValueError):
        raise MyBankCutoverCommandError("cutover command gate is not satisfied") from None


def _read_private_json(path: Path) -> dict[str, Any]:
    if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
        raise ValueError
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_PLAN_BYTES:
        raise ValueError
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError
    return value


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise MyBankCutoverCommandError("private preflight receipt is unavailable")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise MyBankCutoverCommandError("private preflight receipt is unavailable")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".mybank-preflight-", dir=parent)
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


def _validate_receipt(
    receipt: MyBankStatementCutoverReceipt,
    loaded: LoadedMyBankCutoverPlan,
) -> None:
    if (
        receipt.transaction_count != loaded.cutover.expected_transaction_count
        or receipt.candidate_delta != 0
        or receipt.latest_pending_candidate_delta != 0
        or receipt.replay_created
        or receipt.registry_replay_created
        or receipt.after_counts != receipt.replay_counts
        or not receipt.fact_conflict_rejected
    ):
        raise MyBankCutoverCommandError("cutover acceptance receipt conflicts")


def _validate_preflight_receipt(
    path: Path,
    loaded: LoadedMyBankCutoverPlan,
) -> None:
    try:
        receipt = _read_private_json(path)
        _require_keys(
            receipt,
            {
                "schema_version",
                "plan_sha256",
                "target_revision",
                "transaction_count",
                "candidate_delta",
                "replay_zero_delta",
                "fact_conflict_rejected",
            },
        )
        if (
            receipt["schema_version"] != "ledgerbridge.mybank-cutover-preflight.v1"
            or receipt["plan_sha256"] != loaded.plan_sha256
            or receipt["target_revision"] != loaded.target_revision
            or receipt["transaction_count"] != loaded.cutover.expected_transaction_count
            or receipt["candidate_delta"] != 0
            or receipt["replay_zero_delta"] is not True
            or receipt["fact_conflict_rejected"] is not True
        ):
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise MyBankCutoverCommandError("production preflight receipt is invalid") from None


def _environment_text(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if not value or value != value.strip():
        raise MyBankCutoverCommandError("cutover command environment is incomplete")
    return value


def _environment_path(values: Mapping[str, str], name: str) -> Path:
    path = Path(_environment_text(values, name))
    if not path.is_absolute():
        raise MyBankCutoverCommandError("cutover command environment is incomplete")
    return path


def _plan_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _mapping(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError
    _require_keys(value, keys)
    return value


def _require_keys(value: dict[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise ValueError


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError
    return value


def _uuid(value: object) -> UUID:
    return UUID(_text(value))


def _absolute_path(value: object) -> Path:
    path = Path(_text(value))
    if not path.is_absolute():
        raise ValueError
    return path


def _alias(value: object) -> AccountAliasRegistration:
    alias = _mapping(value, {"alias_ref", "alias_kind", "alias_value"})
    return AccountAliasRegistration(
        alias_ref=_uuid(alias["alias_ref"]),
        alias_kind=_text(alias["alias_kind"]),
        alias_value=_text(alias["alias_value"]),
    )
