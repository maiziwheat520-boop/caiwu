from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from ledgerbridge.local_wechat import (
    DIRECTIONLESS,
    LocalWeChatError,
    read_wechat_export,
)

_HEADER = (
    "交易时间",
    "交易类型",
    "交易对方",
    "商品",
    "收/支",
    "金额(元)",
    "支付方式",
    "当前状态",
    "交易单号",
    "商户单号",
    "备注",
)

_COLON = chr(0xFF1A)

#: 2026-03-01 09:00:00 and 2026-03-02 09:00:00 as the spreadsheet stores them.
_FIRST = "46082.375"
_SECOND = "46083.375"


def _column_name(index: int) -> str:
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _row(number: int, values: tuple[str, ...]) -> str:
    cells = "".join(
        f'<c r="{_column_name(index)}{number}" t="inlineStr"><is><t>{value}</t></is></c>'
        for index, value in enumerate(values, start=1)
    )
    return f'<row r="{number}">{cells}</row>'


def _write_synthetic_export(
    path: Path,
    *,
    rows: tuple[tuple[str, ...], ...] | None = None,
    declared_total: int | None = None,
    expenditure_total: str = "26.00",
    period: str = "2026-03-01 00:00:00] 终止时间" + _COLON + "[2026-03-31 23:59:59",
    timezone_note: str = "4. 本账单中所有时间均为UTC+08:00时间",
) -> bytes:
    """One synthetic export. Nothing here is a real transaction."""

    body = rows if rows is not None else (_expenditure(), _directionless())
    expenditure = [row for row in body if row[4] == "支出"]
    income = [row for row in body if row[4] == "收入"]
    neutral = [row for row in body if row[4] == DIRECTIONLESS]
    preamble = (
        ("合成微信支付账单",),
        (f"起始时间{_COLON}[{period}]",),
        (f"导出类型{_COLON}[全部账单]",),
        (f"导出时间{_COLON}[2026-04-01 03:00:00]",),
        (f"共{declared_total if declared_total is not None else len(body)}笔记录",),
        (f"收入{_COLON}{len(income)}笔 {_total(income)}元",),
        (f"支出{_COLON}{len(expenditure)}笔 {expenditure_total}元",),
        (f"中性交易{_COLON}{len(neutral)}笔 {_total(neutral)}元",),
        (timezone_note,),
    )
    lines = [*preamble, _HEADER, *body]
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>"
        + "".join(_row(number, values) for number, values in enumerate(lines, start=1))
        + "</sheetData></worksheet>"
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/>'
            "</Relationships>"
        ),
        "xl/worksheets/sheet1.xml": worksheet,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value.encode("utf-8"))
    return path.read_bytes()


def _total(rows: list[tuple[str, ...]]) -> str:
    return f"{sum(float(row[5]) for row in rows):.2f}"


def _expenditure(serial: str = "1" * 28, amount: str = "26.00") -> tuple[str, ...]:
    return (
        _FIRST,
        "商户消费",
        "合成商户",
        "合成商品",
        "支出",
        amount,
        "零钱通",
        "支付成功",
        serial,
        "2" * 30,
        "/",
    )


def _directionless(serial: str = "3" * 28, amount: str = "100.00") -> tuple[str, ...]:
    return (
        _SECOND,
        "零钱提现",
        "/",
        "/",
        DIRECTIONLESS,
        amount,
        "零钱",
        "提现已到账",
        serial,
        "/",
        "/",
    )


def test_reads_an_export_and_keeps_the_direction_the_file_states(tmp_path: Path) -> None:
    export = read_wechat_export(_write_synthetic_export(tmp_path / "export.xlsx"))

    assert len(export.rows) == 2
    spent, moved = export.rows
    assert spent.amount_minor == 2600
    assert spent.signed_amount_minor == -2600
    # The file declines to give this row a direction, and so does the reader:
    # the magnitude is what the preamble totals it by.
    assert moved.direction == DIRECTIONLESS
    assert moved.amount_minor == 10_000
    assert moved.signed_amount_minor == 10_000
    assert export.export_kind == "全部账单"
    assert f"{export.period_start:%Y-%m-%d}" == "2026-03-01"


def test_refuses_an_export_that_miscounts_its_own_records(tmp_path: Path) -> None:
    raw = _write_synthetic_export(tmp_path / "export.xlsx", declared_total=3)

    with pytest.raises(LocalWeChatError, match="declares 3 records but holds 2"):
        read_wechat_export(raw)


def test_refuses_an_export_whose_rows_miss_its_own_total(tmp_path: Path) -> None:
    raw = _write_synthetic_export(tmp_path / "export.xlsx", expenditure_total="27.00")

    with pytest.raises(LocalWeChatError, match="do not add up to its own total"):
        read_wechat_export(raw)


def test_refuses_an_export_that_does_not_declare_its_timezone(tmp_path: Path) -> None:
    raw = _write_synthetic_export(tmp_path / "export.xlsx", timezone_note="4. 本明细仅供个人对账")

    with pytest.raises(LocalWeChatError, match="does not declare its timezone"):
        read_wechat_export(raw)


def test_refuses_a_row_outside_the_period_the_export_claims(tmp_path: Path) -> None:
    raw = _write_synthetic_export(
        tmp_path / "export.xlsx",
        period="2026-03-01 00:00:00] 终止时间" + _COLON + "[2026-03-01 23:59:59",
    )

    with pytest.raises(LocalWeChatError, match="outside its own period"):
        read_wechat_export(raw)


def test_refuses_an_export_that_repeats_a_serial(tmp_path: Path) -> None:
    raw = _write_synthetic_export(
        tmp_path / "export.xlsx",
        rows=(_expenditure(), _expenditure(amount="26.00")),
        expenditure_total="52.00",
    )

    with pytest.raises(LocalWeChatError, match="repeats a transaction serial"):
        read_wechat_export(raw)


def test_refuses_an_amount_finer_than_a_cent(tmp_path: Path) -> None:
    raw = _write_synthetic_export(
        tmp_path / "export.xlsx",
        rows=(_expenditure(amount="26.001"),),
        expenditure_total="26.00",
    )

    with pytest.raises(LocalWeChatError, match="amount is invalid"):
        read_wechat_export(raw)


def test_refuses_a_direction_the_file_does_not_use(tmp_path: Path) -> None:
    row = list(_expenditure())
    row[4] = "转出"
    raw = _write_synthetic_export(tmp_path / "export.xlsx", rows=(tuple(row),))

    with pytest.raises(LocalWeChatError, match="direction is invalid"):
        read_wechat_export(raw)


def test_refuses_a_workbook_that_is_not_one(tmp_path: Path) -> None:
    with pytest.raises(LocalWeChatError, match="not a readable workbook"):
        read_wechat_export(b"not a workbook")
