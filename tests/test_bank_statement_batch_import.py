from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import ledgerbridge.mybank_statement_cutover as cutover
from ledgerbridge.bank_statement_contract import BankStatementParserProfile


def _counts() -> cutover.ProductionCounts:
    return cutover.ProductionCounts(
        evidence_objects=4,
        encrypted_object_identities=4,
        encrypted_blob_versions=4,
        managed_accounts=5,
        managed_account_lifecycles=5,
        account_registry_operations=5,
        managed_account_aliases=5,
        account_business_unit_assignments=5,
        fact_business_unit_allocation_sets=0,
        fact_business_unit_allocation_items=0,
        bank_statements=1,
        bank_statement_transactions=9,
        bank_statement_observations=9,
        bank_statement_reviews=1,
        candidates=17,
        latest_pending_candidates=3,
        audit_events=40,
    )


def _plan(tmp_path: Path, ordinal: int) -> SimpleNamespace:
    source = tmp_path / f"statement-{ordinal}.xlsx"
    source.write_bytes(f"statement-{ordinal}".encode())
    return SimpleNamespace(
        evidence_ref=uuid4(),
        expected_sha256=f"{ordinal:x}" * 64,
        managed_account_ref=uuid4(),
        period_start=date(2026, 8, ordinal),
        period_end=date(2026, 8, ordinal),
        parser_profile=BankStatementParserProfile.MYBANK_COMPANY_DAILY_XLSX_V2,
        source_path=source.resolve(),
        account_suffix=f"{ordinal:04d}",
        expected_transaction_count=ordinal,
        evidence_mode=cutover.MyBankEvidenceMode.CREATE_NEW,
    )


class _Transaction:
    def __init__(self) -> None:
        self.is_active = True
        self.committed = False
        self.rolled_back = False

    def commit(self) -> None:
        self.is_active = False
        self.committed = True

    def rollback(self) -> None:
        self.is_active = False
        self.rolled_back = True


class _Connection:
    def __init__(self) -> None:
        self.transaction = _Transaction()
        self.statements: list[str] = []

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def begin(self) -> _Transaction:
        return self.transaction

    def execute(self, statement: object) -> None:
        self.statements.append(str(statement))


class _Engine:
    def __init__(self) -> None:
        self.connection = _Connection()

    def connect(self) -> _Connection:
        return self.connection


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial: cutover.ProductionCounts,
    fail_ordinal: int | None = None,
) -> tuple[
    list[cutover.ProductionCounts],
    list[object],
    list[object],
    dict[str, cutover.ProductionCounts],
]:
    state = {"counts": initial}
    observed_gates: list[cutover.ProductionCounts] = []
    boundaries: list[object] = []
    proofs: list[object] = []

    class Boundary:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            self.plan = kwargs["plan"]
            self.committed = False
            self.aborted = False
            boundaries.append(self)

        def write(self, *_args: object) -> None:
            return None

        def commit_publication(self) -> None:
            self.committed = True

        def abort_publication(self) -> None:
            self.aborted = True

    class Runner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(
            self,
            plan: SimpleNamespace,
            *,
            gates: cutover.MyBankStatementCutoverGates,
        ) -> cutover.MyBankStatementCutoverReceipt:
            expected = gates.expected_before
            observed_gates.append(expected)
            ordinal = plan.expected_transaction_count
            if ordinal == fail_ordinal:
                raise cutover.MyBankStatementCutoverError("synthetic batch failure")
            before = state["counts"]
            expected_completed = cutover._expected_after_existing_account(
                expected,
                plan,  # type: ignore[arg-type]
            )
            created = before != expected_completed
            after = (
                replace(
                    before,
                    evidence_objects=before.evidence_objects + 1,
                    encrypted_object_identities=before.encrypted_object_identities + 1,
                    encrypted_blob_versions=before.encrypted_blob_versions + 1,
                    bank_statements=before.bank_statements + 1,
                    bank_statement_transactions=(before.bank_statement_transactions + ordinal),
                    bank_statement_observations=(before.bank_statement_observations + ordinal),
                    bank_statement_reviews=before.bank_statement_reviews + 1,
                    audit_events=before.audit_events + 4 + 2 * ordinal,
                )
                if created
                else before
            )
            state["counts"] = after
            return cutover.MyBankStatementCutoverReceipt(
                statement_ref=uuid4(),
                evidence_ref=plan.evidence_ref,
                managed_account_ref=plan.managed_account_ref,
                registry_created=False,
                created=created,
                registry_replay_created=False,
                replay_created=False,
                transaction_count=ordinal,
                review_status="PENDING",
                before_counts=before,
                after_counts=after,
                replay_counts=after,
                fact_conflict_rejected=False,
            )

    monkeypatch.setattr(cutover, "_DatabaseEvidenceBoundary", Boundary)
    monkeypatch.setattr(cutover, "MyBankExistingAccountStatementRunner", Runner)
    monkeypatch.setattr(cutover, "_DatabaseExistingAccountAuthorizer", lambda value: value)
    monkeypatch.setattr(cutover, "_DatabaseStatementImporter", lambda *args, **kwargs: args[0])
    monkeypatch.setattr(cutover, "_read_production_counts", lambda _bind: state["counts"])
    monkeypatch.setattr(cutover, "_read_schema_revision", lambda _bind: "20260902_0033")
    monkeypatch.setattr(cutover, "parse_bank_statement", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cutover, "_run_database_fact_conflict_probe", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cutover,
        "verify_mybank_cutover_safety_proof",
        lambda proof, **_kwargs: proofs.append(proof),
    )
    return observed_gates, boundaries, proofs, state


