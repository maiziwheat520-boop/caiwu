from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import ledgerbridge.bank_statement_batch_cutover_command as batch_command
from ledgerbridge.bank_statement_batch_cutover_command import (
    BANK_STATEMENT_BATCH_PREFLIGHT_RECEIPT_SCHEMA,
    BANK_STATEMENT_BATCH_PRODUCTION_RECEIPT_SCHEMA,
    BANK_STATEMENT_PRIVATE_BATCH_SCHEMA,
    BankStatementBatchCommittedReceiptError,
    BankStatementBatchCutoverCommandError,
    run_bank_statement_batch_cutover_command,
)
from ledgerbridge.bank_statement_cutover import (
    BankStatementCutoverReceipt,
    ProductionCounts,
)
from ledgerbridge.bank_statement_cutover_plan_builder import (
    BANK_STATEMENT_EXISTING_ACCOUNT_PLAN_SCHEMA,
    LoadedBankStatementPlan,
)


def _private_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _plan_payload(
    root: Path,
    *,
    item_id: str,
    ordinal: int,
    revision: str,
    backup: Path,
    restore: Path,
) -> dict[str, object]:
    source = (root / "items" / item_id / "source.xlsx").resolve()
    digest = hashlib.sha256(f"source-{ordinal}".encode()).hexdigest()
    period = f"2026-08-{20 + ordinal:02d}"
    return {
        "schema_version": BANK_STATEMENT_EXISTING_ACCOUNT_PLAN_SCHEMA,
        "target_revision": revision,
        "parser": {"profile": "mybank_company_daily_xlsx_v2"},
        "source": {
            "path": str(source),
            "sha256": digest,
            "size": 100 + ordinal,
            "account_suffix": f"{ordinal:04d}",
            "period_start": period,
            "period_end": period,
            "transaction_count": ordinal + 1,
            "transaction_set_sha256": hashlib.sha256(
                f"transactions-{ordinal}".encode()
            ).hexdigest(),
            "parser_facts_sha256": hashlib.sha256(f"facts-{ordinal}".encode()).hexdigest(),
            "monthly_transaction_counts": [{"month": "2026-08", "count": ordinal + 1}],
        },
        "scope": {
            "evidence_ref": str(UUID(int=ordinal)),
            "evidence_mode": "CREATE_NEW",
            "owner_entity_ref": str(UUID(int=100 + ordinal)),
            "business_unit_ref": str(UUID(int=200 + ordinal)),
            "owner_kind": "COMPANY",
        },
        "account": {
            "managed_account_ref": str(UUID(int=300 + ordinal)),
            "institution_code": "mybank",
        },
        "audit": {
            "actor": "worker:synthetic-batch-cutover",
            "reason": "operator-confirmed synthetic batch",
        },
        "safety": {
            "backup_directory": str(backup),
            "restore_report": str(restore),
            "key_file": str((root / "key.json").resolve()),
            "artifact_root": str((root / "artifacts").resolve()),
        },
    }


def _batch(tmp_path: Path, *, count: int = 2) -> tuple[Path, list[Path]]:
    root = (tmp_path / "private-batch").resolve()
    root.mkdir()
    revision = "c" * 40
    backup = (tmp_path / "backup").resolve()
    restore = (backup / "restore.json").resolve()
    items: list[dict[str, object]] = []
    plans: list[Path] = []
    for ordinal in range(1, count + 1):
        item_id = f"{ordinal:04d}"
        payload = _plan_payload(
            root,
            item_id=item_id,
            ordinal=ordinal,
            revision=revision,
            backup=backup,
            restore=restore,
        )
        plan_path = _private_json((root / "items" / item_id / "plan.json").resolve(), payload)
        plans.append(plan_path)
        source = payload["source"]
        scope = payload["scope"]
        assert isinstance(source, dict)
        assert isinstance(scope, dict)
        items.append(
            {
                "item_id": item_id,
                "source_group": "synthetic",
                "source_name": f"statement-{ordinal}.xlsx",
                "source_sha256": source["sha256"],
                "source_size": source["size"],
                "account_suffix": source["account_suffix"],
                "period": source["period_start"],
                "transaction_count": source["transaction_count"],
                "evidence_ref": scope["evidence_ref"],
            }
        )
    manifest = _private_json(
        (root / "manifest.json").resolve(),
        {
            "schema_version": BANK_STATEMENT_PRIVATE_BATCH_SCHEMA,
            "target_revision": revision,
            "backup_directory": str(backup),
            "restore_report": str(restore),
            "item_count": count,
            "transaction_count": sum(range(2, count + 2)),
            "items": items,
            "skipped_empty_count": 0,
            "skipped_empty": [],
            "skipped_existing_count": 0,
            "skipped_existing": [],
        },
    )
    return manifest, plans


