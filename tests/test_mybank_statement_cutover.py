from __future__ import annotations

import hashlib
import json
import logging
import os
import traceback
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from test_mybank_statement import _write_synthetic_mybank_xlsx
from test_r1_database_migration import _legacy_r1_database, _upgrade_config

from alembic import command
from ledgerbridge.account_registry import (
    AccountAliasRegistration,
    AccountRegistryPlan,
    AccountRegistryPlanResult,
    ManagedAccountRegistration,
)
from ledgerbridge.bank_statement_persistence import (
    BankStatementImportContext,
    BankStatementImportResult,
    BankStatementPersistenceError,
)
from ledgerbridge.crypto import ENVELOPE_ALGORITHM, ENVELOPE_SCHEMA
from ledgerbridge.encrypted_artifacts import EncryptedArtifactStore
from ledgerbridge.file_key_provider import bootstrap_file_key
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.models import EntityType
from ledgerbridge.mybank_statement import MyBankStatement, MyBankTransaction
from ledgerbridge.mybank_statement_cutover import (
    MyBankCutoverSafetyProof,
    MyBankEvidenceDescriptor,
    MyBankEvidenceMode,
    MyBankExistingAccountStatementPlan,
    MyBankExistingAccountStatementRunner,
    MyBankStatementCutoverError,
    MyBankStatementCutoverGates,
    MyBankStatementCutoverPlan,
    MyBankStatementCutoverRunner,
    ProductionCounts,
    _expected_after_existing_account,
    _read_production_counts,
    _require_existing_account_identity,
    _require_import_identity,
    _require_reusable_existing_evidence,
    run_transactional_database_mybank_existing_account_import,
    run_transactional_database_mybank_statement_cutover,
    verify_mybank_cutover_safety_proof,
)

STATEMENT_REF = UUID("83000000-0000-4000-8000-000000000001")
EVIDENCE_REF = UUID("83000000-0000-4000-8000-000000000002")
ENTITY_REF = UUID("83000000-0000-4000-8000-000000000003")
BUSINESS_UNIT_REF = UUID("83000000-0000-4000-8000-000000000004")
MANAGED_ACCOUNT_REF = UUID("83000000-0000-4000-8000-000000000005")
OTHER_ENTITY_REF = UUID("83000000-0000-4000-8000-000000000006")
REGISTRY_OPERATION_REF = UUID("83000000-0000-4000-8000-000000000007")
ACCOUNT_ALIAS_REF = UUID("83000000-0000-4000-8000-000000000008")
ADMISSION_EVIDENCE_REF = UUID("83000000-0000-4000-8000-000000000009")
ASSIGNMENT_REF = UUID("83000000-0000-4000-8000-000000000010")
MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _statement(source_sha256: str, source_size: int) -> MyBankStatement:
    return MyBankStatement(
        statement_ref=STATEMENT_REF,
        source_sha256=source_sha256,
        source_size=source_size,
        declared_media_type=MEDIA_TYPE,
        currency="CNY",
        institution_code="mybank",
        account_suffix="1357",
        worksheet_index=1,
        header_row_number=8,
        transactions=(
            MyBankTransaction(
                source_event_ref=UUID("83000000-0000-4000-8000-000000000011"),
                source_row_number=9,
                source_row_sha256="1" * 64,
                occurred_at=datetime(2026, 5, 1, 1, 2, 3, tzinfo=UTC),
                amount_minor=12_345,
                balance_minor=98_765,
                counterparty_name="Synthetic counterparty that is not the account owner",
                counterparty_account="0000000000005678",
                counterparty_institution="Synthetic bank",
                transaction_serial="SYNTHETIC-0001",
                transaction_name="transfer",
            ),
            MyBankTransaction(
                source_event_ref=UUID("83000000-0000-4000-8000-000000000012"),
                source_row_number=10,
                source_row_sha256="2" * 64,
                occurred_at=datetime(2026, 5, 2, 4, 5, 6, tzinfo=UTC),
                amount_minor=-2_000,
                balance_minor=96_765,
                counterparty_name="Another synthetic counterparty",
                counterparty_account="0000000000009012",
                counterparty_institution="Synthetic bank",
                transaction_serial="SYNTHETIC-0002",
                transaction_name="purchase",
            ),
        ),
    )


def _before_counts() -> ProductionCounts:
    return ProductionCounts(
        evidence_objects=12,
        encrypted_object_identities=12,
        encrypted_blob_versions=12,
        managed_accounts=0,
        managed_account_lifecycles=0,
        account_registry_operations=0,
        managed_account_aliases=0,
        account_business_unit_assignments=0,
        fact_business_unit_allocation_sets=0,
        fact_business_unit_allocation_items=0,
        bank_statements=0,
        bank_statement_transactions=0,
        bank_statement_observations=0,
        bank_statement_reviews=0,
        candidates=20,
        latest_pending_candidates=14,
        audit_events=1_000,
    )


def _after_counts(transaction_count: int = 2) -> ProductionCounts:
    return ProductionCounts(
        evidence_objects=13,
        encrypted_object_identities=13,
        encrypted_blob_versions=13,
        managed_accounts=1,
        managed_account_lifecycles=1,
        account_registry_operations=1,
        managed_account_aliases=1,
        account_business_unit_assignments=0,
        fact_business_unit_allocation_sets=0,
        fact_business_unit_allocation_items=0,
        bank_statements=1,
        bank_statement_transactions=transaction_count,
        bank_statement_observations=transaction_count,
        bank_statement_reviews=1,
        candidates=20,
        latest_pending_candidates=14,
        audit_events=1_008 + 2 * transaction_count,
    )


def _plan(source_path: Path, digest: str, size: int) -> MyBankStatementCutoverPlan:
    return MyBankStatementCutoverPlan(
        source_path=source_path,
        expected_sha256=digest,
        expected_size=size,
        evidence_ref=EVIDENCE_REF,
        entity_ref=ENTITY_REF,
        business_unit_ref=BUSINESS_UNIT_REF,
        registry_plan=AccountRegistryPlan(
            operation_id=REGISTRY_OPERATION_REF,
            owner_entity_ref=ENTITY_REF,
            expected_owner_kind=EntityType.PERSON,
            expected_registry_revision=0,
            actor_ref="worker:mybank-private-cutover",
            reason="operator-confirmed whole-statement import",
            accounts=(
                ManagedAccountRegistration(
                    managed_account_ref=MANAGED_ACCOUNT_REF,
                    admission_evidence_ref=EVIDENCE_REF,
                    account_key="managed-account:synthetic-personal",
                    institution_code="mybank",
                    account_suffix="1357",
                    account_kind="BANK_CHECKING",
                    aliases=(
                        AccountAliasRegistration(
                            alias_ref=ACCOUNT_ALIAS_REF,
                            alias_kind="ACCOUNT_NUMBER",
                            alias_value="0000 0000 0000 1357",
                        ),
                    ),
                ),
            ),
        ),
        account_suffix="1357",
        expected_transaction_count=2,
        actor="worker:mybank-private-cutover",
        reason="operator-confirmed whole-statement import",
    )


