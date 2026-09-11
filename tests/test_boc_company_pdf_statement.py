"""The BOC company statement mailed monthly.

What makes this document worth its own suite is exactly where it differs from
the online-banking export: the amount is split across debit and credit columns,
an over-long summary wraps onto a continuation row, and the document carries
three interlocking self-checks -- the balance chain started by the opening
balance, the period debit/credit totals, and the closing balance. Missing a row,
reading a continuation as a transaction, or reading a debit as a credit trips at
least one of them.

Every value below is synthetic.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ledgerbridge.bank_statement_contract import BankStatement, BankStatementParserProfile
from ledgerbridge.boc_company_pdf_statement import (
    BocCompanyPdfStatementError,
    _rows,
    _statement,
    parse_boc_company_pdf,
)

_ACCOUNT = "10000000009999"
_SUFFIX = "9999"

HEAD = f"""  账号 {_ACCOUNT}  账户名称 SyntheticCompanyLtd  开户行 Synthetic Branch
  起始日期20260401第  1  页/共  1  页
  币种 人民币(CNY)  账户类型 单位人民币活期基本账户存款  承前页余额 1,000.00
  截止日期 20260430 出账周期 月
 ---------------------------------------------
 |序号|记账日|起息日|交易类型|凭证|凭证号码/业务编号/用途/摘要|借方发生额|贷方发生额|余额|机构/柜员/流水|备注|
 |No.|Bk.D.|Val.D.|Type|Vou.|Vou. No./Trans. No./Details|Debit|Credit|Balance|Reference No.|Notes|
 ---------------------------------------------
"""

FOOT = """ ---------------------------------------------
   借方合计 100.00   贷方合计 5,000.00
   本页余额 5,900.00   本对账期末余额 5,900.00
  Debit Total   Credit Total   Current Page Balance   Balance At the End of the Period
