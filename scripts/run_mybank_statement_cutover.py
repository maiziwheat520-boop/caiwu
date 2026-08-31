"""Run one private MYbank cutover preflight or explicitly enabled production import."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from ledgerbridge.mybank_cutover_command import (
    LoadedMyBankCutoverPlan,
    run_mybank_cutover_command,
)
from ledgerbridge.mybank_statement_cutover import (
    MyBankStatementCutoverGates,
    MyBankStatementCutoverReceipt,
    production_counts_from_cutover_inventory,
    run_transactional_database_mybank_statement_cutover,
)
from scripts.backup_restore import (
    R1_CUTOVER_INVENTORY_SQL,
    CutoverInventory,
    validate_mybank_cutover_inventory_sequence,
)

_MAX_REPORT_BYTES = 4 * 1024 * 1024


def _execute(
    loaded: LoadedMyBankCutoverPlan,
    database_url: str,
    *,
    commit: bool,
) -> MyBankStatementCutoverReceipt:
    before = _load_before_inventory(
        loaded.safety_proof.restore_report,
        expected_revision=loaded.target_revision,
    )
    expected_before = production_counts_from_cutover_inventory(
        before.as_payload(),
        expected_schema_revision=before.schema_revision,
    )
    gates = MyBankStatementCutoverGates(
        schema_revision=before.schema_revision,
        backup_verified=True,
        isolated_restore_verified=True,
        rollback_ready=True,
        expected_before=expected_before,
        verify_fact_conflict=True,
    )

    def accept(
        receipt: MyBankStatementCutoverReceipt,
        connection: Connection,
    ) -> None:
        raw = connection.execute(text(R1_CUTOVER_INVENTORY_SQL)).scalar_one()
        after = CutoverInventory.from_payload(json.loads(raw))
        report = validate_mybank_cutover_inventory_sequence(
            before=before,
            after=after,
            replay=after,
            conflict=after,
            transaction_count=receipt.transaction_count,
            alias_count=len(loaded.cutover.registry_plan.accounts[0].aliases),
            assignment_count=len(loaded.cutover.registry_plan.business_unit_assignments),
        )
        if report.get("replay_delta") != 0 or report.get("conflict_delta") != 0:
            raise RuntimeError("cutover inventory acceptance failed")

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        return run_transactional_database_mybank_statement_cutover(
            engine,
            loaded.cutover,
            gates=gates,
            safety_proof=loaded.safety_proof,
            registry_principal=loaded.principal,
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
    if not isinstance(value, dict):
        raise RuntimeError("cutover safety report is invalid")
    if value.get("revision") != expected_revision:
        raise RuntimeError("cutover safety report is invalid")
    source = value.get("source_database_metadata")
    if not isinstance(source, dict):
        raise RuntimeError("cutover safety report is invalid")
    return CutoverInventory.from_payload(source.get("cutover_inventory"))


def main() -> int:
    try:
        return run_mybank_cutover_command(executor=_execute)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        print("MYBANK_CUTOVER_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