def _existing_account_plan(
    source_path: Path,
    digest: str,
    size: int,
    *,
    evidence_mode: MyBankEvidenceMode = MyBankEvidenceMode.CREATE_NEW,
) -> MyBankExistingAccountStatementPlan:
    return MyBankExistingAccountStatementPlan(
        source_path=source_path,
        expected_sha256=digest,
        expected_size=size,
        evidence_ref=EVIDENCE_REF,
        evidence_mode=evidence_mode,
        entity_ref=ENTITY_REF,
        business_unit_ref=BUSINESS_UNIT_REF,
        managed_account_ref=MANAGED_ACCOUNT_REF,
        account_suffix="1357",
        expected_transaction_count=2,
        expected_owner_kind=EntityType.COMPANY,
        actor="worker:mybank-existing-account-import",
        reason="operator-confirmed existing-account statement import",
    )


def _gates(
    before: ProductionCounts | None = None,
    *,
    schema_revision: str = "20260830_0023",
) -> MyBankStatementCutoverGates:
    return MyBankStatementCutoverGates(
        schema_revision=schema_revision,
        backup_verified=True,
        isolated_restore_verified=True,
        rollback_ready=True,
        expected_before=before or _before_counts(),
    )


def _registry_principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:synthetic-cutover",
        san_uri="spiffe://ledgerbridge.test/synthetic-cutover",
        policy_generation=1,
        capabilities=frozenset({Capability.ACCOUNT_REGISTRY_WRITE}),
        grants=(EntityGrant(entity_ref=ENTITY_REF, allow_account_registry=True),),
    )


def _safety_proof(
    tmp_path: Path,
    before: ProductionCounts,
    *,
    schema_revision: str = "20260830_0023",
) -> MyBankCutoverSafetyProof:
    backup = (tmp_path / "synthetic-encrypted-backup").resolve()
    backup.mkdir()
    ciphertext = backup / "ledgerbridge-backup.tar.gpg"
    ciphertext.write_bytes(b"synthetic encrypted backup ciphertext")
    ciphertext_digest = hashlib.sha256(ciphertext.read_bytes()).hexdigest()
    revision = "a" * 40
    (backup / "backup.json").write_text(
        json.dumps(
            {
                "format": "ledgerbridge-encrypted-backup-v3",
                "created_at": "2026-08-30T00:00:00+00:00",
                "revision": revision,
                "gpg_fingerprint": "A" * 40,
                "ciphertext": ciphertext.name,
                "ciphertext_sha256": ciphertext_digest,
                "postgres_image": "synthetic-postgres-image@sha256:" + "b" * 64,
            }
        ),
        encoding="utf-8",
    )
    (backup / "SHA256SUMS").write_text(
        f"{ciphertext_digest}  {ciphertext.name}\n",
        encoding="utf-8",
    )
    inventory = {
        "schema_revision": schema_revision,
        "candidate_total": before.candidates,
        "latest_pending_candidates": before.latest_pending_candidates,
        "audit_events": before.audit_events,
        "row_counts": {
            "evidence_object": before.evidence_objects,
            "encrypted_object_identity": before.encrypted_object_identities,
            "encrypted_blob_version": before.encrypted_blob_versions,
            "managed_account": before.managed_accounts,
            "managed_account_lifecycle": before.managed_account_lifecycles,
            "account_registry_operation": before.account_registry_operations,
            "managed_account_alias": before.managed_account_aliases,
            "account_business_unit_assignment": before.account_business_unit_assignments,
            "fact_business_unit_allocation_set": before.fact_business_unit_allocation_sets,
            "fact_business_unit_allocation_item": before.fact_business_unit_allocation_items,
            "bank_statement": before.bank_statements,
            "bank_statement_transaction": before.bank_statement_transactions,
            "bank_statement_observation": before.bank_statement_observations,
            "bank_statement_review": before.bank_statement_reviews,
            "journal_entry": before.journal_entries,
            "posting": before.postings,
        },
    }
    report = backup / "restore-rehearsal-synthetic.json"
    report.write_text(
        json.dumps(
            {
                "format": "ledgerbridge-restore-rehearsal-v3",
                "status": "passed",
                "backup": backup.name,
                "revision": revision,
                "source_format": "v3",
                "database_compared_fields": ["cutover_inventory"],
                "source_database_metadata": {"cutover_inventory": inventory},
                "post_restore_database_observations": {"cutover_inventory": inventory},
                "production_unchanged": True,
                "isolated_resources_removed": True,
            }
        ),
        encoding="utf-8",
    )
    return MyBankCutoverSafetyProof(backup_directory=backup, restore_report=report)


class _Parser:
    def __init__(self, statement: MyBankStatement) -> None:
        self.statement = statement
        self.calls: list[tuple[Path, str, str]] = []

    def __call__(
        self,
        source_path: Path,
        *,
        expected_sha256: str,
        managed_account_suffix: str,
    ) -> MyBankStatement:
        self.calls.append((source_path, expected_sha256, managed_account_suffix))
        return self.statement


class _EvidenceWriter:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[Path, MyBankEvidenceDescriptor]] = []
        self._events = events

    def __call__(self, source_path: Path, evidence: MyBankEvidenceDescriptor) -> None:
        if self._events is not None:
            self._events.append("evidence")
        self.calls.append((source_path, evidence))


class _AccountRegistrar:
    def __init__(
        self,
        results: Iterator[AccountRegistryPlanResult | BaseException],
        events: list[str] | None = None,
    ) -> None:
        self._results = results
        self.calls: list[AccountRegistryPlan] = []
        self._events = events

    def register(self, plan: AccountRegistryPlan) -> AccountRegistryPlanResult:
        if self._events is not None:
            self._events.append("registry")
        self.calls.append(plan)
        result = next(self._results)
        if isinstance(result, BaseException):
            raise result
        return result


class _StatementImporter:
    def __init__(
        self,
        results: Iterator[BankStatementImportResult | BaseException],
        events: list[str] | None = None,
    ) -> None:
        self._results = results
        self.calls: list[tuple[MyBankStatement, BankStatementImportContext]] = []
        self._events = events

    def import_statement(
        self,
        statement: MyBankStatement,
        *,
        context: BankStatementImportContext,
    ) -> BankStatementImportResult:
        if self._events is not None:
            self._events.append("statement")
        self.calls.append((statement, context))
        result = next(self._results)
        if isinstance(result, BaseException):
            raise result
        return result


class _ExistingAccountAuthorizer:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[MyBankExistingAccountStatementPlan, MyBankStatement]] = []
        self._events = events

    def authorize(
        self,
        plan: MyBankExistingAccountStatementPlan,
        statement: MyBankStatement,
    ) -> None:
        if self._events is not None:
            self._events.append("account")
        self.calls.append((plan, statement))


class _CountsReader:
    def __init__(self, values: Iterator[ProductionCounts]) -> None:
        self._values = values
        self.calls = 0

    def __call__(self) -> ProductionCounts:
        self.calls += 1
        return next(self._values)


