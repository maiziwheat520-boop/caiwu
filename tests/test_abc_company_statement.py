from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ledgerbridge.abc_company_statement import AbcCompanyStatementError, parse_abc_company_xls
from ledgerbridge.bank_statement_contract import BankStatementParserProfile

_OLE = bytes.fromhex("D0CF11E0A1B11AE1") + b"synthetic-abc-company-xls"


class _Sheet:
    ncols = 8

    def __init__(self, rows: list[list[object]]) -> None:
        self.rows = rows
        self.nrows = len(rows)

    def row_values(self, index: int) -> list[object]:
        return self.rows[index]


class _Book:
    nsheets = 1

    def __init__(self, rows: list[list[object]]) -> None:
        self.sheet = _Sheet(rows)
        self.released = False

    def sheet_by_index(self, index: int) -> _Sheet:
        assert index == 0
        return self.sheet

    def release_resources(self) -> None:
        self.released = True


def _rows() -> list[list[object]]:
    return [
        ["账户明细", "", "", "", "", "", "", ""],
        [
            "账号:41-029800040039018",
            "户名:合成酒店有限公司",
            "币种:人民币",
            "",
            "",
            "起止日期: 2025年09月05日 - 2026年09月04日 ",
            "",
            "",
        ],
        [
            "交易时间",
            "收入金额",
            "支出金额",
            "账户余额",
            "对方账号",
            "对方户名",
            "对方开户行",
            "摘要",
        ],
        ["2026-09-04 12:00:00", 20.0, "", 120.0, "123", "平台资金户", "某银行", "转存"],
        ["2026-09-03 12:00:00", "", 5.0, 100.0, "456", "供应商", "某银行", "转取"],
        ["总收入笔数", "总收入金额", "总支出笔数", "总支出金额", "", "", "", ""],
        ["1", 20.0, "1", 5.0, "", "", "", ""],
    ]


def _parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: list[list[object]]):
    path = (tmp_path / "statement.xls").resolve()
    path.write_bytes(_OLE)
    book = _Book(rows)
    monkeypatch.setattr("ledgerbridge.abc_company_statement.xlrd.open_workbook", lambda **_: book)
    result = parse_abc_company_xls(
        path, expected_sha256=hashlib.sha256(_OLE).hexdigest(), managed_account_suffix="9018"
    )
    assert book.released
    return result


def test_parses_company_xls_with_footer_and_balance_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement = _parse(tmp_path, monkeypatch, _rows())
    assert statement.parser_profile is BankStatementParserProfile.ABC_COMPANY_XLS_V1
    assert [item.amount_minor for item in statement.transactions] == [2000, -500]
    assert statement.account_suffix == "9018"


@pytest.mark.parametrize("mutation", ["footer", "balance", "direction"])
def test_rejects_unreconciled_company_xls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    rows = _rows()
    if mutation == "footer":
        rows[-1][1] = 21.0
    if mutation == "balance":
        rows[3][3] = 121.0
    if mutation == "direction":
        rows[3][2] = 1.0
    with pytest.raises(AbcCompanyStatementError):
        _parse(tmp_path, monkeypatch, rows)


def test_real_supplied_statements_match_reviewed_counts() -> None:
    source_dir = Path(r"E:\wendang\xwechat_files\cmz19960502_79d0\msg\file\2026-09")
    sources = [
        (
            source_dir / "账户明细查询列表 景怡.xls",
            "9018",
            55,
        ),
        (
            source_dir / "账户明细查询列表 (1).xls",
            "3234",
            228,
        ),
    ]
    if not all(path.exists() for path, _, _ in sources):
        pytest.skip("private statements unavailable")
    for path, suffix, count in sources:
        raw = path.read_bytes()
        assert (
            len(
                parse_abc_company_xls(
                    path,
                    expected_sha256=hashlib.sha256(raw).hexdigest(),
                    managed_account_suffix=suffix,
                ).transactions
            )
            == count
        )
