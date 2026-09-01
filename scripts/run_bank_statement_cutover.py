"""Run one private bank-statement cutover preflight or enabled production import."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from ledgerbridge.bank_statement_cutover import (
    BankStatementCutoverGates,
    BankStatementCutoverReceipt,
    BankStatementCutoverSafetyProof,
    production_counts_from_cutover_inventory,
    run_transactional_database_bank_statement_existing_account_import,
)
from ledgerbridge.bank_statement_cutover_command import (
    run_bank_statement_cutover_command,
)
from ledgerbridge.bank_statement_cutover_plan_builder import LoadedBankStatementPlan
from scripts.backup_restore import (
    R1_CUTOVER_INVENTORY_SQL,
    CutoverInventory,
    validate_bank_statement_existing_account_inventory_sequence,
)

_MAX_REPORT_BYTES = 4 * 1024 * 1024


def _execute(
    loaded: LoadedBankStatementPlan,
    database_url: str,
    *,
    commit: bool,
) -> BankStatementCutoverReceipt:
    before = _load_before_inventory(
        loaded.restore_report,
        expected_revision=loaded.target_revision,
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
        receipt: BankStatementCutoverReceipt,
        connection: Connection,
    ) -> None:
        raw = connection.execute(text(R1_CUTOVER_INVENTORY_SQL)).scalar_one()
        after = CutoverInventory.from_payload(json.loads(raw))
        report = validate_bank_statement_existing_account_inventory_sequence(
            before=before,
            after=after,
            replay=after,
            conflict=after,
            transaction_count=receipt.transaction_count,
            evidence_mode=loaded.cutover.evidence_mode,
        )
        if report.get("replay_delta") != 0 or report.get("conflict_delta") != 0:
            raise RuntimeError("cutover inventory acceptance failed")

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        return run_transactional_database_bank_statement_existing_account_import(
            engine,
            loaded.cutover,
            gates=gates,
            safety_proof=BankStatementCutoverSafetyProof(
                backup_directory=loaded.backup_directory,
                restore_report=loaded.restore_report,
            ),
            key_file=loaded.key_file,
            artifact_root=loaded.artifact_root,
            commit=commit,
            acceptance=accept,
        )
    finally:
        engine.dispose()


def _load_before_inventory(path: Path, *, expected_revision: str) -> CutoverInventory:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_REPORT_BYTES:
        raise RuntimeError("cutover safety report is invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("revision") != expected_revision:
        raise RuntimeError("cutover safety report is invalid")
    source = value.get("source_database_metadata")
    if not isinstance(source, dict):
        raise RuntimeError("cutover safety report is invalid")
    return CutoverInventory.from_payload(source.get("cutover_inventory"))


def main() -> int:
    try:
        return run_bank_statement_cutover_command(executor=_execute)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        print("BANK_STATEMENT_CUTOVER_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