class _MappingResult:
    def __init__(self, *, one: object = None, rows: list[object] | None = None) -> None:
        self._one = one
        self._rows = rows or []

    def mappings(self) -> _MappingResult:
        return self

    def one_or_none(self) -> object:
        return self._one

    def all(self) -> list[object]:
        return self._rows


class _IdentitySession:
    def __init__(self, results: Iterator[_MappingResult]) -> None:
        self._results = results

    def execute(self, *_args: object, **_kwargs: object) -> _MappingResult:
        return next(self._results)


def _import_result(*, created: bool, transaction_count: int = 2) -> BankStatementImportResult:
    return BankStatementImportResult(
        statement_ref=STATEMENT_REF,
        managed_account_ref=MANAGED_ACCOUNT_REF,
        created=created,
        transaction_count=transaction_count,
        review_status="PENDING",
        statement_review_count=1,
        accounting_candidate_count=0,
    )


def _registry_result(*, created: bool) -> AccountRegistryPlanResult:
    return AccountRegistryPlanResult(
        operation_id=REGISTRY_OPERATION_REF,
        owner_entity_ref=ENTITY_REF,
        registry_revision=1,
        created=created,
        managed_account_refs=(MANAGED_ACCOUNT_REF,),
    )


def _successful_runner(
    statement: MyBankStatement,
) -> tuple[
    MyBankStatementCutoverRunner,
    _Parser,
    _EvidenceWriter,
    _AccountRegistrar,
    _StatementImporter,
    _CountsReader,
]:
    parser = _Parser(statement)
    evidence_writer = _EvidenceWriter()
    registrar = _AccountRegistrar(
        iter((_registry_result(created=True), _registry_result(created=False)))
    )
    importer = _StatementImporter(
        iter((_import_result(created=True), _import_result(created=False)))
    )
    counts_reader = _CountsReader(iter((_before_counts(), _after_counts(), _after_counts())))
    runner = MyBankStatementCutoverRunner(
        parser=parser,
        evidence_writer=evidence_writer,
        account_registrar=registrar,
        statement_importer=importer,
        counts_reader=counts_reader,
    )
    return runner, parser, evidence_writer, registrar, importer, counts_reader


def _synthetic_source(
    tmp_path: Path, *, name: str = "synthetic-statement.xlsx"
) -> tuple[Path, str]:
    source = tmp_path / name
    source.write_bytes(b"synthetic encrypted-statement plaintext")
    return source, hashlib.sha256(source.read_bytes()).hexdigest()


def test_cutover_creates_whole_statement_facts_without_candidates(tmp_path: Path) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    runner, _, _, _, _, _ = _successful_runner(statement)

    receipt = runner.run(
        _plan(source, digest, source.stat().st_size),
        gates=_gates(),
    )

    assert receipt.created is True
    assert receipt.replay_created is False
    assert receipt.transaction_count == 2
    assert receipt.before_counts == _before_counts()
    assert receipt.after_counts == _after_counts()
    assert receipt.candidate_delta == 0
    assert receipt.latest_pending_candidate_delta == 0


