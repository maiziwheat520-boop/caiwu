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
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Any

MYBANK_TITLE = "浙江网商银行企业账户交易明细"
MYBANK_PARSER_VERSION = "mybank-xlsx-v1"
BOC_PARSER_VERSION = "boc-pdf-layout-v1"

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
    """Parse the verified empty MYBANK daily-statement XLSX schema.

    The currently observed export contains exactly one worksheet and one
    non-empty title cell.  A future non-empty transaction schema is rejected
    until it has its own reviewed parser instead of being guessed here.
    """

    if not isinstance(content, bytes) or not content:
        raise StatementParseError("MYBANK_XLSX_EMPTY")
    if not isinstance(company_name, str) or not company_name.strip():
        raise StatementParseError("MYBANK_COMPANY_NAME_INVALID")
    basename = _basename(source_filename)
    match = _MYBANK_FILENAME_RE.fullmatch(basename)
    if match is None:
        raise StatementParseError("MYBANK_FILENAME_SCHEMA_UNRECOGNIZED")
    statement_date = _parse_date(match.group("date"), "%Y%m%d", "MYBANK_FILENAME_DATE_INVALID")

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
        non_empty: list[tuple[str, object]] = []
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is not None:
                    non_empty.append((cell.coordinate, cell.value))
        if non_empty != [("A1", MYBANK_TITLE)]:
            raise StatementParseError("MYBANK_WORKSHEET_SCHEMA_UNRECOGNIZED")
    finally:
        workbook.close()

    iso_date = statement_date.isoformat()
    summary = StatementPageSummary(
        page_number=1,
        row_count=0,
        debit_total_minor=0,
        credit_total_minor=0,
    )
    return ParsedStatement(
        bank_code="MYBANK",
        source="mybank_daily_statement",
        parser_version=MYBANK_PARSER_VERSION,
        source_filename=basename,
        account_number=match.group("account"),
        account_name=_collapse_spaces(company_name),
        period_start=iso_date,
        period_end=iso_date,
        currency="CNY",
        transactions=(),
        debit_total_minor=0,
        credit_total_minor=0,
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
