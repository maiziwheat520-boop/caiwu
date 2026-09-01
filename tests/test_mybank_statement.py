from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from ledgerbridge.mybank_statement import (
    MyBankStatementError,
    build_mybank_source_manifest,
    parse_mybank_xlsx,
)


def _column_name(index: int) -> str:
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _inline_cell(reference: str, value: str) -> str:
    return f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>'


def _row(number: int, values: tuple[str, ...]) -> str:
    cells = "".join(
        _inline_cell(f"{_column_name(index)}{number}", value)
        for index, value in enumerate(values, start=1)
    )
    return f'<row r="{number}">{cells}</row>'


def _write_synthetic_mybank_xlsx(
    path: Path,
    *,
    amount: str = "+125.34",
    headers: tuple[str, ...] | None = None,
    institution: str = "网商银行账户交易明细",
    account_label: str = "卡号\N{FULLWIDTH COLON}",
    empty: bool = False,
) -> bytes:
    effective_headers = headers or (
        "交易时间",
        "交易金额",
        "余额",
        "对方户名",
        "对方账号",
        "对方机构名称",
        "交易流水号",
        "交易名称",
    )
    metadata_rows = (
        _row(1, (institution,)),
        _row(2, (account_label, "************7968")),
        _row(3, ("币种", "人民币")),
        _row(8, effective_headers),
    )
    transaction_rows = (
        ()
        if empty
        else (
            _row(
                9,
                (
                    "2026-01-02 03:04:05",
                    amount,
                    "5125.34",
                    "合成商户甲",
                    "0000000000005678",
                    "合成银行",
                    "9000000000000000000000000000001",
                    "转账",
                ),
            ),
            _row(
                10,
                (
                    "2026-01-03 06:07:08",
                    "-20.00",
                    "5105.34",
                    "合成商户乙",
                    "0000000000009012",
                    "合成银行",
                    "9000000000000000000000000000002",
                    "消费",
                ),
            ),
        )
    )
    rows = (*metadata_rows, *transaction_rows)
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows)}</sheetData></worksheet>"
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="合成流水" sheetId="1" r:id="rId1"/></sheets></workbook>'
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


def test_parse_mybank_xlsx_produces_traceable_idempotent_transactions(tmp_path: Path) -> None:
    path = (tmp_path / "synthetic-mybank.xlsx").resolve()
    raw = _write_synthetic_mybank_xlsx(path)
    digest = hashlib.sha256(raw).hexdigest()

    first = parse_mybank_xlsx(
        path,
        expected_sha256=digest,
        managed_account_suffix="7968",
    )
    second = parse_mybank_xlsx(
        path,
        expected_sha256=digest,
        managed_account_suffix="7968",
    )

    assert first == second
    assert first.source_sha256 == digest
    assert first.header_row_number == 8
    assert first.currency == "CNY"
    assert len(first.transactions) == 2
    assert first.transactions[0].source_row_number == 9
    assert first.transactions[0].occurred_at.isoformat() == "2026-01-02T03:04:05+08:00"
    assert first.transactions[0].amount_minor == 12_534
    assert first.transactions[0].balance_minor == 512_534
    assert first.transactions[0].source_row_sha256 != first.transactions[1].source_row_sha256
    assert first.transactions[0].source_event_ref == second.transactions[0].source_event_ref


def test_parse_mybank_xlsx_rejects_source_digest_drift(tmp_path: Path) -> None:
    path = (tmp_path / "synthetic-mybank.xlsx").resolve()
    _write_synthetic_mybank_xlsx(path)

    with pytest.raises(MyBankStatementError, match="digest"):
        parse_mybank_xlsx(
            path,
            expected_sha256="0" * 64,
            managed_account_suffix="7968",
        )


def test_parse_mybank_xlsx_rejects_header_mapping_drift(tmp_path: Path) -> None:
    path = (tmp_path / "synthetic-mybank.xlsx").resolve()
    raw = _write_synthetic_mybank_xlsx(
        path,
        headers=(
            "交易时间",
            "交易金额(元)",
            "余额",
            "对方户名",
            "对方账号",
            "对方机构名称",
            "交易流水号",
            "交易名称",
        ),
    )

    with pytest.raises(MyBankStatementError, match="header"):
        parse_mybank_xlsx(
            path,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            managed_account_suffix="7968",
        )


@pytest.mark.parametrize(
    ("institution", "account_label", "message"),
    (
        ("中国建设银行账户交易明细", "卡号\N{FULLWIDTH COLON}", "institution"),
        ("网商银行账户交易明细", "账号", "managed account"),
    ),
)
def test_parse_mybank_xlsx_requires_mybank_and_card_suffix_binding(
    tmp_path: Path,
    institution: str,
    account_label: str,
    message: str,
) -> None:
    path = (tmp_path / "wrong-institution.xlsx").resolve()
    raw = _write_synthetic_mybank_xlsx(
        path,
        institution=institution,
        account_label=account_label,
    )
    with pytest.raises(MyBankStatementError, match=message):
        parse_mybank_xlsx(
            path,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            managed_account_suffix="7968",
        )


def test_build_mybank_source_manifest_rejects_retired_per_row_review_path(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "synthetic-mybank.xlsx").resolve()
    raw = _write_synthetic_mybank_xlsx(path)
    statement = parse_mybank_xlsx(
        path,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        managed_account_suffix="7968",
    )
    with pytest.raises(MyBankStatementError, match="retired"):
        build_mybank_source_manifest(
            statement,
            source_file="mybank-statement.xlsx",
            context=object(),
        )