"""


def line(*fields: str) -> str:
    """One printed row: eleven pipe-delimited cells, continuations included."""

    assert len(fields) == 11
    return " |" + "|".join(fields) + "|\n"


def transaction(
    sequence: str,
    booked: str,
    kind: str,
    details: str,
    debit: str,
    credit: str,
    balance: str,
    reference: str,
    note: str = "",
) -> str:
    return line(
        sequence, booked, booked, kind, "", details, debit, credit, balance, reference, note
    )


def wrapped(details: str) -> str:
    """A wrapped summary: every cell empty except the summary itself."""

    return line("", "", "", "", "", details, "", "", "", "", "")


ROWS = (
    transaction(
        "1",
        "260405",
        "短信收费",
        "SMSP Service Charge",
        "25.00",
        "",
        "975.00",
        "10000/1000000/10000001",
    )
    + transaction(
        "2",
        "260410",
        "实时缴税",
        "00000000/2026041000000000 Synthetic",
        "75.00",
        "",
        "900.00",
        "10000/1000000/10000002",
        "Synthetic Tax Office",
    )
    + wrapped(" Company Limited 00000000000000000")
    + transaction(
        "3",
        "260419",
        "转账收入",
        "OBSS000000000000GIRO000000000000",
        "",
        "5,000.00",
        "5,900.00",
        "10000/1000000/10000003",
        "Synthetic Payer",
    )
)


def page(rows: str = ROWS, foot: str = FOOT, head: str = HEAD) -> str:
    return head + rows + foot


def parse(text: str, suffix: str = _SUFFIX) -> BankStatement:
    return _statement(
        text,
        raw=b"%PDF-1.4 synthetic",
        source_sha256="0" * 64,
        managed_account_suffix=suffix,
    )


def test_a_statement_shaped_page_reads_as_three_transactions() -> None:
    statement = parse(page())
    assert statement.parser_profile is BankStatementParserProfile.BOC_COMPANY_PDF_V1
    assert statement.source_system == "boc_company_pdf_statement"
    assert statement.currency == "CNY"
    assert statement.account_suffix == _SUFFIX
    assert len(statement.transactions) == 3
    assert [item.amount_minor for item in statement.transactions] == [-2500, -7500, 500000]
    assert [item.balance_minor for item in statement.transactions] == [97500, 90000, 590000]
    assert statement.transactions[2].counterparty_name == "Synthetic Payer"
    assert statement.transactions[2].counterparty_account == ""
    assert statement.transactions[0].transaction_name == "短信收费 | SMSP Service Charge"


def test_a_wrapped_summary_joins_the_transaction_above_it() -> None:
    statement = parse(page())
    assert statement.transactions[1].transaction_name.endswith(
        "SyntheticCompany Limited 00000000000000000"
    )


def test_transaction_serials_are_unique_within_a_statement() -> None:
    statement = parse(page())
    serials = {item.transaction_serial for item in statement.transactions}
    assert len(serials) == len(statement.transactions)
    assert all(
        item.transaction_serial.startswith("boc-company-pdf:") for item in statement.transactions
    )


@pytest.mark.parametrize(
    ("broken", "reason"),
    [
        (
            page(
                rows=transaction(
                    "1",
                    "260405",
                    "短信收费",
                    "SMSP Service Charge",
                    "25.00",
                    "",
                    "999.00",
                    "10000/1/1",
                )
            ),
            "a balance that does not follow the opening balance",
        ),
        (
            page(
                rows=transaction(
                    "1",
                    "260405",
                    "短信收费",
                    "SMSP Service Charge",
                    "25.00",
                    "5,000.00",
                    "975.00",
                    "10000/1/1",
                )
            ),
            "an amount in both the debit and the credit column",
        ),
        (
            page(rows=ROWS.replace("|1|260405|", "|1|260301|", 1)),
            "a booking date outside the declared period",
        ),
        (
            page(rows=ROWS, foot=FOOT.replace("借方合计 100.00", "借方合计 90.00")),
            "totals that disagree",
        ),
        (
            page(
                rows=ROWS, foot=FOOT.replace("本对账期末余额 5,900.00", "本对账期末余额 5,901.00")
            ),
            "a closing balance the chain does not reach",
        ),
        (page(rows=ROWS.replace("|3|260419|", "|4|260419|", 1)), "a skipped transaction number"),
        (page(rows=""), "no transactions at all"),
        (page(head=HEAD.replace("承前页余额 1,000.00", "")), "no opening balance"),
        (page(foot=""), "no footer totals"),
        (page(head=HEAD.replace("币种 人民币(CNY)", "币种 USD")), "an unproven currency"),
    ],
)
def test_a_statement_that_cannot_prove_itself_is_refused(broken: str, reason: str) -> None:
    with pytest.raises(BocCompanyPdfStatementError):
        parse(broken)


def test_a_statement_for_another_account_is_refused() -> None:
    with pytest.raises(BocCompanyPdfStatementError):
        parse(page(), suffix="8888")


def test_a_table_that_starts_with_a_continuation_is_refused() -> None:
    with pytest.raises(BocCompanyPdfStatementError):
        _rows(HEAD + wrapped("orphan") + FOOT)


def test_a_continuation_carrying_its_own_facts_is_refused() -> None:
    carrier = line("", "260405", "", "", "", "more", "", "", "", "", "")
    with pytest.raises(BocCompanyPdfStatementError):
        _rows(HEAD + ROWS + carrier + FOOT)


def test_an_empty_last_column_is_a_column_not_a_missing_one() -> None:
    parsed = _rows(HEAD + ROWS + FOOT)
    assert [row.note for row in parsed] == [
        "",
        "Synthetic Tax Office",
        "Synthetic Payer",
    ]


def test_a_row_that_is_not_closed_by_a_pipe_is_refused() -> None:
    with pytest.raises(BocCompanyPdfStatementError):
        _rows(
            HEAD + " |1|260405|260405|短信收费||x|25.00||975.00|10000/1/1|\n".rstrip("|\n") + "\n"
        )


def test_the_file_boundary_refuses_a_source_that_is_not_the_declared_one(tmp_path: Path) -> None:
    source = (tmp_path / "statement.pdf").resolve()
    source.write_bytes(b"%PDF-1.4 synthetic")
    with pytest.raises(BocCompanyPdfStatementError):
        parse_boc_company_pdf(source, expected_sha256="0" * 64, managed_account_suffix=_SUFFIX)


def test_the_file_boundary_refuses_a_source_that_is_not_a_pdf(tmp_path: Path) -> None:
    source = (tmp_path / "statement.pdf").resolve()
    raw = b"not a pdf at all"
    source.write_bytes(raw)
    with pytest.raises(BocCompanyPdfStatementError):
        parse_boc_company_pdf(
            source,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            managed_account_suffix=_SUFFIX,
        )


def test_the_file_boundary_refuses_a_relative_path() -> None:
    with pytest.raises(BocCompanyPdfStatementError):
        parse_boc_company_pdf(
            Path("statement.pdf"), expected_sha256="0" * 64, managed_account_suffix=_SUFFIX
        )
