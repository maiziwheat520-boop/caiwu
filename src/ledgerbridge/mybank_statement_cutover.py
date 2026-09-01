"""Fail-closed orchestration for the private MYbank whole-statement cutover."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from ledgerbridge.account_registry import (
    AccountRegistryOperator,
    AccountRegistryPlan,
    AccountRegistryPlanResult,
)
from ledgerbridge.artifacts import ArtifactStore, PublishedArtifact
from ledgerbridge.bank_statement_persistence import (
    BankStatementImportContext,
    BankStatementImportResult,
    BankStatementImportService,
    BankStatementPersistenceError,
)
from ledgerbridge.crypto import ENVELOPE_ALGORITHM, ENVELOPE_SCHEMA, SecretStreamCipher
from ledgerbridge.encrypted_artifacts import (
    EncryptedArtifactPublication,
    EncryptedArtifactStore,
    EncryptedEnvelopeMetadata,
    EncryptedPublishedArtifact,
)
from ledgerbridge.file_key_provider import FileKeyProvider
from ledgerbridge.internal_read_contract import WorkloadPrincipal
from ledgerbridge.keyring import WrappedKey
from ledgerbridge.models import EntityType
from ledgerbridge.mybank_statement import MyBankStatement, parse_mybank_xlsx

_SCHEMA_REVISION = "20260830_0023"
_SUPPORTED_SCHEMA_REVISIONS = frozenset(
    {
        _SCHEMA_REVISION,
        "20260830_0024",
        "20260830_0025",
        "20260831_0026",
        "20260901_0027",
        "20260901_0028",
        "20260901_0029",
    }
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")
_CONFLICT_NAMESPACE = UUID("581080ab-fcd4-414f-af6a-f00ed1424f87")
_SOURCE_TIME_ZONE = ZoneInfo("Asia/Shanghai")


class MyBankStatementCutoverError(RuntimeError):
    """The cutover could not prove a complete, replay-safe result."""


class MyBankEvidenceMode(StrEnum):
    """Explicit evidence handling for an existing-account statement import."""

    CREATE_NEW = "CREATE_NEW"
    REUSE_EXISTING = "REUSE_EXISTING"


@dataclass(frozen=True, slots=True)
class ProductionCounts:
    evidence_objects: int
    encrypted_object_identities: int
    encrypted_blob_versions: int
    managed_accounts: int
    managed_account_lifecycles: int
    account_registry_operations: int
    managed_account_aliases: int
    account_business_unit_assignments: int
    fact_business_unit_allocation_sets: int
    fact_business_unit_allocation_items: int
    bank_statements: int
    bank_statement_transactions: int
    bank_statement_observations: int
    bank_statement_reviews: int
    candidates: int
    latest_pending_candidates: int
    audit_events: int
    journal_entries: int = 0
    postings: int = 0

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in self._values()):
            raise ValueError("production counts must be non-negative integers")

    def _values(self) -> tuple[int, ...]:
        return (
            self.evidence_objects,
            self.encrypted_object_identities,
            self.encrypted_blob_versions,
            self.managed_accounts,
            self.managed_account_lifecycles,
            self.account_registry_operations,
            self.managed_account_aliases,
            self.account_business_unit_assignments,
            self.fact_business_unit_allocation_sets,
            self.fact_business_unit_allocation_items,
            self.bank_statements,
            self.bank_statement_transactions,
            self.bank_statement_observations,
            self.bank_statement_reviews,
            self.candidates,
            self.latest_pending_candidates,
            self.audit_events,
            self.journal_entries,
            self.postings,
        )


@dataclass(frozen=True, slots=True)
class MyBankStatementCutoverPlan:
    source_path: Path
    expected_sha256: str
    expected_size: int
    evidence_ref: UUID
    entity_ref: UUID
    business_unit_ref: UUID
    registry_plan: AccountRegistryPlan
    account_suffix: str
    expected_transaction_count: int
    actor: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_path, Path) or not self.source_path.is_absolute():
            raise ValueError("source path must be absolute")
        if _DIGEST.fullmatch(self.expected_sha256) is None:
            raise ValueError("expected source digest is invalid")
        if type(self.expected_size) is not int or self.expected_size <= 0:
            raise ValueError("expected source size is invalid")
        if _ACCOUNT_SUFFIX.fullmatch(self.account_suffix) is None:
            raise ValueError("MYbank cutover account suffix is invalid")
        if type(self.expected_transaction_count) is not int or self.expected_transaction_count <= 0:
            raise ValueError("expected transaction count is invalid")
        if not self.actor.strip() or not self.reason.strip():
            raise ValueError("cutover audit context is invalid")
        if self.registry_plan.owner_entity_ref != self.entity_ref:
            raise ValueError("registry owner does not match accounting entity")
        if self.registry_plan.actor_ref != self.actor or self.registry_plan.reason != self.reason:
            raise ValueError("registry audit context does not match cutover")
        if len(self.registry_plan.accounts) != 1 or self.registry_plan.fact_allocations:
            raise ValueError("cutover registry plan must register exactly one account")
        account = self.registry_plan.accounts[0]
        if (
            account.admission_evidence_ref != self.evidence_ref
            or account.institution_code != "mybank"
            or account.account_suffix != self.account_suffix
        ):
            raise ValueError("registry account identity does not match cutover")
        if len(self.registry_plan.business_unit_assignments) > 1 or any(
            assignment.managed_account_ref != account.managed_account_ref
            or assignment.business_unit_id != self.business_unit_ref
            for assignment in self.registry_plan.business_unit_assignments
        ):
            raise ValueError("registry business unit assignment does not match cutover")

    @property
    def managed_account_ref(self) -> UUID:
        return self.registry_plan.accounts[0].managed_account_ref

    @property
    def owner_kind(self) -> EntityType:
        return self.registry_plan.expected_owner_kind


@dataclass(frozen=True, slots=True)
class MyBankExistingAccountStatementPlan:
    """One source-bound MYbank statement for an already registered account."""

    source_path: Path
    expected_sha256: str
    expected_size: int
    evidence_ref: UUID
    evidence_mode: MyBankEvidenceMode
    entity_ref: UUID
    business_unit_ref: UUID
    managed_account_ref: UUID
    account_suffix: str
    expected_transaction_count: int
    expected_owner_kind: EntityType
    actor: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_path, Path) or not self.source_path.is_absolute():
            raise ValueError("source path must be absolute")
        if _DIGEST.fullmatch(self.expected_sha256) is None:
            raise ValueError("expected source digest is invalid")
        if type(self.expected_size) is not int or self.expected_size <= 0:
            raise ValueError("expected source size is invalid")
        if not isinstance(self.evidence_mode, MyBankEvidenceMode):
            raise ValueError("existing-account evidence mode is invalid")
        if _ACCOUNT_SUFFIX.fullmatch(self.account_suffix) is None:
            raise ValueError("MYbank existing-account suffix is invalid")
        if type(self.expected_transaction_count) is not int or self.expected_transaction_count <= 0:
            raise ValueError("expected transaction count is invalid")
        if not isinstance(self.expected_owner_kind, EntityType):
            raise ValueError("expected accounting owner kind is invalid")
        if not self.actor.strip() or not self.reason.strip():
            raise ValueError("existing-account import audit context is invalid")

    @property
    def owner_kind(self) -> EntityType:
        return self.expected_owner_kind


@dataclass(frozen=True, slots=True)
class MyBankStatementCutoverGates:
    schema_revision: str
    backup_verified: bool
    isolated_restore_verified: bool
    rollback_ready: bool
    expected_before: ProductionCounts
    verify_fact_conflict: bool = False


@dataclass(frozen=True, slots=True)
class MyBankCutoverSafetyProof:
    """Paths to one encrypted pre-backup and its passed isolated-restore report."""

    backup_directory: Path
    restore_report: Path

    def __post_init__(self) -> None:
        if not self.backup_directory.is_absolute() or not self.restore_report.is_absolute():
            raise ValueError("cutover safety proof paths must be absolute")


@dataclass(frozen=True, slots=True)
class MyBankEvidenceDescriptor:
    evidence_ref: UUID
    entity_ref: UUID
    business_unit_ref: UUID
    plaintext_sha256: str
    plaintext_size: int
    declared_media_type: str
    display_name: str


@dataclass(frozen=True, slots=True)
class MyBankStatementCutoverReceipt:
    statement_ref: UUID
    evidence_ref: UUID
    managed_account_ref: UUID
    registry_created: bool
    created: bool
    registry_replay_created: bool
    replay_created: bool
    transaction_count: int
    review_status: str
    before_counts: ProductionCounts
    after_counts: ProductionCounts
    replay_counts: ProductionCounts
    fact_conflict_rejected: bool

    @property
    def candidate_delta(self) -> int:
        return self.after_counts.candidates - self.before_counts.candidates

    @property
    def latest_pending_candidate_delta(self) -> int:
        return (
            self.after_counts.latest_pending_candidates
            - self.before_counts.latest_pending_candidates
        )


class _StatementImporter(Protocol):
    def import_statement(
        self,
        statement: MyBankStatement,
        *,
        context: BankStatementImportContext,
    ) -> BankStatementImportResult: ...


class _AccountRegistrar(Protocol):
    def register(self, plan: AccountRegistryPlan) -> AccountRegistryPlanResult: ...


class _ExistingAccountAuthorizer(Protocol):
    def authorize(
        self,
        plan: MyBankExistingAccountStatementPlan,
        statement: MyBankStatement,
    ) -> None: ...


class MyBankExistingAccountStatementRunner:
    """Import one new statement without mutating its existing account registry facts."""

    def __init__(
        self,
        *,
        parser: Callable[..., MyBankStatement],
        evidence_writer: Callable[[Path, MyBankEvidenceDescriptor], None],
        account_authorizer: _ExistingAccountAuthorizer,
        statement_importer: _StatementImporter,
        counts_reader: Callable[[], ProductionCounts],
        schema_reader: Callable[[], str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._parser = parser
        self._evidence_writer = evidence_writer
        self._account_authorizer = account_authorizer
        self._statement_importer = statement_importer
        self._counts_reader = counts_reader
        self._schema_reader = schema_reader
        self._logger = logger or logging.getLogger(__name__)

    def run(
        self,
        plan: MyBankExistingAccountStatementPlan,
        *,
        gates: MyBankStatementCutoverGates,
    ) -> MyBankStatementCutoverReceipt:
        _require_gates(gates)
        if self._schema_reader is not None and self._schema_reader() != gates.schema_revision:
            raise MyBankStatementCutoverError("database schema gate is not satisfied")
        before = self._counts_reader()
        expected_completed = _expected_after_existing_account(gates.expected_before, plan)
        is_fresh = before == gates.expected_before
        is_completed_replay = before == expected_completed
        if not is_fresh and not is_completed_replay:
            raise MyBankStatementCutoverError("existing-account import preflight counts changed")

        try:
            statement = self._parser(
                plan.source_path,
                expected_sha256=plan.expected_sha256,
                managed_account_suffix=plan.account_suffix,
            )
        except Exception:
            self._logger.error("MYbank existing-account source validation failed")
            raise MyBankStatementCutoverError("private statement validation failed") from None
        if (
            statement.source_sha256 != plan.expected_sha256
            or statement.source_size != plan.expected_size
            or statement.institution_code != "mybank"
            or statement.account_suffix != plan.account_suffix
            or not statement.transactions
            or len(statement.transactions) != plan.expected_transaction_count
        ):
            raise MyBankStatementCutoverError("parsed statement source identity conflicts")

        descriptor = MyBankEvidenceDescriptor(
            evidence_ref=plan.evidence_ref,
            entity_ref=plan.entity_ref,
            business_unit_ref=plan.business_unit_ref,
            plaintext_sha256=statement.source_sha256,
            plaintext_size=statement.source_size,
            declared_media_type=statement.declared_media_type,
            display_name=f"mybank-statement-{statement.statement_ref}.xlsx",
        )
        context = BankStatementImportContext(
            owner_entity_ref=plan.entity_ref,
            managed_account_ref=plan.managed_account_ref,
            evidence_ref=plan.evidence_ref,
            actor=plan.actor,
            reason=plan.reason,
        )

        if is_completed_replay:
            try:
                self._account_authorizer.authorize(plan, statement)
                replay = self._statement_importer.import_statement(statement, context=context)
            except Exception:
                self._logger.error("MYbank existing-account completed replay failed")
                raise MyBankStatementCutoverError(
                    "completed existing-account statement replay conflict"
                ) from None
            if replay.created or not _receipt_matches(replay, statement, plan):
                raise MyBankStatementCutoverError(
                    "completed existing-account replay receipt conflicts"
                )
            replay_counts = self._counts_reader()
            if replay_counts != before:
                raise MyBankStatementCutoverError(
                    "completed existing-account replay changed counts"
                )
            return MyBankStatementCutoverReceipt(
                statement_ref=replay.statement_ref,
                evidence_ref=plan.evidence_ref,
                managed_account_ref=replay.managed_account_ref,
                registry_created=False,
                created=False,
                registry_replay_created=False,
                replay_created=False,
                transaction_count=replay.transaction_count,
                review_status=replay.review_status,
                before_counts=before,
                after_counts=replay_counts,
                replay_counts=replay_counts,
                fact_conflict_rejected=False,
            )

        try:
            self._evidence_writer(plan.source_path, descriptor)
            self._account_authorizer.authorize(plan, statement)
            first = self._statement_importer.import_statement(statement, context=context)
        except Exception:
            self._logger.error("MYbank existing-account persistence failed")
            raise MyBankStatementCutoverError(
                "existing-account statement import conflict"
            ) from None
        if not first.created or not _receipt_matches(first, statement, plan):
            raise MyBankStatementCutoverError("first existing-account statement receipt conflicts")
        after = self._counts_reader()
        if after != _expected_after_existing_account(before, plan):
            raise MyBankStatementCutoverError(
                "existing-account post-import count acceptance conflict"
            )

        try:
            self._account_authorizer.authorize(plan, statement)
            replay = self._statement_importer.import_statement(statement, context=context)
        except Exception:
            self._logger.error("MYbank existing-account replay failed")
            raise MyBankStatementCutoverError(
                "existing-account statement replay conflict"
            ) from None
        if (
            replay.created
            or not _receipt_matches(replay, statement, plan)
            or replay != _as_replay(first)
        ):
            raise MyBankStatementCutoverError("existing-account statement replay receipt conflicts")
        replay_counts = self._counts_reader()
        if replay_counts != after:
            raise MyBankStatementCutoverError("existing-account replay count conflict")
        return MyBankStatementCutoverReceipt(
            statement_ref=first.statement_ref,
            evidence_ref=plan.evidence_ref,
            managed_account_ref=first.managed_account_ref,
            registry_created=False,
            created=True,
            registry_replay_created=False,
            replay_created=False,
            transaction_count=first.transaction_count,
            review_status=first.review_status,
            before_counts=before,
            after_counts=after,
            replay_counts=replay_counts,
            fact_conflict_rejected=False,
        )


class MyBankStatementCutoverRunner:
    """Run one checked import and its mandatory idempotent replay."""

    def __init__(
        self,
        *,
        parser: Callable[..., MyBankStatement],
        evidence_writer: Callable[[Path, MyBankEvidenceDescriptor], None],
        account_registrar: _AccountRegistrar,
        statement_importer: _StatementImporter,
        counts_reader: Callable[[], ProductionCounts],
        schema_reader: Callable[[], str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._parser = parser
        self._evidence_writer = evidence_writer
        self._account_registrar = account_registrar
        self._statement_importer = statement_importer
        self._counts_reader = counts_reader
        self._schema_reader = schema_reader
        self._logger = logger or logging.getLogger(__name__)

    def run(
        self,
        plan: MyBankStatementCutoverPlan,
        *,
        gates: MyBankStatementCutoverGates,
    ) -> MyBankStatementCutoverReceipt:
        _require_gates(gates)
        if self._schema_reader is not None and self._schema_reader() != gates.schema_revision:
            raise MyBankStatementCutoverError("database schema gate is not satisfied")
        before = self._counts_reader()
        expected_completed = _expected_after(
            gates.expected_before,
            plan,
        )
        is_fresh = before == gates.expected_before
        is_completed_replay = before == expected_completed
        if not is_fresh and not is_completed_replay:
            raise MyBankStatementCutoverError("cutover preflight counts changed")

        try:
            statement = self._parser(
                plan.source_path,
                expected_sha256=plan.expected_sha256,
                managed_account_suffix=plan.account_suffix,
            )
        except Exception:
            self._logger.error("MYbank cutover source validation failed")
            raise MyBankStatementCutoverError("private statement validation failed") from None

        if (
            statement.source_sha256 != plan.expected_sha256
            or statement.source_size != plan.expected_size
            or statement.institution_code != "mybank"
            or statement.account_suffix != plan.account_suffix
            or not statement.transactions
            or len(statement.transactions) != plan.expected_transaction_count
        ):
            raise MyBankStatementCutoverError("parsed statement source identity conflicts")

        descriptor = MyBankEvidenceDescriptor(
            evidence_ref=plan.evidence_ref,
            entity_ref=plan.entity_ref,
            business_unit_ref=plan.business_unit_ref,
            plaintext_sha256=statement.source_sha256,
            plaintext_size=statement.source_size,
            declared_media_type=statement.declared_media_type,
            display_name=f"mybank-statement-{statement.statement_ref}.xlsx",
        )
        context = BankStatementImportContext(
            owner_entity_ref=plan.entity_ref,
            managed_account_ref=plan.managed_account_ref,
            evidence_ref=plan.evidence_ref,
            actor=plan.actor,
            reason=plan.reason,
        )

        if is_completed_replay:
            try:
                registry_replay = self._account_registrar.register(plan.registry_plan)
                replay = self._statement_importer.import_statement(statement, context=context)
            except Exception:
                self._logger.error("MYbank completed-package replay failed")
                raise MyBankStatementCutoverError("completed statement replay conflict") from None
            if (
                registry_replay.created
                or not _registry_result_matches(registry_replay, plan)
                or replay.created
                or not _receipt_matches(replay, statement, plan)
            ):
                raise MyBankStatementCutoverError("completed statement replay receipt conflicts")
            replay_counts = self._counts_reader()
            if replay_counts != before:
                raise MyBankStatementCutoverError("completed statement replay changed counts")
            return MyBankStatementCutoverReceipt(
                statement_ref=replay.statement_ref,
                evidence_ref=plan.evidence_ref,
                managed_account_ref=replay.managed_account_ref,
                registry_created=False,
                created=False,
                registry_replay_created=False,
                replay_created=False,
                transaction_count=replay.transaction_count,
                review_status=replay.review_status,
                before_counts=before,
                after_counts=replay_counts,
                replay_counts=replay_counts,
                fact_conflict_rejected=False,
            )

        try:
            self._evidence_writer(plan.source_path, descriptor)
            registry_first = self._account_registrar.register(plan.registry_plan)
            first = self._statement_importer.import_statement(statement, context=context)
        except Exception:
            self._logger.error("MYbank cutover persistence failed")
            raise MyBankStatementCutoverError("statement import conflict") from None
        if (
            not registry_first.created
            or not _registry_result_matches(registry_first, plan)
            or not first.created
            or not _receipt_matches(first, statement, plan)
        ):
            raise MyBankStatementCutoverError("first statement import receipt conflicts")

        after = self._counts_reader()
        expected_after = _expected_after(before, plan)
        if after != expected_after:
            raise MyBankStatementCutoverError("post-import count acceptance conflict")

        try:
            registry_replay = self._account_registrar.register(plan.registry_plan)
            replay = self._statement_importer.import_statement(statement, context=context)
        except Exception:
            self._logger.error("MYbank cutover replay failed")
            raise MyBankStatementCutoverError("statement replay conflict") from None
        if (
            registry_replay.created
            or not _registry_result_matches(registry_replay, plan)
            or registry_replay != _as_registry_replay(registry_first)
            or replay.created
            or not _receipt_matches(replay, statement, plan)
            or replay != _as_replay(first)
        ):
            raise MyBankStatementCutoverError("statement replay receipt conflicts")

        replay_counts = self._counts_reader()
        if replay_counts != after:
            raise MyBankStatementCutoverError("statement replay count conflict")

        self._logger.info(
            "MYbank whole-statement cutover passed: transactions=%d candidates_added=0",
            first.transaction_count,
        )
        return MyBankStatementCutoverReceipt(
            statement_ref=first.statement_ref,
            evidence_ref=plan.evidence_ref,
            managed_account_ref=first.managed_account_ref,
            registry_created=registry_first.created,
            created=first.created,
            registry_replay_created=registry_replay.created,
            replay_created=replay.created,
            transaction_count=first.transaction_count,
            review_status=first.review_status,
            before_counts=before,
            after_counts=after,
            replay_counts=replay_counts,
            fact_conflict_rejected=False,
        )


def _require_gates(gates: MyBankStatementCutoverGates) -> None:
    if gates.schema_revision not in _SUPPORTED_SCHEMA_REVISIONS:
        raise MyBankStatementCutoverError("cutover schema gate is not satisfied")
    if gates.backup_verified is not True:
        raise MyBankStatementCutoverError("cutover encrypted backup gate is not satisfied")
    if gates.isolated_restore_verified is not True:
        raise MyBankStatementCutoverError("cutover isolated restore gate is not satisfied")
    if gates.rollback_ready is not True:
        raise MyBankStatementCutoverError("cutover rollback gate is not satisfied")


def verify_mybank_cutover_safety_proof(
    proof: MyBankCutoverSafetyProof,
    *,
    gates: MyBankStatementCutoverGates,
) -> None:
    """Validate real backup artifacts and a count-bearing isolated-restore report."""

    _require_gates(gates)
    try:
        if proof.backup_directory.is_symlink() or proof.restore_report.is_symlink():
            raise ValueError
        backup = proof.backup_directory.resolve(strict=True)
        report_path = proof.restore_report.resolve(strict=True)
        if (
            not backup.is_dir()
            or not report_path.is_file()
            or report_path.parent != backup
            or not report_path.name.startswith("restore-rehearsal-")
            or report_path.suffix != ".json"
        ):
            raise ValueError
        sidecar = _read_proof_json(backup / "backup.json")
        expected_sidecar_fields = {
            "format",
            "created_at",
            "revision",
            "gpg_fingerprint",
            "ciphertext",
            "ciphertext_sha256",
            "postgres_image",
        }
        revision = sidecar.get("revision")
        ciphertext_digest = sidecar.get("ciphertext_sha256")
        if (
            set(sidecar) != expected_sidecar_fields
            or sidecar.get("format") != "ledgerbridge-encrypted-backup-v3"
            or not isinstance(revision, str)
            or _REVISION.fullmatch(revision) is None
            or sidecar.get("ciphertext") != "ledgerbridge-backup.tar.gpg"
            or not isinstance(ciphertext_digest, str)
            or _DIGEST.fullmatch(ciphertext_digest) is None
        ):
            raise ValueError
        ciphertext = backup / "ledgerbridge-backup.tar.gpg"
        checksum = backup / "SHA256SUMS"
        if (
            ciphertext.is_symlink()
            or checksum.is_symlink()
            or not ciphertext.is_file()
            or not checksum.is_file()
            or not hmac.compare_digest(_sha256_file(ciphertext), ciphertext_digest)
            or not hmac.compare_digest(
                checksum.read_text(encoding="utf-8"),
                f"{ciphertext_digest}  ledgerbridge-backup.tar.gpg\n",
            )
        ):
            raise ValueError

        report = _read_proof_json(report_path)
        compared_fields = report.get("database_compared_fields")
        source_metadata = report.get("source_database_metadata")
        restored_metadata = report.get("post_restore_database_observations")
        if (
            report.get("format") != "ledgerbridge-restore-rehearsal-v3"
            or report.get("status") != "passed"
            or report.get("backup") != backup.name
            or report.get("revision") != revision
            or report.get("source_format") != "v3"
            or report.get("production_unchanged") is not True
            or report.get("isolated_resources_removed") is not True
            or not isinstance(compared_fields, list)
            or "cutover_inventory" not in compared_fields
            or not isinstance(source_metadata, dict)
            or not isinstance(restored_metadata, dict)
        ):
            raise ValueError
        source_inventory = source_metadata.get("cutover_inventory")
        restored_inventory = restored_metadata.get("cutover_inventory")
        if source_inventory != restored_inventory:
            raise ValueError
        if (
            _counts_from_cutover_inventory(
                source_inventory,
                expected_schema_revision=gates.schema_revision,
            )
            != gates.expected_before
        ):
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise MyBankStatementCutoverError("cutover backup and restore proof is invalid") from None


def _read_proof_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _counts_from_cutover_inventory(
    value: object,
    *,
    expected_schema_revision: str,
) -> ProductionCounts:
    if not isinstance(value, dict) or set(value) != {
        "schema_revision",
        "candidate_total",
        "latest_pending_candidates",
        "audit_events",
        "row_counts",
    }:
        raise ValueError
    if value.get("schema_revision") != expected_schema_revision:
        raise ValueError
    row_counts = value.get("row_counts")
    if not isinstance(row_counts, dict):
        raise ValueError

    def count(name: str) -> int:
        observed = row_counts.get(name)
        if type(observed) is not int or observed < 0:
            raise ValueError
        return observed

    def scalar(name: str) -> int:
        observed = value.get(name)
        if type(observed) is not int or observed < 0:
            raise ValueError
        return observed

    return ProductionCounts(
        evidence_objects=count("evidence_object"),
        encrypted_object_identities=count("encrypted_object_identity"),
        encrypted_blob_versions=count("encrypted_blob_version"),
        managed_accounts=count("managed_account"),
        managed_account_lifecycles=count("managed_account_lifecycle"),
        account_registry_operations=count("account_registry_operation"),
        managed_account_aliases=count("managed_account_alias"),
        account_business_unit_assignments=count("account_business_unit_assignment"),
        fact_business_unit_allocation_sets=count("fact_business_unit_allocation_set"),
        fact_business_unit_allocation_items=count("fact_business_unit_allocation_item"),
        bank_statements=count("bank_statement"),
        bank_statement_transactions=count("bank_statement_transaction"),
        bank_statement_observations=count("bank_statement_observation"),
        bank_statement_reviews=count("bank_statement_review"),
        candidates=scalar("candidate_total"),
        latest_pending_candidates=scalar("latest_pending_candidates"),
        audit_events=scalar("audit_events"),
        journal_entries=count("journal_entry"),
        postings=count("posting"),
    )


def production_counts_from_cutover_inventory(
    value: object,
    *,
    expected_schema_revision: str,
) -> ProductionCounts:
    """Convert a verified backup inventory into the cutover count gate."""

    try:
        return _counts_from_cutover_inventory(
            value,
            expected_schema_revision=expected_schema_revision,
        )
    except (TypeError, ValueError):
        raise MyBankStatementCutoverError("cutover inventory is invalid") from None


def _expected_after(
    before: ProductionCounts,
    plan: MyBankStatementCutoverPlan,
) -> ProductionCounts:
    transaction_count = plan.expected_transaction_count
    alias_count = len(plan.registry_plan.accounts[0].aliases)
    assignment_count = len(plan.registry_plan.business_unit_assignments)
    return ProductionCounts(
        evidence_objects=before.evidence_objects + 1,
        encrypted_object_identities=before.encrypted_object_identities + 1,
        encrypted_blob_versions=before.encrypted_blob_versions + 1,
        managed_accounts=before.managed_accounts + 1,
        managed_account_lifecycles=before.managed_account_lifecycles + 1,
        account_registry_operations=before.account_registry_operations + 1,
        managed_account_aliases=before.managed_account_aliases + alias_count,
        account_business_unit_assignments=(
            before.account_business_unit_assignments + assignment_count
        ),
        fact_business_unit_allocation_sets=before.fact_business_unit_allocation_sets,
        fact_business_unit_allocation_items=before.fact_business_unit_allocation_items,
        bank_statements=before.bank_statements + 1,
        bank_statement_transactions=before.bank_statement_transactions + transaction_count,
        bank_statement_observations=before.bank_statement_observations + transaction_count,
        bank_statement_reviews=before.bank_statement_reviews + 1,
        candidates=before.candidates,
        latest_pending_candidates=before.latest_pending_candidates,
        audit_events=(
            before.audit_events + 7 + alias_count + assignment_count + 2 * transaction_count
        ),
        journal_entries=before.journal_entries,
        postings=before.postings,
    )


def _expected_after_existing_account(
    before: ProductionCounts,
    plan: MyBankExistingAccountStatementPlan,
) -> ProductionCounts:
    """Expected delta for the plan's explicit evidence mode and existing account."""

    transaction_count = plan.expected_transaction_count
    evidence_delta = int(plan.evidence_mode is MyBankEvidenceMode.CREATE_NEW)
    return ProductionCounts(
        evidence_objects=before.evidence_objects + evidence_delta,
        encrypted_object_identities=before.encrypted_object_identities + evidence_delta,
        encrypted_blob_versions=before.encrypted_blob_versions + evidence_delta,
        managed_accounts=before.managed_accounts,
        managed_account_lifecycles=before.managed_account_lifecycles,
        account_registry_operations=before.account_registry_operations,
        managed_account_aliases=before.managed_account_aliases,
        account_business_unit_assignments=before.account_business_unit_assignments,
        fact_business_unit_allocation_sets=before.fact_business_unit_allocation_sets,
        fact_business_unit_allocation_items=before.fact_business_unit_allocation_items,
        bank_statements=before.bank_statements + 1,
        bank_statement_transactions=before.bank_statement_transactions + transaction_count,
        bank_statement_observations=before.bank_statement_observations + transaction_count,
        bank_statement_reviews=before.bank_statement_reviews + 1,
        candidates=before.candidates,
        latest_pending_candidates=before.latest_pending_candidates,
        audit_events=before.audit_events + 2 + 2 * transaction_count + 2 * evidence_delta,
        journal_entries=before.journal_entries,
        postings=before.postings,
    )


