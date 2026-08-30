"""Fail-closed orchestration for the private MYbank whole-statement cutover."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid5

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ledgerbridge.artifacts import ArtifactStore
from ledgerbridge.bank_statement_persistence import (
    BankStatementImportContext,
    BankStatementImportResult,
    BankStatementImportService,
    BankStatementPersistenceError,
)
from ledgerbridge.crypto import ENVELOPE_ALGORITHM, ENVELOPE_SCHEMA, SecretStreamCipher
from ledgerbridge.encrypted_artifacts import EncryptedArtifactStore
from ledgerbridge.file_key_provider import FileKeyProvider
from ledgerbridge.mybank_statement import MyBankStatement, parse_mybank_xlsx
from ledgerbridge.reconciliation import AccountOwnerKind

_SCHEMA_REVISION = "20260830_0021"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_SAFE_REF = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,199}$")
_ACCOUNT_KIND = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")
_CONFLICT_NAMESPACE = UUID("581080ab-fcd4-414f-af6a-f00ed1424f87")


class MyBankStatementCutoverError(RuntimeError):
    """The cutover could not prove a complete, replay-safe result."""


@dataclass(frozen=True, slots=True)
class ProductionCounts:
    evidence_objects: int
    encrypted_object_identities: int
    encrypted_blob_versions: int
    managed_accounts: int
    managed_account_lifecycles: int
    bank_statements: int
    bank_statement_transactions: int
    bank_statement_observations: int
    bank_statement_reviews: int
    candidates: int
    latest_pending_candidates: int
    audit_events: int

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
            self.bank_statements,
            self.bank_statement_transactions,
            self.bank_statement_observations,
            self.bank_statement_reviews,
            self.candidates,
            self.latest_pending_candidates,
            self.audit_events,
        )


@dataclass(frozen=True, slots=True)
class MyBankStatementCutoverPlan:
    source_path: Path
    expected_sha256: str
    expected_size: int
    evidence_ref: UUID
    entity_ref: UUID
    business_unit_ref: UUID
    managed_account_ref: UUID
    owner_entity_ref: UUID
    owner_ref: str
    owner_kind: AccountOwnerKind
    account_kind: str
    business_unit_attribution_ref: UUID | None
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
        if _SAFE_REF.fullmatch(self.owner_ref) is None:
            raise ValueError("owner reference is invalid")
        if not isinstance(self.owner_kind, AccountOwnerKind):
            raise ValueError("owner kind is invalid")
        if _ACCOUNT_KIND.fullmatch(self.account_kind) is None:
            raise ValueError("account kind is invalid")
        if self.owner_entity_ref != self.entity_ref:
            raise ValueError("owner entity does not match accounting entity")
        if (
            self.business_unit_attribution_ref is not None
            and self.business_unit_attribution_ref != self.business_unit_ref
        ):
            raise ValueError("business unit attribution conflicts with evidence scope")
        if _ACCOUNT_SUFFIX.fullmatch(self.account_suffix) is None:
            raise ValueError("MYbank cutover account suffix is invalid")
        if type(self.expected_transaction_count) is not int or self.expected_transaction_count <= 0:
            raise ValueError("expected transaction count is invalid")
        if not self.actor.strip() or not self.reason.strip():
            raise ValueError("cutover audit context is invalid")


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
    created: bool
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


class MyBankStatementCutoverRunner:
    """Run one checked import and its mandatory idempotent replay."""

    def __init__(
        self,
        *,
        parser: Callable[..., MyBankStatement],
        evidence_writer: Callable[[Path, MyBankEvidenceDescriptor], None],
        statement_importer: _StatementImporter,
        counts_reader: Callable[[], ProductionCounts],
        schema_reader: Callable[[], str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._parser = parser
        self._evidence_writer = evidence_writer
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
            plan.expected_transaction_count,
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
            entity_ref=plan.entity_ref,
            managed_account_ref=plan.managed_account_ref,
            account_key=(f"mybank:{plan.owner_kind.value.lower()}:{plan.account_suffix}"),
            owner_ref=plan.owner_ref,
            owner_kind=plan.owner_kind,
            account_kind=plan.account_kind,
            evidence_ref=plan.evidence_ref,
            actor=plan.actor,
            reason=plan.reason,
        )

        if is_completed_replay:
            try:
                replay = self._statement_importer.import_statement(statement, context=context)
            except Exception:
                self._logger.error("MYbank completed-package replay failed")
                raise MyBankStatementCutoverError("completed statement replay conflict") from None
            if replay.created or not _receipt_matches(replay, statement, plan):
                raise MyBankStatementCutoverError("completed statement replay receipt conflicts")
            replay_counts = self._counts_reader()
            if replay_counts != before:
                raise MyBankStatementCutoverError("completed statement replay changed counts")
            return MyBankStatementCutoverReceipt(
                statement_ref=replay.statement_ref,
                evidence_ref=plan.evidence_ref,
                managed_account_ref=replay.managed_account_ref,
                created=False,
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
            first = self._statement_importer.import_statement(statement, context=context)
        except Exception:
            self._logger.error("MYbank cutover persistence failed")
            raise MyBankStatementCutoverError("statement import conflict") from None
        if not first.created or not _receipt_matches(first, statement, plan):
            raise MyBankStatementCutoverError("first statement import receipt conflicts")

        after = self._counts_reader()
        expected_after = _expected_after(before, len(statement.transactions))
        if after != expected_after:
            raise MyBankStatementCutoverError("post-import count acceptance conflict")

        try:
            replay = self._statement_importer.import_statement(statement, context=context)
        except Exception:
            self._logger.error("MYbank cutover replay failed")
            raise MyBankStatementCutoverError("statement replay conflict") from None
        if (
            replay.created
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
            created=first.created,
            replay_created=replay.created,
            transaction_count=first.transaction_count,
            review_status=first.review_status,
            before_counts=before,
            after_counts=after,
            replay_counts=replay_counts,
            fact_conflict_rejected=False,
        )


def _require_gates(gates: MyBankStatementCutoverGates) -> None:
    if gates.schema_revision != _SCHEMA_REVISION:
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
        if _counts_from_cutover_inventory(source_inventory) != gates.expected_before:
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


def _counts_from_cutover_inventory(value: object) -> ProductionCounts:
    if not isinstance(value, dict) or set(value) != {
        "schema_revision",
        "candidate_total",
        "latest_pending_candidates",
        "audit_events",
        "row_counts",
    }:
        raise ValueError
    if value.get("schema_revision") != _SCHEMA_REVISION:
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
        bank_statements=count("bank_statement"),
        bank_statement_transactions=count("bank_statement_transaction"),
        bank_statement_observations=count("bank_statement_observation"),
        bank_statement_reviews=count("bank_statement_review"),
        candidates=scalar("candidate_total"),
        latest_pending_candidates=scalar("latest_pending_candidates"),
        audit_events=scalar("audit_events"),
    )


def _expected_after(before: ProductionCounts, transaction_count: int) -> ProductionCounts:
    return ProductionCounts(
        evidence_objects=before.evidence_objects + 1,
        encrypted_object_identities=before.encrypted_object_identities + 1,
        encrypted_blob_versions=before.encrypted_blob_versions + 1,
        managed_accounts=before.managed_accounts + 1,
        managed_account_lifecycles=before.managed_account_lifecycles + 1,
        bank_statements=before.bank_statements + 1,
        bank_statement_transactions=before.bank_statement_transactions + transaction_count,
        bank_statement_observations=before.bank_statement_observations + transaction_count,
        bank_statement_reviews=before.bank_statement_reviews + 1,
        candidates=before.candidates,
        latest_pending_candidates=before.latest_pending_candidates,
        audit_events=before.audit_events + 6 + 2 * transaction_count,
    )


def _receipt_matches(
    result: BankStatementImportResult,
    statement: MyBankStatement,
    plan: MyBankStatementCutoverPlan,
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
    runner = MyBankStatementCutoverRunner(
        parser=parse_mybank_xlsx,
        evidence_writer=boundary.write,
        statement_importer=_DatabaseStatementImporter(boundary, plan),
        counts_reader=lambda: _read_production_counts(engine),
        schema_reader=lambda: _read_schema_revision(engine),
        logger=logger,
    )
    receipt = runner.run(plan, gates=replace(gates, verify_fact_conflict=False))
    if not gates.verify_fact_conflict:
        return receipt
    _run_database_fact_conflict_probe(
        engine,
        plan=plan,
        statement=parse_mybank_xlsx(
            plan.source_path,
            expected_sha256=plan.expected_sha256,
            managed_account_suffix=plan.account_suffix,
        ),
    )
    if _read_production_counts(engine) != receipt.after_counts:
        raise MyBankStatementCutoverError("fact conflict changed database counts")
    return replace(receipt, fact_conflict_rejected=True)


class _DatabaseStatementImporter:
    def __init__(
        self,
        boundary: _DatabaseEvidenceBoundary,
        plan: MyBankStatementCutoverPlan,
    ) -> None:
        self._boundary = boundary
        self._plan = plan

    def import_statement(
        self,
        statement: MyBankStatement,
        *,
        context: BankStatementImportContext,
    ) -> BankStatementImportResult:
        session = self._boundary.session()
        try:
            _require_import_identity(session, self._plan, statement, context)
        except BaseException:
            session.close()
            raise
        return BankStatementImportService(lambda: session).import_statement(
            statement,
            context=context,
        )


class _DatabaseEvidenceBoundary:
    """Stage Evidence rows in the transaction committed by the statement service."""

    def __init__(
        self,
        engine: Engine,
        *,
        plan: MyBankStatementCutoverPlan,
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

    def write(self, source_path: Path, evidence: MyBankEvidenceDescriptor) -> None:
        if self._pending_session is not None:
            raise MyBankStatementCutoverError("evidence transaction is already active")
        with source_path.open("rb") as source:
            published = self._store.publish(source)
        metadata = self._store.envelope_metadata(published)
        with self._store.open_verified(published, envelope_metadata=metadata) as verified:
            verified_digest = hashlib.sha256(verified.read()).hexdigest()
        if (
            published.plaintext_sha256.hex() != evidence.plaintext_sha256
            or published.plaintext_size != evidence.plaintext_size
            or verified_digest != evidence.plaintext_sha256
        ):
            raise MyBankStatementCutoverError("encrypted evidence source identity conflicts")

        session = Session(self._engine)
        try:
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
            session.close()
            raise
        self._pending_session = session

    def session(self) -> Session:
        pending = self._pending_session
        if pending is not None:
            self._pending_session = None
            return pending
        return Session(self._engine)


def _require_import_identity(
    session: Session,
    plan: MyBankStatementCutoverPlan,
    statement: MyBankStatement,
    context: BankStatementImportContext,
) -> None:
    """Recheck the explicit owner, account, and evidence identity on every import."""

    expected_account_key = f"mybank:{plan.owner_kind.value.lower()}:{plan.account_suffix}"
    if (
        context.entity_ref != plan.entity_ref
        or context.managed_account_ref != plan.managed_account_ref
        or context.account_key != expected_account_key
        or context.owner_ref != plan.owner_ref
        or context.owner_kind is not plan.owner_kind
        or context.account_kind != plan.account_kind
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
                "ma.account_kind, latest.status AS lifecycle_status "
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
                "account_key": expected_account_key,
            },
        )
        .mappings()
        .all()
    )
    if not accounts:
        return
    if len(accounts) != 1:
        raise MyBankStatementCutoverError("managed account identity is not unique")
    account = accounts[0]
    if (
        account["managed_account_ref"] != plan.managed_account_ref
        or account["entity_id"] != plan.entity_ref
        or account["account_key"] != expected_account_key
        or account["institution_code"] != "mybank"
        or account["account_suffix"] != plan.account_suffix
        or account["owner_ref"] != plan.owner_ref
        or account["owner_kind"] != plan.owner_kind.value
        or account["account_kind"] != plan.account_kind
        or account["lifecycle_status"] != "ACTIVE"
    ):
        raise MyBankStatementCutoverError("managed account conflicts with explicit owner identity")


class _RollbackOnlySession(Session):
    """Let the real import function flush a negative probe without committing it."""

    def commit(self) -> None:
        self.flush()


def _run_database_fact_conflict_probe(
    engine: Engine,
    *,
    plan: MyBankStatementCutoverPlan,
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
    session = _RollbackOnlySession(engine)
    try:
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
                "display_name": f"mybank-conflict-probe-{probe_statement.statement_ref}.xlsx",
                "digest": bytes.fromhex(probe_source_digest),
                "size": probe_statement.source_size,
                "audit": audit_ref,
            },
        )
        context = BankStatementImportContext(
            entity_ref=plan.entity_ref,
            managed_account_ref=plan.managed_account_ref,
            account_key=f"mybank:{plan.owner_kind.value.lower()}:{plan.account_suffix}",
            owner_ref=plan.owner_ref,
            owner_kind=plan.owner_kind,
            account_kind=plan.account_kind,
            evidence_ref=probe_evidence_ref,
            actor=plan.actor,
            reason=plan.reason,
        )
        try:
            BankStatementImportService(lambda: session).import_statement(
                probe_statement,
                context=context,
            )
        except BankStatementPersistenceError as exc:
            database_error = exc.__cause__
            rendered = str(getattr(database_error, "orig", database_error))
            if "overlapping bank statement transaction conflicts with fact" in rendered:
                return
            raise MyBankStatementCutoverError(
                "overlapping fact probe failed for an unexpected reason"
            ) from exc
        raise MyBankStatementCutoverError("overlapping fact conflict was accepted")
    finally:
        session.close()


def _require_unique_scope(
    session: Session,
    evidence: MyBankEvidenceDescriptor,
    *,
    owner_kind: AccountOwnerKind,
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
    expected_entity_type = "PERSON" if owner_kind is AccountOwnerKind.PERSONAL else "COMPANY"
    if scope["entity_type"] != expected_entity_type:
        raise MyBankStatementCutoverError("accounting owner kind conflicts with entity")


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


def _read_schema_revision(engine: Engine) -> str:
    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM public.alembic_version")
        ).scalar_one()
    if not isinstance(revision, str):
        raise MyBankStatementCutoverError("database schema revision is invalid")
    return revision


def _read_production_counts(engine: Engine) -> ProductionCounts:
    with engine.connect() as connection:
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
                    "(SELECT count(*) FROM public.audit_event) AS audit_events"
                )
            )
            .mappings()
            .one()
        )
    return ProductionCounts(**{name: int(value) for name, value in row.items()})