def _counts() -> ProductionCounts:
    return ProductionCounts(
        evidence_objects=10,
        encrypted_object_identities=10,
        encrypted_blob_versions=10,
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
        candidates=20,
        latest_pending_candidates=4,
        audit_events=30,
    )


def _receipts(
    loaded: tuple[LoadedBankStatementPlan, ...],
) -> tuple[BankStatementCutoverReceipt, ...]:
    current = _counts()
    receipts: list[BankStatementCutoverReceipt] = []
    for plan in loaded:
        after = replace(
            current,
            evidence_objects=current.evidence_objects + 1,
            encrypted_object_identities=current.encrypted_object_identities + 1,
            encrypted_blob_versions=current.encrypted_blob_versions + 1,
            bank_statements=current.bank_statements + 1,
            bank_statement_transactions=(
                current.bank_statement_transactions + plan.cutover.expected_transaction_count
            ),
            bank_statement_observations=(
                current.bank_statement_observations + plan.cutover.expected_transaction_count
            ),
            bank_statement_reviews=current.bank_statement_reviews + 1,
            audit_events=current.audit_events + 6,
        )
        receipts.append(
            BankStatementCutoverReceipt(
                statement_ref=uuid4(),
                evidence_ref=plan.cutover.evidence_ref,
                managed_account_ref=plan.cutover.managed_account_ref,
                registry_created=False,
                created=True,
                registry_replay_created=False,
                replay_created=False,
                transaction_count=plan.cutover.expected_transaction_count,
                review_status="PENDING",
                before_counts=current,
                after_counts=after,
                replay_counts=after,
                fact_conflict_rejected=True,
            )
        )
        current = after
    return tuple(receipts)


def _environment(manifest: Path, receipt: Path) -> dict[str, str]:
    return {
        "LEDGERBRIDGE_BANK_STATEMENT_PRIVATE_BATCH": str(manifest),
        "LEDGERBRIDGE_BANK_STATEMENT_BATCH_PREFLIGHT_RECEIPT": str(receipt),
        "LEDGERBRIDGE_BANK_STATEMENT_BATCH_PRODUCTION_RECEIPT": str(
            receipt.with_name("batch-production.json")
        ),
        "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_URL": "postgresql://synthetic",
        "LEDGERBRIDGE_DEPLOYED_REVISION": "c" * 40,
    }


