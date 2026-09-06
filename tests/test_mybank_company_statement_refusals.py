"""Every way an official MYbank company export can be refused.

The parser is the only thing standing between a bank file and the ledger, so
each refusal below is stated against a workbook that differs from a valid one in
exactly one way.  The multi-day range parser in particular had almost none of
these branches exercised.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from ledgerbridge.bank_statement_contract import BankStatement
from ledgerbridge.mybank_statement import (
    MyBankEmptyStatementError,
    MyBankStatementError,
    parse_mybank_company_daily_xlsx,
    parse_mybank_company_range_xlsx,
)
from tests.test_mybank_company_statement import (
    _FW_LEFT,
    _FW_RIGHT,
    _HEADERS,
    _SUFFIX,
    _rows,
    _write_xlsx,
)

_VALID_DIGEST = "a" * 64
_WIDE_HEADERS = tuple(value.replace("(", _FW_LEFT).replace(")", _FW_RIGHT) for value in _HEADERS)


def _range_rows() -> list[tuple[str, ...]]:
    """A two-day, two-transaction export the range parser accepts."""

    return [
        ("浙江网商银行企业账户交易明细",),
        ("企业名称", "合成测试公司", "", "", "企业账号", f"000000000000{_SUFFIX}(人民币)"),
        ("借方交易笔数", "1笔", "", "", "借方交易金额", "￥125.34"),
        ("贷方交易笔数", "1笔", "", "", "贷方交易金额", "￥20.00"),
        _WIDE_HEADERS,
        (
            "synthetic-0001",
            "2026-01-03 06:00:00",
            "2026-01-03 06:07:08",
            "转入",
            "125.34",
            "",
            "5125.34",
            "合成商户甲",
            "0000000000001111",
            "合成银行",
            "货款",
        ),
        (
            "synthetic-0002",
            "2026-01-02 03:00:00",
            "2026-01-02 03:04:05",
            "消费",
            "",
            "20.00",
            "5000.00",
            "合成商户乙",
            "0000000000002222",
            "合成银行",
            "",
        ),
    ]


def _parse_range(path: Path, rows: list[tuple[str, ...]]) -> BankStatement:
    raw = _write_xlsx(path, rows)
    return parse_mybank_company_range_xlsx(
        path,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        managed_account_suffix=_SUFFIX,
    )


def _refuse(path: Path, rows: list[tuple[str, ...]], expected: str) -> None:
    with pytest.raises(MyBankStatementError, match=expected):
        _parse_range(path, rows)


def _replaced(index: int, values: tuple[str, ...]) -> list[tuple[str, ...]]:
    rows = _range_rows()
    rows[index] = values
    return rows


def test_the_range_parser_accepts_a_multi_day_export(tmp_path: Path) -> None:
    statement = _parse_range((tmp_path / "range.xlsx").resolve(), _range_rows())
    assert len(statement.transactions) == 2
    assert {item.occurred_at.date().isoformat() for item in statement.transactions} == {
        "2026-01-02",
        "2026-01-03",
    }


def test_the_daily_parser_refuses_a_second_day_the_range_parser_allows(
    tmp_path: Path,
) -> None:
    # The two parsers differ in exactly this: a daily export must not straddle
    # midnight, and the range export exists because real statements do.
    rows = _rows()
    rows[5] = (rows[5][0], "2026-01-03 06:00:00", "2026-01-03 06:07:08", *rows[5][3:])
    path = (tmp_path / "daily.xlsx").resolve()
    raw = _write_xlsx(path, rows)
    with pytest.raises(MyBankStatementError, match="more than one day"):
        parse_mybank_company_daily_xlsx(
            path,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            managed_account_suffix=_SUFFIX,
        )


# --- arguments the caller controls ----------------------------------------


@pytest.mark.parametrize("digest", ["", "A" * 64, "a" * 63, "a" * 65, "g" * 64])
def test_a_digest_that_is_not_lowercase_hex_is_refused(tmp_path: Path, digest: str) -> None:
    path = (tmp_path / "range.xlsx").resolve()
    _write_xlsx(path, _range_rows())
    with pytest.raises(MyBankStatementError, match="expected source digest is invalid"):
        parse_mybank_company_range_xlsx(
            path, expected_sha256=digest, managed_account_suffix=_SUFFIX
        )


@pytest.mark.parametrize("suffix", ["", "796", "123456789", "79a8"])
def test_a_managed_account_suffix_outside_four_to_eight_digits_is_refused(
    tmp_path: Path,
    suffix: str,
) -> None:
    path = (tmp_path / "range.xlsx").resolve()
    _write_xlsx(path, _range_rows())
    with pytest.raises(MyBankStatementError, match="managed account suffix is invalid"):
        parse_mybank_company_range_xlsx(
            path, expected_sha256=_VALID_DIGEST, managed_account_suffix=suffix
        )


def test_a_file_that_does_not_match_the_expected_digest_is_refused(tmp_path: Path) -> None:
    path = (tmp_path / "range.xlsx").resolve()
    _write_xlsx(path, _range_rows())
    with pytest.raises(MyBankStatementError, match="source digest changed"):
        parse_mybank_company_range_xlsx(
            path, expected_sha256=_VALID_DIGEST, managed_account_suffix=_SUFFIX
        )


# --- the metadata block ----------------------------------------------------


def test_a_workbook_without_the_five_metadata_rows_is_refused(tmp_path: Path) -> None:
    _refuse(
        (tmp_path / "range.xlsx").resolve(),
        _range_rows()[:4],
        "metadata rows are invalid",
    )


def test_a_workbook_with_another_title_is_refused(tmp_path: Path) -> None:
    _refuse(
        (tmp_path / "range.xlsx").resolve(), _replaced(0, ("别家银行明细",)), "title is invalid"
    )


@pytest.mark.parametrize(
    "identity",
    [
        ("公司名称", "合成测试公司", "", "", "企业账号", f"000000000000{_SUFFIX}(人民币)"),
        ("企业名称", "", "", "", "企业账号", f"000000000000{_SUFFIX}(人民币)"),
        ("企业名称", "合成测试公司", "", "", "账号", f"000000000000{_SUFFIX}(人民币)"),
        ("企业名称", "合成测试公司", "多余", "", "企业账号", f"000000000000{_SUFFIX}(人民币)"),
    ],
)
def test_an_identity_row_that_is_not_the_official_shape_is_refused(
    tmp_path: Path,
    identity: tuple[str, ...],
) -> None:
    _refuse((tmp_path / "range.xlsx").resolve(), _replaced(1, identity), "identity is invalid")


@pytest.mark.parametrize(
    "account",
    ["000000000000000(人民币)", "0000000000007968(美元)", "0000000000007968"],
)
def test_a_statement_for_another_account_or_currency_is_refused(
    tmp_path: Path,
    account: str,
) -> None:
    rows = _replaced(1, ("企业名称", "合成测试公司", "", "", "企业账号", account))
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "does not belong to the managed account")


@pytest.mark.parametrize("index", [2, 3])
@pytest.mark.parametrize(
    "summary",
    [
        ("借方笔数", "1笔", "", "", "借方交易金额", "￥125.34"),
        ("借方交易笔数", "一笔", "", "", "借方交易金额", "￥125.34"),
        ("借方交易笔数", "1笔", "", "", "借方交易金额", "125.34"),
        ("借方交易笔数", "1笔", "多余", "", "借方交易金额", "￥125.34"),
    ],
)
def test_a_summary_row_that_is_not_the_official_shape_is_refused(
    tmp_path: Path,
    index: int,
    summary: tuple[str, ...],
) -> None:
    _refuse((tmp_path / "range.xlsx").resolve(), _replaced(index, summary), "summary is invalid")


def test_a_metadata_row_wider_than_the_official_sheet_is_refused(tmp_path: Path) -> None:
    _refuse(
        (tmp_path / "range.xlsx").resolve(),
        _replaced(0, ("浙江网商银行企业账户交易明细", *("x" for _ in range(11)))),
        "contains unexpected columns",
    )


def test_an_unknown_header_row_is_refused(tmp_path: Path) -> None:
    _refuse(
        (tmp_path / "range.xlsx").resolve(),
        _replaced(4, ("流水号", "交易时间", "金额")),
        "range statement header is invalid",
    )


# --- the transaction rows --------------------------------------------------


def test_a_row_wider_than_the_declared_header_is_refused(tmp_path: Path) -> None:
    rows = _range_rows()
    rows[5] = (*rows[5], "多余")
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "unexpected columns")


def test_a_duplicated_transaction_serial_is_refused(tmp_path: Path) -> None:
    rows = _range_rows()
    rows[6] = ("synthetic-0001", *rows[6][1:])
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "serial is duplicated")


def test_a_submitted_time_on_another_day_than_the_transaction_is_refused(
    tmp_path: Path,
) -> None:
    rows = _range_rows()
    rows[5] = (rows[5][0], "2026-01-02 06:00:00", *rows[5][2:])
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "transaction dates conflict")


@pytest.mark.parametrize(("debit", "credit"), [("125.34", "20.00"), ("", "")])
def test_a_row_that_is_neither_purely_debit_nor_purely_credit_is_refused(
    tmp_path: Path,
    debit: str,
    credit: str,
) -> None:
    rows = _range_rows()
    rows[5] = (*rows[5][:4], debit, credit, *rows[5][6:])
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "direction is invalid")


@pytest.mark.parametrize("debit", ["0.00", "125,34", "-125.34", "125.345"])
def test_a_zero_or_malformed_amount_is_refused(tmp_path: Path, debit: str) -> None:
    rows = _range_rows()
    rows[5] = (*rows[5][:4], debit, "", *rows[5][6:])
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "amount is invalid")


def test_a_missing_balance_is_refused(tmp_path: Path) -> None:
    rows = _range_rows()
    rows[5] = (*rows[5][:6], "", *rows[5][7:])
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "balance is invalid")


def test_a_transaction_name_and_note_longer_than_the_column_is_refused(
    tmp_path: Path,
) -> None:
    rows = _range_rows()
    rows[5] = (*rows[5][:3], "转入", *rows[5][4:10], "货" * 300)
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "transaction name is too long")


def test_a_missing_transaction_name_is_refused(tmp_path: Path) -> None:
    rows = _range_rows()
    rows[5] = (*rows[5][:3], "", *rows[5][4:])
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "transaction name is invalid")


def test_an_unparsable_transaction_time_is_refused(tmp_path: Path) -> None:
    rows = _range_rows()
    rows[5] = (rows[5][0], rows[5][1], "2026/01/03 06:07:08", *rows[5][3:])
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "transaction time is invalid")


# --- the arithmetic the statement asserts about itself ---------------------


def test_transactions_running_forwards_in_time_are_refused(tmp_path: Path) -> None:
    rows = _range_rows()
    rows[5], rows[6] = rows[6], rows[5]
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "transaction order is invalid")


def test_a_balance_that_does_not_follow_from_the_amounts_is_refused(
    tmp_path: Path,
) -> None:
    rows = _range_rows()
    rows[5] = (*rows[5][:6], "9999.99", *rows[5][7:])
    _refuse((tmp_path / "range.xlsx").resolve(), rows, "balance chain is invalid")


@pytest.mark.parametrize(
    ("index", "summary", "expected"),
    [
        (
            2,
            ("借方交易笔数", "2笔", "", "", "借方交易金额", "￥125.34"),
            "debit summary is invalid",
        ),
        (
            2,
            ("借方交易笔数", "1笔", "", "", "借方交易金额", "￥125.35"),
            "debit summary is invalid",
        ),
        (
            3,
            ("贷方交易笔数", "2笔", "", "", "贷方交易金额", "￥20.00"),
            "credit summary is invalid",
        ),
        (
            3,
            ("贷方交易笔数", "1笔", "", "", "贷方交易金额", "￥20.01"),
            "credit summary is invalid",
        ),
    ],
)
def test_a_summary_that_disagrees_with_the_rows_is_refused(
    tmp_path: Path,
    index: int,
    summary: tuple[str, ...],
    expected: str,
) -> None:
    _refuse((tmp_path / "range.xlsx").resolve(), _replaced(index, summary), expected)


def test_a_range_export_with_no_transactions_is_reported_as_empty(tmp_path: Path) -> None:
    rows = _range_rows()[:5]
    rows[2] = ("借方交易笔数", "0笔", "", "", "借方交易金额", "￥0")
    rows[3] = ("贷方交易笔数", "0笔", "", "", "贷方交易金额", "￥0")
    with pytest.raises(MyBankEmptyStatementError):
        _parse_range((tmp_path / "range.xlsx").resolve(), rows)


# --- the container the workbook arrives in ---------------------------------


def test_a_relative_statement_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MyBankStatementError, match="must be absolute"):
        parse_mybank_company_range_xlsx(
            Path("range.xlsx"),
            expected_sha256=_VALID_DIGEST,
            managed_account_suffix=_SUFFIX,
        )


def test_a_missing_statement_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(MyBankStatementError, match="file is unavailable"):
        parse_mybank_company_range_xlsx(
            (tmp_path / "absent.xlsx").resolve(),
            expected_sha256=_VALID_DIGEST,
            managed_account_suffix=_SUFFIX,
        )


def test_a_directory_is_not_a_statement(tmp_path: Path) -> None:
    with pytest.raises(MyBankStatementError, match="file must be regular"):
        parse_mybank_company_range_xlsx(
            tmp_path.resolve(),
            expected_sha256=_VALID_DIGEST,
            managed_account_suffix=_SUFFIX,
        )


def test_an_empty_statement_file_is_refused(tmp_path: Path) -> None:
    path = (tmp_path / "empty.xlsx").resolve()
    path.write_bytes(b"")
    with pytest.raises(MyBankStatementError, match="file size is invalid"):
        parse_mybank_company_range_xlsx(
            path, expected_sha256=_VALID_DIGEST, managed_account_suffix=_SUFFIX
        )


def test_a_file_that_is_not_a_zip_container_is_refused(tmp_path: Path) -> None:
    path = (tmp_path / "plain.xlsx").resolve()
    path.write_bytes(b"not a workbook at all")
    with pytest.raises(MyBankStatementError, match="not a valid XLSX container"):
        parse_mybank_company_range_xlsx(
            path,
            expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            managed_account_suffix=_SUFFIX,
        )


def test_a_container_holding_a_formula_is_refused(tmp_path: Path) -> None:
    path = (tmp_path / "formula.xlsx").resolve()
    _write_xlsx(path, _range_rows())
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
        b"<sheetData>", b'<sheetData><row r="99"><c r="A99"><f>SUM(A1:A2)</f></c></row>'
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    with pytest.raises(MyBankStatementError, match="formulas are not accepted"):
        parse_mybank_company_range_xlsx(
            path,
            expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            managed_account_suffix=_SUFFIX,
        )


def test_a_container_with_a_duplicated_entry_is_refused(tmp_path: Path) -> None:
    path = (tmp_path / "duplicate.xlsx").resolve()
    _write_xlsx(path, _range_rows())
    with zipfile.ZipFile(path) as archive:
        parts = [(name, archive.read(name)) for name in archive.namelist()]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in parts:
            archive.writestr(name, value)
        archive.writestr("xl/workbook.xml", parts[0][1])
    with pytest.raises(MyBankStatementError, match="duplicate entries"):
        parse_mybank_company_range_xlsx(
            path,
            expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            managed_account_suffix=_SUFFIX,
        )


# --- the daily parser carries the same refusals ---------------------------


def _parse_daily(path: Path, rows: list[tuple[str, ...]]) -> BankStatement:
    raw = _write_xlsx(path, rows)
    return parse_mybank_company_daily_xlsx(
        path,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        managed_account_suffix=_SUFFIX,
    )


def _refuse_daily(path: Path, rows: list[tuple[str, ...]], expected: str) -> None:
    with pytest.raises(MyBankStatementError, match=expected):
        _parse_daily(path, rows)


def _daily_replaced(index: int, values: tuple[str, ...]) -> list[tuple[str, ...]]:
    rows = _rows()
    rows[index] = values
    return rows


@pytest.mark.parametrize(
    ("rows_factory", "expected"),
    [
        (lambda: _rows()[:4], "metadata rows are invalid"),
        (lambda: _daily_replaced(0, ("别家银行明细",)), "title is invalid"),
        (
            lambda: _daily_replaced(
                1,
                ("公司名称", "合成测试公司", "", "", "企业账号", f"000000000000{_SUFFIX}(人民币)"),
            ),
            "identity is invalid",
        ),
        (
            lambda: _daily_replaced(
                1, ("企业名称", "合成测试公司", "", "", "企业账号", "0000000000000001(人民币)")
            ),
            "does not belong to the managed account",
        ),
        (lambda: _daily_replaced(4, ("流水号", "交易时间")), "statement header is invalid"),
    ],
)
def test_the_daily_parser_refuses_the_same_metadata_faults(
    tmp_path: Path,
    rows_factory: object,
    expected: str,
) -> None:
    _refuse_daily((tmp_path / "daily.xlsx").resolve(), rows_factory(), expected)  # type: ignore[operator]


def test_the_daily_parser_refuses_a_duplicated_serial(tmp_path: Path) -> None:
    rows = _rows()
    rows[6] = ("synthetic-0001", *rows[6][1:])
    _refuse_daily((tmp_path / "daily.xlsx").resolve(), rows, "serial is duplicated")


def test_the_daily_parser_refuses_a_malformed_amount(tmp_path: Path) -> None:
    rows = _rows()
    rows[5] = (*rows[5][:4], "125,34", "", *rows[5][6:])
    _refuse_daily((tmp_path / "daily.xlsx").resolve(), rows, "amount is invalid")


def test_the_daily_parser_refuses_an_overlong_transaction_name(tmp_path: Path) -> None:
    rows = _rows()
    rows[5] = (*rows[5][:3], "转入", *rows[5][4:10], "货" * 300)
    _refuse_daily((tmp_path / "daily.xlsx").resolve(), rows, "transaction name is too long")


@pytest.mark.parametrize("digest", ["", "A" * 64])
def test_the_daily_parser_refuses_a_digest_that_is_not_lowercase_hex(
    tmp_path: Path,
    digest: str,
) -> None:
    path = (tmp_path / "daily.xlsx").resolve()
    _write_xlsx(path, _rows())
    with pytest.raises(MyBankStatementError, match="expected source digest is invalid"):
        parse_mybank_company_daily_xlsx(
            path, expected_sha256=digest, managed_account_suffix=_SUFFIX
        )


def test_the_daily_parser_refuses_an_invalid_suffix(tmp_path: Path) -> None:
    path = (tmp_path / "daily.xlsx").resolve()
    _write_xlsx(path, _rows())
    with pytest.raises(MyBankStatementError, match="managed account suffix is invalid"):
        parse_mybank_company_daily_xlsx(
            path, expected_sha256=_VALID_DIGEST, managed_account_suffix="1"
        )


def test_the_daily_parser_refuses_a_file_that_changed_under_it(tmp_path: Path) -> None:
    path = (tmp_path / "daily.xlsx").resolve()
    _write_xlsx(path, _rows())
    with pytest.raises(MyBankStatementError, match="source digest changed"):
        parse_mybank_company_daily_xlsx(
            path, expected_sha256=_VALID_DIGEST, managed_account_suffix=_SUFFIX
        )