def _gates(before: cutover.ProductionCounts) -> cutover.MyBankStatementCutoverGates:
    return cutover.MyBankStatementCutoverGates(
        schema_revision="20260902_0033",
        backup_verified=True,
        isolated_restore_verified=True,
        rollback_ready=True,
        expected_before=before,
        verify_fact_conflict=True,
    )


def test_batch_advances_item_count_gates_and_preflight_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _counts()
    observed, boundaries, proofs, _ = _install_fakes(monkeypatch, initial=before)
    engine = _Engine()
    plans = (_plan(tmp_path, 1), _plan(tmp_path, 2))

    receipts = cutover.run_transactional_database_bank_statement_existing_account_batch_import(
        engine,  # type: ignore[arg-type]
        plans,  # type: ignore[arg-type]
        gates=_gates(before),
        safety_proof=object(),  # type: ignore[arg-type]
        key_file=tmp_path / "key.json",
        artifact_root=tmp_path / "artifacts",
        commit=False,
    )

    assert len(proofs) == 1
    assert observed == [before, receipts[0].after_counts]
    assert receipts[1].before_counts == receipts[0].after_counts
    assert all(receipt.fact_conflict_rejected for receipt in receipts)
    assert engine.connection.statements == ["SET CONSTRAINTS ALL IMMEDIATE"]
    assert engine.connection.transaction.rolled_back is True
    assert all(boundary.aborted is True for boundary in boundaries)  # type: ignore[attr-defined]


def test_batch_failure_rolls_back_and_aborts_every_staged_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _counts()
    _, boundaries, _, _ = _install_fakes(monkeypatch, initial=before, fail_ordinal=2)
    engine = _Engine()

    with pytest.raises(cutover.MyBankStatementCutoverError, match="synthetic batch failure"):
        cutover.run_transactional_database_bank_statement_existing_account_batch_import(
            engine,  # type: ignore[arg-type]
            (_plan(tmp_path, 1), _plan(tmp_path, 2)),  # type: ignore[arg-type]
            gates=_gates(before),
            safety_proof=object(),  # type: ignore[arg-type]
            key_file=tmp_path / "key.json",
            artifact_root=tmp_path / "artifacts",
            commit=True,
        )

    assert engine.connection.transaction.rolled_back is True
    assert engine.connection.transaction.committed is False
    assert len(boundaries) == 2
    assert all(boundary.aborted is True for boundary in boundaries)  # type: ignore[attr-defined]