def _registry_result_matches(
    result: AccountRegistryPlanResult,
    plan: MyBankStatementCutoverPlan,
) -> bool:
    return (
        result.operation_id == plan.registry_plan.operation_id
        and result.owner_entity_ref == plan.entity_ref
        and result.registry_revision == plan.registry_plan.expected_registry_revision + 1
        and result.managed_account_refs == (plan.managed_account_ref,)
    )


def _as_registry_replay(result: AccountRegistryPlanResult) -> AccountRegistryPlanResult:
    return AccountRegistryPlanResult(
        operation_id=result.operation_id,
        owner_entity_ref=result.owner_entity_ref,
        registry_revision=result.registry_revision,
        created=False,
        managed_account_refs=result.managed_account_refs,
    )


def _receipt_matches(
    result: BankStatementImportResult,
    statement: MyBankStatement,
    plan: MyBankStatementCutoverPlan | MyBankExistingAccountStatementPlan,
) -> bool:
    return (
        result.statement_ref == statement.statement_ref
        and result.managed_account_ref == plan.managed_account_ref
        and result.transaction_count == len(statement.transactions)
        and result.review_status == "PENDING"
        and result.statement_review_count == 1
        and result.accounting_candidate_count == 0
    )


def _as_replay(result: BankStatementImportResult) -> BankStatementImportResult:
    return BankStatementImportResult(
        statement_ref=result.statement_ref,
        managed_account_ref=result.managed_account_ref,
        created=False,
        transaction_count=result.transaction_count,
        review_status=result.review_status,
        statement_review_count=result.statement_review_count,
        accounting_candidate_count=result.accounting_candidate_count,
    )


