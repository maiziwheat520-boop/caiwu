"""Read-only parity check against one legacy payroll period and bank workbooks.

Never prints names or account numbers. This is a migration gate, not an import.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import xlrd  # type: ignore[import-untyped]

from ledgerbridge.payroll_legacy_xlsx import import_calculated_payroll
from ledgerbridge.payroll_module import FixedRemittance, PayrollDraft, lock_draft, normalize_account
from ledgerbridge.payroll_xls import export_remittance_workbooks


def _rows(path: Path) -> Counter[tuple[str, str, int]]:
    sheet = xlrd.open_workbook(path).sheet_by_name("Sheet1")
    result: Counter[tuple[str, str, int]] = Counter()
    for index in range(1, sheet.nrows):
        payee = str(sheet.cell_value(index, 0)).strip()
        if not payee:
            continue
        account = normalize_account(str(sheet.cell_value(index, 1)))
        amount = int(Decimal(str(sheet.cell_value(index, 2))) * 100)
        result[(payee, account, amount)] += 1
    return result


def _fixed(path: Path) -> tuple[FixedRemittance, ...]:
    document = json.loads(path.read_text(encoding="utf-8"))
    raw = document.get("其他", [])
    return tuple(
        FixedRemittance(
            payee_name=str(item[0]),
            account_number=str(item[1]),
            amount_minor=int(Decimal(str(item[2])) * 100),
            memo=str(item[3]),
        )
        for item in raw
    )


def _cash_splits(path: Path, draft: PayrollDraft) -> dict[str, int]:
    sheet = xlrd.open_workbook(path).sheet_by_name("现金发放")
    splits: dict[str, int] = {}
    for index in range(1, sheet.nrows):
        payee = str(sheet.cell_value(index, 0)).strip()
        if not payee or payee == "合计":
            continue
        amount = int(Decimal(str(sheet.cell_value(index, 2))) * 100)
        matches = [line for line in draft.lines if line.payee_name == payee]
        if len(matches) != 1:
            raise ValueError("cash payee did not match exactly one wage line")
        line = matches[0]
        if line.cash_amount_minor == 0:
            splits[line.employee_name] = amount
        elif line.cash_amount_minor != amount:
            raise ValueError("legacy cash amount differs from full-cash wage line")
    return splits


def verify(workbook: Path, bank_dir: Path, fixed_json: Path) -> int:
    draft = import_calculated_payroll(
        workbook, company_id="legacy-hotel", rules_version="legacy-parity-v1"
    )
    stem = f"{draft.period[2:4]}.{int(draft.period[5:7])}月"
    cash_splits = _cash_splits(bank_dir / f"{stem} 现金发放表.xls", draft)
    supplemental_rows = _rows(bank_dir / f"{stem} 补发代发表.xls")
    supplements: dict[str, int] = {}
    for payee, account, amount in supplemental_rows.elements():
        matches = [
            line for line in draft.lines
            if line.payee_name == payee
            and normalize_account(line.account_number) == account
        ]
        if len(matches) != 1:
            raise ValueError("supplemental payee did not match exactly one wage line")
        name = matches[0].employee_name
        supplements[name] = supplements.get(name, 0) + amount
    draft = import_calculated_payroll(
        workbook,
        company_id="legacy-hotel",
        rules_version="legacy-parity-v1",
        cash_splits_minor=cash_splits,
        supplemental_minor=supplements,
    )
    draft = PayrollDraft(
        company_id=draft.company_id,
        period=draft.period,
        revision=draft.revision,
        rules_version=draft.rules_version,
        lines=draft.lines,
        fixed_remittances=_fixed(fixed_json),
    )
    locked = lock_draft(draft)
    failures = 0
    for generated in export_remittance_workbooks(locked):
        expected = _rows(bank_dir / generated.filename)
        actual = _rows_from_bytes(generated.content)
        same = expected == actual
        failures += not same
        print(
            f"{generated.filename}: {'OK' if same else 'MISMATCH'}; "
            f"rows {sum(actual.values())}/{sum(expected.values())}; "
            f"minor {sum(key[2] * count for key, count in actual.items())}/"
            f"{sum(key[2] * count for key, count in expected.items())}"
        )
        if not same:
            generated_only = actual - expected
            legacy_only = expected - actual
            print(
                "  unmatched generated amounts:",
                sorted(
                    amount
                    for (_payee, _account, amount), count in generated_only.items()
                    for _ in range(count)
                ),
            )
            print(
                "  unmatched legacy amounts:",
                sorted(
                    amount
                    for (_payee, _account, amount), count in legacy_only.items()
                    for _ in range(count)
                ),
            )
    return failures


def _rows_from_bytes(content: bytes) -> Counter[tuple[str, str, int]]:
    sheet = xlrd.open_workbook(file_contents=content).sheet_by_name("Sheet1")
    result: Counter[tuple[str, str, int]] = Counter()
    for index in range(1, sheet.nrows):
        payee = str(sheet.cell_value(index, 0)).strip()
        if not payee:
            continue
        account = normalize_account(str(sheet.cell_value(index, 1)))
        amount = int(Decimal(str(sheet.cell_value(index, 2))) * 100)
        result[(payee, account, amount)] += 1
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("bank_dir", type=Path)
    parser.add_argument("fixed_json", type=Path)
    arguments = parser.parse_args()
    failures = verify(arguments.workbook, arguments.bank_dir, arguments.fixed_json)
    raise SystemExit(1 if failures else 0)
