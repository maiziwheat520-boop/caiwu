"""Strict, memory-only parsers for the currently verified bank exports.

Optional document dependencies are deliberately imported at call time.  This
keeps the core package importable without parser extras and, more importantly,
keeps decrypted document bytes inside the caller's process.
"""

from __future__ import annotations

import importlib
import io
import re
import warnings
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree

MYBANK_TITLE = "浙江网商银行企业账户交易明细"
MYBANK_PARSER_VERSION = "mybank-xlsx-v2"
BOC_PARSER_VERSION = "boc-pdf-layout-v1"
MAX_MYBANK_XLSX_BYTES = 10 * 1024 * 1024
MAX_MYBANK_XLSX_MEMBERS = 256
MAX_MYBANK_XLSX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_MYBANK_XLSX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_MYBANK_ROWS = 10_000
MAX_MYBANK_DATA_CELLS = MAX_MYBANK_ROWS * 11

_MYBANK_FILENAME_RE = re.compile(
    r"^(?P<account>\d+)-日账单-(?P<date>\d{8})-交易明细-"
    r"解压密码是企业统一社会信用代码后六位\.xlsx$",
    re.IGNORECASE,
)
_DATE = r"\d{4}-\d{2}-\d{2}"
_TIME = r"\d{2}:\d{2}:\d{2}"
_MONEY = r"(?:\d{1,3}(?:,\d{3})*|\d+)\.\d{2}"
_SIGNED_MONEY = rf"[+-]?{_MONEY}"
_PAGE_RANGE_RE = re.compile(
    rf"^\s*交易区间[:\uff1a]\s*(?P<start>{_DATE})\s*至\s*(?P<end>{_DATE})"
    rf"\s+客户姓名[:\uff1a]\s*(?P<name>.+?)\s+页数\s*[:\uff1a]\s*"
    r"(?P<page>\d+)\s*/\s*(?P<pages>\d+)\s*$"
)
_PAGE_TOTALS_RE = re.compile(
    rf"^\s*借记卡号[:\uff1a]\s*(?P<card>\d+)\s+借方发生数[:\uff1a]\s*"
    rf"(?P<debit>{_MONEY})\s+贷方发生数[:\uff1a]\s*(?P<credit>{_MONEY})"
    r"\s+行数\s*[:\uff1a]\s*(?P<rows>\d+)\s*$"
)
_ACCOUNT_RE = re.compile(
    r"^\s*账号[:\uff1a]\s*(?P<account>\d+).*?打印时间[:\uff1a]\s*"
    r"(?P<printed>\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\s*$"
)
_MAIN_LINE_RE = re.compile(
    rf"^\s*(?P<date>{_DATE})\s+(?P<time>{_TIME})\s+人民币\s+"
    rf"(?P<amount>{_SIGNED_MONEY})\s+(?P<balance>{_SIGNED_MONEY})(?:\s|$)"
)
_CONTINUATION_FORBIDDEN_RE = re.compile(
    rf"(?:{_DATE})|(?:{_TIME})|(?<![\d,])(?:{_SIGNED_MONEY})(?![\d,])"
)
_MYBANK_ACCOUNT_RE = re.compile(r"(?P<account>\d+)\(人民币\)")
_MYBANK_COUNT_RE = re.compile(r"(?P<count>\d+)笔")
_MYBANK_SUMMARY_MONEY_RE = re.compile(rf"￥(?P<amount>0|{_MONEY})")
_MYBANK_HEADERS = (
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
_COLUMN_HEADERS = (
    "记账日期",
    "记账时间",
    "币别",
    "金额",
    "余额",
    "交易名称",
    "渠道",
    "网点名称",
    "附言",
    "对方账户名",
    "对方卡号/账号",
    "对方开户行",
)
_TEXT_COLUMN_FIELDS = (
    "transaction_name",
    "channel",
    "branch_name",
    "note",
    "counterparty_name",
    "counterparty_account",
    "counterparty_bank",
)


class StatementParseError(ValueError):
    """The document does not exactly match a verified statement schema."""


class StatementDependencyError(StatementParseError):
    """An optional parser dependency is not installed or is incompatible."""


@dataclass(frozen=True, slots=True)
class StatementTransaction:
    """One normalized bank transaction; all money is signed CNY minor units."""

    sequence: int
    bank_transaction_id: str
    submitted_at: str
    booked_at: str
    amount_minor: int
    balance_minor: int
    currency: str
    transaction_name: str
    channel: str
    branch_name: str
    note: str
    counterparty_name: str
    counterparty_account: str
    counterparty_bank: str
    source_page: int

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "bank_transaction_id": self.bank_transaction_id,
            "submitted_at": self.submitted_at,
            "booked_at": self.booked_at,
            "amount_minor": self.amount_minor,
            "balance_minor": self.balance_minor,
            "currency": self.currency,
            "transaction_name": self.transaction_name,
            "channel": self.channel,
            "branch_name": self.branch_name,
            "note": self.note,
            "counterparty_name": self.counterparty_name,
            "counterparty_account": self.counterparty_account,
            "counterparty_bank": self.counterparty_bank,
            "source_page": self.source_page,
        }


