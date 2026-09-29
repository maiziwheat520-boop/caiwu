"""Static contracts for the payroll workbench migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from scripts.backup_restore import MYBANK_CUTOVER_SCHEMA_REVISIONS

REVISION = "20260929_0053"


def _sql() -> str:
    path = (
        Path(__file__).resolve().parents[1] / "alembic/versions/20260929_0053_payroll_workbench.py"
    )
    spec = importlib.util.spec_from_file_location("payroll_workbench_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._UPGRADE_SQL)


def test_payroll_workbench_remains_in_the_single_migration_chain() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["20260929_0054"]
    assert scripts.get_revision("20260929_0054").down_revision == REVISION
    assert REVISION in MYBANK_CUTOVER_SCHEMA_REVISIONS
    # The archive is intentionally not releasable until its restore inventory
    # and isolated recovery gate cover source bytes, cells and access controls.
    assert "20260929_0054" not in MYBANK_CUTOVER_SCHEMA_REVISIONS


def test_payroll_schema_keeps_locked_versions_and_receipts_immutable() -> None:
    sql = _sql()
    assert "CREATE UNIQUE INDEX payroll_one_locked_version_per_batch" in sql
    assert "terminal payroll version is immutable" in sql
    assert "locked payroll version is immutable" in sql
    assert "payroll_export_receipt_append_only" in sql
    assert "payroll_command_receipt_append_only" in sql
    assert "remittance export requires a locked payroll version" in sql


def test_payroll_schema_keeps_account_plaintext_off_the_api_role() -> None:
    sql = _sql()
    assert "GRANT SELECT ON payroll.payee_account, payroll.fixed_remittance" in sql
    assert "GRANT SELECT ON payroll.payee_account TO ledgerbridge_api" not in sql
    assert "account_suffix text GENERATED ALWAYS AS" in sql


def test_payroll_schema_binds_reconciliation_month_and_minor_units() -> None:
    sql = _sql()
    assert "reconciliation_month text NOT NULL" in sql
    assert "net_amount_minor bigint NOT NULL" in sql
    assert "bank_amount_minor bigint GENERATED ALWAYS AS" in sql
    assert "cash_amount_minor <= net_amount_minor" in sql
