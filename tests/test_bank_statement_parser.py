from __future__ import annotations

import re
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]

from ledgerbridge.bank_statement_parser import (
    MYBANK_TITLE,
    StatementParseError,
    StatementTransaction,
    _validate_boc_transactions,
    parse_mybank_xlsx,
)

ACCOUNT = "8888888701250688"
COMPANY = "测试企业有限公司"
FILENAME = f"{ACCOUNT}-日账单-20260825-交易明细-解压密码是企业统一社会信用代码后六位.xlsx"
MYBANK_HEADERS = (
    "账务流水号",
    "提交时间",
    "交易时间",
    "交易名称",
    "借方金额(收)",
    "贷方金额(支)",
    "余额",
    "对方户名",
    "对方账号",
    "对方机构",
    "备注",
)


def _mybank_xlsx(*, company: str = COMPANY, account: str = ACCOUNT) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "sheet1"
    rows = (
        (MYBANK_TITLE,),
        ("企业名称", company, None, None, "企业账号", f"{account}(人民币)"),
        ("借方交易笔数", "1笔", None, None, "借方交易金额", "￥188.01"),
        ("贷方交易笔数", "0笔", None, None, "贷方交易金额", "￥0"),
        MYBANK_HEADERS,
        (
            "TX-0001",
            "2026-08-25 12:00:01",
            "2026-08-25 12:00:00",
            "收款",
            "188.01",
            None,
            "1000.00",
            "对方企业",
            "6222000000000000",
            "测试银行",
            None,
        ),
    )
    for row in rows:
        worksheet.append(row)
    source = BytesIO()
    workbook.save(source)
    workbook.close()

    forged = BytesIO()
    replacement = b'<dimension ref="A1:A1"/>'
    replaced = False
    with ZipFile(source, "r") as incoming, ZipFile(forged, "w", ZIP_DEFLATED) as outgoing:
        for member in incoming.infolist():
            content = incoming.read(member.filename)
            if member.filename == "xl/worksheets/sheet1.xml":
                content, substitutions = re.subn(
                    rb'<dimension ref="[^"]+"\s*/>',
                    replacement,
                    content,
                    count=1,
                )
                replaced = substitutions == 1
            outgoing.writestr(member, content)
    assert replaced
    return forged.getvalue()


def _transaction(*, booked_at: str = "2026-08-25T12:00:00") -> StatementTransaction:
    return StatementTransaction(
        sequence=1,
        bank_transaction_id="",
        submitted_at="",
        booked_at=booked_at,
        amount_minor=100,
        balance_minor=1000,
        currency="CNY",
        transaction_name="测试交易",
        channel="",
        branch_name="",
        note="",
        counterparty_name="测试对手",
        counterparty_account="",
        counterparty_bank="",
        source_page=1,
    )


def test_mybank_ignores_forged_dimension_and_parses_real_transaction() -> None:
    statement = parse_mybank_xlsx(
        _mybank_xlsx(),
        source_filename=FILENAME,
        company_name=COMPANY,
    )

    assert statement.bank_code == "MYBANK"
    assert statement.parser_version == "mybank-xlsx-v2"
    assert statement.debit_total_minor == 18801
    assert statement.credit_total_minor == 0
    assert len(statement.transactions) == 1
    assert statement.transactions[0].bank_transaction_id == "TX-0001"


@pytest.mark.parametrize(
    ("company", "account"),
    [
        ("其他企业有限公司", ACCOUNT),
        (COMPANY, "9999999999999999"),
    ],
)
def test_mybank_rejects_internal_identity_mismatch(company: str, account: str) -> None:
    with pytest.raises(StatementParseError, match=r"^MYBANK_HEADER_IDENTITY_MISMATCH$"):
        parse_mybank_xlsx(
            _mybank_xlsx(company=company, account=account),
            source_filename=FILENAME,
            company_name=COMPANY,
        )


@pytest.mark.parametrize(
    ("transactions", "error_code"),
    [
        (
            (_transaction(booked_at="2026-08-24T23:59:59"),),
            "BOC_TRANSACTION_OUTSIDE_STATEMENT_PERIOD",
        ),
        (
            (
                _transaction(booked_at="2026-08-25T11:00:00"),
                _transaction(booked_at="2026-08-25T12:00:00"),
            ),
            "BOC_TRANSACTION_ORDER_INVALID",
        ),
        ((_transaction(), _transaction()), "BOC_DUPLICATE_TRANSACTION"),
    ],
)
def test_boc_transaction_sequence_rejects_high_value_counterexamples(
    transactions: tuple[StatementTransaction, ...],
    error_code: str,
) -> None:
    with pytest.raises(StatementParseError, match=rf"^{error_code}$"):
        _validate_boc_transactions(
            transactions,
            period_start="2026-08-25",
            period_end="2026-08-25",
        )


def test_transaction_to_dict_preserves_source_identifiers() -> None:
    transaction = StatementTransaction(
        sequence=7,
        bank_transaction_id="TX-0007",
        submitted_at="2026-08-25T12:00:01",
        booked_at="2026-08-25T12:00:00",
        amount_minor=18801,
        balance_minor=100000,
        currency="CNY",
        transaction_name="收款",
        channel="",
        branch_name="",
        note="",
        counterparty_name="测试对手",
        counterparty_account="",
        counterparty_bank="",
        source_page=1,
    )

    result = transaction.to_dict()

    assert result["bank_transaction_id"] == "TX-0007"
    assert result["submitted_at"] == "2026-08-25T12:00:01"