@pytest.mark.parametrize("schema_revision", ("20260830_0025", "20260902_0032"))
def test_cutover_accepts_reviewed_schema_revision(
    tmp_path: Path,
    schema_revision: str,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    parser = _Parser(statement)
    runner = MyBankStatementCutoverRunner(
        parser=parser,
        evidence_writer=_EvidenceWriter(),
        account_registrar=_AccountRegistrar(
            iter((_registry_result(created=True), _registry_result(created=False)))
        ),
        statement_importer=_StatementImporter(
            iter((_import_result(created=True), _import_result(created=False)))
        ),
        counts_reader=_CountsReader(iter((_before_counts(), _after_counts(), _after_counts()))),
        schema_reader=lambda: schema_revision,
    )

    receipt = runner.run(
        _plan(source, digest, source.stat().st_size),
        gates=_gates(schema_revision=schema_revision),
    )

    assert receipt.created is True


def test_current_integrated_0025_safety_proof_is_accepted(tmp_path: Path) -> None:
    proof = _safety_proof(
        tmp_path,
        _before_counts(),
        schema_revision="20260830_0025",
    )

    verify_mybank_cutover_safety_proof(
        proof,
        gates=_gates(schema_revision="20260830_0025"),
    )


def test_cutover_orders_evidence_registry_statement_then_replays_registry_and_statement(
    tmp_path: Path,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    events: list[str] = []
    evidence_writer = _EvidenceWriter(events)
    registrar = _AccountRegistrar(
        iter((_registry_result(created=True), _registry_result(created=False))),
        events,
    )
    importer = _StatementImporter(
        iter((_import_result(created=True), _import_result(created=False))),
        events,
    )
    runner = MyBankStatementCutoverRunner(
        parser=_Parser(statement),
        evidence_writer=evidence_writer,
        account_registrar=registrar,
        statement_importer=importer,
        counts_reader=_CountsReader(iter((_before_counts(), _after_counts(), _after_counts()))),
    )

    runner.run(_plan(source, digest, source.stat().st_size), gates=_gates())

    assert events == ["evidence", "registry", "statement", "registry", "statement"]


def test_cutover_stops_before_statement_when_registry_plan_fails(tmp_path: Path) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    events: list[str] = []
    importer = _StatementImporter(iter(()), events)
    runner = MyBankStatementCutoverRunner(
        parser=_Parser(statement),
        evidence_writer=_EvidenceWriter(events),
        account_registrar=_AccountRegistrar(iter((RuntimeError("registry rejected"),)), events),
        statement_importer=importer,
        counts_reader=_CountsReader(iter((_before_counts(),))),
    )

    with pytest.raises(MyBankStatementCutoverError, match="import conflict"):
        runner.run(_plan(source, digest, source.stat().st_size), gates=_gates())

    assert events == ["evidence", "registry"]
    assert importer.calls == []


def test_statement_identity_check_rejects_missing_registered_account(
    tmp_path: Path,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    plan = _plan(source, digest, source.stat().st_size)
    session = _IdentitySession(
        iter(
            (
                _MappingResult(one={"entity_type": EntityType.PERSON.value}),
                _MappingResult(
                    one={
                        "entity_id": ENTITY_REF,
                        "business_unit_id": BUSINESS_UNIT_REF,
                        "media_type": MEDIA_TYPE,
                        "plaintext_sha256": bytes.fromhex(digest),
                        "plaintext_size": source.stat().st_size,
                    }
                ),
                _MappingResult(rows=[]),
            )
        )
    )
    context = BankStatementImportContext(
        owner_entity_ref=ENTITY_REF,
        managed_account_ref=MANAGED_ACCOUNT_REF,
        evidence_ref=EVIDENCE_REF,
        actor=plan.actor,
        reason=plan.reason,
    )

    with pytest.raises(MyBankStatementCutoverError, match="not registered"):
        _require_import_identity(cast(Session, session), plan, statement, context)


def test_cutover_binds_evidence_to_exact_digest_size_and_explicit_scope(tmp_path: Path) -> None:
    source, digest = _synthetic_source(
        tmp_path,
        name="misleading-company-owner-account-9999.xlsx",
    )
    statement = _statement(digest, source.stat().st_size)
    runner, parser, evidence_writer, registrar, importer, _ = _successful_runner(statement)
    plan = _plan(source, digest, source.stat().st_size)

    runner.run(plan, gates=_gates())

    assert parser.calls == [(source, digest, "1357")]
    assert len(evidence_writer.calls) == 1
    evidence_path, evidence = evidence_writer.calls[0]
    assert evidence_path == source
    assert evidence.evidence_ref == EVIDENCE_REF
    assert evidence.plaintext_sha256 == digest
    assert evidence.plaintext_size == source.stat().st_size
    assert evidence.entity_ref == ENTITY_REF
    assert evidence.business_unit_ref == BUSINESS_UNIT_REF
    assert evidence.declared_media_type == MEDIA_TYPE
    assert evidence.display_name == f"mybank-statement-{STATEMENT_REF}.xlsx"
    assert source.name not in repr(evidence)

    assert len(importer.calls) == 2
    assert registrar.calls == [plan.registry_plan, plan.registry_plan]
    for imported_statement, context in importer.calls:
        assert imported_statement is statement
        assert context.owner_entity_ref == ENTITY_REF
        assert context.managed_account_ref == MANAGED_ACCOUNT_REF
        assert context.evidence_ref == EVIDENCE_REF
    assert all(context.owner_entity_ref != OTHER_ENTITY_REF for _, context in importer.calls)


def test_existing_account_plan_preserves_registry_and_posting_counts(tmp_path: Path) -> None:
    source, digest = _synthetic_source(tmp_path)
    before = replace(
        _before_counts(),
        managed_accounts=5,
        managed_account_lifecycles=5,
        account_registry_operations=5,
        managed_account_aliases=5,
        account_business_unit_assignments=5,
        journal_entries=3,
        postings=6,
    )

    after = _expected_after_existing_account(
        before,
        _existing_account_plan(source, digest, source.stat().st_size),
    )

    assert after.managed_accounts == before.managed_accounts
    assert after.managed_account_lifecycles == before.managed_account_lifecycles
    assert after.account_registry_operations == before.account_registry_operations
    assert after.managed_account_aliases == before.managed_account_aliases
    assert after.account_business_unit_assignments == before.account_business_unit_assignments
    assert after.bank_statements == before.bank_statements + 1
    assert after.bank_statement_transactions == before.bank_statement_transactions + 2
    assert after.bank_statement_observations == before.bank_statement_observations + 2
    assert after.bank_statement_reviews == before.bank_statement_reviews + 1
    assert after.candidates == before.candidates
    assert after.latest_pending_candidates == before.latest_pending_candidates
    assert after.journal_entries == before.journal_entries
    assert after.postings == before.postings
    assert after.audit_events == before.audit_events + 8


def test_existing_account_reuse_mode_preserves_evidence_and_uses_statement_audits_only(
    tmp_path: Path,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    before = replace(
        _before_counts(),
        managed_accounts=5,
        managed_account_lifecycles=5,
        account_registry_operations=5,
        managed_account_aliases=5,
        account_business_unit_assignments=5,
    )
    plan = _existing_account_plan(
        source,
        digest,
        source.stat().st_size,
        evidence_mode=MyBankEvidenceMode.REUSE_EXISTING,
    )

    after = _expected_after_existing_account(before, plan)

    assert after.evidence_objects == before.evidence_objects
    assert after.encrypted_object_identities == before.encrypted_object_identities
    assert after.encrypted_blob_versions == before.encrypted_blob_versions
    assert after.bank_statements == before.bank_statements + 1
    assert after.bank_statement_transactions == before.bank_statement_transactions + 2
    assert after.audit_events == before.audit_events + 6


class _ReusableEvidenceStore:
    def __init__(self, plaintext: bytes) -> None:
        self._plaintext = plaintext
        self.opened = False

    def open_verified(self, *_args: object, **_kwargs: object) -> BytesIO:
        self.opened = True
        return BytesIO(self._plaintext)


def test_reusable_existing_evidence_requires_exact_row_and_active_encrypted_blob(
    tmp_path: Path,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    descriptor = MyBankEvidenceDescriptor(
        evidence_ref=EVIDENCE_REF,
        entity_ref=ENTITY_REF,
        business_unit_ref=BUSINESS_UNIT_REF,
        plaintext_sha256=digest,
        plaintext_size=source.stat().st_size,
        declared_media_type=MEDIA_TYPE,
        display_name="synthetic.xlsx",
    )
    session = _IdentitySession(
        iter(
            (
                _MappingResult(
                    one={
                        "evidence_ref": EVIDENCE_REF,
                        "entity_id": ENTITY_REF,
                        "business_unit_id": BUSINESS_UNIT_REF,
                        "media_type": MEDIA_TYPE,
                        "plaintext_sha256": bytes.fromhex(digest),
                        "plaintext_size": source.stat().st_size,
                    }
                ),
                _MappingResult(
                    rows=[
                        {
                            "object_ref": "a" * 64,
                            "identity_evidence_ref": EVIDENCE_REF,
                            "blob_evidence_ref": EVIDENCE_REF,
                            "ciphertext_sha256": b"c" * 32,
                            "ciphertext_size": 128,
                            "storage_key": "sha256/aa/synthetic.enc",
                            "envelope_schema": ENVELOPE_SCHEMA,
                            "algorithm": ENVELOPE_ALGORITHM,
                            "chunk_size": 65_536,
                            "stream_header": b"h" * 24,
                            "wrapped_key_generation": "synthetic-v1",
                            "wrapped_key_nonce": b"n" * 24,
                            "wrapped_key_ciphertext": b"w" * 48,
                            "purpose": "ledgerbridge-artifact-v2",
                        }
                    ]
                ),
            )
        )
    )
    store = _ReusableEvidenceStore(source.read_bytes())

    _require_reusable_existing_evidence(
        cast(Session, session),
        cast(EncryptedArtifactStore, store),
        descriptor,
    )

    assert store.opened is True


def test_reusable_existing_evidence_rejects_missing_encrypted_lineage(tmp_path: Path) -> None:
    source, digest = _synthetic_source(tmp_path)
    descriptor = MyBankEvidenceDescriptor(
        evidence_ref=EVIDENCE_REF,
        entity_ref=ENTITY_REF,
        business_unit_ref=BUSINESS_UNIT_REF,
        plaintext_sha256=digest,
        plaintext_size=source.stat().st_size,
        declared_media_type=MEDIA_TYPE,
        display_name="synthetic.xlsx",
    )
    session = _IdentitySession(
        iter(
            (
                _MappingResult(
                    one={
                        "evidence_ref": EVIDENCE_REF,
                        "entity_id": ENTITY_REF,
                        "business_unit_id": BUSINESS_UNIT_REF,
                        "media_type": MEDIA_TYPE,
                        "plaintext_sha256": bytes.fromhex(digest),
                        "plaintext_size": source.stat().st_size,
                    }
                ),
                _MappingResult(rows=[]),
            )
        )
    )

    with pytest.raises(MyBankStatementCutoverError, match="encrypted lineage"):
        _require_reusable_existing_evidence(
            cast(Session, session),
            cast(EncryptedArtifactStore, _ReusableEvidenceStore(source.read_bytes())),
            descriptor,
        )


def test_existing_account_plan_rejects_empty_statement_count(tmp_path: Path) -> None:
    source, digest = _synthetic_source(tmp_path)

    with pytest.raises(ValueError, match="transaction count"):
        replace(
            _existing_account_plan(source, digest, source.stat().st_size),
            expected_transaction_count=0,
        )


def test_existing_account_runner_writes_evidence_then_imports_and_replays(
    tmp_path: Path,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    plan = _existing_account_plan(source, digest, source.stat().st_size)
    before = replace(
        _before_counts(),
        managed_accounts=5,
        managed_account_lifecycles=5,
        account_registry_operations=5,
        managed_account_aliases=5,
        account_business_unit_assignments=5,
    )
    after = _expected_after_existing_account(before, plan)
    events: list[str] = []
    evidence_writer = _EvidenceWriter(events)
    authorizer = _ExistingAccountAuthorizer(events)
    importer = _StatementImporter(
        iter((_import_result(created=True), _import_result(created=False))),
        events,
    )
    runner = MyBankExistingAccountStatementRunner(
        parser=_Parser(statement),
        evidence_writer=evidence_writer,
        account_authorizer=authorizer,
        statement_importer=importer,
        counts_reader=_CountsReader(iter((before, after, after))),
    )

    receipt = runner.run(plan, gates=_gates(before))

    assert events == ["evidence", "account", "statement", "account", "statement"]
    assert len(evidence_writer.calls) == 1
    assert len(authorizer.calls) == 2
    assert receipt.registry_created is False
    assert receipt.registry_replay_created is False
    assert receipt.created is True
    assert receipt.replay_created is False
    assert receipt.candidate_delta == 0
    assert receipt.after_counts.postings == receipt.before_counts.postings


def test_existing_account_completed_replay_writes_no_evidence(tmp_path: Path) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    plan = _existing_account_plan(source, digest, source.stat().st_size)
    baseline = replace(
        _before_counts(),
        managed_accounts=5,
        managed_account_lifecycles=5,
        account_registry_operations=5,
        managed_account_aliases=5,
        account_business_unit_assignments=5,
    )
    completed = _expected_after_existing_account(baseline, plan)
    evidence_writer = _EvidenceWriter()
    authorizer = _ExistingAccountAuthorizer()
    runner = MyBankExistingAccountStatementRunner(
        parser=_Parser(statement),
        evidence_writer=evidence_writer,
        account_authorizer=authorizer,
        statement_importer=_StatementImporter(iter((_import_result(created=False),))),
        counts_reader=_CountsReader(iter((completed, completed))),
    )

    receipt = runner.run(plan, gates=_gates(baseline))

    assert evidence_writer.calls == []
    assert len(authorizer.calls) == 1
    assert receipt.created is False
    assert receipt.after_counts == receipt.replay_counts == completed


@pytest.mark.parametrize(
    ("lifecycle_status", "assignment_count", "message"),
    (
        ("CLOSED", 1, "ACTIVE"),
        ("ACTIVE", 0, "business-unit assignment"),
        ("ACTIVE", 2, "business-unit assignment"),
    ),
)
def test_existing_account_identity_requires_active_account_and_one_period_binding(
    tmp_path: Path,
    lifecycle_status: str,
    assignment_count: int,
    message: str,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    plan = _existing_account_plan(source, digest, source.stat().st_size)
    session = _IdentitySession(
        iter(
            (
                _MappingResult(one={"entity_type": EntityType.COMPANY.value}),
                _MappingResult(
                    one={
                        "evidence_ref": EVIDENCE_REF,
                        "entity_id": ENTITY_REF,
                        "business_unit_id": BUSINESS_UNIT_REF,
                        "media_type": MEDIA_TYPE,
                        "plaintext_sha256": bytes.fromhex(digest),
                        "plaintext_size": source.stat().st_size,
                    }
                ),
                _MappingResult(
                    one={
                        "managed_account_ref": MANAGED_ACCOUNT_REF,
                        "entity_id": ENTITY_REF,
                        "institution_code": "mybank",
                        "account_suffix": "1357",
                        "owner_ref": str(ENTITY_REF),
                        "owner_kind": "COMPANY",
                        "lifecycle_status": lifecycle_status,
                        "assignment_count": assignment_count,
                        "matching_assignment_count": 1 if assignment_count else 0,
                    }
                ),
            )
        )
    )

    with pytest.raises(MyBankStatementCutoverError, match=message):
        _require_existing_account_identity(cast(Session, session), plan, statement)


def test_existing_account_identity_accepts_exact_active_period_binding(tmp_path: Path) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    plan = _existing_account_plan(source, digest, source.stat().st_size)
    session = _IdentitySession(
        iter(
            (
                _MappingResult(one={"entity_type": EntityType.COMPANY.value}),
                _MappingResult(
                    one={
                        "evidence_ref": EVIDENCE_REF,
                        "entity_id": ENTITY_REF,
                        "business_unit_id": BUSINESS_UNIT_REF,
                        "media_type": MEDIA_TYPE,
                        "plaintext_sha256": bytes.fromhex(digest),
                        "plaintext_size": source.stat().st_size,
                    }
                ),
                _MappingResult(
                    one={
                        "managed_account_ref": MANAGED_ACCOUNT_REF,
                        "entity_id": ENTITY_REF,
                        "institution_code": "mybank",
                        "account_suffix": "1357",
                        "owner_ref": str(ENTITY_REF),
                        "owner_kind": "COMPANY",
                        "lifecycle_status": "ACTIVE",
                        "assignment_count": 1,
                        "matching_assignment_count": 1,
                    }
                ),
            )
        )
    )

    _require_existing_account_identity(cast(Session, session), plan, statement)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("schema_revision", "20260830_0020", "schema"),
        ("schema_revision", "20260901_0030", "schema"),
        ("backup_verified", False, "backup"),
        ("isolated_restore_verified", False, "isolated restore"),
        ("rollback_ready", False, "rollback"),
    ),
)
def test_cutover_rejects_missing_safety_gate_before_side_effects(
    tmp_path: Path,
    field: str,
    value: str | bool,
    message: str,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    runner, parser, evidence_writer, registrar, importer, counts_reader = _successful_runner(
        statement
    )
    gates = _gates()
    if field == "schema_revision":
        assert isinstance(value, str)
        gates = replace(gates, schema_revision=value)
    else:
        assert isinstance(value, bool)
        if field == "backup_verified":
            gates = replace(gates, backup_verified=value)
        elif field == "isolated_restore_verified":
            gates = replace(gates, isolated_restore_verified=value)
        elif field == "rollback_ready":
            gates = replace(gates, rollback_ready=value)
        else:
            raise AssertionError("unknown synthetic safety gate")

    with pytest.raises(MyBankStatementCutoverError, match=message):
        runner.run(_plan(source, digest, source.stat().st_size), gates=gates)

    assert parser.calls == []
    assert evidence_writer.calls == []
    assert registrar.calls == []
    assert importer.calls == []
    assert counts_reader.calls == 0


def test_database_cutover_requires_untampered_backup_and_restore_proof(
    tmp_path: Path,
) -> None:
    proof = _safety_proof(tmp_path, _before_counts())
    gates = _gates()

    verify_mybank_cutover_safety_proof(proof, gates=gates)

    (proof.backup_directory / "ledgerbridge-backup.tar.gpg").write_bytes(b"tampered")
    with pytest.raises(MyBankStatementCutoverError, match="backup and restore proof"):
        verify_mybank_cutover_safety_proof(proof, gates=gates)


def test_cutover_rejects_preflight_count_drift_before_reading_private_source(
    tmp_path: Path,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    parser = _Parser(statement)
    evidence_writer = _EvidenceWriter()
    registrar = _AccountRegistrar(iter(()))
    importer = _StatementImporter(iter((_import_result(created=True),)))
    drifted = replace(_before_counts(), candidates=21)
    counts_reader = _CountsReader(iter((drifted,)))
    runner = MyBankStatementCutoverRunner(
        parser=parser,
        evidence_writer=evidence_writer,
        account_registrar=registrar,
        statement_importer=importer,
        counts_reader=counts_reader,
    )

    with pytest.raises(MyBankStatementCutoverError, match="preflight"):
        runner.run(_plan(source, digest, source.stat().st_size), gates=_gates())

    assert parser.calls == []
    assert evidence_writer.calls == []
    assert importer.calls == []
    assert counts_reader.calls == 1


@pytest.mark.parametrize(
    ("statement_digest", "size_delta"),
    (("f" * 64, 0), (None, 1)),
)
def test_cutover_rejects_parser_identity_drift_before_evidence_write(
    tmp_path: Path,
    statement_digest: str | None,
    size_delta: int,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    observed_digest = statement_digest or digest
    statement = _statement(observed_digest, source.stat().st_size + size_delta)
    parser = _Parser(statement)
    evidence_writer = _EvidenceWriter()
    registrar = _AccountRegistrar(iter(()))
    importer = _StatementImporter(iter((_import_result(created=True),)))
    counts_reader = _CountsReader(iter((_before_counts(),)))
    runner = MyBankStatementCutoverRunner(
        parser=parser,
        evidence_writer=evidence_writer,
        account_registrar=registrar,
        statement_importer=importer,
        counts_reader=counts_reader,
    )

    with pytest.raises(MyBankStatementCutoverError, match="source identity"):
        runner.run(_plan(source, digest, source.stat().st_size), gates=_gates())

    assert len(parser.calls) == 1
    assert evidence_writer.calls == []
    assert importer.calls == []


def test_cutover_replays_same_package_with_created_false_and_no_count_drift(
    tmp_path: Path,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    runner, _, evidence_writer, registrar, importer, counts_reader = _successful_runner(statement)

    receipt = runner.run(_plan(source, digest, source.stat().st_size), gates=_gates())

    assert [result_context for _, result_context in importer.calls] == [
        importer.calls[0][1],
        importer.calls[0][1],
    ]
    assert len(evidence_writer.calls) == 1
    assert len(registrar.calls) == 2
    assert counts_reader.calls == 3
    assert receipt.replay_created is False
    assert receipt.after_counts == receipt.replay_counts == _after_counts()


def test_cutover_fails_closed_when_replay_receipt_conflicts_with_first_import(
    tmp_path: Path,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    parser = _Parser(statement)
    evidence_writer = _EvidenceWriter()
    registrar = _AccountRegistrar(
        iter((_registry_result(created=True), _registry_result(created=False)))
    )
    importer = _StatementImporter(
        iter((_import_result(created=True), _import_result(created=False, transaction_count=3)))
    )
    counts_reader = _CountsReader(iter((_before_counts(), _after_counts())))
    runner = MyBankStatementCutoverRunner(
        parser=parser,
        evidence_writer=evidence_writer,
        account_registrar=registrar,
        statement_importer=importer,
        counts_reader=counts_reader,
    )

    with pytest.raises(MyBankStatementCutoverError, match="conflict"):
        runner.run(_plan(source, digest, source.stat().st_size), gates=_gates())

    assert len(importer.calls) == 2


def test_cutover_fails_closed_when_first_import_is_an_unexpected_replay(
    tmp_path: Path,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    parser = _Parser(statement)
    evidence_writer = _EvidenceWriter()
    registrar = _AccountRegistrar(iter((_registry_result(created=True),)))
    importer = _StatementImporter(iter((_import_result(created=False),)))
    counts_reader = _CountsReader(iter((_before_counts(),)))
    runner = MyBankStatementCutoverRunner(
        parser=parser,
        evidence_writer=evidence_writer,
        account_registrar=registrar,
        statement_importer=importer,
        counts_reader=counts_reader,
    )

    with pytest.raises(MyBankStatementCutoverError, match="conflict"):
        runner.run(_plan(source, digest, source.stat().st_size), gates=_gates())

    assert len(importer.calls) == 1


def test_completed_package_replay_skips_evidence_write_and_changes_no_counts(
    tmp_path: Path,
) -> None:
    source, digest = _synthetic_source(tmp_path)
    statement = _statement(digest, source.stat().st_size)
    parser = _Parser(statement)
    evidence_writer = _EvidenceWriter()
    registrar = _AccountRegistrar(iter((_registry_result(created=False),)))
    importer = _StatementImporter(iter((_import_result(created=False),)))
    counts_reader = _CountsReader(iter((_after_counts(), _after_counts())))
    runner = MyBankStatementCutoverRunner(
        parser=parser,
        evidence_writer=evidence_writer,
        account_registrar=registrar,
        statement_importer=importer,
        counts_reader=counts_reader,
    )

    receipt = runner.run(
        _plan(source, digest, source.stat().st_size),
        gates=_gates(),
    )

    assert receipt.created is False
    assert receipt.replay_created is False
    assert receipt.before_counts == receipt.after_counts == receipt.replay_counts
    assert evidence_writer.calls == []
    assert len(registrar.calls) == 1
    assert len(importer.calls) == 1
    assert counts_reader.calls == 2


def test_cutover_error_and_logs_do_not_disclose_private_path_or_source_content(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source, digest = _synthetic_source(tmp_path, name="private-owner-identity.xlsx")
    private_marker = "SYNTHETIC-PRIVATE-FINANCIAL-CONTENT"

    def failing_parser(
        source_path: Path,
        *,
        expected_sha256: str,
        managed_account_suffix: str,
    ) -> MyBankStatement:
        del expected_sha256, managed_account_suffix
        raise BankStatementPersistenceError(f"{source_path}: {private_marker}")

    evidence_writer = _EvidenceWriter()
    registrar = _AccountRegistrar(iter(()))
    importer = _StatementImporter(iter((_import_result(created=True),)))
    counts_reader = _CountsReader(iter((_before_counts(),)))
    logger = logging.getLogger("ledgerbridge.test.mybank-cutover")
    runner = MyBankStatementCutoverRunner(
        parser=cast(Callable[..., MyBankStatement], failing_parser),
        evidence_writer=evidence_writer,
        account_registrar=registrar,
        statement_importer=importer,
        counts_reader=counts_reader,
        logger=logger,
    )

    with (
        caplog.at_level(logging.INFO, logger=logger.name),
        pytest.raises(MyBankStatementCutoverError) as raised,
    ):
        runner.run(_plan(source, digest, source.stat().st_size), gates=_gates())

    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    rendered_traceback = "".join(traceback.format_exception(raised.type, raised.value, raised.tb))
    for rendered in (str(raised.value), rendered_logs, rendered_traceback):
        assert str(source) not in rendered
        assert source.name not in rendered
        assert private_marker not in rendered
    assert evidence_writer.calls == []
    assert importer.calls == []


def test_success_receipt_does_not_disclose_source_path_or_transaction_fields(
    tmp_path: Path,
) -> None:
    source, digest = _synthetic_source(tmp_path, name="private-owner-identity.xlsx")
    statement = _statement(digest, source.stat().st_size)
    runner, _, _, _, _, _ = _successful_runner(statement)

    receipt = runner.run(_plan(source, digest, source.stat().st_size), gates=_gates())

    rendered = repr(receipt)
    assert str(source) not in rendered
    assert source.name not in rendered
    for transaction in statement.transactions:
        assert transaction.counterparty_name not in rendered
        assert transaction.counterparty_account not in rendered


def test_database_cutover_persists_encrypted_evidence_and_replays_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEDGERBRIDGE_ENV", raising=False)
    with _legacy_r1_database(reader=True) as database_url:
        command.upgrade(_upgrade_config(database_url), "head")
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO public.entity (id, entity_type, name) "
                    "VALUES (:entity, 'PERSON', 'Synthetic cutover entity')"
                ),
                {"entity": ENTITY_REF},
            )
            connection.execute(
                text(
                    "INSERT INTO public.business_unit (id, entity_id, ref, label) "
                    "VALUES (:unit, :entity, 'synthetic-cutover', 'Synthetic Cutover')"
                ),
                {"unit": BUSINESS_UNIT_REF, "entity": ENTITY_REF},
            )
        source = (tmp_path / "synthetic-mybank.xlsx").resolve()
        raw = _write_synthetic_mybank_xlsx(source)
        digest = hashlib.sha256(raw).hexdigest()
        os.chmod(tmp_path, 0o700)
        key_file = (tmp_path / "evidence-key.json").resolve()
        bootstrap_file_key(key_file, generation="synthetic-v1")
        artifact_root = (tmp_path / "artifacts").resolve()
        artifact_root.mkdir(mode=0o700)
        before = ProductionCounts(
            evidence_objects=0,
            encrypted_object_identities=0,
            encrypted_blob_versions=0,
            managed_accounts=0,
            managed_account_lifecycles=0,
            account_registry_operations=0,
            managed_account_aliases=0,
            account_business_unit_assignments=0,
            fact_business_unit_allocation_sets=0,
            fact_business_unit_allocation_items=0,
            bank_statements=0,
            bank_statement_transactions=0,
            bank_statement_observations=0,
            bank_statement_reviews=0,
            candidates=0,
            latest_pending_candidates=0,
            audit_events=0,
        )
        gates = replace(
            _gates(before, schema_revision="20260902_0031"),
            verify_fact_conflict=True,
        )
        safety_proof = _safety_proof(
            tmp_path,
            before,
            schema_revision="20260902_0031",
        )
        cutover_plan = _plan(source, digest, len(raw))
        registered = cutover_plan.registry_plan.accounts[0]
        registered = replace(
            registered,
            account_suffix="7968",
            aliases=(replace(registered.aliases[0], alias_value="0000 0000 0000 7968"),),
        )
        cutover_plan = replace(
            cutover_plan,
            account_suffix="7968",
            registry_plan=replace(cutover_plan.registry_plan, accounts=(registered,)),
        )

        preflight = run_transactional_database_mybank_statement_cutover(
            engine,
            cutover_plan,
            gates=gates,
            safety_proof=safety_proof,
            registry_principal=_registry_principal(),
            key_file=key_file,
            artifact_root=artifact_root,
            commit=False,
        )
        assert preflight.created is True
        assert preflight.fact_conflict_rejected is True
        assert not [
            path
            for path in artifact_root.rglob("*")
            if path.is_file() and path.name != ".quota.lock"
        ]

        receipt = run_transactional_database_mybank_statement_cutover(
            engine,
            cutover_plan,
            gates=gates,
            safety_proof=safety_proof,
            registry_principal=_registry_principal(),
            key_file=key_file,
            artifact_root=artifact_root,
            commit=True,
        )

        assert receipt.created is True
        assert receipt.replay_created is False
        assert receipt.after_counts == ProductionCounts(
            evidence_objects=1,
            encrypted_object_identities=1,
            encrypted_blob_versions=1,
            managed_accounts=1,
            managed_account_lifecycles=1,
            account_registry_operations=1,
            managed_account_aliases=1,
            account_business_unit_assignments=0,
            fact_business_unit_allocation_sets=0,
            fact_business_unit_allocation_items=0,
            bank_statements=1,
            bank_statement_transactions=2,
            bank_statement_observations=2,
            bank_statement_reviews=1,
            candidates=0,
            latest_pending_candidates=0,
            audit_events=12,
        )
        with engine.connect() as connection:
            evidence = connection.execute(
                text(
                    "SELECT entity_id, business_unit_id, media_type, plaintext_sha256, "
                    "plaintext_size FROM public.evidence_object WHERE evidence_ref=:evidence"
                ),
                {"evidence": EVIDENCE_REF},
            ).one()
            assert evidence.entity_id == ENTITY_REF
            assert evidence.business_unit_id == BUSINESS_UNIT_REF
            assert evidence.media_type == MEDIA_TYPE
            assert bytes(evidence.plaintext_sha256).hex() == digest
            assert evidence.plaintext_size == len(raw)
        engine.dispose()


def test_database_existing_account_import_preserves_registry_candidates_and_postings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LEDGERBRIDGE_ENV", raising=False)
    with _legacy_r1_database(reader=True) as database_url:
        command.upgrade(_upgrade_config(database_url), "head")
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO public.entity (id, entity_type, name) "
                    "VALUES (:entity, 'COMPANY', 'Synthetic existing-account entity')"
                ),
                {"entity": ENTITY_REF},
            )
            connection.execute(
                text(
                    "INSERT INTO public.business_unit (id, entity_id, ref, label) "
                    "VALUES (:unit, :entity, 'synthetic-existing', 'Synthetic Existing')"
                ),
                {"unit": BUSINESS_UNIT_REF, "entity": ENTITY_REF},
            )

            def append_audit(
                action: str,
                payload: dict[str, object],
                *,
                rule_version: str,
            ) -> UUID:
                value = connection.execute(
                    text(
                        "SELECT public.append_audit_event("
                        ":actor, :action, :reason, :rule, CAST(:payload AS jsonb))"
                    ),
                    {
                        "actor": "worker:synthetic-existing-account",
                        "action": action,
                        "reason": "synthetic existing-account setup",
                        "rule": rule_version,
                        "payload": json.dumps(payload),
                    },
                ).scalar_one()
                assert isinstance(value, UUID)
                return value

            evidence_audit = append_audit(
                "evidence.object.create",
                {
                    "evidence_ref": str(ADMISSION_EVIDENCE_REF),
                    "entity_id": str(ENTITY_REF),
                    "business_unit_id": str(BUSINESS_UNIT_REF),
                },
                rule_version="ledgerbridge.mybank-private-cutover.v1",
            )
            connection.execute(
                text(
                    "INSERT INTO public.evidence_object "
                    "(evidence_ref, entity_id, business_unit_id, media_type, display_name, "
                    "plaintext_sha256, plaintext_size, audit_event_id) VALUES "
                    "(:evidence, :entity, :unit, 'application/octet-stream', "
                    "'synthetic-admission.bin', :digest, 1, :audit)"
                ),
                {
                    "evidence": ADMISSION_EVIDENCE_REF,
                    "entity": ENTITY_REF,
                    "unit": BUSINESS_UNIT_REF,
                    "digest": b"a" * 32,
                    "audit": evidence_audit,
                },
            )
            account_audit = append_audit(
                "account_registry.account.register",
                {
                    "managed_account_ref": str(MANAGED_ACCOUNT_REF),
                    "owner_entity_ref": str(ENTITY_REF),
                    "owner_kind": "COMPANY",
                    "admission_evidence_ref": str(ADMISSION_EVIDENCE_REF),
                    "account_key": "managed-account:synthetic-existing",
                    "institution_code": "mybank",
                    "account_suffix": "7968",
                    "account_kind": "BANK_CHECKING",
                },
                rule_version="ledgerbridge.account-registry.v1",
            )
            connection.execute(
                text(
                    "INSERT INTO public.managed_account "
                    "(managed_account_ref, entity_id, account_key, institution_code, "
                    "account_suffix, owner_ref, owner_kind, account_kind, "
                    "admission_evidence_ref, audit_event_id, created_at) VALUES "
                    "(:account, :entity, 'managed-account:synthetic-existing', 'mybank', "
                    "'7968', :owner_ref, 'COMPANY', 'BANK_CHECKING', :evidence, :audit, "
                    "(SELECT occurred_at FROM public.audit_event WHERE id=:audit))"
                ),
                {
                    "account": MANAGED_ACCOUNT_REF,
                    "entity": ENTITY_REF,
                    "owner_ref": str(ENTITY_REF),
                    "evidence": ADMISSION_EVIDENCE_REF,
                    "audit": account_audit,
                },
            )
            lifecycle_audit = append_audit(
                "managed_account.lifecycle",
                {
                    "managed_account_ref": str(MANAGED_ACCOUNT_REF),
                    "revision": 1,
                    "status": "ACTIVE",
                },
                rule_version="ledgerbridge.bank-statement.v1",
            )
            connection.execute(
                text(
                    "INSERT INTO public.managed_account_lifecycle "
                    "(managed_account_ref, revision, status, audit_event_id, effective_at) "
                    "VALUES (:account, 1, 'ACTIVE', :audit, "
                    "(SELECT occurred_at FROM public.audit_event WHERE id=:audit))"
                ),
                {"account": MANAGED_ACCOUNT_REF, "audit": lifecycle_audit},
            )
            assignment_audit = append_audit(
                "account_registry.business_unit.assign",
                {
                    "assignment_ref": str(ASSIGNMENT_REF),
                    "managed_account_ref": str(MANAGED_ACCOUNT_REF),
                    "business_unit_id": str(BUSINESS_UNIT_REF),
                    "business_unit_ref_snapshot": "synthetic-existing",
                    "business_unit_label_snapshot": "Synthetic Existing",
                    "effective_from": "2026-01-01",
                    "effective_to": "",
                },
                rule_version="ledgerbridge.account-registry.v1",
            )
            connection.execute(
                text(
                    "INSERT INTO public.account_business_unit_assignment "
                    "(assignment_ref, owner_entity_id, managed_account_ref, business_unit_id, "
                    "business_unit_ref_snapshot, business_unit_label_snapshot, effective_from, "
                    "effective_to, audit_event_id, created_at) VALUES "
                    "(:assignment, :entity, :account, :unit, 'synthetic-existing', "
                    "'Synthetic Existing', DATE '2026-01-01', NULL, :audit, "
                    "(SELECT occurred_at FROM public.audit_event WHERE id=:audit))"
                ),
                {
                    "assignment": ASSIGNMENT_REF,
                    "entity": ENTITY_REF,
                    "account": MANAGED_ACCOUNT_REF,
                    "unit": BUSINESS_UNIT_REF,
                    "audit": assignment_audit,
                },
            )

        source = (tmp_path / "synthetic-existing-account.xlsx").resolve()
        raw = _write_synthetic_mybank_xlsx(source)
        digest = hashlib.sha256(raw).hexdigest()
        plan = replace(
            _existing_account_plan(source, digest, len(raw)),
            account_suffix="7968",
        )
        before = _read_production_counts(engine)
        os.chmod(tmp_path, 0o700)
        key_file = (tmp_path / "existing-account-evidence-key.json").resolve()
        bootstrap_file_key(key_file, generation="synthetic-existing-v1")
        artifact_root = (tmp_path / "existing-account-artifacts").resolve()
        artifact_root.mkdir(mode=0o700)
        gates = replace(
            _gates(before, schema_revision="20260902_0031"),
            verify_fact_conflict=True,
        )
        safety_proof = _safety_proof(
            tmp_path,
            before,
            schema_revision="20260902_0031",
        )

        preflight = run_transactional_database_mybank_existing_account_import(
            engine,
            plan,
            gates=gates,
            safety_proof=safety_proof,
            key_file=key_file,
            artifact_root=artifact_root,
            commit=False,
        )
        assert preflight.created is True
        assert preflight.fact_conflict_rejected is True
        assert _read_production_counts(engine) == before

        receipt = run_transactional_database_mybank_existing_account_import(
            engine,
            plan,
            gates=gates,
            safety_proof=safety_proof,
            key_file=key_file,
            artifact_root=artifact_root,
            commit=True,
        )

        assert receipt.registry_created is False
        assert receipt.registry_replay_created is False
        assert receipt.candidate_delta == 0
        assert receipt.after_counts.managed_accounts == before.managed_accounts
        assert receipt.after_counts.journal_entries == before.journal_entries
        assert receipt.after_counts.postings == before.postings
        assert receipt.after_counts.bank_statements == before.bank_statements + 1
        assert receipt.after_counts.bank_statement_transactions == (
            before.bank_statement_transactions + 2
        )
        engine.dispose()
