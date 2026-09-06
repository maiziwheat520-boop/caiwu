"""Every refusal the BOC company account-detail parser makes.

The export carries both sides of every payment, so the parser has to decide
which side is the managed account before it can name a counterparty. Each
test removes one of the proofs that decision rests on - the file, the account
identity, the declared period, the header, the per-row shape, the balance
chain, the declared totals - and fixes that the parser refuses rather than
guessing which column belongs to whom.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import xlrd  # type: ignore[import-untyped]

from ledgerbridge import boc_company_statement
from ledgerbridge.bank_statement_contract import BankStatement
from ledgerbridge.boc_company_statement import BocCompanyStatementError, parse_boc_company_xls
from tests.test_boc_company_statement import (
    _ACCOUNT,
    _OLE,
    _Book,
    _metadata,
    _rows,
    _transaction,
)

DIGEST = hashlib.sha256(_OLE).hexdigest()
SUFFIX = "6492"


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[list[Any]] | None = None,
    raw: bytes = _OLE,
    book: object | None = None,
    workbook_error: Exception | None = None,
    digest: str | None = None,
    suffix: str = SUFFIX,
    source_path: Path | None = None,
) -> BankStatement:
    """Parse one synthetic export, with exactly one thing about it changed."""

    path = (tmp_path / "statement.xls").resolve()
    path.write_bytes(raw)
    prepared = book if book is not None else _Book(rows if rows is not None else _rows())

    def open_workbook(**_: object) -> object:
        if workbook_error is not None:
            raise workbook_error
        return prepared

    monkeypatch.setattr("ledgerbridge.boc_company_statement.xlrd.open_workbook", open_workbook)
    return parse_boc_company_xls(
        path if source_path is None else source_path,
        expected_sha256=hashlib.sha256(raw).hexdigest() if digest is None else digest,
        managed_account_suffix=suffix,
    )


# --- the arguments the caller supplies --------------------------------------


@pytest.mark.parametrize("digest", ["", "abc", "A" * 64])
def test_an_expected_digest_that_is_not_lowercase_hex_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, digest: str
) -> None:
    with pytest.raises(BocCompanyStatementError, match="expected source digest is invalid"):
        _run(tmp_path, monkeypatch, digest=digest)


@pytest.mark.parametrize("suffix", ["", "649", "6492649264", "649a"])
def test_a_managed_account_suffix_that_is_not_four_to_eight_digits_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    with pytest.raises(BocCompanyStatementError, match="managed account suffix is invalid"):
        _run(tmp_path, monkeypatch, suffix=suffix)


def test_a_file_whose_digest_does_not_match_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(BocCompanyStatementError, match="source digest changed"):
        _run(tmp_path, monkeypatch, digest="0" * 64)


def test_a_file_that_is_not_a_legacy_excel_container_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(BocCompanyStatementError, match="not a legacy Excel container"):
        _run(tmp_path, monkeypatch, raw=b"PK\x03\x04 not an OLE container")


# --- reading the file -------------------------------------------------------


def test_a_relative_statement_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(BocCompanyStatementError, match="must be an absolute XLS file"):
        _run(tmp_path, monkeypatch, source_path=Path("statement.xls"))


def test_a_statement_that_is_not_an_xls_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = (tmp_path / "statement.xlsx").resolve()
    other.write_bytes(_OLE)
    with pytest.raises(BocCompanyStatementError, match="must be an absolute XLS file"):
        _run(tmp_path, monkeypatch, source_path=other)


def test_a_statement_that_cannot_be_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = (tmp_path / "directory.xls").resolve()
    directory.mkdir()
    with pytest.raises(BocCompanyStatementError, match="statement could not be read"):
        _run(tmp_path, monkeypatch, source_path=directory)


# --- the workbook -----------------------------------------------------------


@pytest.mark.parametrize(
    "error", [xlrd.XLRDError("corrupt"), ValueError("corrupt"), IndexError("corrupt")]
)
def test_a_workbook_that_cannot_be_opened_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    with pytest.raises(BocCompanyStatementError, match="statement workbook is invalid"):
        _run(tmp_path, monkeypatch, workbook_error=error)


@pytest.mark.parametrize("nsheets", [0, 2])
def test_a_workbook_that_does_not_hold_exactly_one_worksheet_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nsheets: int
) -> None:
    book = _Book(_rows())
    book.nsheets = nsheets
    with pytest.raises(BocCompanyStatementError, match="workbook shape is invalid"):
        _run(tmp_path, monkeypatch, book=book)
    assert book.released


@pytest.mark.parametrize("ncols", [37, 39])
def test_a_worksheet_of_the_wrong_width_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ncols: int
) -> None:
    book = _Book(_rows())
    book.sheet.ncols = ncols
    with pytest.raises(BocCompanyStatementError, match="workbook width is invalid"):
        _run(tmp_path, monkeypatch, book=book)
    assert book.released


# --- the export's own metadata ----------------------------------------------


def test_an_export_too_short_to_hold_a_transaction_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(BocCompanyStatementError, match="contains no transaction rows"):
        _run(tmp_path, monkeypatch, rows=_rows()[:9])


@pytest.mark.parametrize("row", [_metadata("Some other label", _ACCOUNT), [""] * 4])
def test_a_metadata_row_without_its_own_label_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: list[str]
) -> None:
    rows = _rows()
    rows[1] = row
    with pytest.raises(BocCompanyStatementError, match="metadata is missing or ambiguous"):
        _run(tmp_path, monkeypatch, rows=rows)


@pytest.mark.parametrize("value", ["", "x" * 301])
def test_a_metadata_value_that_is_empty_or_oversize_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    rows = _rows()
    rows[1] = _metadata("Inquirer account number", value)
    with pytest.raises(BocCompanyStatementError, match="Inquirer account number is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


@pytest.mark.parametrize("account", ["12345", "12345678649a"])
def test_an_account_number_that_is_not_the_expected_shape_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, account: str
) -> None:
    rows = _rows()
    rows[1] = _metadata("Inquirer account number", account)
    with pytest.raises(BocCompanyStatementError, match="does not belong to the managed account"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_an_export_for_another_account_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(BocCompanyStatementError, match="does not belong to the managed account"):
        _run(tmp_path, monkeypatch, suffix="1234")


@pytest.mark.parametrize("value", ["not a number", "-1"])
def test_a_declared_count_that_is_not_a_whole_non_negative_number_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    rows = _rows()
    rows[2] = _metadata("Total number", value)
    with pytest.raises(BocCompanyStatementError, match="transaction count is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_declared_total_that_is_not_a_number_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[4] = _metadata("Total Debit Amount of Payments", "not a number")
    with pytest.raises(BocCompanyStatementError, match="debit total is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_declared_total_with_excess_precision_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[4] = _metadata("Total Debit Amount of Payments", "5.001")
    with pytest.raises(BocCompanyStatementError, match="debit total has excess precision"):
        _run(tmp_path, monkeypatch, rows=rows)


@pytest.mark.parametrize("value", ["20260301/20260430", "2026030-20260430", "not a range"])
def test_a_time_range_that_is_not_two_dates_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    rows = _rows()
    rows[7] = _metadata("Time Range", value)
    with pytest.raises(BocCompanyStatementError, match="statement period is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_time_range_that_ends_before_it_starts_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[7] = _metadata("Time Range", "20260430-20260301")
    with pytest.raises(BocCompanyStatementError, match="statement period is reversed"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_time_range_that_is_not_a_real_date_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[7] = _metadata("Time Range", "20261340-20260430")
    with pytest.raises(BocCompanyStatementError, match="statement date is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_header_row_that_is_not_the_one_boc_exports_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[8][13] = "[Amount]"
    with pytest.raises(BocCompanyStatementError, match="header is missing or ambiguous"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_header_row_of_the_wrong_width_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[8] = rows[8][:37]
    with pytest.raises(BocCompanyStatementError, match="header is missing or ambiguous"):
        _run(tmp_path, monkeypatch, rows=rows)


# --- each transaction row ---------------------------------------------------


@pytest.mark.parametrize("row", [[""] * 38, ["x"] * 37])
def test_a_transaction_row_that_is_empty_or_the_wrong_width_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: list[str]
) -> None:
    rows = _rows()
    rows.append(row)
    with pytest.raises(BocCompanyStatementError, match="transaction row is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


@pytest.mark.parametrize("value", ["2026-04-01", "20261340"])
def test_a_transaction_date_that_is_not_a_real_date_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    rows = _rows()
    rows[9][10] = value
    with pytest.raises(BocCompanyStatementError, match="statement date is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


@pytest.mark.parametrize("value", ["9:00:00", "99:00:00"])
def test_a_transaction_time_that_is_not_a_real_time_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    rows = _rows()
    rows[9][11] = value
    with pytest.raises(BocCompanyStatementError, match="statement time is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_transaction_outside_the_declared_period_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[9][10] = "20260501"
    with pytest.raises(BocCompanyStatementError, match="falls outside its period"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_transaction_in_another_currency_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[9][12] = "USD"
    with pytest.raises(BocCompanyStatementError, match="transaction currency is not proven"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_zero_transaction_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _rows()
    rows[9][13] = "0.00"
    with pytest.raises(BocCompanyStatementError, match="contains a zero transaction"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_transaction_amount_with_excess_precision_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[9][13] = "10.001"
    with pytest.raises(BocCompanyStatementError, match="amount has excess precision"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_transaction_naming_an_account_that_is_not_this_statement_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which column is the counterparty depends on which side is us."""

    rows = _rows()
    rows[9][8] = "999999999999"
    with pytest.raises(BocCompanyStatementError, match="conflicts with statement identity"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_transaction_without_an_account_holder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[9][9] = ""
    with pytest.raises(BocCompanyStatementError, match="account holder is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_transaction_with_no_description_at_all_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With every free-text column blank the business type is the last name left."""

    rows = _rows()
    for column in (1, 23, 24, 25, 26):
        rows[9][column] = ""
    with pytest.raises(BocCompanyStatementError, match="business type is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


# --- the block as a whole ---------------------------------------------------


def test_a_broken_balance_chain_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _rows()
    rows[10][14] = "106.00"
    with pytest.raises(BocCompanyStatementError, match="balance chain is broken"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_two_rows_that_are_the_same_fact_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated fact would post twice, so identity is checked before the totals."""

    rows = _rows()[:9]
    rows.append(_transaction(amount="10.00", balance="110.00", credit=True))
    rows.append(_transaction(amount="-10.00", balance="100.00", credit=False))
    rows.append(_transaction(amount="10.00", balance="110.00", credit=True))
    with pytest.raises(BocCompanyStatementError, match="duplicate transaction facts"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_more_transactions_than_the_parser_will_hold_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boc_company_statement, "_MAX_ROWS", 1)
    with pytest.raises(BocCompanyStatementError, match="transaction count is invalid"):
        _run(tmp_path, monkeypatch)


def test_transactions_that_are_not_in_order_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[9][10], rows[10][10] = rows[10][10], rows[9][10]
    with pytest.raises(BocCompanyStatementError, match="transactions are not ordered"):
        _run(tmp_path, monkeypatch, rows=rows)