def test_completed_batch_replay_is_zero_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _counts()
    observed, _, proofs, state = _install_fakes(monkeypatch, initial=before)
    plans = (_plan(tmp_path, 1), _plan(tmp_path, 2))

    first = cutover.run_transactional_database_bank_statement_existing_account_batch_import(
        _Engine(),  # type: ignore[arg-type]
        plans,  # type: ignore[arg-type]
        gates=_gates(before),
        safety_proof=object(),  # type: ignore[arg-type]
        key_file=tmp_path / "key.json",
        artifact_root=tmp_path / "artifacts",
        commit=True,
    )
    completed = state["counts"]
    expected_completed = before
    for plan in plans:
        expected_completed = cutover._expected_after_existing_account(
            expected_completed,
            plan,  # type: ignore[arg-type]
        )
    assert completed == expected_completed
    replay = cutover.run_transactional_database_bank_statement_existing_account_batch_import(
        _Engine(),  # type: ignore[arg-type]
        plans,  # type: ignore[arg-type]
        gates=_gates(before),
        safety_proof=object(),  # type: ignore[arg-type]
        key_file=tmp_path / "key.json",
        artifact_root=tmp_path / "artifacts",
        commit=True,
    )

    assert len(proofs) == 2
    assert all(receipt.created for receipt in first)
    assert all(receipt.created is False for receipt in replay)
    assert all(receipt.before_counts == completed for receipt in replay)
    assert all(receipt.after_counts == completed for receipt in replay)
    assert state["counts"] == completed
    assert observed[-2:] == [
        cutover._expected_before_existing_account(
            completed,
            plan,  # type: ignore[arg-type]
        )
        for plan in plans
    ]


def test_eighteen_item_acceptance_failure_aborts_the_whole_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _counts()
    _, boundaries, _, _ = _install_fakes(monkeypatch, initial=before)
    engine = _Engine()
    plans = tuple(_plan(tmp_path, ordinal) for ordinal in range(1, 19))

    def reject(
        _receipts: tuple[cutover.MyBankStatementCutoverReceipt, ...],
        _connection: object,
    ) -> None:
        raise RuntimeError("synthetic acceptance failure")

    with pytest.raises(RuntimeError, match="synthetic acceptance failure"):
        cutover.run_transactional_database_bank_statement_existing_account_batch_import(
            engine,  # type: ignore[arg-type]
            plans,  # type: ignore[arg-type]
            gates=_gates(before),
            safety_proof=object(),  # type: ignore[arg-type]
            key_file=tmp_path / "key.json",
            artifact_root=tmp_path / "artifacts",
            commit=True,
            acceptance=reject,
        )

    assert engine.connection.transaction.rolled_back is True
    assert engine.connection.transaction.committed is False
    assert len(boundaries) == 18
    assert all(boundary.aborted is True for boundary in boundaries)  # type: ignore[attr-defined]


def test_item_deferred_constraints_are_validated_then_redeferred() -> None:
    statements: list[str] = []

    class Session:
        def execute(self, statement: object) -> None:
            statements.append(str(statement))

    cutover._validate_and_redefer_transaction_constraints(Session())  # type: ignore[arg-type]

    assert statements == [
        "SET CONSTRAINTS ALL IMMEDIATE",
        "SET CONSTRAINTS ALL DEFERRED",
    ]


def test_adjacent_same_unit_assignments_cover_a_range_statement() -> None:
    unit = uuid4()

    assert cutover._assignments_cover_statement_period(
        (
            {
                "business_unit_id": unit,
                "effective_from": date(2025, 9, 2),
                "effective_to": date(2026, 8, 21),
            },
            {
                "business_unit_id": unit,
                "effective_from": date(2026, 8, 21),
                "effective_to": date(2026, 8, 25),
            },
            {
                "business_unit_id": unit,
                "effective_from": date(2026, 8, 25),
                "effective_to": None,
            },
        ),
        business_unit_ref=unit,
        period_start=date(2025, 9, 2),
        period_end=date(2026, 9, 2),
    )


def test_assignment_coverage_rejects_gap_or_unit_change() -> None:
    unit = uuid4()
    other = uuid4()
    base = (
        {
            "business_unit_id": unit,
            "effective_from": date(2025, 9, 2),
            "effective_to": date(2026, 8, 20),
        },
        {
            "business_unit_id": unit,
            "effective_from": date(2026, 8, 21),
            "effective_to": None,
        },
    )
    changed = (base[0], {**base[1], "business_unit_id": other})

    assert not cutover._assignments_cover_statement_period(
        base,
        business_unit_ref=unit,
        period_start=date(2025, 9, 2),
        period_end=date(2026, 9, 2),
    )
    assert not cutover._assignments_cover_statement_period(
        changed,
        business_unit_ref=unit,
        period_start=date(2025, 9, 2),
        period_end=date(2026, 9, 2),
    )
