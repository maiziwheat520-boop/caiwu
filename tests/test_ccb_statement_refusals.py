"""Every way a CCB personal XLS export can be refused.

The export is a legacy Excel workbook whose cells the bank formats as text, so
the adapter's only proof that a row is a transaction is the shape of the sheet
around it: one worksheet, nine columns, a title it recognises, a metadata band
naming the account and period, and a contiguous serial column.  Each refusal
below is one of those proofs failing.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
import xlrd  # type: ignore[import-untyped]

from ledgerbridge.ccb_statement import (
    CcbStatementError,
    _optional_text,
    _parse_metadata_date,
    _parse_minor,
    _parse_sequence,
    _parse_transaction_date,
    _required_text,
    _split_counterparty,
    _transaction_name,
    parse_ccb_personal_xls,
)
from tests.test_ccb_bank_statement import _OLE, _SUFFIX, _rows

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")


class _Sheet:
    def __init__(self, rows: list[list[Any]], *, ncols: int = 9) -> None:
        self._rows = rows
        self.nrows = len(rows)
        self.ncols = ncols

    def row_values(self, index: int) -> list[Any]:
        return self._rows[index]


class _Book:
    def __init__(self, rows: list[list[Any]], *, nsheets: int = 1, ncols: int = 9) -> None:
        self._sheet = _Sheet(rows, ncols=ncols)
        self.nsheets = nsheets
        self.released = False

    def sheet_by_index(self, index: int) -> _Sheet:
        assert index == 0
        return self._sheet

    def release_resources(self) -> None:
        self.released = True


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: list[list[Any]] | None = None,
    book: object = None,
    open_error: Exception | None = None,
    source_bytes: bytes = _OLE,
) -> tuple[Path, str]:
    source = (tmp_path / "synthetic.xls").resolve()
    source.write_bytes(source_bytes)

    def _open(**_: object) -> object:
        if open_error is not None:
            raise open_error
        return book if book is not None else _Book(rows if rows is not None else _rows())

    monkeypatch.setattr("ledgerbridge.ccb_statement.xlrd.open_workbook", _open)
    return source, hashlib.sha256(source_bytes).hexdigest()


def _refuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    *,
    suffix: str = _SUFFIX,
    **prepare: Any,
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch, **prepare)
    with pytest.raises(CcbStatementError, match=message):
        parse_ccb_personal_xls(source, expected_sha256=digest, managed_account_suffix=suffix)


# --- the caller's arguments and the file itself -----------------------------


def test_the_expected_digest_must_be_a_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _prepare(tmp_path, monkeypatch)
    with pytest.raises(CcbStatementError, match="expected source digest is invalid"):
        parse_ccb_personal_xls(source, expected_sha256="nope", managed_account_suffix=_SUFFIX)


def test_the_managed_account_suffix_must_be_four_to_eight_digits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    with pytest.raises(CcbStatementError, match="managed account suffix is invalid"):
        parse_ccb_personal_xls(source, expected_sha256=digest, managed_account_suffix="75")


def test_a_source_whose_bytes_moved_since_approval_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _prepare(tmp_path, monkeypatch)
    with pytest.raises(CcbStatementError, match="source digest changed"):
        parse_ccb_personal_xls(source, expected_sha256="c" * 64, managed_account_suffix=_SUFFIX)


def test_a_source_that_is_not_a_legacy_workbook_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "not a legacy Excel container",
        source_bytes=b"PK\x03\x04modern-xlsx",
    )


def test_a_relative_source_path_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch)
    with pytest.raises(CcbStatementError, match="statement path must be absolute"):
        parse_ccb_personal_xls(
            Path("synthetic.xls"), expected_sha256="c" * 64, managed_account_suffix=_SUFFIX
        )


def test_a_source_that_does_not_exist_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path, monkeypatch)
    with pytest.raises(CcbStatementError, match="statement file is unavailable"):
        parse_ccb_personal_xls(
            (tmp_path / "absent.xls").resolve(),
            expected_sha256="c" * 64,
            managed_account_suffix=_SUFFIX,
        )


def test_a_directory_is_not_a_statement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch)
    directory = tmp_path / "statement-dir"
    directory.mkdir()
    with pytest.raises(CcbStatementError, match="statement file must be regular"):
        parse_ccb_personal_xls(
            directory.resolve(), expected_sha256="c" * 64, managed_account_suffix=_SUFFIX
        )


def test_an_empty_source_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(tmp_path, monkeypatch, "statement file size is invalid", source_bytes=b"")


@_POSIX_ONLY
def test_a_source_reached_through_a_symlink_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    link = tmp_path / "link.xls"
    link.symlink_to(source)
    with pytest.raises(CcbStatementError, match="statement file must be regular"):
        parse_ccb_personal_xls(link, expected_sha256=digest, managed_account_suffix=_SUFFIX)


def test_a_source_that_cannot_be_opened_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    real_open = os.open

    def _refusing_open(path: object, flags: int, *args: object) -> int:
        if str(path).endswith("synthetic.xls"):
            raise OSError("permission denied")
        return real_open(path, flags, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _refusing_open)
    with pytest.raises(CcbStatementError, match="statement file cannot be opened"):
        parse_ccb_personal_xls(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


def test_a_source_that_fails_mid_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    real_fdopen = os.fdopen

    class _FailingStream:
        def __init__(self, descriptor: int) -> None:
            self._stream = real_fdopen(descriptor, "rb")

        def __enter__(self) -> _FailingStream:
            return self

        def __exit__(self, *exc: object) -> None:
            self._stream.close()

        def read(self, size: int) -> bytes:
            del size
            raise OSError("the device disappeared")

    monkeypatch.setattr(os, "fdopen", lambda descriptor, mode: _FailingStream(descriptor))
    with pytest.raises(CcbStatementError, match="statement file cannot be read"):
        parse_ccb_personal_xls(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


def test_a_source_that_shrinks_between_stat_and_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    real_fdopen = os.fdopen

    class _TruncatingStream:
        def __init__(self, descriptor: int) -> None:
            self._stream = real_fdopen(descriptor, "rb")

        def __enter__(self) -> _TruncatingStream:
            return self

        def __exit__(self, *exc: object) -> None:
            self._stream.close()

        def read(self, size: int) -> bytes:
            return self._stream.read(size)[:-1]

    monkeypatch.setattr(os, "fdopen", lambda descriptor, mode: _TruncatingStream(descriptor))
    with pytest.raises(CcbStatementError, match="statement file changed while reading"):
        parse_ccb_personal_xls(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


# --- the workbook -----------------------------------------------------------


def test_a_workbook_xlrd_cannot_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement workbook is invalid",
        open_error=xlrd.XLRDError("unsupported format"),
    )


def test_a_workbook_with_more_than_one_worksheet_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A second sheet could hold rows the totals never covered.
    _refuse(
        tmp_path,
        monkeypatch,
        "exactly one worksheet",
        book=_Book(_rows(), nsheets=2),
    )


@pytest.mark.parametrize(("nrows", "ncols"), [(4, 9), (6, 8)])
def test_a_worksheet_of_the_wrong_shape_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nrows: int, ncols: int
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "worksheet dimensions are invalid",
        book=_Book(_rows()[:nrows], ncols=ncols),
    )


def test_a_non_text_cell_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The bank exports every cell as text; a float would already have lost the
    # exact minor units the ledger needs.
    rows: list[list[Any]] = [list(row) for row in _rows()]
    rows[4][5] = -12.34
    _refuse(tmp_path, monkeypatch, "non-text cell", rows=rows)


@pytest.mark.parametrize("value", ["x" * 301, "备注\x00"])
def test_an_unstorable_or_oversize_cell_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    rows = _rows()
    rows[4][7] = value
    _refuse(tmp_path, monkeypatch, "cell text is invalid", rows=rows)


# --- the title and metadata band --------------------------------------------


def test_a_workbook_that_does_not_name_the_bank_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[0][4] = "某银行交易清单"
    _refuse(tmp_path, monkeypatch, "institution and export type are not proven", rows=rows)


def test_a_statement_for_another_account_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[1][1] = "卡号/账号:000000000000009999"
    _refuse(tmp_path, monkeypatch, "does not belong to the managed account", rows=rows)


def test_a_statement_without_an_account_holder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[1][3] = "客户名称:" + "x" * 301
    _refuse(tmp_path, monkeypatch, "cell text is invalid", rows=rows)


def test_a_metadata_cell_without_its_label_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[1][1] = "000000000000007564"
    _refuse(tmp_path, monkeypatch, "statement metadata is invalid", rows=rows)


def test_a_metadata_cell_with_nothing_after_its_label_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[1][3] = "客户名称:"
    _refuse(tmp_path, monkeypatch, "statement metadata is incomplete", rows=rows)


def test_a_metadata_date_that_does_not_exist_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[1][5] = "起始日期:20261301"
    _refuse(tmp_path, monkeypatch, "metadata date is invalid", rows=rows)


def test_a_missing_column_header_row_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[3][0] = "序"
    _refuse(tmp_path, monkeypatch, "header is missing or ambiguous", rows=rows)


# --- transaction rows -------------------------------------------------------


def test_a_blank_row_inside_the_transaction_block_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows.insert(5, ["", "", "", "", "", "", "", "", ""])
    _refuse(tmp_path, monkeypatch, "unexpected empty transaction row", rows=rows)


def test_a_row_of_the_wrong_width_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[4] = rows[4][:8]
    _refuse(tmp_path, monkeypatch, "transaction column count is invalid", rows=rows)


def test_a_serial_column_with_a_gap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A gap means a row the bank printed is not in the file we are importing.
    rows = _rows()
    rows[5][0] = "3"
    _refuse(tmp_path, monkeypatch, "sequence is not contiguous", rows=rows)


@pytest.mark.parametrize(
    ("column", "value"),
    [(2, "美元"), (3, "汇")],
)
def test_a_row_in_another_currency_or_settlement_mode_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, column: int, value: str
) -> None:
    rows = _rows()
    rows[4][column] = value
    _refuse(tmp_path, monkeypatch, "transaction currency is not proven", rows=rows)


def test_a_transaction_date_that_does_not_exist_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[4][4] = "20260532"
    _refuse(tmp_path, monkeypatch, "transaction date is invalid", rows=rows)


def test_two_identical_facts_on_one_statement_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[5] = list(rows[4])
    rows[5][0] = "2"
    _refuse(tmp_path, monkeypatch, "without a unique fact identity", rows=rows)


def test_rows_that_run_backwards_in_time_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[4][4] = "20260603"
    rows[5][4] = "20260501"
    _refuse(tmp_path, monkeypatch, "transactions are not date ordered", rows=rows)


def test_rows_outside_the_declared_period_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[5][4] = "20260604"
    _refuse(tmp_path, monkeypatch, "outside the metadata period", rows=rows)


def test_a_row_without_a_summary_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _rows()
    rows[4][1] = ""
    _refuse(tmp_path, monkeypatch, "statement summary is missing", rows=rows)


# --- the helpers, stated directly -------------------------------------------


def test_a_metadata_or_transaction_date_must_be_eight_digits() -> None:
    with pytest.raises(CcbStatementError, match="metadata date is invalid"):
        _parse_metadata_date("2026-05-01")
    with pytest.raises(CcbStatementError, match="transaction date is invalid"):
        _parse_transaction_date("2026-05-01")


@pytest.mark.parametrize("value", ["", "one", "-1", "0"])
def test_a_serial_that_is_not_a_positive_row_number_is_refused(value: str) -> None:
    with pytest.raises(CcbStatementError, match="transaction sequence is invalid"):
        _parse_sequence(value)


@pytest.mark.parametrize("value", ["", "1.234", "abc", "1,23.45"])
def test_only_renminbi_shaped_text_is_money(value: str) -> None:
    with pytest.raises(CcbStatementError, match="statement amount is invalid"):
        _parse_minor(value, field="amount")


def test_an_amount_beyond_the_safe_integer_range_is_refused() -> None:
    with pytest.raises(CcbStatementError, match="statement balance is out of range"):
        _parse_minor("99999999999999999", field="balance")


def test_a_thousands_separated_amount_is_read_as_minor_units() -> None:
    assert _parse_minor("1,234.56", field="amount") == 123456


def test_a_required_field_may_not_be_empty() -> None:
    with pytest.raises(CcbStatementError, match="statement summary is missing"):
        _required_text("", field="summary")


def test_an_optional_field_is_still_bounded() -> None:
    assert _optional_text("", field="location note") == ""
    with pytest.raises(CcbStatementError, match="statement location note is invalid"):
        _optional_text("x" * 301, field="location note")


def test_an_absent_counterparty_yields_two_empty_fields() -> None:
    assert _split_counterparty("") == ("", "")


def test_a_counterparty_without_a_separator_is_read_by_its_shape() -> None:
    # Digits alone can only be an account; anything else can only be a name.
    assert _split_counterparty("6222000000001234") == ("6222000000001234", "")
    assert _split_counterparty("合成商户") == ("", "合成商户")


def test_a_counterparty_splits_on_the_first_separator() -> None:
    assert _split_counterparty("1111/甲/乙") == ("1111", "甲/乙")


def test_a_transaction_name_longer_than_the_field_is_refused() -> None:
    with pytest.raises(CcbStatementError, match="summary and location note are too long"):
        _transaction_name("x" * 200, "y" * 200)


def test_a_transaction_name_without_a_location_note_is_the_summary_alone() -> None:
    assert _transaction_name("消费", "") == "消费"
