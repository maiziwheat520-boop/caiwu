"""Every way an unlocked ABC personal PDF export can be refused.

The adapter reads a layout render of a PDF the bank produced, so column
positions, the page marker and the balance chain are the only things proving
the rows belong together.  Each refusal below is a fact the export has to
establish about itself before any of it reaches the ledger.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from ledgerbridge.abc_statement import (
    AbcStatementError,
    _bounded_text,
    _find_header,
    _join_parts,
    _money_minor,
    _parse_date,
    _parse_time,
    _split_counterparty,
    parse_abc_personal_pdf,
)
from tests.test_abc_bank_statement import _ACCOUNT, _HEADERS, _SUFFIX, _line

_PDF_BYTES = b"%PDF-1.7\nsynthetic-abc"
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")


def _row(
    *,
    date_text: str = "20260501",
    time_text: str = "090000",
    summary: str = "消费",
    amount: str = "-12.34",
    balance: str = "100.00",
    counterparty: str = "合成商户",
    log_number: str = "A000000001",
    channel: str = "网银",
    note: str = "合成附言",
) -> str:
    return _line(
        (date_text, time_text, summary, amount, balance, counterparty, log_number, channel, note)
    )


_SECOND_ROW = _row(
    date_text="20260603",
    time_text="",
    summary="转入",
    amount="20.00",
    balance="120.00",
    counterparty="-",
    log_number="A000000002",
    channel="掌银",
    note="",
)


def _page(
    *,
    title: str = "中国农业银行账户活期交易明细清单",
    owner: str = "合成测试用户",
    account: str = _ACCOUNT,
    currency: str = "人民币",
    cash: str = "汇",
    start: str = "20260501",
    end: str = "20260603",
    serial: str = "10000000000000000001",
    marker: str = "第1页\uff0c共1页",
    body: tuple[str, ...] | None = None,
    header_count: int = 1,
    footer: str | None = "核对说明",
) -> str:
    lines = [
        title,
        f"户名: {owner}                                  账户: {account}",
        f"币种: {currency}                                        汇钞标识: {cash}",
        f"起止日期: {start} - {end}                        电子流水号: {serial}",
        "",
    ]
    lines.extend([_line(_HEADERS)] * header_count)
    lines.extend(body if body is not None else (_row(), _SECOND_ROW))
    if footer is not None:
        lines.append(footer)
    lines.append(marker)
    return "\n".join(lines)


class _StubPage:
    def __init__(self, text: object, error: Exception | None = None) -> None:
        self._text = text
        self._error = error

    def extract_text(self, *, extraction_mode: str) -> object:
        assert extraction_mode == "layout"
        if self._error is not None:
            raise self._error
        return self._text


class _StubReader:
    def __init__(self, pages: list[_StubPage], *, encrypted: bool = False) -> None:
        self.is_encrypted = encrypted
        self.pages = pages


class _UnreadablePagesReader:
    is_encrypted = False

    @property
    def pages(self) -> list[_StubPage]:
        raise RuntimeError("the page tree is damaged")


def _reader_for(*texts: str) -> _StubReader:
    return _StubReader([_StubPage(text) for text in texts])


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reader: object = None,
    reader_error: Exception | None = None,
    source_bytes: bytes = _PDF_BYTES,
) -> tuple[Path, str]:
    source = (tmp_path / "synthetic-abc.pdf").resolve()
    source.write_bytes(source_bytes)

    def _factory(_: object) -> object:
        if reader_error is not None:
            raise reader_error
        return reader if reader is not None else _reader_for(_page())

    monkeypatch.setattr("ledgerbridge.abc_statement.PdfReader", _factory)
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
    with pytest.raises(AbcStatementError, match=message):
        parse_abc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=suffix)


# --- the caller's arguments and the file itself -----------------------------


def test_the_expected_digest_must_be_a_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _prepare(tmp_path, monkeypatch)
    with pytest.raises(AbcStatementError, match="expected source digest is invalid"):
        parse_abc_personal_pdf(source, expected_sha256="nope", managed_account_suffix=_SUFFIX)


def test_the_managed_account_suffix_must_be_four_to_eight_digits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    with pytest.raises(AbcStatementError, match="managed account suffix is invalid"):
        parse_abc_personal_pdf(source, expected_sha256=digest, managed_account_suffix="86420000000")


def test_a_source_whose_bytes_moved_since_approval_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _prepare(tmp_path, monkeypatch)
    with pytest.raises(AbcStatementError, match="source digest changed"):
        parse_abc_personal_pdf(source, expected_sha256="b" * 64, managed_account_suffix=_SUFFIX)


def test_a_source_that_is_not_a_pdf_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(tmp_path, monkeypatch, "statement is not a PDF", source_bytes=b"PK\x03\x04nope")


def test_a_relative_source_path_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch)
    with pytest.raises(AbcStatementError, match="statement path must be absolute"):
        parse_abc_personal_pdf(
            Path("synthetic-abc.pdf"), expected_sha256="b" * 64, managed_account_suffix=_SUFFIX
        )


def test_a_source_that_does_not_exist_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path, monkeypatch)
    with pytest.raises(AbcStatementError, match="statement file is unavailable"):
        parse_abc_personal_pdf(
            (tmp_path / "absent.pdf").resolve(),
            expected_sha256="b" * 64,
            managed_account_suffix=_SUFFIX,
        )


def test_a_directory_is_not_a_statement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _prepare(tmp_path, monkeypatch)
    directory = tmp_path / "statement-dir"
    directory.mkdir()
    with pytest.raises(AbcStatementError, match="statement file must be regular"):
        parse_abc_personal_pdf(
            directory.resolve(), expected_sha256="b" * 64, managed_account_suffix=_SUFFIX
        )


def test_an_empty_source_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _refuse(tmp_path, monkeypatch, "statement file size is invalid", source_bytes=b"")


@_POSIX_ONLY
def test_a_source_reached_through_a_symlink_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    link = tmp_path / "link.pdf"
    link.symlink_to(source)
    with pytest.raises(AbcStatementError, match="statement file must be regular"):
        parse_abc_personal_pdf(link, expected_sha256=digest, managed_account_suffix=_SUFFIX)


def test_a_source_that_cannot_be_opened_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(tmp_path, monkeypatch)
    real_open = os.open

    def _refusing_open(path: object, flags: int, *args: object) -> int:
        if str(path).endswith("synthetic-abc.pdf"):
            raise OSError("permission denied")
        return real_open(path, flags, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _refusing_open)
    with pytest.raises(AbcStatementError, match="statement file cannot be opened"):
        parse_abc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


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
    with pytest.raises(AbcStatementError, match="statement file cannot be read"):
        parse_abc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


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
    with pytest.raises(AbcStatementError, match="statement file changed while reading"):
        parse_abc_personal_pdf(source, expected_sha256=digest, managed_account_suffix=_SUFFIX)


# --- opening the document ---------------------------------------------------


def test_a_document_pypdf_cannot_open_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement PDF is invalid",
        reader_error=RuntimeError("the xref table is damaged"),
    )


def test_a_document_whose_page_tree_cannot_be_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path, monkeypatch, "statement pages cannot be read", reader=_UnreadablePagesReader()
    )


def test_a_document_with_no_pages_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(tmp_path, monkeypatch, "statement page count is invalid", reader=_StubReader([]))


def test_a_reader_without_layout_extraction_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "PDF layout extraction is unavailable",
        reader=_StubReader([_StubPage("", TypeError("unexpected keyword extraction_mode"))]),
    )


def test_a_page_whose_text_cannot_be_extracted_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement page text cannot be extracted",
        reader=_StubReader([_StubPage("", RuntimeError("font program is broken"))]),
    )


@pytest.mark.parametrize("text", ["", "   ", "第一页\x00", 42])
def test_a_page_whose_text_is_not_storable_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: object
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement page text is invalid",
        reader=_StubReader([_StubPage(text)]),
    )


# --- page structure ---------------------------------------------------------


def test_a_document_that_does_not_name_the_bank_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "institution and export type are not proven",
        reader=_reader_for(_page(title="某银行交易清单")),
    )


def test_a_page_whose_period_runs_backwards_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement period is invalid",
        reader=_reader_for(_page(start="20260603", end="20260501")),
    )


def test_a_statement_in_another_currency_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement currency is not CNY",
        reader=_reader_for(_page(currency="美元")),
    )


@pytest.mark.parametrize("header_count", [0, 2])
def test_a_column_header_that_is_missing_or_doubled_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header_count: int
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "header is missing or ambiguous",
        reader=_reader_for(_page(header_count=header_count)),
    )


def test_a_page_marker_above_the_header_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The marker closes the transaction block, so a marker printed before the
    # header would make the block empty in a way the row loop cannot see.
    text = _page()
    lines = text.splitlines()
    marker = lines.pop()
    _refuse(
        tmp_path,
        monkeypatch,
        "statement page structure is invalid",
        reader=_reader_for("\n".join([lines[0], marker, *lines[1:]])),
    )


def test_a_page_with_no_transactions_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement page contains no transactions",
        reader=_reader_for(_page(body=("核对说明",), footer=None)),
    )


def test_a_statement_for_another_account_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "does not belong to the managed account",
        reader=_reader_for(_page(account="622200000000111122")),
    )


def test_pages_that_disagree_about_the_account_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement page metadata is inconsistent",
        reader=_reader_for(
            _page(marker="第1页\uff0c共2页"),
            _page(marker="第2页\uff0c共2页", owner="另一位用户"),
        ),
    )


# --- transaction rows -------------------------------------------------------


def test_a_transaction_below_the_footer_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Anything after the footer belongs to another block; folding it back into
    # the transaction list would import rows the totals never covered.
    _refuse(
        tmp_path,
        monkeypatch,
        "transaction appears after footer",
        reader=_reader_for(_page(body=(_row(), "核对说明", _SECOND_ROW), footer=None)),
    )


def test_a_row_whose_log_number_is_not_ten_characters_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "transaction log number is invalid",
        reader=_reader_for(_page(body=(_row(log_number="A0001"), _SECOND_ROW))),
    )


def test_rows_that_run_backwards_in_time_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "transactions are not date ordered",
        reader=_reader_for(
            _page(
                body=(
                    _row(date_text="20260603", balance="120.00"),
                    _row(
                        date_text="20260501",
                        amount="-12.34",
                        balance="107.66",
                        log_number="A000000002",
                    ),
                )
            )
        ),
    )


def test_two_identical_facts_on_one_statement_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The two rows differ only in their log number, which the fact digest does
    # not cover, so the statement carries no way to tell them apart.
    zero = _row(amount="0.00", balance="100.00")
    _refuse(
        tmp_path,
        monkeypatch,
        "without a unique fact identity",
        reader=_reader_for(_page(body=(zero, zero))),
    )


def test_a_balance_chain_that_does_not_reconcile_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "balance chain does not reconcile",
        reader=_reader_for(_page(body=(_row(), _row(**{"log_number": "A000000002"})))),
    )


def test_a_row_with_no_describable_content_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _refuse(
        tmp_path,
        monkeypatch,
        "statement text is invalid",
        reader=_reader_for(_page(body=(_row(summary=""), _SECOND_ROW))),
    )


# --- the helpers, stated directly -------------------------------------------


def test_a_counterparty_cell_with_two_account_numbers_is_ambiguous() -> None:
    with pytest.raises(AbcStatementError, match="counterparty account is ambiguous"):
        _split_counterparty("甲 6222000000001234 乙 6222000000005678")


@pytest.mark.parametrize("value", ["", "-"])
def test_an_absent_counterparty_yields_two_empty_fields(value: str) -> None:
    assert _split_counterparty(value) == ("", "")


def test_a_counterparty_cell_without_an_account_is_all_name() -> None:
    assert _split_counterparty("合成商户") == ("合成商户", "")


def test_a_counterparty_cell_splits_into_name_and_account() -> None:
    assert _split_counterparty("甲 6222000000001234") == ("甲", "6222000000001234")


def test_a_date_that_does_not_exist_is_refused() -> None:
    with pytest.raises(AbcStatementError, match="statement date is invalid"):
        _parse_date("20261301")


def test_a_missing_transaction_time_reads_as_midnight() -> None:
    assert _parse_time("").hour == 0


@pytest.mark.parametrize("value", ["9000", "990000"])
def test_a_transaction_time_that_is_not_a_clock_reading_is_refused(value: str) -> None:
    with pytest.raises(AbcStatementError, match="transaction time is invalid"):
        _parse_time(value)


@pytest.mark.parametrize("value", ["12", "1.5", "abc", "1,23.45"])
def test_only_two_decimal_places_of_renminbi_are_money(value: str) -> None:
    with pytest.raises(AbcStatementError, match="transaction amount is invalid"):
        _money_minor(value, field="amount")


def test_an_amount_beyond_the_safe_integer_range_is_refused() -> None:
    with pytest.raises(AbcStatementError, match="transaction balance is out of range"):
        _money_minor("99999999999999999.00", field="balance")


def test_a_thousands_separated_amount_is_read_as_minor_units() -> None:
    assert _money_minor("1,234.56", field="amount") == 123456


@pytest.mark.parametrize("value", ["", "   "])
def test_a_required_text_field_may_not_be_blank(value: str) -> None:
    with pytest.raises(AbcStatementError, match="statement text is invalid"):
        _bounded_text(value, required=True)


@pytest.mark.parametrize("value", ["x" * 301, "备注\x00"])
def test_an_unstorable_or_oversize_text_field_is_refused(value: str) -> None:
    with pytest.raises(AbcStatementError, match="statement text is invalid"):
        _bounded_text(value, required=False)


def test_a_header_whose_columns_are_out_of_order_is_not_a_header() -> None:
    with pytest.raises(AbcStatementError, match="header is missing or ambiguous"):
        _find_header(["".join(reversed(_HEADERS))])


def test_continuation_parts_are_joined_with_a_single_space() -> None:
    assert _join_parts("", "续") == "续"
    assert _join_parts("对手", "续") == "对手 续"


def test_a_repeated_metadata_line_is_ambiguous_rather_than_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _page()
    lines = text.splitlines()
    _refuse(
        tmp_path,
        monkeypatch,
        "statement owner is invalid",
        reader=_reader_for("\n".join([*lines[:2], lines[1], *lines[2:]])),
    )


def test_blank_layout_lines_between_rows_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _prepare(
        tmp_path, monkeypatch, reader=_reader_for(_page(body=(_row(), "", _SECOND_ROW)))
    )
    statement = parse_abc_personal_pdf(
        source, expected_sha256=digest, managed_account_suffix=_SUFFIX
    )
    assert len(statement.transactions) == 2