@dataclass(frozen=True, slots=True)
class StatementPageSummary:
    page_number: int
    row_count: int
    debit_total_minor: int
    credit_total_minor: int

    def to_dict(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "row_count": self.row_count,
            "debit_total_minor": self.debit_total_minor,
            "credit_total_minor": self.credit_total_minor,
        }


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    """Unified result shared by the MYBANK and BOC parsers."""

    bank_code: str
    source: str
    parser_version: str
    source_filename: str
    account_number: str
    account_name: str
    period_start: str
    period_end: str
    currency: str
    transactions: tuple[StatementTransaction, ...]
    debit_total_minor: int
    credit_total_minor: int
    page_count: int
    page_summaries: tuple[StatementPageSummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "bank_code": self.bank_code,
            "source": self.source,
            "parser_version": self.parser_version,
            "source_filename": self.source_filename,
            "account_number": self.account_number,
            "account_name": self.account_name,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "currency": self.currency,
            "transactions": [transaction.to_dict() for transaction in self.transactions],
            "debit_total_minor": self.debit_total_minor,
            "credit_total_minor": self.credit_total_minor,
            "page_count": self.page_count,
            "page_summaries": [summary.to_dict() for summary in self.page_summaries],
        }


@dataclass(frozen=True, slots=True)
class _BocPage:
    page_number: int
    page_count: int
    period_start: str
    period_end: str
    account_name: str
    card_number: str
    account_number: str
    transactions: tuple[StatementTransaction, ...]
    summary: StatementPageSummary


def parse_mybank_xlsx(
    content: bytes,
    *,
    source_filename: str,
    company_name: str,
) -> ParsedStatement:
    """Parse the complete, currently verified MYBANK daily-statement schema."""

    if not isinstance(content, bytes) or not content:
        raise StatementParseError("MYBANK_XLSX_EMPTY")
    if len(content) > MAX_MYBANK_XLSX_BYTES:
        raise StatementParseError("MYBANK_XLSX_SIZE_LIMIT_EXCEEDED")
    if not isinstance(company_name, str) or not company_name.strip():
        raise StatementParseError("MYBANK_COMPANY_NAME_INVALID")
    basename = _basename(source_filename)
    match = _MYBANK_FILENAME_RE.fullmatch(basename)
    if match is None:
        raise StatementParseError("MYBANK_FILENAME_SCHEMA_UNRECOGNIZED")
    statement_date = _parse_date(match.group("date"), "%Y%m%d", "MYBANK_FILENAME_DATE_INVALID")

    data_max_row = _validate_mybank_worksheet_xml(content)
    openpyxl = _optional_module("openpyxl", "OPENPYXL_DEPENDENCY_UNAVAILABLE")
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="Workbook contains no default style, apply openpyxl's default",
                category=UserWarning,
                module=r"openpyxl\.styles\.stylesheet",
            )
            workbook = openpyxl.load_workbook(
                io.BytesIO(content),
                read_only=True,
                data_only=False,
            )
    except Exception:
        raise StatementParseError("MYBANK_XLSX_INVALID") from None
    try:
        if workbook.sheetnames != ["sheet1"]:
            raise StatementParseError("MYBANK_WORKBOOK_SCHEMA_UNRECOGNIZED")
        worksheet = workbook["sheet1"]
        try:
            worksheet.reset_dimensions()
            rows = [
                tuple(row)
                for row in worksheet.iter_rows(
                    min_row=1,
                    max_row=data_max_row,
                    min_col=1,
                    max_col=11,
                    values_only=True,
                )
            ]
        except Exception:
            raise StatementParseError("MYBANK_XLSX_INVALID") from None
    finally:
        workbook.close()

    if len(rows) != data_max_row or len(rows) < 5:
        raise StatementParseError("MYBANK_WORKSHEET_SCHEMA_UNRECOGNIZED")
    account_number = match.group("account")
    _validate_mybank_headers(
        rows[:5],
        company_name=company_name,
        account_number=account_number,
    )
    debit_count, debit_total = _parse_mybank_summary_row(
        rows[2],
        count_label="借方交易笔数",
        amount_label="借方交易金额",
    )
    credit_count, credit_total = _parse_mybank_summary_row(
        rows[3],
        count_label="贷方交易笔数",
        amount_label="贷方交易金额",
    )
    transactions = tuple(
        _parse_mybank_transaction(
            row,
            sequence=index,
            statement_date=statement_date,
        )
        for index, row in enumerate(rows[5:], start=1)
    )
    iso_date = statement_date.isoformat()
    _validate_mybank_transactions(
        transactions,
        statement_date=iso_date,
        debit_count=debit_count,
        debit_total_minor=debit_total,
        credit_count=credit_count,
        credit_total_minor=credit_total,
    )
    summary = StatementPageSummary(
        page_number=1,
        row_count=len(transactions),
        debit_total_minor=debit_total,
        credit_total_minor=credit_total,
    )
    return ParsedStatement(
        bank_code="MYBANK",
        source="mybank_daily_statement",
        parser_version=MYBANK_PARSER_VERSION,
        source_filename=basename,
        account_number=account_number,
        account_name=_collapse_spaces(company_name),
        period_start=iso_date,
        period_end=iso_date,
        currency="CNY",
        transactions=transactions,
        debit_total_minor=debit_total,
        credit_total_minor=credit_total,
        page_count=1,
        page_summaries=(summary,),
    )


