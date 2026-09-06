"""Every refusal the ABC company account-detail parser makes.

A company statement becomes an immutable financial fact the moment it parses,
so the parser proves the export rather than interpreting it: the file it was
told to read, the account it was told to belong to, the shape ABC actually
exports, and a transaction block whose direction, chronology, balance chain
and footer totals all agree. Each test removes exactly one of those proofs.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
import xlrd  # type: ignore[import-untyped]

from ledgerbridge.abc_company_statement import AbcCompanyStatementError, parse_abc_company_xls
from ledgerbridge.bank_statement_contract import BankStatement
from tests.test_abc_company_statement import _OLE, _Book, _rows

DIGEST = hashlib.sha256(_OLE).hexdigest()
SUFFIX = "9018"


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

    monkeypatch.setattr("ledgerbridge.abc_company_statement.xlrd.open_workbook", open_workbook)
    return parse_abc_company_xls(
        path if source_path is None else source_path,
        expected_sha256=hashlib.sha256(raw).hexdigest() if digest is None else digest,
        managed_account_suffix=suffix,
    )


# --- the arguments the caller supplies --------------------------------------


@pytest.mark.parametrize("digest", ["", "abc", "A" * 64, "0" * 63])
def test_an_expected_digest_that_is_not_lowercase_hex_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, digest: str
) -> None:
    with pytest.raises(AbcCompanyStatementError, match="expected source digest is invalid"):
        _run(tmp_path, monkeypatch, digest=digest)


@pytest.mark.parametrize("suffix", ["", "901", "901801801", "90a8"])
def test_a_managed_account_suffix_that_is_not_four_to_eight_digits_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    with pytest.raises(AbcCompanyStatementError, match="managed account suffix is invalid"):
        _run(tmp_path, monkeypatch, suffix=suffix)


def test_a_file_whose_digest_does_not_match_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The digest names which bytes were approved, so it is checked, not assumed."""

    with pytest.raises(AbcCompanyStatementError, match="source digest changed"):
        _run(tmp_path, monkeypatch, digest="0" * 64)


def test_a_file_that_is_not_a_legacy_excel_container_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(AbcCompanyStatementError, match="not a legacy Excel container"):
        _run(tmp_path, monkeypatch, raw=b"PK\x03\x04 not an OLE container")


# --- reading the file -------------------------------------------------------


def test_a_relative_statement_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(AbcCompanyStatementError, match="statement path must be absolute"):
        _run(tmp_path, monkeypatch, source_path=Path("statement.xls"))


def test_an_empty_statement_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(AbcCompanyStatementError, match="statement file is invalid"):
        _run(tmp_path, monkeypatch, raw=b"")


def test_a_directory_is_not_a_statement_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = (tmp_path / "directory.xls").resolve()
    directory.mkdir()
    with pytest.raises(AbcCompanyStatementError, match="statement file is invalid"):
        _run(tmp_path, monkeypatch, source_path=directory)


def test_a_statement_file_that_shrinks_while_it_is_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fdopen", lambda descriptor, mode: _ShortStream(descriptor, b""))
    with pytest.raises(AbcCompanyStatementError, match="file changed while reading"):
        _run(tmp_path, monkeypatch)


# --- the workbook -----------------------------------------------------------


@pytest.mark.parametrize(
    "error", [xlrd.XLRDError("corrupt"), ValueError("corrupt"), OSError("corrupt")]
)
def test_a_workbook_that_cannot_be_opened_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    with pytest.raises(AbcCompanyStatementError, match="statement workbook is invalid"):
        _run(tmp_path, monkeypatch, workbook_error=error)


@pytest.mark.parametrize("nsheets", [0, 2])
def test_a_workbook_that_does_not_hold_exactly_one_worksheet_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nsheets: int
) -> None:
    book = _Book(_rows())
    book.nsheets = nsheets
    with pytest.raises(AbcCompanyStatementError, match="must contain one worksheet"):
        _run(tmp_path, monkeypatch, book=book)
    assert book.released


@pytest.mark.parametrize(("ncols", "rows"), [(7, None), (9, None), (8, 5)])
def test_a_worksheet_whose_dimensions_are_wrong_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ncols: int, rows: int | None
) -> None:
    book = _Book(_rows() if rows is None else _rows()[:rows])
    book.sheet.ncols = ncols
    with pytest.raises(AbcCompanyStatementError, match="statement dimensions are invalid"):
        _run(tmp_path, monkeypatch, book=book)
    assert book.released