def run_database_mybank_statement_cutover(
    engine: Engine,
    plan: MyBankStatementCutoverPlan,
    *,
    gates: MyBankStatementCutoverGates,
    safety_proof: MyBankCutoverSafetyProof,
    registry_principal: WorkloadPrincipal,
    key_file: Path,
    artifact_root: Path,
    logger: logging.Logger | None = None,
) -> MyBankStatementCutoverReceipt:
    """Compose the cutover over PostgreSQL and encrypted durable evidence storage."""

    verify_mybank_cutover_safety_proof(safety_proof, gates=gates)
    boundary = _DatabaseEvidenceBoundary(
        engine,
        plan=plan,
        key_file=key_file,
        artifact_root=artifact_root,
    )
    return _run_database_cutover(
        engine,
        plan,
        gates=gates,
        registry_principal=registry_principal,
        boundary=boundary,
        logger=logger,
    )


def run_transactional_database_mybank_statement_cutover(
    engine: Engine,
    plan: MyBankStatementCutoverPlan,
    *,
    gates: MyBankStatementCutoverGates,
    safety_proof: MyBankCutoverSafetyProof,
    registry_principal: WorkloadPrincipal,
    key_file: Path,
    artifact_root: Path,
    commit: bool,
    acceptance: Callable[[MyBankStatementCutoverReceipt, Connection], None] | None = None,
    logger: logging.Logger | None = None,
) -> MyBankStatementCutoverReceipt:
    """Run import, replay, and conflict acceptance under one outer transaction."""

    if type(commit) is not bool:
        raise MyBankStatementCutoverError("transactional cutover mode is invalid")
    verify_mybank_cutover_safety_proof(safety_proof, gates=gates)
    with engine.connect() as connection:
        transaction = connection.begin()
        boundary = _DatabaseEvidenceBoundary(
            connection,
            plan=plan,
            key_file=key_file,
            artifact_root=artifact_root,
        )
        try:
            receipt = _run_database_cutover(
                connection,
                plan,
                gates=gates,
                registry_principal=registry_principal,
                boundary=boundary,
                defer_publication_commit=True,
                logger=logger,
            )
            if acceptance is not None:
                acceptance(receipt, connection)
            if commit:
                transaction.commit()
                boundary.commit_publication()
            else:
                transaction.rollback()
                boundary.abort_publication()
            return receipt
        except BaseException:
            if transaction.is_active:
                transaction.rollback()
            boundary.abort_publication()
            raise