def parse_boc_pdf(
    content: bytes,
    *,
    password: str,
    source_filename: str,
) -> ParsedStatement:
    """Parse a verified encrypted BOC transaction-detail PDF in memory."""

    if not isinstance(content, bytes) or not content.startswith(b"%PDF-"):
        raise StatementParseError("BOC_PDF_INVALID")
    if not isinstance(password, str) or not password:
        raise StatementParseError("BOC_PASSWORD_REQUIRED")
    basename = _basename(source_filename)
    if not basename.casefold().endswith(".pdf"):
        raise StatementParseError("BOC_FILENAME_INVALID")

    pypdf = _optional_module("pypdf", "PYPDF_DEPENDENCY_UNAVAILABLE")
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
    except Exception:
        raise StatementParseError("BOC_PDF_INVALID") from None
    if not bool(reader.is_encrypted):
        raise StatementParseError("BOC_PDF_NOT_ENCRYPTED")
    try:
        decrypted = reader.decrypt(password)
    except Exception:
        raise StatementParseError("BOC_PDF_DECRYPT_FAILED") from None
    if not decrypted:
        raise StatementParseError("BOC_PDF_DECRYPT_FAILED")

    try:
        page_count = len(reader.pages)
    except Exception:
        raise StatementParseError("BOC_PDF_PAGE_ACCESS_FAILED") from None
    if page_count <= 0:
        raise StatementParseError("BOC_PDF_HAS_NO_PAGES")

    pages: list[_BocPage] = []
    sequence = 1
    for page_index in range(page_count):
        try:
            text = reader.pages[page_index].extract_text(extraction_mode="layout")
        except TypeError:
            raise StatementDependencyError("PYPDF_LAYOUT_MODE_UNAVAILABLE") from None
        except Exception:
            raise StatementParseError("BOC_PDF_TEXT_EXTRACTION_FAILED") from None
        if not isinstance(text, str) or not text.strip():
            raise StatementParseError("BOC_PAGE_TEXT_MISSING")
        parsed_page = _parse_boc_page(
            text,
            physical_page=page_index + 1,
            document_page_count=page_count,
            first_sequence=sequence,
        )
        pages.append(parsed_page)
        sequence += len(parsed_page.transactions)

    first = pages[0]
    for page in pages[1:]:
        if (
            page.page_count != first.page_count
            or page.period_start != first.period_start
            or page.period_end != first.period_end
            or page.account_name != first.account_name
            or page.card_number != first.card_number
            or page.account_number != first.account_number
        ):
            raise StatementParseError("BOC_PAGE_METADATA_INCONSISTENT")

    extracted_transactions = tuple(
        transaction for page in pages for transaction in page.transactions
    )
    transactions = _correct_amount_signs_by_balance(extracted_transactions)
    _validate_boc_transactions(
        transactions,
        period_start=first.period_start,
        period_end=first.period_end,
    )
    _validate_boc_page_totals(transactions, pages)
    debit_total = sum(page.summary.debit_total_minor for page in pages)
    credit_total = sum(page.summary.credit_total_minor for page in pages)
    if sum(-item.amount_minor for item in transactions if item.amount_minor < 0) != debit_total:
        raise StatementParseError("BOC_DOCUMENT_DEBIT_TOTAL_MISMATCH")
    if sum(item.amount_minor for item in transactions if item.amount_minor > 0) != credit_total:
        raise StatementParseError("BOC_DOCUMENT_CREDIT_TOTAL_MISMATCH")
    return ParsedStatement(
        bank_code="BOC",
        source="boc_transaction_statement",
        parser_version=BOC_PARSER_VERSION,
        source_filename=basename,
        account_number=first.account_number,
        account_name=first.account_name,
        period_start=first.period_start,
        period_end=first.period_end,
        currency="CNY",
        transactions=transactions,
        debit_total_minor=debit_total,
        credit_total_minor=credit_total,
        page_count=page_count,
        page_summaries=tuple(page.summary for page in pages),
    )


