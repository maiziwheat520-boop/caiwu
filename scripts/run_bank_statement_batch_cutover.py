"""Run one private bank-statement batch preflight or enabled production import."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from ledgerbridge.bank_statement_batch_cutover_command import (
    BankStatementBatchCommittedReceiptError,
    run_bank_statement_batch_cutover_command,
    validate_bank_statement_batch_receipts,
)
from ledgerbridge.bank_statement_cutover import (
    BankStatementCutoverGates,
    BankStatementCutoverReceipt,
    BankStatementCutoverSafetyProof,
    production_counts_from_cutover_inventory,
    run_transactional_database_bank_statement_existing_account_batch_import,
)
from ledgerbridge.bank_statement_cutover_plan_builder import LoadedBankStatementPlan
from scripts.backup_restore import R1_CUTOVER_INVENTORY_SQL, CutoverInventory

_MAX_REPORT_BYTES = 4 * 1024 * 1024


def _execute(
    loaded: tuple[LoadedBankStatementPlan, ...],
    database_url: str,
    *,
    commit: bool,
) -> tuple[BankStatementCutoverReceipt, ...]:
    first = loaded[0]
    before = _load_before_inventory(
        first.restore_report,
        expected_revision=first.target_revision,
    )
    expected_before = production_counts_from_cutover_inventory(
        before.as_payload(),
        expected_schema_revision=before.schema_revision,
    )
    gates = BankStatementCutoverGates(
        schema_revision=before.schema_revision,
        backup_verified=True,
        isolated_restore_verified=True,
        rollback_ready=True,
        expected_before=expected_before,
        verify_fact_conflict=True,
    )

    def accept(
        receipts: tuple[BankStatementCutoverReceipt, ...],
        connection: Connection,
    ) -> None:
        validate_bank_statement_batch_receipts(receipts, loaded)
        raw = connection.execute(text(R1_CUTOVER_INVENTORY_SQL)).scalar_one()
        after = CutoverInventory.from_payload(json.loads(raw))
        observed = production_counts_from_cutover_inventory(
            after.as_payload(),
            expected_schema_revision=before.schema_revision,
        )
        if observed != receipts[-1].after_counts:
            raise RuntimeError("batch cutover inventory acceptance failed")

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        return run_transactional_database_bank_statement_existing_account_batch_import(
            engine,
            tuple(item.cutover for item in loaded),
            gates=gates,
            safety_proof=BankStatementCutoverSafetyProof(
                backup_directory=first.backup_directory,
                restore_report=first.restore_report,
            ),
            key_file=first.key_file,
            artifact_root=first.artifact_root,
            commit=commit,
            acceptance=accept,
        )
    finally:
        engine.dispose()


def _load_before_inventory(path: Path, *, expected_revision: str) -> CutoverInventory:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_REPORT_BYTES:
        raise RuntimeError("batch cutover safety report is invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("revision") != expected_revision:
        raise RuntimeError("batch cutover safety report is invalid")
    source = value.get("source_database_metadata")
    if not isinstance(source, dict):
        raise RuntimeError("batch cutover safety report is invalid")
    return CutoverInventory.from_payload(source.get("cutover_inventory"))


def main() -> int:
    try:
        return run_bank_statement_batch_cutover_command(executor=_execute)
    except BankStatementBatchCommittedReceiptError:
        print("BANK_STATEMENT_BATCH_COMMITTED_RECEIPT_PENDING", file=sys.stderr)
        return 2
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        print("BANK_STATEMENT_BATCH_CUTOVER_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