def run_transactional_database_mybank_existing_account_import(
    engine: Engine,
    plan: MyBankExistingAccountStatementPlan,
    *,
    gates: MyBankStatementCutoverGates,
    safety_proof: MyBankCutoverSafetyProof,
    key_file: Path,
    artifact_root: Path,
    commit: bool,
    acceptance: Callable[[MyBankStatementCutoverReceipt, Connection], None] | None = None,
    logger: logging.Logger | None = None,
) -> MyBankStatementCutoverReceipt:
    """Import one statement for an existing account under one rollback boundary."""

    if type(commit) is not bool:
        raise MyBankStatementCutoverError("transactional existing-account mode is invalid")
    verify_mybank_cutover_safety_proof(safety_proof, gates=gates)
    with engine.connect() as connection:
        transaction = connection.begin()
        boundary = _DatabaseEvidenceBoundary(
            connection,
            plan=plan,
            key_file=key_file,
            artifact_root=artifact_root,
        )
        try:
            runner = MyBankExistingAccountStatementRunner(
                parser=parse_mybank_xlsx,
                evidence_writer=boundary.write,
                account_authorizer=_DatabaseExistingAccountAuthorizer(boundary),
                statement_importer=_DatabaseStatementImporter(
                    boundary,
                    plan,
                    commit_publication=False,
                ),
                counts_reader=lambda: _read_production_counts(connection),
                schema_reader=lambda: _read_schema_revision(connection),
                logger=logger,
            )
            receipt = runner.run(plan, gates=replace(gates, verify_fact_conflict=False))
            if gates.verify_fact_conflict:
                statement = parse_mybank_xlsx(
                    plan.source_path,
                    expected_sha256=plan.expected_sha256,
                    managed_account_suffix=plan.account_suffix,
                )
                _run_database_fact_conflict_probe(
                    connection,
                    plan=plan,
                    statement=statement,
                )
                if _read_production_counts(connection) != receipt.after_counts:
                    raise MyBankStatementCutoverError(
                        "existing-account fact conflict changed database counts"
                    )
                receipt = replace(receipt, fact_conflict_rejected=True)
            if acceptance is not None:
                acceptance(receipt, connection)
            if commit:
                transaction.commit()
                boundary.commit_publication()
            else:
                transaction.rollback()
                boundary.abort_publication()
            return receipt
        except BaseException:
            if transaction.is_active:
                transaction.rollback()
            boundary.abort_publication()
            raise


