"""A counterparty account too wide for its column is repaired, not guessed.

ABC renders the personal statement as fixed-width columns.  When the printed
counterparty account is wider than the column the bank reserved for it, it runs
into the log-number column and takes the log number's leading characters with
it, so the log number arrives too long and the row is refused.  Every fixture
here is synthetic; the widths, not the values, are what is under test.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ledgerbridge.abc_statement import AbcStatementError, parse_abc_personal_pdf
from tests.test_abc_bank_statement import _ACCOUNT, _HEADERS, _SUFFIX, _WIDTHS, _line

_COUNTERPARTY_WIDTH = _WIDTHS[5]
_LOG_WIDTH = _WIDTHS[6]
#: What the bank writes when a row carries a merchant's reference instead of a
#: log number: two characters and fourteen digits, two wider than the column.
_MERCHANT_REFERENCE = "商户" + "3" * 14
_FITTING_ACCOUNT = "1" * _COUNTERPARTY_WIDTH
_OVERFLOWING_ACCOUNT = "2" * (_COUNTERPARTY_WIDTH + 1)


class _Page:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self, *, extraction_mode: str) -> str:
        assert extraction_mode == "layout"
        return self._text


class _Reader:
    def __init__(self, text: str) -> None:
        self.is_encrypted = False
        self.pages = [_Page(text)]


def _page_text(
    *,
    counterparty: str,
    log_number: str = "A000000001",
    channel: str = "网银",
) -> str:
    return "\n".join(
        (
            "中国农业银行账户活期交易明细清单",
            f"户名: 合成测试用户                                  账户: {_ACCOUNT}",
            "币种: 人民币                                        汇钞标识: 汇",
            "起止日期: 20260501 - 20260603                        电子流水号: 10000000000000000001",
            "",
            _line(_HEADERS),
            _line(
                (
                    "20260501",
                    "090000",
                    "消费",
                    "-12.34",
                    "100.00",
                    counterparty,
                    log_number,
                    channel,
                    "合成附言",
                )
            ),
            _line(
                (
                    "20260603",
                    "",
                    "转入",
                    "20.00",
                    "120.00",
                    "-",
                    "A000000002",
                    "掌银",
                    "",
                )
            ),
            "核对说明",
            "第1页\uff0c共1页",
        )
    )


def _parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page_text: str) -> object:
    source = (tmp_path / "synthetic-abc.pdf").resolve()
    source.write_bytes(b"%PDF-1.7\nsynthetic-abc")
    monkeypatch.setattr("ledgerbridge.abc_statement.PdfReader", lambda _: _Reader(page_text))
    return parse_abc_personal_pdf(
        source,
        expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        managed_account_suffix=_SUFFIX,
    )


def test_an_account_that_overflows_its_column_keeps_the_log_number_whole(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement = _parse(tmp_path, monkeypatch, _page_text(counterparty=_OVERFLOWING_ACCOUNT))

    first = statement.transactions[0]
    assert first.counterparty_account == _OVERFLOWING_ACCOUNT
    assert first.counterparty_name == ""


def test_an_account_that_fits_its_column_is_read_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statement = _parse(tmp_path, monkeypatch, _page_text(counterparty=_FITTING_ACCOUNT))

    assert statement.transactions[0].counterparty_account == _FITTING_ACCOUNT


def test_an_overflowing_account_leaves_the_rest_of_the_row_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overflowed = _parse(tmp_path, monkeypatch, _page_text(counterparty=_OVERFLOWING_ACCOUNT))
    fitting = _parse(tmp_path, monkeypatch, _page_text(counterparty=_FITTING_ACCOUNT))

    assert [item.transaction_name for item in overflowed.transactions] == [
        item.transaction_name for item in fitting.transactions
    ]
    assert [item.amount_minor for item in overflowed.transactions] == [
        item.amount_minor for item in fitting.transactions
    ]


def test_a_log_column_that_is_too_long_for_an_overflow_is_still_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(AbcStatementError, match="log number is invalid"):
        _parse(
            tmp_path,
            monkeypatch,
            _page_text(counterparty=_FITTING_ACCOUNT, log_number="A0000000011"),
        )


def test_a_name_running_into_the_log_column_is_refused_rather_than_repaired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(AbcStatementError, match="log number is invalid"):
        _parse(
            tmp_path,
            monkeypatch,
            _page_text(counterparty="合成商户" + "名" * (_COUNTERPARTY_WIDTH - 3)),
        )


def test_a_merchant_reference_overflowing_its_column_is_made_whole(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ABC sometimes prints a merchant's own reference where the log number goes.

    It is two characters wider than the column, so its last digits are printed
    where the channel belongs. It is the only identifier that row carries, and
    it becomes the row's identity, so it is taken back whole rather than
    truncated to something that merely looks like a log number.
    """

    statement = _parse(
        tmp_path,
        monkeypatch,
        _page_text(
            counterparty=_FITTING_ACCOUNT,
            log_number=_MERCHANT_REFERENCE,
            channel="",
        ),
    )

    other = _parse(
        tmp_path,
        monkeypatch,
        _page_text(
            counterparty=_FITTING_ACCOUNT,
            log_number="商户" + "4" * 14,
            channel="",
        ),
    )

    assert len(_MERCHANT_REFERENCE) > _LOG_WIDTH
    assert len(statement.transactions) == 2
    # The reference is what identifies the row, so a different one is a
    # different fact - the digits taken back from the channel column are not
    # decoration.
    assert statement.transactions[0].transaction_serial != other.transactions[0].transaction_serial


def test_a_channel_carrying_real_text_is_not_eaten_by_the_log_column(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair may only take digits that complete a reference, never a channel."""

    with pytest.raises(AbcStatementError, match="log number is invalid"):
        _parse(
            tmp_path,
            monkeypatch,
            _page_text(
                counterparty=_FITTING_ACCOUNT,
                log_number="商户" + "3" * 13,
                channel="网银",
            ),
        )


def test_a_reference_of_another_shape_is_still_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the form the bank was seen to write is admitted, not anything non-numeric."""

    with pytest.raises(AbcStatementError, match="log number is invalid"):
        _parse(
            tmp_path,
            monkeypatch,
            _page_text(
                counterparty=_FITTING_ACCOUNT,
                log_number="收款" + "3" * 14,
                channel="",
            ),
        )