def test_batch_preflight_and_production_bind_the_same_digest(
    tmp_path: Path,
) -> None:
    manifest, _ = _batch(tmp_path)
    receipt = (tmp_path / "batch-preflight.json").resolve()
    calls: list[bool] = []

    def execute(
        loaded: tuple[LoadedBankStatementPlan, ...],
        database_url: str,
        *,
        commit: bool,
    ) -> tuple[BankStatementCutoverReceipt, ...]:
        assert database_url == "postgresql://synthetic"
        calls.append(commit)
        return _receipts(loaded)

    common = _environment(manifest, receipt)
    assert (
        run_bank_statement_batch_cutover_command(
            ["--preflight-only"],
            environ={
                **common,
                "LEDGERBRIDGE_ENV": "test",
                "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "isolated",
            },
            executor=execute,
        )
        == 0
    )
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["schema_version"] == BANK_STATEMENT_BATCH_PREFLIGHT_RECEIPT_SCHEMA
    assert len(payload["batch_sha256"]) == 64
    assert payload["item_count"] == 2
    assert payload["transaction_count"] == 5

    assert (
        run_bank_statement_batch_cutover_command(
            ["--execute-production"],
            environ={
                **common,
                "LEDGERBRIDGE_ENV": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_PRODUCTION_EXECUTION": ("execute-reviewed-cutover-v1"),
            },
            executor=execute,
        )
        == 0
    )
    assert calls == [False, True]
    production = json.loads(receipt.with_name("batch-production.json").read_text(encoding="utf-8"))
    assert production["schema_version"] == BANK_STATEMENT_BATCH_PRODUCTION_RECEIPT_SCHEMA
    assert production["batch_sha256"] == payload["batch_sha256"]
    assert [item["plan_sha256"] for item in production["items"]] == [
        item["plan_sha256"] for item in payload["items"]
    ]


def test_batch_rejects_a_manifest_item_tampered_against_its_plan(
    tmp_path: Path,
) -> None:
    manifest, _ = _batch(tmp_path, count=1)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["items"][0]["source_size"] += 1
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BankStatementBatchCutoverCommandError):
        run_bank_statement_batch_cutover_command(
            ["--preflight-only"],
            environ={
                **_environment(manifest, (tmp_path / "receipt.json").resolve()),
                "LEDGERBRIDGE_ENV": "test",
                "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "isolated",
            },
            executor=lambda *_args, **_kwargs: (),
        )


def test_batch_rejects_a_plan_changed_after_preflight(
    tmp_path: Path,
) -> None:
    manifest, plans = _batch(tmp_path, count=1)
    receipt = (tmp_path / "receipt.json").resolve()

    def execute(
        loaded: tuple[LoadedBankStatementPlan, ...],
        _database_url: str,
        *,
        commit: bool,
    ) -> tuple[BankStatementCutoverReceipt, ...]:
        assert isinstance(commit, bool)
        return _receipts(loaded)

    common = _environment(manifest, receipt)
    run_bank_statement_batch_cutover_command(
        ["--preflight-only"],
        environ={
            **common,
            "LEDGERBRIDGE_ENV": "test",
            "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "isolated",
        },
        executor=execute,
    )
    plan = json.loads(plans[0].read_text(encoding="utf-8"))
    plan["audit"]["reason"] = "changed after review"
    plans[0].write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(
        BankStatementBatchCutoverCommandError,
        match="preflight receipt is invalid",
    ):
        run_bank_statement_batch_cutover_command(
            ["--execute-production"],
            environ={
                **common,
                "LEDGERBRIDGE_ENV": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_PRODUCTION_EXECUTION": ("execute-reviewed-cutover-v1"),
            },
            executor=execute,
        )