def _run_database_cutover(
    bind: Engine | Connection,
    plan: MyBankStatementCutoverPlan,
    *,
    gates: MyBankStatementCutoverGates,
    registry_principal: WorkloadPrincipal,
    boundary: _DatabaseEvidenceBoundary,
    defer_publication_commit: bool = False,
    logger: logging.Logger | None = None,
) -> MyBankStatementCutoverReceipt:
    runner = MyBankStatementCutoverRunner(
        parser=parse_mybank_xlsx,
        evidence_writer=boundary.write,
        account_registrar=_DatabaseAccountRegistrar(boundary, registry_principal),
        statement_importer=_DatabaseStatementImporter(
            boundary,
            plan,
            commit_publication=not defer_publication_commit,
        ),
        counts_reader=lambda: _read_production_counts(bind),
        schema_reader=lambda: _read_schema_revision(bind),
        logger=logger,
    )
    receipt = runner.run(plan, gates=replace(gates, verify_fact_conflict=False))
    if not gates.verify_fact_conflict:
        return receipt
    _run_database_fact_conflict_probe(
        bind,
        plan=plan,
        statement=parse_mybank_xlsx(
            plan.source_path,
            expected_sha256=plan.expected_sha256,
            managed_account_suffix=plan.account_suffix,
        ),
    )
    if _read_production_counts(bind) != receipt.after_counts:
        raise MyBankStatementCutoverError("fact conflict changed database counts")
    return replace(receipt, fact_conflict_rejected=True)


class _DatabaseExistingAccountAuthorizer:
    def __init__(self, boundary: _DatabaseEvidenceBoundary) -> None:
        self._boundary = boundary

    def authorize(
        self,
        plan: MyBankExistingAccountStatementPlan,
        statement: MyBankStatement,
    ) -> None:
        session = self._boundary.ensure_session()
        try:
            _require_existing_account_identity(session, plan, statement)
            if plan.evidence_mode is MyBankEvidenceMode.REUSE_EXISTING:
                self._boundary.require_reusable_evidence(
                    session,
                    MyBankEvidenceDescriptor(
                        evidence_ref=plan.evidence_ref,
                        entity_ref=plan.entity_ref,
                        business_unit_ref=plan.business_unit_ref,
                        plaintext_sha256=plan.expected_sha256,
                        plaintext_size=plan.expected_size,
                        declared_media_type=statement.declared_media_type,
                        display_name=f"mybank-statement-{statement.statement_ref}.xlsx",
                    ),
                )
            self._boundary.set_statement_expectation(self._boundary.evidence_staged)
        except BaseException:
            try:
                session.rollback()
            finally:
                try:
                    session.close()
                finally:
                    self._boundary.clear(session)
            raise


class _DatabaseStatementImporter:
    def __init__(
        self,
        boundary: _DatabaseEvidenceBoundary,
        plan: MyBankStatementCutoverPlan | MyBankExistingAccountStatementPlan,
        *,
        commit_publication: bool = True,
    ) -> None:
        self._boundary = boundary
        self._plan = plan
        self._commit_publication = commit_publication

    def import_statement(
        self,
        statement: MyBankStatement,
        *,
        context: BankStatementImportContext,
    ) -> BankStatementImportResult:
        session = self._boundary.session()
        try:
            if isinstance(self._plan, MyBankExistingAccountStatementPlan):
                _require_existing_account_identity(session, self._plan, statement)
            else:
                _require_import_identity(session, self._plan, statement, context)
            result = BankStatementImportService(lambda: session).import_statement(
                statement,
                context=context,
                session=session,
            )
            expected_created = self._boundary.take_statement_expectation()
            if result.created is not expected_created or not _receipt_matches(
                result, statement, self._plan
            ):
                raise MyBankStatementCutoverError("statement import receipt conflicts")
            session.commit()
            if self._commit_publication:
                self._boundary.commit_publication()
            return result
        except BaseException:
            try:
                session.rollback()
            finally:
                try:
                    session.close()
                finally:
                    self._boundary.clear(session)
            raise
        finally:
            session.close()


class _DatabaseAccountRegistrar:
    def __init__(
        self,
        boundary: _DatabaseEvidenceBoundary,
        principal: WorkloadPrincipal,
    ) -> None:
        self._boundary = boundary
        self._principal = principal

    def register(self, plan: AccountRegistryPlan) -> AccountRegistryPlanResult:
        session = self._boundary.ensure_session()
        expected_created = self._boundary.evidence_staged
        try:
            result = AccountRegistryOperator(lambda: session).apply(
                plan,
                principal=self._principal,
                session=session,
            )
            if (
                result.operation_id != plan.operation_id
                or result.created is not expected_created
                or result.owner_entity_ref != plan.owner_entity_ref
                or result.registry_revision != plan.expected_registry_revision + 1
                or result.managed_account_refs
                != tuple(account.managed_account_ref for account in plan.accounts)
            ):
                raise MyBankStatementCutoverError("registry plan receipt conflicts")
            self._boundary.set_statement_expectation(expected_created)
            return result
        except BaseException:
            try:
                session.rollback()
            finally:
                try:
                    session.close()
                finally:
                    self._boundary.clear(session)
            raise


