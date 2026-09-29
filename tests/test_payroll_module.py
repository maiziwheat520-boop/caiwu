from __future__ import annotations

from io import BytesIO

import pytest
import xlrd

from ledgerbridge.payroll_module import (
    EmployeeType,
    FixedRemittance,
    PaymentChannel,
    PayrollComponent,
    PayrollDraft,
    PayrollDraftLine,
    PayrollValidationError,
    compact_remittance_memo,
    lock_draft,
    validate_draft,
)
from ledgerbridge.payroll_xls import export_remittance_workbooks


def _line(**overrides: object) -> PayrollDraftLine:
    values: dict[str, object] = {
        "employee_id": "employee_001",
        "employee_name": "员工甲",
        "payee_name": "收款人甲",
        "account_number": "6222 0000 0000 1234",
        "employee_type": EmployeeType.REGULAR,
        "payment_channel": PaymentChannel.MYBANK,
        "location": "星汇",
        "job_group": "前台",
        "attendance_days": "31",
        "components": (
            PayrollComponent("BASE", 500_000),
            PayrollComponent("ALLOWANCE", 20_000),
            PayrollComponent("DEDUCTION", -1_234),
        ),
        "cash_amount_minor": 100_000,
        "memo": "工资 住宿100 餐补200 全勤300 绩效400",
    }
    values.update(overrides)
    return PayrollDraftLine(**values)  # type: ignore[arg-type]


def _draft(*lines: PayrollDraftLine) -> PayrollDraft:
    return PayrollDraft(
        company_id="company_hotel_001",
        period="2026-08",
        revision=1,
        rules_version="payroll-rules.2026-09.v1",
        lines=lines or (_line(),),
    )


def test_employee_type_requirements_fail_closed() -> None:
    regular = _line(job_group="", attendance_days="")
    temporary = _line(
        employee_id="employee_002",
        employee_type=EmployeeType.TEMPORARY,
        job_group="",
        attendance_days="",
    )
    payee_only = _line(employee_id="employee_003", employee_type=EmployeeType.PAYEE_ONLY)

    codes = {issue.code for issue in validate_draft(_draft(regular, temporary, payee_only))}

    assert "MISSING_JOB_GROUP" in codes
    assert "MISSING_ATTENDANCE" in codes
    assert "PAYEE_ONLY_HAS_WAGE" in codes
    assert sum(code == "MISSING_JOB_GROUP" for code in codes) == 1


def test_cash_and_bank_amounts_must_close() -> None:
    invalid = _line(cash_amount_minor=600_000)
    issues = validate_draft(_draft(invalid))
    assert {issue.code for issue in issues} == {
        "INVALID_CASH_SPLIT",
        "NONPOSITIVE_BANK_PAY",
    }


def test_lock_is_order_independent_and_binds_rules() -> None:
    first = _line(employee_id="employee_001")
    second = _line(employee_id="employee_002", payee_name="收款人乙", account_number="62220002")

    forward = lock_draft(_draft(first, second))
    reverse = lock_draft(_draft(second, first))

    assert forward.content_sha256 == reverse.content_sha256
    changed = lock_draft(
        PayrollDraft(
            company_id=forward.company_id,
            period=forward.period,
            revision=forward.revision,
            rules_version="payroll-rules.2026-09.v2",
            lines=(first, second),
        )
    )
    assert changed.content_sha256 != forward.content_sha256


def test_account_and_payee_conflicts_block_locking() -> None:
    first = _line(employee_id="employee_001")
    second = _line(employee_id="employee_002", payee_name="另一个人")
    with pytest.raises(PayrollValidationError) as error:
        lock_draft(_draft(first, second))
    assert {issue.code for issue in error.value.issues} == {"ACCOUNT_PAYEE_CONFLICT"}


def test_memo_retains_amount_within_bank_limit() -> None:
    memo = compact_remittance_memo(
        "住宿100 餐补200 全勤300 夜班400 好评500 绩效600 加班700 其他说明",
        123_456,
    )
    assert len(memo) <= 40
    assert memo.endswith("应发1234.56")


def test_locked_version_exports_five_legacy_workbooks() -> None:
    locked = lock_draft(_draft())

    workbooks = export_remittance_workbooks(locked)

    assert [item.filename for item in workbooks] == [
        "26.8月 代发星汇.xls",
        "26.8月 代发薇旭.xls",
        "26.8月 代发雅阁.xls",
        "26.8月 代发逸豪景怡.xls",
        "26.8月 代发青居客一品餐饮.xls",
    ]
    assert sum(item.row_count for item in workbooks) == 1
    assert sum(item.total_amount_minor for item in workbooks) == 418_766
    star = workbooks[0]
    book = xlrd.open_workbook(file_contents=BytesIO(star.content).getvalue())
    sheet = book.sheet_by_name("Sheet1")
    assert sheet.cell_value(1, 0) == "收款人甲"
    assert sheet.cell_value(1, 1) == "6222000000001234"
    assert sheet.cell_value(1, 2) == pytest.approx(4187.66)
    assert str(sheet.cell_value(1, 3)).endswith("应发4187.66")


def test_fixed_remittances_export_as_the_sixth_database_owned_workbook() -> None:
    fixed = (
        FixedRemittance(
            payee_name="固定收款人",
            account_number="6222000099991234",
            amount_minor=500_000,
            memo="工资",
        ),
    )
    source = _draft()
    locked = lock_draft(
        PayrollDraft(
            company_id=source.company_id,
            period=source.period,
            revision=source.revision,
            rules_version=source.rules_version,
            lines=source.lines,
            fixed_remittances=fixed,
        )
    )

    first = export_remittance_workbooks(locked)
    second = export_remittance_workbooks(locked)

    assert first[-1].filename == "26.8月 代发其他.xls"
    assert first[-1].row_count == 1
    assert first[-1].total_amount_minor == 500_000
    assert first[-1].sha256 == second[-1].sha256


def test_supplemental_pay_is_in_net_but_in_a_separate_bank_workbook() -> None:
    locked = lock_draft(_draft(_line(supplemental_amount_minor=15_000)))

    workbooks = export_remittance_workbooks(locked)

    assert workbooks[0].total_amount_minor == 403_766
    assert workbooks[-1].filename == "26.8月 补发代发表.xls"
    assert workbooks[-1].total_amount_minor == 15_000
    assert sum(item.total_amount_minor for item in workbooks) == 418_766


def test_supplemental_cannot_exceed_bank_pay() -> None:
    issues = validate_draft(_draft(_line(supplemental_amount_minor=500_000)))
    assert "INVALID_SUPPLEMENTAL_SPLIT" in {issue.code for issue in issues}
