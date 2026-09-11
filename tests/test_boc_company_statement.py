import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from ledgerbridge.bank_statement_contract import BankStatement, BankStatementParserProfile
from ledgerbridge.boc_company_statement import (
    _HEADER_MARKERS,
    BocCompanyStatementError,
    parse_boc_company_csv,
    parse_boc_company_xls,
)

_OLE = bytes.fromhex("D0CF11E0A1B11AE1") + b"synthetic-boc-company"
_ACCOUNT = "123456786492"


class _Sheet:
    ncols = 38

    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows
        self.nrows = len(rows)

    def row_values(self, index: int) -> list[str]:
        return self.rows[index]


class _Book:
    nsheets = 1

    def __init__(self, rows: list[list[str]]) -> None:
        self.sheet = _Sheet(rows)
        self.released = False

    def sheet_by_index(self, index: int) -> _Sheet:
        assert index == 0
        return self.sheet

    def release_resources(self) -> None:
        self.released = True


def _metadata(label: str, value: str) -> list[str]:
    return [label, value, *([""] * 36)]


def _transaction(*, amount: str, balance: str, credit: bool) -> list[str]:
    row = [""] * 38
    row[0] = "CREDIT" if credit else "DEBIT"
    row[1] = "Synthetic business type"
    row[10] = "20260401" if credit else "20260402"
    row[11] = "09:00:00"
    row[12] = "CNY"
    row[13] = amount
    row[14] = balance
    row[17] = "reference-credit" if credit else "reference-debit"
    row[22] = "record-credit" if credit else "record-debit"
    row[23] = "Synthetic reference"
    if credit:
        row[4], row[5], row[3] = "9988", "Payer", "Payer bank"
        row[8], row[9] = _ACCOUNT, "Synthetic Company"
    else:
        row[4], row[5] = _ACCOUNT, "Synthetic Company"
        row[8], row[9], row[7] = "8877", "Payee", "Payee bank"
    return row


def _rows() -> list[list[str]]:
    return [
        [""] * 38,
        _metadata("Inquirer account number", _ACCOUNT),
        _metadata("Total number", "2"),
        _metadata("Total Numbers of Debited Payments", "1"),
        _metadata("Total Debit Amount of Payments", "5.00"),
        _metadata("Total Numbers of Credited Payments", "1"),
        _metadata("Total Credit Amount of Payments", "10.00"),
        _metadata("Time Range", "20260301-20260430"),
        [f"[{marker}]" for marker in _HEADER_MARKERS],
        _transaction(amount="10.00", balance="110.00", credit=True),
        _transaction(amount="-5.00", balance="105.00", credit=False),
    ]


def _parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[list[str]]) -> BankStatement:
    source = (tmp_path / "statement.xls").resolve()
    source.write_bytes(_OLE)
    book = _Book(rows)
    monkeypatch.setattr("ledgerbridge.boc_company_statement.xlrd.open_workbook", lambda **_: book)
    statement = parse_boc_company_xls(
        source,
        expected_sha256=hashlib.sha256(_OLE).hexdigest(),
        managed_account_suffix="6492",
    )
    assert book.released
    return statement


def test_parser_reconciles_company_xls_and_selects_counterparty_by_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement = _parse(tmp_path, monkeypatch, _rows())
    assert statement.parser_profile is BankStatementParserProfile.BOC_COMPANY_XLS_V1
    assert statement.source_system == "boc_company_xls_export"
    assert len(statement.transactions) == 2
    assert statement.transactions[0].counterparty_name == "Payer"
    assert statement.transactions[1].counterparty_name == "Payee"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows[10].__setitem__(14, "109.99"),
        lambda rows: rows[10].__setitem__(5, "Wrong owner"),
        lambda rows: rows[2].__setitem__(1, "3"),
        lambda rows: rows[10].__setitem__(10, "20260228"),
    ],
)
def test_parser_rejects_balance_identity_total_and_period_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[list[list[str]]], None],
) -> None:
    rows = _rows()
    mutate(rows)
    with pytest.raises(BocCompanyStatementError):
        _parse(tmp_path, monkeypatch, rows)


def _csv_bytes(rows: list[list[str]], *, encoding: str = "gbk", tabs: bool = True) -> bytes:
    """Render the same rows as the text container online banking produces.

    The workbook's leading title row has no counterpart here, so the metadata
    block starts at row 0. Online banking suffixes fields with a tab so that
    Excel does not read account numbers as numbers, and pads the metadata rows
    past the header width; both are reproduced so the reader has to normalise
    them.
    """

    lines: list[str] = []
    for index, row in enumerate(rows):
        values = list(row)
        while values and not values[-1]:
            values.pop()
        if index < 7:
            values = [*values, "", ""]
        rendered = [f"{value}\t" if tabs and value else value for value in values]
        lines.append(",".join(f'"{value}"' for value in rendered))
    return "\r\n".join(lines).encode(encoding)


def _parse_csv(tmp_path: Path, raw: bytes) -> BankStatement:
    source = (tmp_path / "statement.csv").resolve()
    source.write_bytes(raw)
    return parse_boc_company_csv(
        source,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        managed_account_suffix="6492",
    )


@pytest.mark.parametrize("encoding", ["gbk", "utf-8-sig"])
def test_csv_container_reads_the_same_statement_in_either_encoding(
    tmp_path: Path, encoding: str
) -> None:
    statement = _parse_csv(tmp_path, _csv_bytes(_rows()[1:], encoding=encoding))
    assert statement.parser_profile is BankStatementParserProfile.BOC_COMPANY_CSV_V1
    assert statement.source_system == "boc_company_csv_export"
    assert statement.header_row_number == 8
    assert len(statement.transactions) == 2
    assert statement.transactions[0].counterparty_name == "Payer"
    assert statement.transactions[1].counterparty_name == "Payee"


def test_both_containers_of_one_export_agree_on_transaction_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property the shared reader exists to hold.

    Re-importing one month from the other container must be recognised as the
    same transactions, not booked a second time.
    """

    workbook = _parse(tmp_path, monkeypatch, _rows())
    text = _parse_csv(tmp_path, _csv_bytes(_rows()[1:]))
    assert [item.transaction_serial for item in workbook.transactions] == [
        item.transaction_serial for item in text.transactions
    ]
    assert [item.transaction_name for item in workbook.transactions] == [
        item.transaction_name for item in text.transactions
    ]


def test_csv_container_rejects_an_undecodable_source(tmp_path: Path) -> None:
    with pytest.raises(BocCompanyStatementError):
        _parse_csv(tmp_path, "not a statement\r\n".encode("gbk"))


def test_csv_container_rejects_a_row_wider_than_the_known_layout(tmp_path: Path) -> None:
    rows = _rows()[1:]
    rows[8] = [*rows[8], "surplus"]
    with pytest.raises(BocCompanyStatementError):
        _parse_csv(tmp_path, _csv_bytes(rows))