class _DatabaseEvidenceBoundary:
    """Stage Evidence rows in the transaction committed by the statement service."""

    def __init__(
        self,
        engine: Engine | Connection,
        *,
        plan: MyBankStatementCutoverPlan | MyBankExistingAccountStatementPlan,
        key_file: Path,
        artifact_root: Path,
    ) -> None:
        self._engine = engine
        self._plan = plan
        provider = FileKeyProvider(key_file)
        cipher = SecretStreamCipher(provider)
        cipher.self_test()
        durable = ArtifactStore(
            artifact_root,
            max_bytes=150 * 1024 * 1024,
            total_max_bytes=10 * 1024 * 1024 * 1024,
            staging_max_bytes=512 * 1024 * 1024,
        )
        self._store = EncryptedArtifactStore(
            durable,
            cipher,
            max_plaintext_bytes=134_217_728,
        )
        self._pending_session: Session | None = None
        self._pending_publication: EncryptedArtifactPublication | None = None
        self._evidence_staged = False
        self._expected_statement_created: bool | None = None

    def write(self, source_path: Path, evidence: MyBankEvidenceDescriptor) -> None:
        if self._pending_session is not None:
            raise MyBankStatementCutoverError("evidence transaction is already active")
        if self._pending_publication is not None:
            raise MyBankStatementCutoverError("evidence publication is already active")
        session: Session | None = None
        try:
            if (
                isinstance(self._plan, MyBankExistingAccountStatementPlan)
                and self._plan.evidence_mode is MyBankEvidenceMode.REUSE_EXISTING
            ):
                session = _new_session(self._engine)
                _require_unique_scope(session, evidence, owner_kind=self._plan.owner_kind)
                _require_reusable_existing_evidence(session, self._store, evidence)
                self._pending_session = session
                self._evidence_staged = True
                return
            with source_path.open("rb") as source:
                publication = self._store.begin_publication(source)
            self._pending_publication = publication
            published = publication.artifact
            metadata = self._store.envelope_metadata(published)
            with self._store.open_verified(published, envelope_metadata=metadata) as verified:
                verified_digest = hashlib.sha256(verified.read()).hexdigest()
            if (
                published.plaintext_sha256.hex() != evidence.plaintext_sha256
                or published.plaintext_size != evidence.plaintext_size
                or verified_digest != evidence.plaintext_sha256
            ):
                raise MyBankStatementCutoverError("encrypted evidence source identity conflicts")

            session = _new_session(self._engine)
            _require_unique_scope(session, evidence, owner_kind=self._plan.owner_kind)
            _require_new_evidence(session, evidence)
            evidence_audit = _append_audit(
                session,
                actor=self._plan.actor,
                action="evidence.object.create",
                reason=self._plan.reason,
                payload={
                    "evidence_ref": str(evidence.evidence_ref),
                    "entity_id": str(evidence.entity_ref),
                    "business_unit_id": str(evidence.business_unit_ref),
                },
            )
            session.execute(
                text(
                    "INSERT INTO public.evidence_object "
                    "(evidence_ref, entity_id, business_unit_id, media_type, display_name, "
                    "plaintext_sha256, plaintext_size, audit_event_id) VALUES "
                    "(:evidence, :entity, :unit, :media_type, :display_name, "
                    ":digest, :size, :audit)"
                ),
                {
                    "evidence": evidence.evidence_ref,
                    "entity": evidence.entity_ref,
                    "unit": evidence.business_unit_ref,
                    "media_type": evidence.declared_media_type,
                    "display_name": evidence.display_name,
                    "digest": bytes.fromhex(evidence.plaintext_sha256),
                    "size": evidence.plaintext_size,
                    "audit": evidence_audit,
                },
            )
            blob_ref = UUID(
                bytes=hashlib.sha256(evidence.evidence_ref.bytes + b"blob-v1").digest()[:16]
            )
            blob_payload: dict[str, object] = {
                "rotation_mode": "GENESIS",
                "blob_ref": str(blob_ref),
                "evidence_ref": str(evidence.evidence_ref),
                "predecessor_blob_ref": None,
                "object_ref": published.object_ref,
                "ciphertext_sha256": published.ciphertext.sha256.hex(),
                "ciphertext_size": published.ciphertext.byte_size,
                "storage_key": published.storage_key,
                "envelope_schema": ENVELOPE_SCHEMA,
                "algorithm": ENVELOPE_ALGORITHM,
                "chunk_size": metadata.chunk_size,
                "stream_header": metadata.stream_header.hex(),
                "wrapped_key_generation": metadata.wrapped_key.generation,
                "wrapped_key_nonce": metadata.wrapped_key.nonce.hex(),
                "wrapped_key_ciphertext": metadata.wrapped_key.ciphertext.hex(),
                "purpose": "ledgerbridge-artifact-v2",
            }
            blob_audit = _append_audit(
                session,
                actor=self._plan.actor,
                action="evidence.blob.version",
                reason=self._plan.reason,
                payload=blob_payload,
            )
            session.execute(
                text(
                    "INSERT INTO public.encrypted_object_identity(object_ref, evidence_ref) "
                    "VALUES (:object_ref, :evidence)"
                ),
                {"object_ref": published.object_ref, "evidence": evidence.evidence_ref},
            )
            session.execute(
                text(
                    "INSERT INTO public.encrypted_blob_version "
                    "(blob_ref, evidence_ref, object_ref, ciphertext_sha256, ciphertext_size, "
                    "storage_key, envelope_schema, algorithm, chunk_size, stream_header, "
                    "wrapped_key_generation, wrapped_key_nonce, wrapped_key_ciphertext, purpose, "
                    "audit_event_id) VALUES "
                    "(:blob, :evidence, :object_ref, :ciphertext_sha, :ciphertext_size, "
                    ":storage_key, :envelope_schema, :algorithm, :chunk_size, :stream_header, "
                    ":generation, :nonce, :wrapped, :purpose, :audit)"
                ),
                {
                    "blob": blob_ref,
                    "evidence": evidence.evidence_ref,
                    "object_ref": published.object_ref,
                    "ciphertext_sha": published.ciphertext.sha256,
                    "ciphertext_size": published.ciphertext.byte_size,
                    "storage_key": published.storage_key,
                    "envelope_schema": ENVELOPE_SCHEMA,
                    "algorithm": ENVELOPE_ALGORITHM,
                    "chunk_size": metadata.chunk_size,
                    "stream_header": metadata.stream_header,
                    "generation": metadata.wrapped_key.generation,
                    "nonce": metadata.wrapped_key.nonce,
                    "wrapped": metadata.wrapped_key.ciphertext,
                    "purpose": "ledgerbridge-artifact-v2",
                    "audit": blob_audit,
                },
            )
        except BaseException:
            if session is not None:
                try:
                    session.rollback()
                finally:
                    try:
                        session.close()
                    finally:
                        self._abort_publication()
            else:
                self._abort_publication()
            raise
        assert session is not None
        self._pending_session = session
        self._evidence_staged = True

    def session(self) -> Session:
        pending = self._pending_session
        if pending is not None:
            self._pending_session = None
            self._evidence_staged = False
            return pending
        return _new_session(self._engine)

    def ensure_session(self) -> Session:
        pending = self._pending_session
        if pending is None:
            pending = _new_session(self._engine)
            self._pending_session = pending
        return pending

    def require_reusable_evidence(
        self,
        session: Session,
        evidence: MyBankEvidenceDescriptor,
    ) -> None:
        _require_reusable_existing_evidence(session, self._store, evidence)

    def clear(self, session: Session) -> None:
        if self._pending_session is session:
            self._pending_session = None
        self._evidence_staged = False
        self._expected_statement_created = None
        self._abort_publication()

    def commit_publication(self) -> None:
        publication = self._pending_publication
        if publication is not None:
            publication.commit()
            self._pending_publication = None

    def _abort_publication(self) -> None:
        publication = self._pending_publication
        if publication is not None:
            publication.abort()
            self._pending_publication = None

    def abort_publication(self) -> None:
        self._abort_publication()

    @property
    def evidence_staged(self) -> bool:
        return self._evidence_staged

    def set_statement_expectation(self, created: bool) -> None:
        if self._expected_statement_created is not None:
            raise MyBankStatementCutoverError("statement transaction expectation conflicts")
        self._expected_statement_created = created

    def take_statement_expectation(self) -> bool:
        created = self._expected_statement_created
        self._expected_statement_created = None
        if created is None:
            raise MyBankStatementCutoverError("statement transaction is not registry-authorized")
        return created


def _require_import_identity(
    session: Session,
    plan: MyBankStatementCutoverPlan,
    statement: MyBankStatement,
    context: BankStatementImportContext,
) -> None:
    """Recheck the explicit owner, account, and evidence identity on every import."""

    registered = plan.registry_plan.accounts[0]
    if (
        context.owner_entity_ref != plan.entity_ref
        or context.managed_account_ref != plan.managed_account_ref
        or context.evidence_ref != plan.evidence_ref
        or statement.institution_code != "mybank"
        or statement.account_suffix != plan.account_suffix
        or statement.source_sha256 != plan.expected_sha256
        or statement.source_size != plan.expected_size
    ):
        raise MyBankStatementCutoverError("explicit import identity conflicts")

    _require_unique_scope(
        session,
        MyBankEvidenceDescriptor(
            evidence_ref=plan.evidence_ref,
            entity_ref=plan.entity_ref,
            business_unit_ref=plan.business_unit_ref,
            plaintext_sha256=plan.expected_sha256,
            plaintext_size=plan.expected_size,
            declared_media_type=statement.declared_media_type,
            display_name=f"mybank-statement-{statement.statement_ref}.xlsx",
        ),
        owner_kind=plan.owner_kind,
    )
    evidence = (
        session.execute(
            text(
                "SELECT evidence_ref, entity_id, business_unit_id, media_type, "
                "plaintext_sha256, plaintext_size FROM public.evidence_object "
                "WHERE evidence_ref=:evidence"
            ),
            {"evidence": plan.evidence_ref},
        )
        .mappings()
        .one_or_none()
    )
    if evidence is None or (
        evidence["entity_id"] != plan.entity_ref
        or evidence["business_unit_id"] != plan.business_unit_ref
        or evidence["media_type"] != statement.declared_media_type
        or bytes(evidence["plaintext_sha256"]).hex() != plan.expected_sha256
        or int(evidence["plaintext_size"]) != plan.expected_size
    ):
        raise MyBankStatementCutoverError("statement evidence binding conflicts")

    accounts = (
        session.execute(
            text(
                "SELECT ma.managed_account_ref, ma.entity_id, ma.account_key, "
                "ma.institution_code, ma.account_suffix, ma.owner_ref, ma.owner_kind, "
                "ma.account_kind, ma.admission_evidence_ref, "
                "latest.status AS lifecycle_status "
                "FROM public.managed_account ma JOIN LATERAL "
                "(SELECT status FROM public.managed_account_lifecycle lifecycle "
                "WHERE lifecycle.managed_account_ref=ma.managed_account_ref "
                "ORDER BY lifecycle.revision DESC LIMIT 1) latest ON true "
                "WHERE ma.managed_account_ref=:account OR "
                "(ma.entity_id=:entity AND ma.account_key=:account_key)"
            ),
            {
                "account": plan.managed_account_ref,
                "entity": plan.entity_ref,
                "account_key": registered.account_key,
            },
        )
        .mappings()
        .all()
    )
    if not accounts:
        raise MyBankStatementCutoverError("managed account is not registered")
    if len(accounts) != 1:
        raise MyBankStatementCutoverError("managed account identity is not unique")
    account = accounts[0]
    if (
        account["managed_account_ref"] != plan.managed_account_ref
        or account["entity_id"] != plan.entity_ref
        or account["account_key"] != registered.account_key
        or account["institution_code"] != "mybank"
        or account["account_suffix"] != plan.account_suffix
        or account["owner_ref"] != str(plan.entity_ref)
        or account["owner_kind"] != _legacy_owner_kind(plan.owner_kind)
        or account["account_kind"] != registered.account_kind
        or account["admission_evidence_ref"] != plan.evidence_ref
        or account["lifecycle_status"] != "ACTIVE"
    ):
        raise MyBankStatementCutoverError("managed account conflicts with explicit owner identity")