def _parse_boc_page(
    text: str,
    *,
    physical_page: int,
    document_page_count: int,
    first_sequence: int,
) -> _BocPage:
    lines = text.splitlines()
    range_match = _unique_line_match(lines, _PAGE_RANGE_RE, "BOC_PAGE_RANGE_HEADER_INVALID")
    totals_match = _unique_line_match(lines, _PAGE_TOTALS_RE, "BOC_PAGE_TOTALS_HEADER_INVALID")
    account_match = _unique_line_match(lines, _ACCOUNT_RE, "BOC_PAGE_ACCOUNT_HEADER_INVALID")
    page_number = int(range_match.group("page"))
    declared_page_count = int(range_match.group("pages"))
    if page_number != physical_page or declared_page_count != document_page_count:
        raise StatementParseError("BOC_PAGE_NUMBER_MISMATCH")
    period_start = _parse_date(
        range_match.group("start"), "%Y-%m-%d", "BOC_PERIOD_DATE_INVALID"
    ).isoformat()
    period_end = _parse_date(
        range_match.group("end"), "%Y-%m-%d", "BOC_PERIOD_DATE_INVALID"
    ).isoformat()
    if period_start > period_end:
        raise StatementParseError("BOC_PERIOD_RANGE_INVALID")
    _parse_datetime(
        account_match.group("printed"),
        "%Y/%m/%d %H:%M:%S",
        "BOC_PRINT_TIME_INVALID",
    )

    header_index, starts = _find_column_header(lines)
    footer_index = _find_footer(lines, header_index + 1)
    body_lines = lines[header_index + 1 : footer_index]
    expected_rows = int(totals_match.group("rows"))
    dated_lines = sum(1 for line in body_lines if _MAIN_LINE_RE.match(line) is not None)
    if dated_lines != expected_rows:
        raise StatementParseError("BOC_PAGE_DATED_LINE_COUNT_MISMATCH")
    transactions = _parse_boc_transactions(
        body_lines,
        starts=starts,
        page_number=page_number,
        first_sequence=first_sequence,
    )
    if len(transactions) != expected_rows:
        raise StatementParseError("BOC_PAGE_ROW_COUNT_MISMATCH")

    expected_debit = _money_minor(totals_match.group("debit"), "BOC_PAGE_TOTAL_INVALID")
    expected_credit = _money_minor(totals_match.group("credit"), "BOC_PAGE_TOTAL_INVALID")
    summary = StatementPageSummary(
        page_number=page_number,
        row_count=expected_rows,
        debit_total_minor=expected_debit,
        credit_total_minor=expected_credit,
    )
    return _BocPage(
        page_number=page_number,
        page_count=declared_page_count,
        period_start=period_start,
        period_end=period_end,
        account_name=_collapse_spaces(range_match.group("name")),
        card_number=totals_match.group("card"),
        account_number=account_match.group("account"),
        transactions=transactions,
        summary=summary,
    )


