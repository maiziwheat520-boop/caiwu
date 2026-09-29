"""Deterministic bank-remittance workbook Adapter for locked payroll versions."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO

import xlwt  # type: ignore[import-untyped]

from ledgerbridge.payroll_module import LockedPayrollVersion, remittance_rows


@dataclass(frozen=True, slots=True)
class PayrollWorkbook:
    filename: str
    content: bytes
    sha256: str
    row_count: int
    total_amount_minor: int


# These full-width marks are bank-template content and must not be normalized.
_TEMPLATE_CELLS: tuple[tuple[int, int, str], ...] = (
    (0, 0, "收款方名称                              （必输，本文格式，仅支持个人账号）"),
    (0, 1, "收款方账号                                 （必输，本文格式，支持银行和支付宝）"),
    (0, 2, "金额                     （必输，文本格式）"),
    (0, 3, "附言/用途                      （必输，文本格式，最多40字）"),
    (0, 5, "填写说明："),
    (1, 5, "注意事项"),
    (1, 6, "1、收款方名称、收款方账号、金额、附言/用途为必输栏位；收款方仅支持个人账号。"),
    (2, 6, "2、一个文件最高支持10000笔记录。"),
    (3, 6, "3、收款方账号支持填写支付宝账号。"),
    (4, 6, "4、模板中录入的数字及文字均需设置为文本格式。"),
    (5, 6, "5、纯数字内容粘贴后，务必双击确认左上角绿色三角标。"),
    (6, 6, '6、粘贴超长数字时，Excel会将16位开始的数字变为"0"，务必检查。'),
    (10, 5, "填写说明"),
    (10, 6, "字段名称"),
    (10, 7, "是否必输"),
    (10, 8, "填写说明"),
    (11, 6, "收款方名称"),
    (11, 7, "必输"),
    (11, 8, "为收款方账户实名认证的姓名。"),
    (12, 6, "收款方账号"),
    (12, 7, "必输"),
    (12, 8, "银行账号或支付宝登录号。"),
    (13, 6, "金额"),
    (13, 7, "必输"),
    (13, 8, "精确到两位小数，单位：元。"),
    (14, 6, "附言/用途"),
    (14, 7, "必输"),
    (14, 8, '最多40字，如"工资"。'),
    (15, 5, "填写示例："),
    (17, 5, "收款方名称"),
    (17, 6, "收款方账号"),
    (17, 7, "金额"),
    (17, 8, "附言/用途"),
    (18, 5, "张三"),
    (18, 6, "6666666666666666"),
    (18, 7, "6523.4"),
    (18, 8, "工资"),
)
_COL_WIDTHS = {
    0: 8352,
    1: 9888,
    2: 5888,
    3: 23008,
    4: 2784,
    5: 3712,
    6: 4640,
    7: 2784,
    8: 3744,
    15: 14752,
}


def _render(rows: tuple[tuple[str, str, int, str], ...]) -> bytes:
    workbook = xlwt.Workbook(encoding="utf-8")
    sheet = workbook.add_sheet("Sheet1", cell_overwrite_ok=True)
    for column, width in _COL_WIDTHS.items():
        sheet.col(column).width = width
    for row, column, value in _TEMPLATE_CELLS:
        sheet.write(row, column, value)
    amount_style = xlwt.easyxf(num_format_str="0.00")
    for index, (payee, account, amount_minor, memo) in enumerate(rows, start=1):
        sheet.write(index, 0, payee)
        sheet.write(index, 1, account)
        sheet.write(index, 2, amount_minor / 100, amount_style)
        sheet.write(index, 3, memo)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def export_remittance_workbooks(
    locked: LockedPayrollVersion,
) -> tuple[PayrollWorkbook, ...]:
    """Create the existing MyBank workbooks from one immutable version."""

    stem = f"{locked.period[2:4]}.{int(locked.period[5:7])}月"
    result: list[PayrollWorkbook] = []
    for group, rows in remittance_rows(locked).items():
        content = _render(rows)
        filename = (
            f"{stem} 补发代发表.xls"
            if group == "补发"
            else f"{stem} 代发{group}.xls"
        )
        result.append(
            PayrollWorkbook(
                filename=filename,
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
                row_count=len(rows),
                total_amount_minor=sum(item[2] for item in rows),
            )
        )
    return tuple(result)