def _require_existing_account_identity(
    session: Session,
    plan: MyBankExistingAccountStatementPlan,
    statement: MyBankStatement,
) -> None:
    """Bind a new statement to one ACTIVE account and one covering unit assignment."""

    if (
        statement.institution_code != "mybank"
        or statement.account_suffix != plan.account_suffix
        or statement.source_sha256 != plan.expected_sha256
        or statement.source_size != plan.expected_size
        or not statement.transactions
        or len(statement.transactions) != plan.expected_transaction_count
    ):
        raise MyBankStatementCutoverError("existing-account statement identity conflicts")
    descriptor = MyBankEvidenceDescriptor(
        evidence_ref=plan.evidence_ref,
        entity_ref=plan.entity_ref,
        business_unit_ref=plan.business_unit_ref,
        plaintext_sha256=plan.expected_sha256,
        plaintext_size=plan.expected_size,
        declared_media_type=statement.declared_media_type,
        display_name=f"mybank-statement-{statement.statement_ref}.xlsx",
    )
    _require_unique_scope(session, descriptor, owner_kind=plan.owner_kind)
    _require_existing_evidence_binding(session, descriptor)

    period_start = min(
        item.occurred_at.astimezone(_SOURCE_TIME_ZONE).date() for item in statement.transactions
    )
    period_end = max(
        item.occurred_at.astimezone(_SOURCE_TIME_ZONE).date() for item in statement.transactions
    )
    account = (
        session.execute(
            text(
                "SELECT ma.managed_account_ref, ma.entity_id, ma.institution_code, "
                "ma.account_suffix, ma.owner_ref, ma.owner_kind, "
                "latest.status AS lifecycle_status, "
                "(SELECT count(*) FROM public.account_business_unit_assignment assignment "
                "WHERE assignment.owner_entity_id=:entity "
                "AND assignment.managed_account_ref=ma.managed_account_ref "
                "AND assignment.effective_from <= :period_start "
                "AND (assignment.effective_to IS NULL "
                "OR assignment.effective_to > :period_end)) AS assignment_count "
                ",(SELECT count(*) FROM public.account_business_unit_assignment assignment "
                "WHERE assignment.owner_entity_id=:entity "
                "AND assignment.managed_account_ref=ma.managed_account_ref "
                "AND assignment.business_unit_id=:unit "
                "AND assignment.effective_from <= :period_start "
                "AND (assignment.effective_to IS NULL "
                "OR assignment.effective_to > :period_end)) AS matching_assignment_count "
                "FROM public.managed_account ma JOIN LATERAL "
                "(SELECT status FROM public.managed_account_lifecycle lifecycle "
                "WHERE lifecycle.managed_account_ref=ma.managed_account_ref "
                "ORDER BY lifecycle.revision DESC LIMIT 1) latest ON true "
                "WHERE ma.managed_account_ref=:account AND ma.entity_id=:entity"
            ),
            {
                "account": plan.managed_account_ref,
                "entity": plan.entity_ref,
                "unit": plan.business_unit_ref,
                "period_start": period_start,
                "period_end": period_end,
            },
        )
        .mappings()
        .one_or_none()
    )
    if account is None:
        raise MyBankStatementCutoverError("managed account is not registered")
    if (
        account["managed_account_ref"] != plan.managed_account_ref
        or account["entity_id"] != plan.entity_ref
        or account["institution_code"] != "mybank"
        or account["account_suffix"] != plan.account_suffix
        or account["owner_ref"] != str(plan.entity_ref)
        or account["owner_kind"] != _legacy_owner_kind(plan.owner_kind)
    ):
        raise MyBankStatementCutoverError("managed account conflicts with explicit owner identity")
    if account["lifecycle_status"] != "ACTIVE":
        raise MyBankStatementCutoverError("managed account must be ACTIVE")
    if int(account["assignment_count"]) != 1 or int(account["matching_assignment_count"]) != 1:
        raise MyBankStatementCutoverError(
            "managed account requires one covering business-unit assignment"
        )


def _run_database_fact_conflict_probe(
    engine: Engine | Connection,
    *,
    plan: MyBankStatementCutoverPlan | MyBankExistingAccountStatementPlan,
    statement: MyBankStatement,
) -> None:
    """Prove a changed fact for an overlapping serial is rejected and rolled back."""

    first = statement.transactions[0]
    probe_name = f"{statement.statement_ref}:overlap-fact-conflict-v1"
    probe_evidence_ref = uuid5(_CONFLICT_NAMESPACE, f"evidence:{probe_name}")
    probe_source_digest = hashlib.sha256(
        bytes.fromhex(statement.source_sha256) + b":overlap-fact-conflict-v1"
    ).hexdigest()
    probe_statement = replace(
        statement,
        statement_ref=uuid5(_CONFLICT_NAMESPACE, f"statement:{probe_name}"),
        source_sha256=probe_source_digest,
        transactions=(
            replace(
                first,
                source_event_ref=uuid5(_CONFLICT_NAMESPACE, f"event:{probe_name}"),
                source_row_sha256=hashlib.sha256(
                    bytes.fromhex(first.source_row_sha256) + b":fact-conflict-v1"
                ).hexdigest(),
                amount_minor=first.amount_minor + 1,
            ),
        ),
    )
    session = _new_session(engine)
    try:
        try:
            with session.begin_nested():
                audit_ref = _append_audit(
                    session,
                    actor=plan.actor,
                    action="evidence.object.create",
                    reason=plan.reason,
                    payload={
                        "evidence_ref": str(probe_evidence_ref),
                        "entity_id": str(plan.entity_ref),
                        "business_unit_id": str(plan.business_unit_ref),
                        "probe": "overlap-fact-conflict-v1",
                    },
                )
                session.execute(
                    text(
                        "INSERT INTO public.evidence_object "
                        "(evidence_ref, entity_id, business_unit_id, media_type, display_name, "
                        "plaintext_sha256, plaintext_size, audit_event_id) VALUES "
                        "(:evidence, :entity, :unit, :media_type, :display_name, "
                        ":digest, :size, :audit)"
                    ),
                    {
                        "evidence": probe_evidence_ref,
                        "entity": plan.entity_ref,
                        "unit": plan.business_unit_ref,
                        "media_type": statement.declared_media_type,
                        "display_name": (
                            f"mybank-conflict-probe-{probe_statement.statement_ref}.xlsx"
                        ),
                        "digest": bytes.fromhex(probe_source_digest),
                        "size": probe_statement.source_size,
                        "audit": audit_ref,
                    },
                )
                context = BankStatementImportContext(
                    owner_entity_ref=plan.entity_ref,
                    managed_account_ref=plan.managed_account_ref,
                    evidence_ref=probe_evidence_ref,
                    actor=plan.actor,
                    reason=plan.reason,
                )
                BankStatementImportService(lambda: session).import_statement(
                    probe_statement,
                    context=context,
                    session=session,
                )
                raise MyBankStatementCutoverError("overlapping fact conflict was accepted")
        except MyBankStatementCutoverError:
            raise
        except BankStatementPersistenceError as exc:
            database_error = exc.__cause__
            rendered = str(getattr(database_error, "orig", database_error))
            if "overlapping bank statement transaction conflicts with fact" not in rendered:
                raise MyBankStatementCutoverError(
                    "overlapping fact probe failed for an unexpected reason"
                ) from exc
    finally:
        session.close()


