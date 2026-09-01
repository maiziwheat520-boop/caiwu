from __future__ import annotations

import re
import runpy
from pathlib import Path
from typing import Any, cast

import pytest

MIGRATION = Path("alembic/versions/20260901_0028_company_report_composition.py")


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _sql() -> str:
    return re.sub(r"\s+", " ", _source()).lower()


def test_composition_migration_adds_one_bounded_reader_function() -> None:
    source = _source()
    sql = _sql()

    assert re.search(r'revision\s*=\s*["\']20260901_0028["\']', source)
    assert re.search(r'down_revision\s*=\s*["\']20260901_0027["\']', source)
    assert "create function company_reporting_read.get_company_report_composition_v1_as_of" in sql
    assert "returns table(composition jsonb)" in sql
    assert "security definer set search_path = pg_catalog" in sql
    assert "get_company_report_v1_as_of" in sql
    assert "account_statement" not in sql
    assert "not in ('confirmed_candidate', 'posted_ledger')" in sql


def test_composition_uses_immutable_category_snapshots_and_reconciles_totals() -> None:
    sql = _sql()

    assert sql.count("revision.category_code_snapshot") == 2
    assert sql.count("revision.category_label_snapshot") == 2
    assert sql.count("attribution.category_code_snapshot") == 2
    assert sql.count("attribution.category_label_snapshot") == 2
    assert "left join public.posting_attribution" in sql
    assert "confirmed_positive_minor" in sql
    assert "confirmed_negative_minor" in sql
    assert "revenue_minor" in sql
    assert "expense_minor" in sql
    assert sql.count("does not reconcile to report totals") == 2
    assert "order by amount_minor desc" in sql
    assert sql.count("jsonb_array_length") == 4
    assert sql.count("exceeds the category limit") == 2


def test_composition_reader_has_exact_reader_acl_and_safe_downgrade() -> None:
    sql = _sql()
    downgrade = _source().lower().split("def downgrade() -> none:", maxsplit=1)[1]

    for denied in (
        "public",
        "ledgerbridge_api",
        "ledgerbridge_worker",
        "ledgerbridge_app",
        "ledgerbridge_backup",
    ):
        assert re.search(
            rf"revoke all on function .*get_company_report_composition_v1_as_of.* from {denied}",
            sql,
        )
    assert re.search(
        r"grant execute on function .*get_company_report_composition_v1_as_of.* "
        r"to ledgerbridge_reader",
        sql,
    )
    assert "drop function company_reporting_read.get_company_report_composition_v1_as_of" in re.sub(
        r"\s+", " ", downgrade
    )
    assert "drop table" not in downgrade
    assert "drop schema" not in downgrade


@pytest.mark.parametrize("backup_role_exists", [False, True])
def test_composition_migration_handles_optional_backup_role(
    backup_role_exists: bool,
) -> None:
    namespace = runpy.run_path(str(MIGRATION))
    executed_sql: list[str] = []

    class _RoleResult:
        def scalar_one(self) -> bool:
            return backup_role_exists

    class _Connection:
        def execute(self, statement: object) -> _RoleResult:
            assert "ledgerbridge_backup" in str(statement)
            return _RoleResult()

    class _MigrationOp:
        def get_bind(self) -> _Connection:
            return _Connection()

        def execute(self, statement: str) -> None:
            executed_sql.append(statement)

    upgrade = cast(Any, namespace["upgrade"])
    upgrade.__globals__["op"] = _MigrationOp()
    upgrade()

    assert len(executed_sql) == 1
    if backup_role_exists:
        assert executed_sql[0].count("FROM ledgerbridge_backup;") == 1
    else:
        assert "ledgerbridge_backup" not in executed_sql[0]