def test_a_cell_that_is_neither_text_nor_a_number_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[3][4] = 123
    with pytest.raises(AbcCompanyStatementError, match="statement cell type is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


@pytest.mark.parametrize("text", ["x" * 301, "before\x00after"])
def test_a_cell_whose_text_cannot_be_stored_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    rows = _rows()
    rows[3][6] = text
    with pytest.raises(AbcCompanyStatementError, match="statement text is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


# --- the export's shape -----------------------------------------------------


def test_an_export_that_does_not_announce_itself_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[0][0] = "其他导出"
    with pytest.raises(AbcCompanyStatementError, match="export shape is not proven"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_an_export_whose_header_row_is_not_the_expected_one_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[2][7] = "备注"
    with pytest.raises(AbcCompanyStatementError, match="export shape is not proven"):
        _run(tmp_path, monkeypatch, rows=rows)


@pytest.mark.parametrize("value", ["账号", "账号:", "户名:合成酒店有限公司"])
def test_a_metadata_cell_without_its_own_label_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    rows = _rows()
    rows[1][0] = value
    with pytest.raises(AbcCompanyStatementError, match="statement metadata is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_an_export_in_another_currency_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[1][2] = "币种:美元"
    with pytest.raises(AbcCompanyStatementError, match="statement currency is not proven"):
        _run(tmp_path, monkeypatch, rows=rows)


@pytest.mark.parametrize(
    "value",
    [
        "起止日期: 2025-09-05 - 2026-09-04 ",
        "起止日期: 2025年09月05日 ",
        "起止日期: 不是日期 ",
    ],
)
def test_a_period_that_is_not_two_dates_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    rows = _rows()
    rows[1][5] = value
    with pytest.raises(AbcCompanyStatementError, match="statement period is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_an_account_number_that_is_not_the_expected_shape_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[1][0] = "账号:41-0298"
    with pytest.raises(AbcCompanyStatementError, match="does not belong to the managed account"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_an_export_for_another_account_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The managed suffix is the only thing tying an export to this ledger."""

    with pytest.raises(AbcCompanyStatementError, match="does not belong to the managed account"):
        _run(tmp_path, monkeypatch, suffix="1234")


# --- the transaction block --------------------------------------------------


def test_an_export_with_no_transaction_rows_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[3][0] = "2026-09-04"
    rows[4][0] = "2026-09-03"
    with pytest.raises(AbcCompanyStatementError, match="footer or transaction range is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_an_export_with_rows_beyond_its_totals_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows.append(["", "", "", "", "", "", "", ""])
    with pytest.raises(AbcCompanyStatementError, match="footer or transaction range is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_an_export_without_its_totals_labels_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[5][0] = "合计"
    with pytest.raises(AbcCompanyStatementError, match="totals footer is missing"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_transaction_without_a_summary_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[3][7] = "  "
    with pytest.raises(AbcCompanyStatementError, match="statement summary is missing"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_two_transactions_that_are_the_same_fact_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repeated fact would post twice, so identity is checked before anything else."""

    rows = _rows()
    rows[4] = list(rows[3])
    rows[-1] = ["2", 40.0, "0", 0.0, "", "", "", ""]
    with pytest.raises(AbcCompanyStatementError, match="duplicate fact identity"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_transactions_that_are_not_reverse_chronological_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[3][0] = "2026-09-03 12:00:00"
    rows[4][0] = "2026-09-04 12:00:00"
    with pytest.raises(AbcCompanyStatementError, match="not reverse chronological"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_transactions_outside_the_declared_period_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[1][5] = "起止日期: 2026年09月04日 - 2026年09月04日 "
    with pytest.raises(AbcCompanyStatementError, match="exceed declared period"):
        _run(tmp_path, monkeypatch, rows=rows)


# --- the numbers ------------------------------------------------------------


def test_an_amount_that_is_not_a_number_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[3][3] = "不是金额"
    with pytest.raises(AbcCompanyStatementError, match="statement balance is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


@pytest.mark.parametrize("value", ["1.234", "1e18"])
def test_an_amount_that_is_not_a_whole_number_of_cents_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    rows = _rows()
    rows[3][3] = value
    with pytest.raises(AbcCompanyStatementError, match="statement balance is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)


def test_a_transaction_count_that_is_not_a_whole_number_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[-1][0] = "1.5"
    with pytest.raises(AbcCompanyStatementError, match="statement count is invalid"):
        _run(tmp_path, monkeypatch, rows=rows)
