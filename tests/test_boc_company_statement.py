import hashlib
from pathlib import Path

import pytest

from ledgerbridge.bank_statement_contract import BankStatementParserProfile
from ledgerbridge.boc_company_statement import (
    _HEADER_MARKERS,
    BocCompanyStatementError,
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


def _parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[list[str]]):
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate
) -> None:
    rows = _rows()
    mutate(rows)
    with pytest.raises(BocCompanyStatementError):
        _parse(tmp_path, monkeypatch, rows)