@pytest.mark.parametrize("mutation", ["reorder", "delete"])
def test_batch_rejects_manifest_membership_changed_after_preflight(
    tmp_path: Path,
    mutation: str,
) -> None:
    manifest, _ = _batch(tmp_path)
    receipt = (tmp_path / "receipt.json").resolve()

    def execute(
        loaded: tuple[LoadedBankStatementPlan, ...],
        _database_url: str,
        *,
        commit: bool,
    ) -> tuple[BankStatementCutoverReceipt, ...]:
        assert isinstance(commit, bool)
        return _receipts(loaded)

    common = _environment(manifest, receipt)
    run_bank_statement_batch_cutover_command(
        ["--preflight-only"],
        environ={
            **common,
            "LEDGERBRIDGE_ENV": "test",
            "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "isolated",
        },
        executor=execute,
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if mutation == "reorder":
        payload["items"].reverse()
    else:
        payload["items"].pop()
        payload["item_count"] = 1
        payload["transaction_count"] = payload["items"][0]["transaction_count"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        BankStatementBatchCutoverCommandError,
        match="preflight receipt is invalid",
    ):
        run_bank_statement_batch_cutover_command(
            ["--execute-production"],
            environ={
                **common,
                "LEDGERBRIDGE_ENV": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_PRODUCTION_EXECUTION": ("execute-reviewed-cutover-v1"),
            },
            executor=execute,
        )


def test_batch_rejects_a_missing_item_plan(tmp_path: Path) -> None:
    manifest, plans = _batch(tmp_path, count=1)
    plans[0].unlink()

    with pytest.raises(BankStatementBatchCutoverCommandError):
        run_bank_statement_batch_cutover_command(
            ["--preflight-only"],
            environ={
                **_environment(manifest, (tmp_path / "receipt.json").resolve()),
                "LEDGERBRIDGE_ENV": "test",
                "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "isolated",
            },
            executor=lambda *_args, **_kwargs: (),
        )


def test_batch_production_requires_a_preflight_receipt(tmp_path: Path) -> None:
    manifest, _ = _batch(tmp_path, count=1)
    called = False

    def execute(
        loaded: tuple[LoadedBankStatementPlan, ...],
        _database_url: str,
        *,
        commit: bool,
    ) -> tuple[BankStatementCutoverReceipt, ...]:
        nonlocal called
        called = True
        return _receipts(loaded)

    with pytest.raises(
        BankStatementBatchCutoverCommandError,
        match="preflight receipt is invalid",
    ):
        run_bank_statement_batch_cutover_command(
            ["--execute-production"],
            environ={
                **_environment(manifest, (tmp_path / "missing-receipt.json").resolve()),
                "LEDGERBRIDGE_ENV": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_PRODUCTION_EXECUTION": ("execute-reviewed-cutover-v1"),
            },
            executor=execute,
        )
    assert called is False


def test_batch_rejects_non_contiguous_item_receipts(tmp_path: Path) -> None:
    manifest, _ = _batch(tmp_path)

    def execute(
        loaded: tuple[LoadedBankStatementPlan, ...],
        _database_url: str,
        *,
        commit: bool,
    ) -> tuple[BankStatementCutoverReceipt, ...]:
        assert commit is False
        receipts = _receipts(loaded)
        return (
            receipts[0],
            replace(receipts[1], before_counts=_counts()),
        )

    with pytest.raises(
        BankStatementBatchCutoverCommandError,
        match="acceptance receipts conflict",
    ):
        run_bank_statement_batch_cutover_command(
            ["--preflight-only"],
            environ={
                **_environment(manifest, (tmp_path / "receipt.json").resolve()),
                "LEDGERBRIDGE_ENV": "test",
                "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "isolated",
            },
            executor=execute,
        )


def test_production_receipt_failure_reports_committed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = _batch(tmp_path, count=1)
    receipt = (tmp_path / "receipt.json").resolve()

    def execute(
        loaded: tuple[LoadedBankStatementPlan, ...],
        _database_url: str,
        *,
        commit: bool,
    ) -> tuple[BankStatementCutoverReceipt, ...]:
        return _receipts(loaded)

    common = _environment(manifest, receipt)
    run_bank_statement_batch_cutover_command(
        ["--preflight-only"],
        environ={
            **common,
            "LEDGERBRIDGE_ENV": "test",
            "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "isolated",
        },
        executor=execute,
    )

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(batch_command, "_write_private_json", fail_write)

    with pytest.raises(
        BankStatementBatchCommittedReceiptError,
        match="committed but durable receipt persistence failed",
    ):
        run_bank_statement_batch_cutover_command(
            ["--execute-production"],
            environ={
                **common,
                "LEDGERBRIDGE_ENV": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_DATABASE_TARGET": "production",
                "LEDGERBRIDGE_BANK_STATEMENT_PRODUCTION_EXECUTION": ("execute-reviewed-cutover-v1"),
            },
            executor=execute,
        )