def _parse_boc_transactions(
    lines: list[str],
    *,
    starts: tuple[int, ...],
    page_number: int,
    first_sequence: int,
) -> tuple[StatementTransaction, ...]:
    records: list[list[str]] = []
    for line in lines:
        if not line.strip():
            continue
        main_match = _MAIN_LINE_RE.match(line)
        if main_match is not None:
            cells = [
                main_match.group("date"),
                main_match.group("time"),
                "人民币",
                main_match.group("amount"),
                main_match.group("balance"),
                *_slice_columns(line, starts[5:]),
            ]
            records.append(cells)
            continue
        if not records:
            raise StatementParseError("BOC_PAGE_BODY_PREFIX_UNRECOGNIZED")
        prefix = line[: starts[5]]
        if _CONTINUATION_FORBIDDEN_RE.search(prefix) is not None:
            raise StatementParseError("BOC_TRANSACTION_CONTINUATION_INVALID")
        text_cells = _slice_columns(line, starts[5:])
        if not any(text_cells):
            continue
        current = records[-1]
        for index, value in enumerate(text_cells, start=5):
            if value:
                current[index] = _join_parts(current[index], value)

    transactions: list[StatementTransaction] = []
    for offset, cells in enumerate(records):
        try:
            booked = datetime.strptime(f"{cells[0]} {cells[1]}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise StatementParseError("BOC_TRANSACTION_DATETIME_INVALID") from None
        text_values = [_collapse_spaces(value) for value in cells[5:]]
        transaction = StatementTransaction(
            sequence=first_sequence + offset,
            bank_transaction_id="",
            submitted_at="",
            booked_at=booked.isoformat(timespec="seconds"),
            amount_minor=_money_minor(cells[3], "BOC_TRANSACTION_AMOUNT_INVALID"),
            balance_minor=_money_minor(cells[4], "BOC_TRANSACTION_BALANCE_INVALID"),
            currency="CNY",
            transaction_name=text_values[0],
            channel=text_values[1],
            branch_name=text_values[2],
            note=text_values[3],
            counterparty_name=text_values[4],
            counterparty_account=text_values[5],
            counterparty_bank=text_values[6],
            source_page=page_number,
        )
        transactions.append(transaction)
    return tuple(transactions)


def _correct_amount_signs_by_balance(
    transactions: tuple[StatementTransaction, ...],
) -> tuple[StatementTransaction, ...]:
    """Correct only PDF-extraction sign errors using the reverse-order balance chain."""

    if not transactions:
        return transactions
    corrected: list[StatementTransaction] = []
    for current, older in pairwise(transactions):
        balance_delta = current.balance_minor - older.balance_minor
        if abs(balance_delta) != abs(current.amount_minor):
            raise StatementParseError("BOC_BALANCE_CHAIN_AMOUNT_MISMATCH")
        corrected.append(
            current
            if current.amount_minor == balance_delta
            else replace(current, amount_minor=balance_delta)
        )
    corrected.append(transactions[-1])
    return tuple(corrected)


def _validate_boc_page_totals(
    transactions: tuple[StatementTransaction, ...],
    pages: list[_BocPage],
) -> None:
    by_page: dict[int, list[StatementTransaction]] = {}
    for transaction in transactions:
        by_page.setdefault(transaction.source_page, []).append(transaction)
    for page in pages:
        page_transactions = by_page.get(page.page_number, [])
        if len(page_transactions) != page.summary.row_count:
            raise StatementParseError("BOC_PAGE_ROW_COUNT_MISMATCH")
        actual_debit = sum(
            -item.amount_minor for item in page_transactions if item.amount_minor < 0
        )
        actual_credit = sum(
            item.amount_minor for item in page_transactions if item.amount_minor > 0
        )
        if actual_debit != page.summary.debit_total_minor:
            raise StatementParseError("BOC_PAGE_DEBIT_TOTAL_MISMATCH")
        if actual_credit != page.summary.credit_total_minor:
            raise StatementParseError("BOC_PAGE_CREDIT_TOTAL_MISMATCH")


def _validate_boc_transactions(
    transactions: tuple[StatementTransaction, ...],
    *,
    period_start: str,
    period_end: str,
) -> None:
    start_date = date.fromisoformat(period_start)
    end_date = date.fromisoformat(period_end)
    previous_booked: datetime | None = None
    seen: set[tuple[object, ...]] = set()
    for transaction in transactions:
        booked = datetime.fromisoformat(transaction.booked_at)
        if not start_date <= booked.date() <= end_date:
            raise StatementParseError("BOC_TRANSACTION_OUTSIDE_STATEMENT_PERIOD")
        if previous_booked is not None and booked > previous_booked:
            raise StatementParseError("BOC_TRANSACTION_ORDER_INVALID")
        previous_booked = booked
        identity: tuple[object, ...] = (
            transaction.booked_at,
            transaction.amount_minor,
            transaction.balance_minor,
            transaction.currency,
            transaction.transaction_name,
            transaction.channel,
            transaction.branch_name,
            transaction.note,
            transaction.counterparty_name,
            transaction.counterparty_account,
            transaction.counterparty_bank,
        )
        if identity in seen:
            raise StatementParseError("BOC_DUPLICATE_TRANSACTION")
        seen.add(identity)


def _validate_mybank_headers(
    rows: list[tuple[object, ...]],
    *,
    company_name: str,
    account_number: str,
) -> None:
    expected_title = (MYBANK_TITLE, *([None] * 10))
    expected_identity = (
        "企业名称",
        company_name,
        None,
        None,
        "企业账号",
        f"{account_number}(人民币)",
        *([None] * 5),
    )
    if rows[0] != expected_title or rows[1] != expected_identity:
        raise StatementParseError("MYBANK_HEADER_IDENTITY_MISMATCH")
    if rows[4] != _MYBANK_HEADERS:
        raise StatementParseError("MYBANK_COLUMN_HEADER_INVALID")


def _parse_mybank_summary_row(
    row: tuple[object, ...],
    *,
    count_label: str,
    amount_label: str,
) -> tuple[int, int]:
    if (
        len(row) != 11
        or row[0] != count_label
        or row[4] != amount_label
        or any(row[index] is not None for index in (2, 3, 6, 7, 8, 9, 10))
        or not isinstance(row[1], str)
        or not isinstance(row[5], str)
    ):
        raise StatementParseError("MYBANK_SUMMARY_SCHEMA_INVALID")
    count_match = _MYBANK_COUNT_RE.fullmatch(row[1])
    amount_match = _MYBANK_SUMMARY_MONEY_RE.fullmatch(row[5])
    if count_match is None or amount_match is None:
        raise StatementParseError("MYBANK_SUMMARY_SCHEMA_INVALID")
    amount_text = amount_match.group("amount")
    amount_minor = (
        0 if amount_text == "0" else _money_minor(amount_text, "MYBANK_SUMMARY_SCHEMA_INVALID")
    )
    return int(count_match.group("count")), amount_minor


def _parse_mybank_transaction(
    row: tuple[object, ...],
    *,
    sequence: int,
    statement_date: date,
) -> StatementTransaction:
    if len(row) != 11 or all(value is None for value in row):
        raise StatementParseError("MYBANK_TRANSACTION_ROW_INVALID")
    transaction_id = _mybank_required_text(row[0], "MYBANK_TRANSACTION_ID_INVALID")
    submitted = _mybank_datetime(row[1], "MYBANK_SUBMITTED_AT_INVALID")
    booked = _mybank_datetime(row[2], "MYBANK_BOOKED_AT_INVALID")
    if submitted.date() != statement_date or booked.date() != statement_date:
        raise StatementParseError("MYBANK_TRANSACTION_DATE_MISMATCH")
    debit = _mybank_optional_money(row[4])
    credit = _mybank_optional_money(row[5])
    if (debit is None) == (credit is None):
        raise StatementParseError("MYBANK_TRANSACTION_DIRECTION_INVALID")
    if debit is not None:
        if debit <= 0:
            raise StatementParseError("MYBANK_TRANSACTION_AMOUNT_INVALID")
        amount_minor = debit
    else:
        if credit is None or credit <= 0:
            raise StatementParseError("MYBANK_TRANSACTION_AMOUNT_INVALID")
        amount_minor = -credit
    return StatementTransaction(
        sequence=sequence,
        bank_transaction_id=transaction_id,
        submitted_at=submitted.isoformat(timespec="seconds"),
        booked_at=booked.isoformat(timespec="seconds"),
        amount_minor=amount_minor,
        balance_minor=_mybank_required_money(row[6], "MYBANK_TRANSACTION_BALANCE_INVALID"),
        currency="CNY",
        transaction_name=_mybank_required_text(row[3], "MYBANK_TRANSACTION_NAME_INVALID"),
        channel="",
        branch_name="",
        note=_mybank_optional_text(row[10]),
        counterparty_name=_mybank_optional_text(row[7]),
        counterparty_account=_mybank_optional_text(row[8]),
        counterparty_bank=_mybank_optional_text(row[9]),
        source_page=1,
    )


def _validate_mybank_transactions(
    transactions: tuple[StatementTransaction, ...],
    *,
    statement_date: str,
    debit_count: int,
    debit_total_minor: int,
    credit_count: int,
    credit_total_minor: int,
) -> None:
    _validate_boc_transactions(
        transactions,
        period_start=statement_date,
        period_end=statement_date,
    )
    transaction_ids: set[str] = set()
    for current, older in pairwise(transactions):
        if current.balance_minor - older.balance_minor != current.amount_minor:
            raise StatementParseError("MYBANK_BALANCE_CHAIN_MISMATCH")
    for transaction in transactions:
        if transaction.bank_transaction_id in transaction_ids:
            raise StatementParseError("MYBANK_DUPLICATE_TRANSACTION_ID")
        transaction_ids.add(transaction.bank_transaction_id)
    actual_debits = [item.amount_minor for item in transactions if item.amount_minor > 0]
    actual_credits = [-item.amount_minor for item in transactions if item.amount_minor < 0]
    if len(actual_debits) != debit_count or sum(actual_debits) != debit_total_minor:
        raise StatementParseError("MYBANK_DEBIT_SUMMARY_MISMATCH")
    if len(actual_credits) != credit_count or sum(actual_credits) != credit_total_minor:
        raise StatementParseError("MYBANK_CREDIT_SUMMARY_MISMATCH")


def _mybank_required_text(value: object, error_code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StatementParseError(error_code)
    return _collapse_spaces(value)


def _mybank_optional_text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise StatementParseError("MYBANK_TRANSACTION_TEXT_INVALID")
    return _collapse_spaces(value)


def _mybank_datetime(value: object, error_code: str) -> datetime:
    if not isinstance(value, str):
        raise StatementParseError(error_code)
    return _parse_datetime(value, "%Y-%m-%d %H:%M:%S", error_code)


def _mybank_optional_money(value: object) -> int | None:
    if value is None:
        return None
    return _mybank_required_money(value, "MYBANK_TRANSACTION_AMOUNT_INVALID")


def _mybank_required_money(value: object, error_code: str) -> int:
    if not isinstance(value, str):
        raise StatementParseError(error_code)
    return _money_minor(value, error_code)


def _validate_mybank_worksheet_xml(content: bytes) -> int:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_MYBANK_XLSX_MEMBERS:
                raise StatementParseError("MYBANK_XLSX_ARCHIVE_LIMIT_EXCEEDED")
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise StatementParseError("MYBANK_XLSX_ARCHIVE_SCHEMA_INVALID")
            total_size = 0
            for member in members:
                if member.flag_bits & 0x1:
                    raise StatementParseError("MYBANK_XLSX_ARCHIVE_SCHEMA_INVALID")
                if member.file_size > MAX_MYBANK_XLSX_MEMBER_BYTES:
                    raise StatementParseError("MYBANK_XLSX_ARCHIVE_LIMIT_EXCEEDED")
                total_size += member.file_size
                if total_size > MAX_MYBANK_XLSX_UNCOMPRESSED_BYTES:
                    raise StatementParseError("MYBANK_XLSX_ARCHIVE_LIMIT_EXCEEDED")
            worksheet_entries = [
                name
                for name in names
                if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name) is not None
            ]
            if worksheet_entries != ["xl/worksheets/sheet1.xml"]:
                raise StatementParseError("MYBANK_WORKBOOK_SCHEMA_UNRECOGNIZED")
            worksheet_xml = _read_zip_member_bounded(
                archive,
                archive.getinfo(worksheet_entries[0]),
            )
    except StatementParseError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
        raise StatementParseError("MYBANK_XLSX_INVALID") from None

    data_cells: list[str] = []
    data_max_row = 0
    try:
        for _event, element in ElementTree.iterparse(io.BytesIO(worksheet_xml), events=("end",)):
            if _xml_local_name(element.tag) != "c":
                element.clear()
                continue
            cell_reference = element.attrib.get("r")
            child_names = {_xml_local_name(child.tag) for child in element}
            if "f" in child_names:
                raise StatementParseError("MYBANK_FORMULA_NOT_ALLOWED")
            has_value = bool(child_names.intersection({"v", "is"}))
            has_string_type = element.attrib.get("t") in {"s", "str", "inlineStr"}
            if has_value or has_string_type:
                cell_match = (
                    re.fullmatch(r"(?P<column>[A-Z]{1,3})(?P<row>[1-9]\d*)", cell_reference)
                    if cell_reference is not None
                    else None
                )
                if cell_match is None:
                    raise StatementParseError("MYBANK_WORKSHEET_SCHEMA_UNRECOGNIZED")
                row_number = int(cell_match.group("row"))
                if cell_match.group("column") not in tuple("ABCDEFGHIJK"):
                    raise StatementParseError("MYBANK_WORKSHEET_SCHEMA_UNRECOGNIZED")
                if row_number > MAX_MYBANK_ROWS:
                    raise StatementParseError("MYBANK_XLSX_ARCHIVE_LIMIT_EXCEEDED")
                data_cells.append(cell_match.group(0))
                data_max_row = max(data_max_row, row_number)
                if len(data_cells) > MAX_MYBANK_DATA_CELLS:
                    raise StatementParseError("MYBANK_XLSX_ARCHIVE_LIMIT_EXCEEDED")
            element.clear()
    except ElementTree.ParseError:
        raise StatementParseError("MYBANK_XLSX_INVALID") from None
    if not data_cells or data_cells[0] != "A1" or len(data_cells) != len(set(data_cells)):
        raise StatementParseError("MYBANK_WORKSHEET_SCHEMA_UNRECOGNIZED")
    return data_max_row


def _read_zip_member_bounded(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        with archive.open(member, "r") as stream:
            for chunk in _bounded_chunks(stream, MAX_MYBANK_XLSX_MEMBER_BYTES):
                chunks.append(chunk)
                total += len(chunk)
    except (OSError, RuntimeError, zipfile.BadZipFile):
        raise StatementParseError("MYBANK_XLSX_INVALID") from None
    if total != member.file_size:
        raise StatementParseError("MYBANK_XLSX_INVALID")
    return b"".join(chunks)


def _bounded_chunks(stream: Any, maximum: int) -> Iterator[bytes]:
    total = 0
    while total <= maximum:
        chunk = stream.read(min(64 * 1024, maximum + 1 - total))
        if not isinstance(chunk, bytes):
            raise StatementParseError("MYBANK_XLSX_INVALID")
        if not chunk:
            return
        total += len(chunk)
        yield chunk
    raise StatementParseError("MYBANK_XLSX_ARCHIVE_LIMIT_EXCEEDED")


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_column_header(lines: list[str]) -> tuple[int, tuple[int, ...]]:
    matches: list[tuple[int, tuple[int, ...]]] = []
    for line_index, line in enumerate(lines):
        starts: list[int] = []
        cursor = 0
        for header in _COLUMN_HEADERS:
            position = line.find(header, cursor)
            if position < 0:
                break
            starts.append(position)
            cursor = position + len(header)
        if len(starts) == len(_COLUMN_HEADERS):
            cells = _slice_columns(line, tuple(starts))
            if tuple(cells) == _COLUMN_HEADERS:
                matches.append((line_index, tuple(starts)))
    if len(matches) != 1:
        raise StatementParseError("BOC_COLUMN_HEADER_INVALID")
    return matches[0]


def _slice_columns(line: str, starts: tuple[int, ...]) -> list[str]:
    cells: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else None
        cells.append(line[start:end].strip())
    return cells


def _find_footer(lines: list[str], start: int) -> int:
    matches = [
        index
        for index in range(start, len(lines))
        if lines[index].strip() == "END" or lines[index].strip().startswith("温馨提示")
    ]
    if not matches:
        raise StatementParseError("BOC_PAGE_FOOTER_MISSING")
    return matches[0]


def _unique_line_match(
    lines: list[str],
    pattern: re.Pattern[str],
    error_code: str,
) -> re.Match[str]:
    matches = [match for line in lines if (match := pattern.fullmatch(line)) is not None]
    if len(matches) != 1:
        raise StatementParseError(error_code)
    return matches[0]


def _money_minor(value: str, error_code: str) -> int:
    try:
        amount = Decimal(value.replace(",", ""))
    except (InvalidOperation, ValueError):
        raise StatementParseError(error_code) from None
    if not amount.is_finite() or amount.as_tuple().exponent != -2:
        raise StatementParseError(error_code)
    return int(amount * 100)


def _optional_module(name: str, error_code: str) -> Any:
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        raise StatementDependencyError(error_code) from None


def _basename(source_filename: str) -> str:
    if not isinstance(source_filename, str) or not source_filename.strip():
        raise StatementParseError("SOURCE_FILENAME_INVALID")
    return PurePosixPath(source_filename.replace("\\", "/")).name


def _parse_date(value: str, format_string: str, error_code: str) -> date:
    try:
        return datetime.strptime(value, format_string).date()
    except ValueError:
        raise StatementParseError(error_code) from None


def _parse_datetime(value: str, format_string: str, error_code: str) -> datetime:
    try:
        return datetime.strptime(value, format_string)
    except ValueError:
        raise StatementParseError(error_code) from None


def _collapse_spaces(value: str) -> str:
    return " ".join(value.split())


def _join_parts(left: str, right: str) -> str:
    if not left:
        return right
    return f"{left} {right}"
