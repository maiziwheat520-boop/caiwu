"""Fail-closed parser for MYbank XLSX statement exports."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path, PurePosixPath
from typing import Final, Never
from uuid import UUID, uuid5
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from ledgerbridge.bank_statement_contract import (
    MYBANK_COMPANY_DAILY_XLSX_V2,
    MYBANK_COMPANY_RANGE_XLSX_V3,
    MYBANK_XLSX_V1,
    BankStatementParserProfile,
)
from ledgerbridge.bank_statement_contract import (
    BankStatement as MyBankStatement,
)
from ledgerbridge.bank_statement_contract import (
    BankStatementTransaction as MyBankTransaction,
)
from ledgerbridge.text import contains_unstorable_text

_XLSX_MEDIA_TYPE: Final = MYBANK_XLSX_V1.declared_media_type
_MAIN_NS: Final = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NAMESPACE: Final = UUID("e2c4d403-fbc3-4dbf-9fd4-cc886eef5c15")
_EXPECTED_HEADERS: Final = (
    "交易时间",
    "交易金额",
    "余额",
    "对方户名",
    "对方账号",
    "对方机构名称",
    "交易流水号",
    "交易名称",
)
_COMPANY_TITLE: Final = "浙江网商银行企业账户交易明细"
_COMPANY_HEADERS: Final = (
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
_FULLWIDTH_LEFT_PARENTHESIS: Final = "\N{FULLWIDTH LEFT PARENTHESIS}"
_FULLWIDTH_RIGHT_PARENTHESIS: Final = "\N{FULLWIDTH RIGHT PARENTHESIS}"
_COMPANY_RANGE_HEADERS: Final = {
    (
        "账务流水号",
        "提交时间",
        "交易时间",
        "交易名称",
        f"借方金额{_FULLWIDTH_LEFT_PARENTHESIS}收{_FULLWIDTH_RIGHT_PARENTHESIS}",
        f"贷方金额{_FULLWIDTH_LEFT_PARENTHESIS}支{_FULLWIDTH_RIGHT_PARENTHESIS}",
        "余额",
        "对方户名",
        "对方账号",
        "对方机构",
        "备注",
    ): {
        "serial": 0,
        "submitted_at": 1,
        "occurred_at": 2,
        "transaction_name": 3,
        "debit": 4,
        "credit": 5,
        "balance": 6,
        "counterparty_name": 7,
        "counterparty_account": 8,
        "counterparty_institution": 9,
        "note": 10,
    },
    (
        "账务流水号",
        "交易时间",
        "交易名称",
        f"借方金额{_FULLWIDTH_LEFT_PARENTHESIS}收{_FULLWIDTH_RIGHT_PARENTHESIS}",
        f"贷方金额{_FULLWIDTH_LEFT_PARENTHESIS}支{_FULLWIDTH_RIGHT_PARENTHESIS}",
        "余额",
        "对方户名",
        "对方机构",
        "备注",
    ): {
        "serial": 0,
        "occurred_at": 1,
        "transaction_name": 2,
        "debit": 3,
        "credit": 4,
        "balance": 5,
        "counterparty_name": 6,
        "counterparty_institution": 7,
        "note": 8,
    },
    (
        "账务流水号",
        "交易时间",
        f"借方金额{_FULLWIDTH_LEFT_PARENTHESIS}收{_FULLWIDTH_RIGHT_PARENTHESIS}",
        f"贷方金额{_FULLWIDTH_LEFT_PARENTHESIS}支{_FULLWIDTH_RIGHT_PARENTHESIS}",
        "余额",
        "对方户名",
        "对方账号",
        "对方机构",
        "备注",
    ): {
        "serial": 0,
        "occurred_at": 1,
        "debit": 2,
        "credit": 3,
        "balance": 4,
        "counterparty_name": 5,
        "counterparty_account": 6,
        "counterparty_institution": 7,
        "note": 8,
    },
}
_COMPANY_ACCOUNT = re.compile(r"^(?P<account>[0-9]+)\(人民币\)$")
_COMPANY_COUNT = re.compile(r"^(?P<count>[0-9]+)笔$")
_COMPANY_SUMMARY_MONEY = re.compile(
    r"^￥(?P<amount>0|(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)\.[0-9]{2})$"
)
_UNSIGNED_MONEY = re.compile(r"^(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")
_CELL_REFERENCE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
_MONEY = re.compile(r"^[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_COMPANY_NAMESPACE: Final = UUID("18cc4a49-5483-4a07-bdd7-84352d63de34")
_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_XML_BYTES = 20 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 200
_MAX_TOTAL_UNCOMPRESSED_BYTES = 80 * 1024 * 1024
_MAX_ROWS = 100_000
_MAX_COLUMNS = 100


class MyBankStatementError(RuntimeError):
    """The source file could not prove a valid MYbank statement."""


class MyBankEmptyStatementError(MyBankStatementError):
    """The source is a valid MYbank export with no transactions to import."""


def parse_mybank_company_range_xlsx(
    source_path: Path,
    *,
    expected_sha256: str,
    managed_account_suffix: str,
) -> MyBankStatement:
    """Parse one official multi-day MYbank company statement export."""

    if not _DIGEST.fullmatch(expected_sha256):
        raise MyBankStatementError("expected source digest is invalid")
    if not _ACCOUNT_SUFFIX.fullmatch(managed_account_suffix):
        raise MyBankStatementError("managed account suffix is invalid")
    raw = _read_source(source_path)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if source_sha256 != expected_sha256:
        raise MyBankStatementError("source digest changed")
    rows = _read_workbook_rows(raw, expected_sheet_name="sheet1")
    if len(rows) < 5 or tuple(number for number, _ in rows[:5]) != (1, 2, 3, 4, 5):
        raise MyBankStatementError("company statement metadata rows are invalid")

    title = _company_width(rows[0][1])
    identity = _company_width(rows[1][1])
    if title != (_COMPANY_TITLE, *("" for _ in range(10))):
        raise MyBankStatementError("company statement title is invalid")
    if (
        identity[0] != "企业名称"
        or not identity[1]
        or identity[4] != "企业账号"
        or any(identity[index] for index in (2, 3, 6, 7, 8, 9, 10))
    ):
        raise MyBankStatementError("company statement identity is invalid")
    company_name = _company_text(identity[1], field="company name", required=True, maximum=200)
    account_match = _COMPANY_ACCOUNT.fullmatch(identity[5])
    if account_match is None or not account_match.group("account").endswith(managed_account_suffix):
        raise MyBankStatementError("company statement does not belong to the managed account")
    account_sha256 = hashlib.sha256(account_match.group("account").encode("ascii")).hexdigest()
    company_sha256 = hashlib.sha256(company_name.encode("utf-8")).hexdigest()
    debit_count, debit_total = _company_summary(
        rows[2][1], count_label="借方交易笔数", amount_label="借方交易金额"
    )
    credit_count, credit_total = _company_summary(
        rows[3][1], count_label="贷方交易笔数", amount_label="贷方交易金额"
    )

    headers = tuple(rows[4][1])
    try:
        columns = _COMPANY_RANGE_HEADERS[headers]
    except KeyError:
        raise MyBankStatementError("company range statement header is invalid") from None

    transactions: list[MyBankTransaction] = []
    submitted_values: list[str | None] = []
    known_serials: set[str] = set()
    for expected_row_number, (source_row_number, raw_values) in enumerate(rows[5:], start=6):
        if source_row_number != expected_row_number:
            raise MyBankStatementError("company statement transaction rows are not contiguous")
        if len(raw_values) > len(headers):
            raise MyBankStatementError("company range statement contains unexpected columns")
        values = (*raw_values, *("" for _ in range(len(headers) - len(raw_values))))
        if not any(values):
            raise MyBankStatementError("company statement transaction row is empty")

        serial = _company_text(values[columns["serial"]], field="transaction serial", required=True)
        if serial in known_serials:
            raise MyBankStatementError("company statement transaction serial is duplicated")
        known_serials.add(serial)
        occurred_at = _parse_occurred_at(values[columns["occurred_at"]])
        submitted_index = columns.get("submitted_at")
        submitted_raw = values[submitted_index] if submitted_index is not None else ""
        if submitted_raw:
            submitted_at = _parse_occurred_at(submitted_raw)
            if submitted_at.date() != occurred_at.date():
                raise MyBankStatementError("company statement transaction dates conflict")
            submitted_values.append(submitted_at.isoformat())
        else:
            submitted_values.append(None)

        debit = _company_optional_minor(values[columns["debit"]])
        credit = _company_optional_minor(values[columns["credit"]])
        if (debit is None) == (credit is None):
            raise MyBankStatementError("company statement transaction direction is invalid")
        amount_minor = debit if debit is not None else -credit  # type: ignore[operator]
        if amount_minor == 0:
            raise MyBankStatementError("company statement transaction amount is invalid")
        balance_minor = _company_required_minor(values[columns["balance"]], field="balance")

        note = _company_text(values[columns["note"]], field="note")
        name_index = columns.get("transaction_name")
        transaction_name = (
            _company_text(values[name_index], field="transaction name", required=True)
            if name_index is not None
            else "银行未提供交易名称"
        )
        combined_name = transaction_name if not note else f"{transaction_name} | {note}"
        if len(combined_name) > 300:
            raise MyBankStatementError("company statement transaction name is too long")

        normalized_values = tuple(
            _company_text(value, field="transaction text")
            if index not in {columns["debit"], columns["credit"], columns["balance"]}
            else value
            for index, value in enumerate(values)
        )
        row_sha256 = hashlib.sha256(
            json.dumps(normalized_values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        source_event_ref = uuid5(
            _COMPANY_NAMESPACE,
            f"mybank-company-range-event:{source_sha256}:{source_row_number}:{row_sha256}",
        )
        counterparty_account_index = columns.get("counterparty_account")
        transactions.append(
            MyBankTransaction(
                source_event_ref=source_event_ref,
                source_row_number=source_row_number,
                source_row_sha256=row_sha256,
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                balance_minor=balance_minor,
                counterparty_name=_company_text(
                    values[columns["counterparty_name"]], field="counterparty name"
                ),
                counterparty_account=(
                    _company_text(values[counterparty_account_index], field="counterparty account")
                    if counterparty_account_index is not None
                    else ""
                ),
                counterparty_institution=_company_text(
                    values[columns["counterparty_institution"]],
                    field="counterparty institution",
                ),
                transaction_serial=serial,
                transaction_name=combined_name,
            )
        )

    _validate_company_transactions(
        transactions,
        debit_count=debit_count,
        debit_total_minor=debit_total,
        credit_count=credit_count,
        credit_total_minor=credit_total,
    )
    if not transactions:
        raise MyBankEmptyStatementError("company statement contains no transactions")
    parser_facts_sha256 = hashlib.sha256(
        json.dumps(
            {
                "account_sha256": account_sha256,
                "company_sha256": company_sha256,
                "credit_count": credit_count,
                "credit_total_minor": credit_total,
                "debit_count": debit_count,
                "debit_total_minor": debit_total,
                "header": headers,
                "row_sha256": [item.source_row_sha256 for item in transactions],
                "submitted_at": submitted_values,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return MyBankStatement(
        statement_ref=uuid5(_COMPANY_NAMESPACE, f"mybank-company-range:{source_sha256}"),
        source_sha256=source_sha256,
        source_size=len(raw),
        declared_media_type=MYBANK_COMPANY_RANGE_XLSX_V3.declared_media_type,
        currency="CNY",
        institution_code=MYBANK_COMPANY_RANGE_XLSX_V3.institution_code,
        account_suffix=managed_account_suffix,
        worksheet_index=1,
        header_row_number=5,
        transactions=tuple(transactions),
        parser_profile=BankStatementParserProfile.MYBANK_COMPANY_RANGE_XLSX_V3,
        source_system=MYBANK_COMPANY_RANGE_XLSX_V3.source_system,
        parser_facts_sha256=parser_facts_sha256,
    )


def parse_mybank_company_daily_xlsx(
    source_path: Path,
    *,
    expected_sha256: str,
    managed_account_suffix: str,
) -> MyBankStatement:
    """Parse one digest-bound, single-day MYbank company statement."""

    if not _DIGEST.fullmatch(expected_sha256):
        raise MyBankStatementError("expected source digest is invalid")
    if not _ACCOUNT_SUFFIX.fullmatch(managed_account_suffix):
        raise MyBankStatementError("managed account suffix is invalid")
    raw = _read_source(source_path)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if source_sha256 != expected_sha256:
        raise MyBankStatementError("source digest changed")
    rows = _read_workbook_rows(raw, expected_sheet_name="sheet1")
    if len(rows) < 5 or tuple(number for number, _ in rows[:5]) != (1, 2, 3, 4, 5):
        raise MyBankStatementError("company statement metadata rows are invalid")
    title = _company_width(rows[0][1])
    identity = _company_width(rows[1][1])
    headers = _company_width(rows[4][1])
    if title != (_COMPANY_TITLE, *("" for _ in range(10))):
        raise MyBankStatementError("company statement title is invalid")
    if (
        identity[0] != "企业名称"
        or not identity[1]
        or identity[4] != "企业账号"
        or any(identity[index] for index in (2, 3, 6, 7, 8, 9, 10))
    ):
        raise MyBankStatementError("company statement identity is invalid")
    company_name = _company_text(identity[1], field="company name", required=True, maximum=200)
    account_match = _COMPANY_ACCOUNT.fullmatch(identity[5])
    if account_match is None or not account_match.group("account").endswith(managed_account_suffix):
        raise MyBankStatementError("company statement does not belong to the managed account")
    account_sha256 = hashlib.sha256(account_match.group("account").encode("ascii")).hexdigest()
    company_sha256 = hashlib.sha256(company_name.encode("utf-8")).hexdigest()
    debit_count, debit_total = _company_summary(
        rows[2][1], count_label="借方交易笔数", amount_label="借方交易金额"
    )
    credit_count, credit_total = _company_summary(
        rows[3][1], count_label="贷方交易笔数", amount_label="贷方交易金额"
    )
    if headers != _COMPANY_HEADERS:
        raise MyBankStatementError("company statement header is invalid")

    transactions: list[MyBankTransaction] = []
    submitted_values: list[str] = []
    known_serials: set[str] = set()
    statement_day: date | None = None
    for expected_row_number, (source_row_number, raw_values) in enumerate(rows[5:], start=6):
        if source_row_number != expected_row_number:
            raise MyBankStatementError("company statement transaction rows are not contiguous")
        values = _company_width(raw_values)
        if not any(values):
            raise MyBankStatementError("company statement transaction row is empty")
        serial = _company_text(values[0], field="transaction serial", required=True)
        if serial in known_serials:
            raise MyBankStatementError("company statement transaction serial is duplicated")
        known_serials.add(serial)
        submitted_at = _parse_occurred_at(values[1])
        occurred_at = _parse_occurred_at(values[2])
        if submitted_at.date() != occurred_at.date():
            raise MyBankStatementError("company statement transaction dates conflict")
        if statement_day is None:
            statement_day = occurred_at.date()
        elif occurred_at.date() != statement_day:
            raise MyBankStatementError("company statement contains more than one day")
        debit = _company_optional_minor(values[4])
        credit = _company_optional_minor(values[5])
        if (debit is None) == (credit is None):
            raise MyBankStatementError("company statement transaction direction is invalid")
        if debit is not None:
            amount_minor = debit
        else:
            if credit is None:  # pragma: no cover - guarded by direction check
                raise MyBankStatementError("company statement transaction direction is invalid")
            amount_minor = -credit
        if amount_minor == 0:
            raise MyBankStatementError("company statement transaction amount is invalid")
        balance_minor = _company_required_minor(values[6], field="balance")
        transaction_name = _company_text(values[3], field="transaction name", required=True)
        note = _company_text(values[10], field="note")
        combined_name = transaction_name if not note else f"{transaction_name} | {note}"
        if len(combined_name) > 300:
            raise MyBankStatementError("company statement transaction name is too long")
        transaction_values = tuple(
            _company_text(value, field="transaction text") if index not in {4, 5, 6} else value
            for index, value in enumerate(values)
        )
        row_sha256 = hashlib.sha256(
            json.dumps(transaction_values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        source_event_ref = uuid5(
            _COMPANY_NAMESPACE,
            f"mybank-company-daily-event:{source_sha256}:{source_row_number}:{row_sha256}",
        )
        transactions.append(
            MyBankTransaction(
                source_event_ref=source_event_ref,
                source_row_number=source_row_number,
                source_row_sha256=row_sha256,
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                balance_minor=balance_minor,
                counterparty_name=_company_text(values[7], field="counterparty name"),
                counterparty_account=_company_text(values[8], field="counterparty account"),
                counterparty_institution=_company_text(values[9], field="counterparty institution"),
                transaction_serial=serial,
                transaction_name=combined_name,
            )
        )
        submitted_values.append(submitted_at.isoformat())

    _validate_company_transactions(
        transactions,
        debit_count=debit_count,
        debit_total_minor=debit_total,
        credit_count=credit_count,
        credit_total_minor=credit_total,
    )
    if not transactions:
        raise MyBankEmptyStatementError("company statement contains no transactions")
    parser_facts_sha256 = hashlib.sha256(
        json.dumps(
            {
                "account_sha256": account_sha256,
                "company_sha256": company_sha256,
                "credit_count": credit_count,
                "credit_total_minor": credit_total,
                "debit_count": debit_count,
                "debit_total_minor": debit_total,
                "row_sha256": [item.source_row_sha256 for item in transactions],
                "submitted_at": submitted_values,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return MyBankStatement(
        statement_ref=uuid5(_COMPANY_NAMESPACE, f"mybank-company-daily-statement:{source_sha256}"),
        source_sha256=source_sha256,
        source_size=len(raw),
        declared_media_type=MYBANK_COMPANY_DAILY_XLSX_V2.declared_media_type,
        currency="CNY",
        institution_code=MYBANK_COMPANY_DAILY_XLSX_V2.institution_code,
        account_suffix=managed_account_suffix,
        worksheet_index=1,
        header_row_number=5,
        transactions=tuple(transactions),
        parser_profile=BankStatementParserProfile.MYBANK_COMPANY_DAILY_XLSX_V2,
        source_system=MYBANK_COMPANY_DAILY_XLSX_V2.source_system,
        parser_facts_sha256=parser_facts_sha256,
    )


def parse_mybank_xlsx(
    source_path: Path,
    *,
    expected_sha256: str,
    managed_account_suffix: str,
) -> MyBankStatement:
    """Parse one digest-bound MYbank export without trusting formulas or macros."""

    if not _DIGEST.fullmatch(expected_sha256):
        raise MyBankStatementError("expected source digest is invalid")
    if not _ACCOUNT_SUFFIX.fullmatch(managed_account_suffix):
        raise MyBankStatementError("managed account suffix is invalid")
    raw = _read_source(source_path)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if source_sha256 != expected_sha256:
        raise MyBankStatementError("source digest changed")
    rows = _read_workbook_rows(raw)
    header_position = _find_header(rows)
    header_row_number, _ = rows[header_position]
    metadata_rows = rows[:header_position]
    metadata = " ".join(value for _, values in metadata_rows for value in values if value)
    if "网商银行" not in metadata:
        raise MyBankStatementError("statement institution is not proven")
    card_values = tuple(
        values[index + 1]
        for _, values in metadata_rows
        for index, value in enumerate(values[:-1])
        if value.strip().rstrip("\N{FULLWIDTH COLON}:").strip() == "卡号"
        and values[index + 1].strip()
    )
    if len(card_values) != 1 or not card_values[0].strip().endswith(managed_account_suffix):
        raise MyBankStatementError("statement does not belong to the managed account")
    if "人民币" not in metadata and "CNY" not in metadata.upper():
        raise MyBankStatementError("statement currency is not proven")

    transactions: list[MyBankTransaction] = []
    known_serials: set[str] = set()
    for source_row_number, values in rows[header_position + 1 :]:
        normalized = _normalized_width(values)
        if not any(normalized):
            continue
        if any(normalized[len(_EXPECTED_HEADERS) :]):
            raise MyBankStatementError("statement contains unexpected populated columns")
        transaction_values = normalized[: len(_EXPECTED_HEADERS)]
        if not all(transaction_values[index] for index in (0, 1, 2, 6, 7)):
            raise MyBankStatementError("statement transaction is incomplete")
        transaction_serial = transaction_values[6]
        if transaction_serial in known_serials:
            raise MyBankStatementError("statement transaction serial is duplicated")
        known_serials.add(transaction_serial)
        occurred_at = _parse_occurred_at(transaction_values[0])
        amount_minor = _parse_minor(transaction_values[1], field="amount")
        balance_minor = _parse_minor(transaction_values[2], field="balance")
        row_sha256 = hashlib.sha256(
            json.dumps(
                transaction_values,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        source_event_ref = uuid5(
            _NAMESPACE,
            f"mybank-event:{source_sha256}:{source_row_number}:{row_sha256}",
        )
        transactions.append(
            MyBankTransaction(
                source_event_ref=source_event_ref,
                source_row_number=source_row_number,
                source_row_sha256=row_sha256,
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                balance_minor=balance_minor,
                counterparty_name=transaction_values[3],
                counterparty_account=transaction_values[4],
                counterparty_institution=transaction_values[5],
                transaction_serial=transaction_serial,
                transaction_name=transaction_values[7],
            )
        )
    if not transactions:
        raise MyBankEmptyStatementError("statement contains no transactions")
    return MyBankStatement(
        statement_ref=uuid5(_NAMESPACE, f"mybank-statement:{source_sha256}"),
        source_sha256=source_sha256,
        source_size=len(raw),
        declared_media_type=_XLSX_MEDIA_TYPE,
        currency="CNY",
        institution_code="mybank",
        account_suffix=managed_account_suffix,
        worksheet_index=1,
        header_row_number=header_row_number,
        transactions=tuple(transactions),
        parser_profile=BankStatementParserProfile.MYBANK_XLSX_V1,
        source_system=MYBANK_XLSX_V1.source_system,
        parser_facts_sha256=hashlib.sha256(
            (f"mybank_xlsx_v1:{source_sha256}:{header_row_number}:{len(transactions)}").encode(
                "ascii"
            )
        ).hexdigest(),
    )


def build_mybank_source_manifest(
    statement: MyBankStatement,
    *,
    source_file: str,
    context: object,
) -> Never:
    """Reject the retired per-transaction review-manifest path."""

    _ = (statement, source_file, context)
    raise MyBankStatementError(
        "per-transaction MYbank review manifests are retired; use BankStatementPersistenceService"
    )


def _company_width(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) > len(_COMPANY_HEADERS):
        raise MyBankStatementError("company statement contains unexpected columns")
    return (*values, *("" for _ in range(len(_COMPANY_HEADERS) - len(values))))


def _company_summary(
    values: tuple[str, ...],
    *,
    count_label: str,
    amount_label: str,
) -> tuple[int, int]:
    row = _company_width(values)
    if (
        row[0] != count_label
        or row[4] != amount_label
        or any(row[index] for index in (2, 3, 6, 7, 8, 9, 10))
    ):
        raise MyBankStatementError("company statement summary is invalid")
    count_match = _COMPANY_COUNT.fullmatch(row[1])
    amount_match = _COMPANY_SUMMARY_MONEY.fullmatch(row[5])
    if count_match is None or amount_match is None:
        raise MyBankStatementError("company statement summary is invalid")
    amount_text = amount_match.group("amount")
    amount_minor = 0 if amount_text == "0" else _parse_minor(amount_text, field="summary")
    return int(count_match.group("count")), amount_minor


def _company_optional_minor(value: str) -> int | None:
    if not value:
        return None
    if _UNSIGNED_MONEY.fullmatch(value) is None:
        raise MyBankStatementError("company statement transaction amount is invalid")
    return _parse_minor(value, field="transaction amount")


def _company_required_minor(value: str, *, field: str) -> int:
    if not value:
        raise MyBankStatementError(f"company statement {field} is invalid")
    return _parse_minor(value, field=field)


def _company_text(
    value: str,
    *,
    field: str,
    required: bool = False,
    maximum: int = 300,
) -> str:
    normalized = " ".join(value.split())
    if (
        (required and not normalized)
        or len(normalized) > maximum
        or contains_unstorable_text(normalized)
    ):
        raise MyBankStatementError(f"company statement {field} is invalid")
    return normalized


def _validate_company_transactions(
    transactions: list[MyBankTransaction],
    *,
    debit_count: int,
    debit_total_minor: int,
    credit_count: int,
    credit_total_minor: int,
) -> None:
    previous_occurred_at: datetime | None = None
    for transaction in transactions:
        if previous_occurred_at is not None and transaction.occurred_at > previous_occurred_at:
            raise MyBankStatementError("company statement transaction order is invalid")
        previous_occurred_at = transaction.occurred_at
    for current, older in pairwise(transactions):
        if current.balance_minor - older.balance_minor != current.amount_minor:
            raise MyBankStatementError("company statement balance chain is invalid")
    debits = [item.amount_minor for item in transactions if item.amount_minor > 0]
    credits = [-item.amount_minor for item in transactions if item.amount_minor < 0]
    if len(debits) != debit_count or sum(debits) != debit_total_minor:
        raise MyBankStatementError("company statement debit summary is invalid")
    if len(credits) != credit_count or sum(credits) != credit_total_minor:
        raise MyBankStatementError("company statement credit summary is invalid")


def _read_source(path: Path) -> bytes:
    if not path.is_absolute():
        raise MyBankStatementError("statement path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MyBankStatementError("statement file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MyBankStatementError("statement file must be regular")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_FILE_BYTES:
        raise MyBankStatementError("statement file size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MyBankStatementError("statement file cannot be opened") from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(_MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise MyBankStatementError("statement file cannot be read") from exc
    if len(raw) != metadata.st_size or len(raw) > _MAX_FILE_BYTES:
        raise MyBankStatementError("statement file changed while reading")
    return raw


def _read_workbook_rows(
    raw: bytes,
    *,
    expected_sheet_name: str | None = None,
) -> list[tuple[int, tuple[str, ...]]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (OSError, zipfile.BadZipFile) as exc:
        raise MyBankStatementError("statement is not a valid XLSX container") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > _MAX_ARCHIVE_ENTRIES:
            raise MyBankStatementError("statement archive entry count is invalid")
        names = [info.filename for info in infos]
        if len(set(names)) != len(names):
            raise MyBankStatementError("statement archive contains duplicate entries")
        total_size = 0
        for info in infos:
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts or info.flag_bits & 0x1:
                raise MyBankStatementError("statement archive entry is unsafe")
            total_size += info.file_size
            if info.file_size > _MAX_XML_BYTES or total_size > _MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise MyBankStatementError("statement archive is too large")
        workbook_root = _parse_xml(_read_entry(archive, "xl/workbook.xml"))
        sheets = workbook_root.findall(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet")
        if len(sheets) != 1:
            raise MyBankStatementError("statement must contain exactly one worksheet")
        if expected_sheet_name is not None and sheets[0].get("name") != expected_sheet_name:
            raise MyBankStatementError("statement worksheet name is invalid")
        relationship_id = sheets[0].get(f"{{{_OFFICE_REL_NS}}}id")
        if not relationship_id:
            raise MyBankStatementError("statement worksheet relationship is missing")
        relationships = _parse_xml(_read_entry(archive, "xl/_rels/workbook.xml.rels"))
        targets = [
            item.get("Target")
            for item in relationships.findall(f"{{{_REL_NS}}}Relationship")
            if item.get("Id") == relationship_id
        ]
        if len(targets) != 1 or not targets[0]:
            raise MyBankStatementError("statement worksheet target is invalid")
        worksheet_path = _resolve_worksheet_path(targets[0])
        shared_strings = _read_shared_strings(archive)
        worksheet = _parse_xml(_read_entry(archive, worksheet_path))
        if worksheet.find(f".//{{{_MAIN_NS}}}f") is not None:
            raise MyBankStatementError("statement formulas are not accepted")
        return _worksheet_rows(worksheet, shared_strings)


def _read_entry(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise MyBankStatementError("statement archive is incomplete") from exc
    if info.file_size <= 0 or info.file_size > _MAX_XML_BYTES:
        raise MyBankStatementError("statement XML part size is invalid")
    try:
        return archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise MyBankStatementError("statement XML part cannot be read") from exc


def _parse_xml(raw: bytes) -> ElementTree.Element:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise MyBankStatementError("statement XML declarations are unsafe")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise MyBankStatementError("statement XML is invalid") from exc


def _resolve_worksheet_path(target: str) -> str:
    path = PurePosixPath(target)
    if path.is_absolute() or ".." in path.parts:
        raise MyBankStatementError("statement worksheet path is unsafe")
    resolved = path if path.parts and path.parts[0] == "xl" else PurePosixPath("xl") / path
    value = resolved.as_posix()
    if not value.startswith("xl/worksheets/") or not value.endswith(".xml"):
        raise MyBankStatementError("statement worksheet path is unexpected")
    return value


def _read_shared_strings(archive: zipfile.ZipFile) -> tuple[str, ...]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return ()
    if len(raw) > _MAX_XML_BYTES:
        raise MyBankStatementError("statement shared strings are too large")
    root = _parse_xml(raw)
    values = [
        "".join(text.text or "" for text in item.iter(f"{{{_MAIN_NS}}}t"))
        for item in root.findall(f"{{{_MAIN_NS}}}si")
    ]
    if len(values) > _MAX_ROWS * _MAX_COLUMNS:
        raise MyBankStatementError("statement shared string count is invalid")
    return tuple(values)


def _worksheet_rows(
    worksheet: ElementTree.Element,
    shared_strings: tuple[str, ...],
) -> list[tuple[int, tuple[str, ...]]]:
    rows: list[tuple[int, tuple[str, ...]]] = []
    known_rows: set[int] = set()
    known_cells: set[str] = set()
    for row in worksheet.findall(f".//{{{_MAIN_NS}}}sheetData/{{{_MAIN_NS}}}row"):
        row_number_raw = row.get("r")
        if not row_number_raw or not row_number_raw.isdigit():
            raise MyBankStatementError("statement row identity is invalid")
        row_number = int(row_number_raw)
        if row_number <= 0 or row_number > _MAX_ROWS or row_number in known_rows:
            raise MyBankStatementError("statement row count is invalid")
        known_rows.add(row_number)
        values: list[str] = []
        for cell in row.findall(f"{{{_MAIN_NS}}}c"):
            reference = cell.get("r", "")
            match = _CELL_REFERENCE.fullmatch(reference)
            if not match or int(match.group(2)) != row_number or reference in known_cells:
                raise MyBankStatementError("statement cell reference is invalid")
            known_cells.add(reference)
            column = _column_index(match.group(1))
            if column > _MAX_COLUMNS:
                raise MyBankStatementError("statement column count is invalid")
            while len(values) < column:
                values.append("")
            values[column - 1] = _cell_text(cell, shared_strings)
        rows.append((row_number, tuple(values)))
    if not rows or len(rows) > _MAX_ROWS:
        raise MyBankStatementError("statement contains no readable rows")
    return rows


def _column_index(name: str) -> int:
    value = 0
    for character in name:
        value = value * 26 + ord(character) - 64
    return value


def _cell_text(cell: ElementTree.Element, shared_strings: tuple[str, ...]) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.iter(f"{{{_MAIN_NS}}}t")).strip()
    value = cell.findtext(f"{{{_MAIN_NS}}}v", default="")
    if cell_type == "s":
        try:
            index = int(value)
            return shared_strings[index].strip()
        except (ValueError, IndexError) as exc:
            raise MyBankStatementError("statement shared string reference is invalid") from exc
    if cell_type in (None, "n", "str"):
        return value.strip()
    raise MyBankStatementError("statement cell type is unsupported")


def _find_header(rows: list[tuple[int, tuple[str, ...]]]) -> int:
    positions = [
        index
        for index, (_, values) in enumerate(rows[:50])
        if _normalized_width(values)[: len(_EXPECTED_HEADERS)] == _EXPECTED_HEADERS
        and not any(_normalized_width(values)[len(_EXPECTED_HEADERS) :])
    ]
    if len(positions) != 1:
        raise MyBankStatementError("statement header is missing or ambiguous")
    return positions[0]


def _normalized_width(values: tuple[str, ...]) -> tuple[str, ...]:
    width = max(len(values), len(_EXPECTED_HEADERS))
    return tuple(values[index].strip() if index < len(values) else "" for index in range(width))


def _parse_occurred_at(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise MyBankStatementError("statement transaction time is invalid") from exc
    return parsed.replace(tzinfo=_SHANGHAI)


def _parse_minor(value: str, *, field: str) -> int:
    if not _MONEY.fullmatch(value):
        raise MyBankStatementError(f"statement {field} is invalid")
    try:
        decimal_value = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise MyBankStatementError(f"statement {field} is invalid") from exc
    minor = decimal_value * 100
    if minor != minor.to_integral_value() or abs(minor) > 9_007_199_254_740_991:
        raise MyBankStatementError(f"statement {field} is out of range")
    return int(minor)