def _require_unique_scope(
    session: Session,
    evidence: MyBankEvidenceDescriptor,
    *,
    owner_kind: EntityType,
) -> None:
    scope = (
        session.execute(
            text(
                "SELECT e.id AS entity_id, e.entity_type, b.id AS business_unit_id "
                "FROM public.entity e JOIN public.business_unit b ON b.entity_id=e.id "
                "WHERE e.id=:entity AND b.id=:unit AND b.retired_at IS NULL"
            ),
            {"entity": evidence.entity_ref, "unit": evidence.business_unit_ref},
        )
        .mappings()
        .one_or_none()
    )
    if scope is None:
        raise MyBankStatementCutoverError("accounting scope binding conflicts")
    if scope["entity_type"] != owner_kind.value:
        raise MyBankStatementCutoverError("accounting owner kind conflicts with entity")


def _legacy_owner_kind(owner_kind: EntityType) -> str:
    return "PERSONAL" if owner_kind is EntityType.PERSON else "COMPANY"


def _require_new_evidence(session: Session, evidence: MyBankEvidenceDescriptor) -> None:
    matches = (
        session.execute(
            text(
                "SELECT evidence_ref, entity_id, business_unit_id, media_type, "
                "plaintext_sha256, plaintext_size FROM public.evidence_object "
                "WHERE evidence_ref=:evidence OR "
                "(plaintext_sha256=:digest AND plaintext_size=:size)"
            ),
            {
                "evidence": evidence.evidence_ref,
                "digest": bytes.fromhex(evidence.plaintext_sha256),
                "size": evidence.plaintext_size,
            },
        )
        .mappings()
        .all()
    )
    if matches:
        raise MyBankStatementCutoverError("statement evidence already exists or conflicts")


def _require_existing_evidence_binding(
    session: Session,
    evidence: MyBankEvidenceDescriptor,
) -> None:
    row = (
        session.execute(
            text(
                "SELECT evidence_ref, entity_id, business_unit_id, media_type, "
                "plaintext_sha256, plaintext_size FROM public.evidence_object "
                "WHERE evidence_ref=:evidence"
            ),
            {"evidence": evidence.evidence_ref},
        )
        .mappings()
        .one_or_none()
    )
    if row is None or (
        row["evidence_ref"] != evidence.evidence_ref
        or row["entity_id"] != evidence.entity_ref
        or row["business_unit_id"] != evidence.business_unit_ref
        or row["media_type"] != evidence.declared_media_type
        or bytes(row["plaintext_sha256"]).hex() != evidence.plaintext_sha256
        or int(row["plaintext_size"]) != evidence.plaintext_size
    ):
        raise MyBankStatementCutoverError("statement evidence binding conflicts")


def _require_reusable_existing_evidence(
    session: Session,
    store: EncryptedArtifactStore,
    evidence: MyBankEvidenceDescriptor,
) -> None:
    """Prove the exact Evidence and its single active encrypted lineage are reusable."""

    _require_existing_evidence_binding(session, evidence)
    rows = (
        session.execute(
            text(
                "SELECT identity.object_ref, identity.evidence_ref AS identity_evidence_ref, "
                "blob.evidence_ref AS blob_evidence_ref, blob.ciphertext_sha256, "
                "blob.ciphertext_size, blob.storage_key, blob.envelope_schema, blob.algorithm, "
                "blob.chunk_size, blob.stream_header, blob.wrapped_key_generation, "
                "blob.wrapped_key_nonce, blob.wrapped_key_ciphertext, blob.purpose "
                "FROM public.encrypted_object_identity identity "
                "JOIN public.encrypted_blob_version blob "
                "ON blob.object_ref=identity.object_ref "
                "AND blob.evidence_ref=identity.evidence_ref "
                "WHERE identity.evidence_ref=:evidence AND NOT EXISTS ("
                "SELECT 1 FROM public.encrypted_blob_version child "
                "WHERE child.predecessor_blob_ref=blob.blob_ref)"
            ),
            {"evidence": evidence.evidence_ref},
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        raise MyBankStatementCutoverError("reusable evidence encrypted lineage conflicts")
    row = rows[0]
    if (
        row["identity_evidence_ref"] != evidence.evidence_ref
        or row["blob_evidence_ref"] != evidence.evidence_ref
        or row["envelope_schema"] != ENVELOPE_SCHEMA
        or row["algorithm"] != ENVELOPE_ALGORITHM
        or row["purpose"] != "ledgerbridge-artifact-v2"
    ):
        raise MyBankStatementCutoverError("reusable evidence encrypted metadata conflicts")
    artifact = EncryptedPublishedArtifact(
        object_ref=row["object_ref"],
        plaintext_sha256=bytes.fromhex(evidence.plaintext_sha256),
        plaintext_size=evidence.plaintext_size,
        ciphertext=PublishedArtifact(
            sha256=bytes(row["ciphertext_sha256"]),
            byte_size=int(row["ciphertext_size"]),
            storage_key=row["storage_key"],
            created=False,
        ),
    )
    metadata = EncryptedEnvelopeMetadata(
        chunk_size=int(row["chunk_size"]),
        stream_header=bytes(row["stream_header"]),
        wrapped_key=WrappedKey(
            generation=row["wrapped_key_generation"],
            nonce=bytes(row["wrapped_key_nonce"]),
            ciphertext=bytes(row["wrapped_key_ciphertext"]),
        ),
    )
    with store.open_verified(artifact, envelope_metadata=metadata) as verified:
        if hashlib.sha256(verified.read()).hexdigest() != evidence.plaintext_sha256:
            raise MyBankStatementCutoverError("reusable evidence plaintext identity conflicts")


def _append_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    reason: str,
    payload: dict[str, object],
) -> UUID:
    audit_ref = session.execute(
        text(
            "SELECT public.append_audit_event(:actor, :action, :reason, :rule_version, "
            "CAST(:payload AS jsonb))"
        ),
        {
            "actor": actor,
            "action": action,
            "reason": reason,
            "rule_version": "ledgerbridge.mybank-private-cutover.v1",
            "payload": json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        },
    ).scalar_one()
    if not isinstance(audit_ref, UUID):
        raise MyBankStatementCutoverError("audit append returned an invalid identity")
    return audit_ref


def _new_session(bind: Engine | Connection) -> Session:
    if isinstance(bind, Connection):
        return Session(bind, join_transaction_mode="rollback_only")
    return Session(bind)


def _read_schema_revision(engine: Engine | Connection) -> str:
    if isinstance(engine, Connection):
        revision = engine.execute(
            text("SELECT version_num FROM public.alembic_version")
        ).scalar_one()
    else:
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM public.alembic_version")
            ).scalar_one()
    if not isinstance(revision, str):
        raise MyBankStatementCutoverError("database schema revision is invalid")
    return revision


def _read_production_counts(engine: Engine | Connection) -> ProductionCounts:
    connection = engine if isinstance(engine, Connection) else engine.connect()
    try:
        row = (
            connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM public.evidence_object) AS evidence_objects, "
                    "(SELECT count(*) FROM public.encrypted_object_identity) "
                    "AS encrypted_object_identities, "
                    "(SELECT count(*) FROM public.encrypted_blob_version) "
                    "AS encrypted_blob_versions, "
                    "(SELECT count(*) FROM public.managed_account) AS managed_accounts, "
                    "(SELECT count(*) FROM public.managed_account_lifecycle) "
                    "AS managed_account_lifecycles, "
                    "(SELECT count(*) FROM public.account_registry_operation) "
                    "AS account_registry_operations, "
                    "(SELECT count(*) FROM public.managed_account_alias) "
                    "AS managed_account_aliases, "
                    "(SELECT count(*) FROM public.account_business_unit_assignment) "
                    "AS account_business_unit_assignments, "
                    "(SELECT count(*) FROM public.fact_business_unit_allocation_set) "
                    "AS fact_business_unit_allocation_sets, "
                    "(SELECT count(*) FROM public.fact_business_unit_allocation_item) "
                    "AS fact_business_unit_allocation_items, "
                    "(SELECT count(*) FROM public.bank_statement) AS bank_statements, "
                    "(SELECT count(*) FROM public.bank_statement_transaction) "
                    "AS bank_statement_transactions, "
                    "(SELECT count(*) FROM public.bank_statement_observation) "
                    "AS bank_statement_observations, "
                    "(SELECT count(*) FROM public.bank_statement_review) "
                    "AS bank_statement_reviews, "
                    "(SELECT count(*) FROM public.candidate) AS candidates, "
                    "(SELECT count(*) FROM public.candidate c JOIN LATERAL "
                    "(SELECT status FROM public.candidate_revision cr "
                    "WHERE cr.candidate_id=c.id ORDER BY cr.revision DESC LIMIT 1) latest ON true "
                    "WHERE latest.status='PENDING') AS latest_pending_candidates, "
                    "(SELECT count(*) FROM public.audit_event) AS audit_events, "
                    "(SELECT count(*) FROM public.journal_entry) AS journal_entries, "
                    "(SELECT count(*) FROM public.posting) AS postings"
                )
            )
            .mappings()
            .one()
        )
    finally:
        if not isinstance(engine, Connection):
            connection.close()
    return ProductionCounts(**{name: int(value) for name, value in row.items()})
