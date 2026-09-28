from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from ledgerbridge.reconciliation_legacy_xlsx import (
    LegacyReconciliationError,
    preflight_legacy_reconciliation,
)


def _workbook(path: Path, *, closing: float = 6700) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "26.8"
    sheet["A21"] = "收入共计"
    sheet["B21"] = 10000
    sheet["A23"] = "支出明细"
    sheet["A30"] = "支出共计"
    sheet["B30"] = 4000
    sheet["A32"] = "结余"
    sheet["B32"] = 500
    sheet["D32"] = "消杀漏记补记"
    sheet["E32"] = -50
    sheet["A33"] = "文杰房租"
    sheet["B33"] = 100
    sheet["A34"] = "支出共计"
    sheet["B34"] = -4000
    sheet["D34"] = "历史差错调整"
    sheet["E34"] = -30
    sheet["A35"] = "收入共计"
    sheet["B35"] = 10000
    sheet["D35"] = "银行扣款历史调整"
    sheet["E35"] = -20
    sheet["A36"] = "五月漏记收入补记"
    sheet["B36"] = 200
    sheet["A41"] = "共计"
    sheet["B41"] = closing
    sheet["A45"] = "序号"
    sheet["A46"] = 1
    sheet["B46"] = "24.09 表 E38 饺子店报销"
    sheet["C46"] = 10
    sheet["D46"] = -10
    sheet["E46"] = 20
    sheet["F46"] = "支出符号纠正"
    sheet["A47"] = 2
    sheet["B47"] = "24.12 表 E36 利息"
    sheet["C47"] = 5
    sheet["D47"] = -5
    sheet["E47"] = 10
    sheet["F47"] = "支出符号纠正"
    workbook.save(path)


def test_preflight_preserves_effective_and_origin_periods(tmp_path: Path) -> None:
    path = tmp_path / "reconciliation.xlsx"
    _workbook(path)

    result = preflight_legacy_reconciliation(path, "26.8")

    assert result.period == "2026-08"
    assert result.income_amount_minor == 1_000_000
    assert result.expense_amount_minor == 400_000
    assert result.opening_balance_minor == 50_000
    assert result.closing_balance_minor == 670_000
    assert result.bridge_amount_minor == result.closing_balance_minor
    assert [item.origin_period for item in result.adjustments] == ["2024-09", "2024-12"]
    assert {item.effective_month for item in result.adjustments} == {"2026-08"}
    assert result.checks == (
        "CONTROL_TOTALS_FOUND",
        "CLOSING_BRIDGE_BALANCED",
        "ADJUSTMENTS_BALANCED",
    )


def test_preflight_rejects_an_unbalanced_closing(tmp_path: Path) -> None:
    path = tmp_path / "reconciliation.xlsx"
    _workbook(path, closing=6699)

    with pytest.raises(LegacyReconciliationError, match="期末桥接不平"):
        preflight_legacy_reconciliation(path, "26.8")
