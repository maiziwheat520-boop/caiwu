"""Every refusal the MYbank personal XLSX reader makes.

An XLSX is a zip of XML written by someone else, so this reader treats the
container as hostile: bounded entries, no traversal, no DOCTYPE, no formulas,
one worksheet, and cell references that have to agree with the row they sit
in. Only after all of that does it look at whether the rows say what a MYbank
statement says. Each test removes one of those guards.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from ledgerbridge import mybank_statement
from ledgerbridge.bank_statement_contract import BankStatement
from ledgerbridge.mybank_statement import MyBankStatementError, parse_mybank_xlsx
from tests.test_mybank_statement import _row

HEADERS = (
    "交易时间",
    "交易金额",
    "余额",
    "对方户名",
    "对方账号",
    "对方机构名称",
    "交易流水号",
    "交易名称",
)
SUFFIX = "7968"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class _ShortStream:
    """A stream that hands back fewer bytes than the file claimed to hold."""

    def __init__(self, descriptor: int, raw: bytes) -> None:
        self._descriptor = descriptor
        self._raw = raw

    def __enter__(self) -> _ShortStream:
        return self

    def __exit__(self, *args: object) -> None:
        os.close(self._descriptor)

    def read(self, size: int) -> bytes:
        return self._raw


def _txn(**overrides: str) -> tuple[str, ...]:
    values = {
        "occurred_at": "2026-01-02 03:04:05",
        "amount": "+125.34",
        "balance": "5125.34",
        "counterparty_name": "合成商户甲",
        "counterparty_account": "0000000000005678",
        "counterparty_institution": "合成银行",
        "serial": "9000000000000000000000000000001",
        "name": "转账",
    }
    values.update(overrides)
    return tuple(values.values())


def _sheet_rows() -> list[str]:
    return [
        _row(1, ("网商银行账户交易明细",)),
        _row(2, ("卡号\N{FULLWIDTH COLON}", "************7968")),
        _row(3, ("币种", "人民币")),
        _row(8, HEADERS),
        _row(9, _txn()),
        _row(
            10,
            _txn(
                occurred_at="2026-01-03 06:07:08",
                amount="-20.00",
                balance="5105.34",
                serial="9000000000000000000000000000002",
                name="消费",
            ),
        ),
    ]


def _worksheet(rows: Iterable[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{MAIN_NS}">'
        f"<sheetData>{''.join(rows)}</sheetData></worksheet>"
    )


def _files(
    *,
    rows: Iterable[str] | None = None,
    sheets: str = '<sheet name="合成流水" sheetId="1" r:id="rId1"/>',
    worksheet_target: str = "worksheets/sheet1.xml",
    relationship_id: str = "rId1",
) -> dict[str, str]:
    return {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/></Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{PKG_REL_NS}">'
            '<Relationship Id="rId1" Type="x" Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">'
            f"<sheets>{sheets}</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<Relationships xmlns="{PKG_REL_NS}">'
            f'<Relationship Id="{relationship_id}" Type="x" '
            f'Target="{worksheet_target}"/></Relationships>'
        ),
        "xl/worksheets/sheet1.xml": _worksheet(_sheet_rows() if rows is None else rows),
    }


def _parse(
    tmp_path: Path,
    *,
    files: Mapping[str, str] | None = None,
    rows: Iterable[str] | None = None,
    raw: bytes | None = None,
    digest: str | None = None,
    suffix: str = SUFFIX,
    source_path: Path | None = None,
) -> BankStatement:
    """Parse one synthetic export, with exactly one thing about it changed."""

    path = (tmp_path / "statement.xlsx").resolve()
    if raw is None:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in (files if files is not None else _files(rows=rows)).items():
                archive.writestr(name, value.encode("utf-8"))
        raw = path.read_bytes()
    else:
        path.write_bytes(raw)
    return parse_mybank_xlsx(
        path if source_path is None else source_path,
        expected_sha256=hashlib.sha256(raw).hexdigest() if digest is None else digest,
        managed_account_suffix=suffix,
    )


# --- the arguments the caller supplies --------------------------------------


@pytest.mark.parametrize("digest", ["", "abc", "A" * 64])
def test_an_expected_digest_that_is_not_lowercase_hex_is_refused(
    tmp_path: Path, digest: str
) -> None:
    with pytest.raises(MyBankStatementError, match="expected source digest is invalid"):
        _parse(tmp_path, digest=digest)


@pytest.mark.parametrize("suffix", ["", "796", "79687968796", "796a"])
def test_a_managed_account_suffix_that_is_not_four_to_eight_digits_is_refused(
    tmp_path: Path, suffix: str
) -> None:
    with pytest.raises(MyBankStatementError, match="managed account suffix is invalid"):
        _parse(tmp_path, suffix=suffix)


# --- reading the file -------------------------------------------------------


def test_a_statement_file_that_cannot_be_opened_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_open(*args: object, **kwargs: object) -> int:
        raise PermissionError("no descriptor")

    monkeypatch.setattr(os, "open", refuse_open)
    with pytest.raises(MyBankStatementError, match="statement file cannot be opened"):
        _parse(tmp_path)


def test_a_statement_file_that_cannot_be_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_fdopen(descriptor: int, mode: str) -> object:
        os.close(descriptor)
        raise OSError("no stream")

    monkeypatch.setattr(os, "fdopen", refuse_fdopen)
    with pytest.raises(MyBankStatementError, match="statement file cannot be read"):
        _parse(tmp_path)


def test_a_statement_file_that_shrinks_while_it_is_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fdopen", lambda descriptor, mode: _ShortStream(descriptor, b""))
    with pytest.raises(MyBankStatementError, match="file changed while reading"):
        _parse(tmp_path)


# --- the zip container ------------------------------------------------------


def test_a_file_that_is_not_a_zip_container_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MyBankStatementError, match="not a valid XLSX container"):
        _parse(tmp_path, raw=b"not a zip archive at all")


def test_an_archive_with_no_entries_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.xlsx"
    with zipfile.ZipFile(path, "w"):
        pass
    with pytest.raises(MyBankStatementError, match="archive entry count is invalid"):
        _parse(tmp_path, raw=path.read_bytes())


def test_an_archive_entry_that_escapes_the_container_is_refused(tmp_path: Path) -> None:
    """A traversing name would let an unpacker write outside the workbook."""

    path = tmp_path / "unsafe.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(zipfile.ZipInfo("../escaped.xml"), b"<x/>")
        for name, value in _files().items():
            archive.writestr(name, value.encode("utf-8"))
    with pytest.raises(MyBankStatementError, match="archive entry is unsafe"):
        _parse(tmp_path, raw=path.read_bytes())


def test_an_archive_that_unpacks_to_more_than_the_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mybank_statement, "_MAX_TOTAL_UNCOMPRESSED_BYTES", 16)
    with pytest.raises(MyBankStatementError, match="archive is too large"):
        _parse(tmp_path)


def test_an_archive_missing_a_part_the_reader_needs_is_refused(tmp_path: Path) -> None:
    files = _files()
    del files["xl/workbook.xml"]
    with pytest.raises(MyBankStatementError, match="archive is incomplete"):
        _parse(tmp_path, files=files)


def test_an_empty_xml_part_is_refused(tmp_path: Path) -> None:
    files = _files()
    files["xl/workbook.xml"] = ""
    with pytest.raises(MyBankStatementError, match="XML part size is invalid"):
        _parse(tmp_path, files=files)


def test_an_xml_part_that_cannot_be_decompressed_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_read(self: zipfile.ZipFile, name: object, pwd: bytes | None = None) -> bytes:
        raise zipfile.BadZipFile("bad CRC-32")

    monkeypatch.setattr(zipfile.ZipFile, "read", refuse_read)
    with pytest.raises(MyBankStatementError, match="XML part cannot be read"):
        _parse(tmp_path)


# --- the XML itself ---------------------------------------------------------


@pytest.mark.parametrize(
    "workbook",
    [
        '<!DOCTYPE workbook [<!ENTITY x "y">]><workbook/>',
        "<!doctype workbook><workbook/>",
    ],
)
def test_xml_that_declares_entities_is_refused(tmp_path: Path, workbook: str) -> None:
    """An external entity would let the workbook read files off the host."""

    files = _files()
    files["xl/workbook.xml"] = workbook
    with pytest.raises(MyBankStatementError, match="XML declarations are unsafe"):
        _parse(tmp_path, files=files)


def test_xml_that_does_not_parse_is_refused(tmp_path: Path) -> None:
    files = _files()
    files["xl/workbook.xml"] = "<workbook><sheets></workbook>"
    with pytest.raises(MyBankStatementError, match="statement XML is invalid"):
        _parse(tmp_path, files=files)


@pytest.mark.parametrize(
    "sheets",
    ["", '<sheet name="a" r:id="rId1"/><sheet name="b" r:id="rId2"/>'],
)
def test_a_workbook_that_does_not_hold_exactly_one_worksheet_is_refused(
    tmp_path: Path, sheets: str
) -> None:
    with pytest.raises(MyBankStatementError, match="exactly one worksheet"):
        _parse(tmp_path, files=_files(sheets=sheets))


def test_a_worksheet_without_a_relationship_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MyBankStatementError, match="worksheet relationship is missing"):
        _parse(tmp_path, files=_files(sheets='<sheet name="a" sheetId="1"/>'))


def test_a_worksheet_relationship_that_resolves_to_nothing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MyBankStatementError, match="worksheet target is invalid"):
        _parse(tmp_path, files=_files(relationship_id="rId9"))


def test_a_worksheet_target_that_escapes_the_container_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MyBankStatementError, match="worksheet path is unsafe"):
        _parse(tmp_path, files=_files(worksheet_target="../../etc/passwd"))


@pytest.mark.parametrize("target", ["other/sheet1.xml", "worksheets/sheet1.bin"])
def test_a_worksheet_target_outside_the_worksheets_folder_is_refused(
    tmp_path: Path, target: str
) -> None:
    with pytest.raises(MyBankStatementError, match="worksheet path is unexpected"):
        _parse(tmp_path, files=_files(worksheet_target=target))


def test_a_worksheet_that_carries_formulas_is_refused(tmp_path: Path) -> None:
    """A formula would make the stored value depend on whoever opens the file."""

    files = _files()
    files["xl/worksheets/sheet1.xml"] = files["xl/worksheets/sheet1.xml"].replace(
        "</sheetData>", "</sheetData><f>SUM(A1:A2)</f>"
    )
    with pytest.raises(MyBankStatementError, match="formulas are not accepted"):
        _parse(tmp_path, files=files)


# --- rows and cells ---------------------------------------------------------


@pytest.mark.parametrize("attribute", ['r=""', 'r="nine"', ""])
def test_a_row_without_a_numeric_identity_is_refused(tmp_path: Path, attribute: str) -> None:
    with pytest.raises(MyBankStatementError, match="row identity is invalid"):
        _parse(tmp_path, rows=[f'<row {attribute}><c r="A1"><v>1</v></c></row>'])


@pytest.mark.parametrize("number", ["100001", "1"])
def test_a_row_number_outside_the_bound_or_repeated_is_refused(tmp_path: Path, number: str) -> None:
    rows = [_row(1, ("first",)), f'<row r="{number}"><c r="A{number}"><v>1</v></c></row>']
    with pytest.raises(MyBankStatementError, match="row count is invalid"):
        _parse(tmp_path, rows=rows)


@pytest.mark.parametrize("reference", ["", "A", "A2", "A1"])
def test_a_cell_reference_that_does_not_match_its_row_is_refused(
    tmp_path: Path, reference: str
) -> None:
    cells = f'<c r="A1"><v>1</v></c><c r="{reference}"><v>2</v></c>'
    with pytest.raises(MyBankStatementError, match="cell reference is invalid"):
        _parse(tmp_path, rows=[f'<row r="1">{cells}</row>'])


def test_a_cell_beyond_the_column_bound_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MyBankStatementError, match="column count is invalid"):
        _parse(tmp_path, rows=['<row r="1"><c r="CW1"><v>1</v></c></row>'])


def test_a_worksheet_with_no_rows_at_all_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MyBankStatementError, match="contains no readable rows"):
        _parse(tmp_path, rows=[])


def test_a_cell_of_an_unsupported_type_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MyBankStatementError, match="cell type is unsupported"):
        _parse(tmp_path, rows=['<row r="1"><c r="A1" t="e"><v>#REF!</v></c></row>'])


def test_a_shared_string_reference_that_points_nowhere_is_refused(tmp_path: Path) -> None:
    files = _files()
    files["xl/sharedStrings.xml"] = f'<sst xmlns="{MAIN_NS}"><si><t>only one</t></si></sst>'
    files["xl/worksheets/sheet1.xml"] = _worksheet(
        ['<row r="1"><c r="A1" t="s"><v>7</v></c></row>']
    )
    with pytest.raises(MyBankStatementError, match="shared string reference is invalid"):
        _parse(tmp_path, files=files)


def test_shared_strings_and_plain_values_are_read_the_same_way(tmp_path: Path) -> None:
    """A cell may name its text or inline it; both have to reach the same reader."""

    files = _files()
    files["xl/sharedStrings.xml"] = (
        f'<sst xmlns="{MAIN_NS}"><si><t>网商银行账户交易明细</t></si>'
        "<si><t>卡号\N{FULLWIDTH COLON}</t></si><si><t>************7968</t></si></sst>"
    )
    shared = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2" t="s"><v>2</v></c></row>'
        '<row r="3"><c r="A3" t="str"><v>币种</v></c><c r="B3"><v>人民币</v></c></row>'
    )
    files["xl/worksheets/sheet1.xml"] = _worksheet([shared, *_sheet_rows()[3:]])

    statement = _parse(tmp_path, files=files)

    assert len(statement.transactions) == 2


# --- what the rows have to say ----------------------------------------------


def test_an_export_that_does_not_name_mybank_is_refused(tmp_path: Path) -> None:
    rows = _sheet_rows()
    rows[0] = _row(1, ("某银行账户交易明细",))
    with pytest.raises(MyBankStatementError, match="institution is not proven"):
        _parse(tmp_path, rows=rows)


def test_an_export_for_another_account_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MyBankStatementError, match="does not belong to the managed account"):
        _parse(tmp_path, suffix="1234")


def test_an_export_that_does_not_name_its_currency_is_refused(tmp_path: Path) -> None:
    rows = _sheet_rows()
    rows[2] = _row(3, ("币种", "未知"))
    with pytest.raises(MyBankStatementError, match="currency is not proven"):
        _parse(tmp_path, rows=rows)


def test_an_export_without_the_expected_header_row_is_refused(tmp_path: Path) -> None:
    rows = _sheet_rows()
    rows[3] = _row(8, ("交易时间", "金额", *HEADERS[2:]))
    with pytest.raises(MyBankStatementError, match="header is missing or ambiguous"):
        _parse(tmp_path, rows=rows)


def test_a_blank_row_among_the_transactions_is_skipped(tmp_path: Path) -> None:
    """Exports pad with empty rows; an empty row is nothing, not a bad row."""

    rows = [*_sheet_rows(), _row(11, ("", "", ""))]

    statement = _parse(tmp_path, rows=rows)

    assert len(statement.transactions) == 2


def test_a_row_with_columns_past_the_header_is_refused(tmp_path: Path) -> None:
    rows = _sheet_rows()
    rows[4] = _row(9, (*_txn(), "extra"))
    with pytest.raises(MyBankStatementError, match="unexpected populated columns"):
        _parse(tmp_path, rows=rows)


@pytest.mark.parametrize("field", ["occurred_at", "amount", "balance", "serial", "name"])
def test_a_transaction_missing_a_required_field_is_refused(tmp_path: Path, field: str) -> None:
    rows = _sheet_rows()
    rows[4] = _row(9, _txn(**{field: ""}))
    with pytest.raises(MyBankStatementError, match="transaction is incomplete"):
        _parse(tmp_path, rows=rows)


def test_two_transactions_with_the_same_serial_are_refused(tmp_path: Path) -> None:
    """The serial is the provider's own identity for a payment; it cannot repeat."""

    rows = _sheet_rows()
    rows[5] = _row(10, _txn(occurred_at="2026-01-03 06:07:08", amount="-20.00", balance="5105.34"))
    with pytest.raises(MyBankStatementError, match="transaction serial is duplicated"):
        _parse(tmp_path, rows=rows)


def test_a_transaction_time_that_is_not_a_timestamp_is_refused(tmp_path: Path) -> None:
    rows = _sheet_rows()
    rows[4] = _row(9, _txn(occurred_at="2026/01/02 03:04:05"))
    with pytest.raises(MyBankStatementError, match="transaction time is invalid"):
        _parse(tmp_path, rows=rows)


@pytest.mark.parametrize("amount", ["一百", "125.345", "1,25.34"])
def test_an_amount_that_is_not_money_is_refused(tmp_path: Path, amount: str) -> None:
    rows = _sheet_rows()
    rows[4] = _row(9, _txn(amount=amount))
    with pytest.raises(MyBankStatementError, match="statement amount is invalid"):
        _parse(tmp_path, rows=rows)


def test_an_amount_beyond_the_representable_range_is_refused(tmp_path: Path) -> None:
    rows = _sheet_rows()
    rows[4] = _row(9, _txn(amount="99999999999999999999"))
    with pytest.raises(MyBankStatementError, match="statement amount is out of range"):
        _parse(tmp_path, rows=rows)
