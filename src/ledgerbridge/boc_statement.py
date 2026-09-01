"""Fail-closed parser adapter for encrypted BOC personal-account PDF exports."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from ledgerbridge.bank_statement_contract import (
    BOC_PERSONAL_PDF_V1,
    BankStatement,
    BankStatementParserProfile,
    BankStatementTransaction,
)
from ledgerbridge.text import contains_unstorable_text

_NAMESPACE: Final = UUID("ff48b37f-7fb2-4f65-9290-b4c662071c13")
_PASSWORD_REGISTRY_ENV: Final = "LEDGERBRIDGE_BANK_STATEMENT_PASSWORD_REGISTRY"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")
_ACCOUNT_NUMBER = re.compile(r"^[0-9]{12,30}$")
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
_COLUMN_HEADERS: Final = (
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
_SHANGHAI: Final = ZoneInfo("Asia/Shanghai")
_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_PASSWORD_REGISTRY_BYTES = 64 * 1024
_MAX_PAGES = 500
_MAX_ROWS = 100_000
_MAX_PAGE_TEXT = 4 * 1024 * 1024
_MAX_TEXT = 300


class BocStatementError(RuntimeError):
    """The source file could not prove a valid BOC personal statement."""


@dataclass(frozen=True, slots=True)
class _ParsedTransaction:
    sequence: int
    occurred_at: datetime
    amount_minor: int
    balance_minor: int
    transaction_name: str
    channel: str
    branch_name: str
    note: str
    counterparty_name: str
    counterparty_account: str
    counterparty_institution: str
    source_page: int


@dataclass(frozen=True, slots=True)
class _PageSummary:
    page_number: int
    row_count: int
    debit_total_minor: int
    credit_total_minor: int


@dataclass(frozen=True, slots=True)
class _ParsedPage:
    page_number: int
    page_count: int
    period_start: date
    period_end: date
    account_name: str
    card_number: str
    account_number: str
    transactions: tuple[_ParsedTransaction, ...]
    summary: _PageSummary


def parse_boc_personal_pdf(
    source_path: Path,
    *,
    expected_sha256: str,
    managed_account_suffix: str,
) -> BankStatement:
    """Parse one digest-bound encrypted BOC PDF using an external secret registry."""

    if _DIGEST.fullmatch(expected_sha256) is None:
        raise BocStatementError("expected source digest is invalid")
    if _ACCOUNT_SUFFIX.fullmatch(managed_account_suffix) is None:
        raise BocStatementError("managed account suffix is invalid")
    raw = _read_regular_file(source_path, maximum=_MAX_FILE_BYTES, secret=False)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if source_sha256 != expected_sha256:
        raise BocStatementError("source digest changed")
    if not raw.startswith(b"%PDF-"):
        raise BocStatementError("statement is not a PDF")
    password = _password_for_source(source_sha256)
    reader = _open_encrypted_pdf(raw, password)
    try:
        page_count = len(reader.pages)
    except Exception:
        raise BocStatementError("statement pages cannot be read") from None
    if page_count < 1 or page_count > _MAX_PAGES:
        raise BocStatementError("statement page count is invalid")

    pages: list[_ParsedPage] = []
    sequence = 1
    for page_index in range(page_count):
        try:
            text = reader.pages[page_index].extract_text(extraction_mode="layout")
        except TypeError:
            raise BocStatementError("PDF layout extraction is unavailable") from None
        except Exception:
            raise BocStatementError("statement page text cannot be extracted") from None
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > _MAX_PAGE_TEXT
            or contains_unstorable_text(text)
        ):
            raise BocStatementError("statement page text is invalid")
        page = _parse_page(
            text,
            physical_page=page_index + 1,
            document_page_count=page_count,
            first_sequence=sequence,
        )
        pages.append(page)
        sequence += len(page.transactions)

    first = pages[0]
    if _ACCOUNT_NUMBER.fullmatch(first.account_number) is None or not first.account_number.endswith(
        managed_account_suffix
    ):
        raise BocStatementError("statement does not belong to the managed account")
    for page in pages[1:]:
        if (
            page.page_count != first.page_count
            or page.period_start != first.period_start
            or page.period_end != first.period_end
            or page.account_name != first.account_name
            or page.card_number != first.card_number
            or page.account_number != first.account_number
        ):
            raise BocStatementError("statement page metadata is inconsistent")

    extracted = tuple(item for page in pages for item in page.transactions)
    transactions = _correct_amount_signs_by_balance(extracted)
    if not transactions or len(transactions) > _MAX_ROWS:
        raise BocStatementError("statement transaction count is invalid")
    _validate_transactions(
        transactions,
        period_start=first.period_start,
        period_end=first.period_end,
    )
    _validate_page_totals(transactions, pages)
    debit_total = sum(page.summary.debit_total_minor for page in pages)
    credit_total = sum(page.summary.credit_total_minor for page in pages)
    if sum(-item.amount_minor for item in transactions if item.amount_minor < 0) != debit_total:
        raise BocStatementError("statement debit total does not reconcile")
    if sum(item.amount_minor for item in transactions if item.amount_minor > 0) != credit_total:
        raise BocStatementError("statement credit total does not reconcile")

    generic_transactions = tuple(
        _to_generic_transaction(item, source_sha256=source_sha256) for item in transactions
    )
    months = Counter(item.occurred_at.strftime("%Y-%m") for item in transactions)
    parser_facts_sha256 = hashlib.sha256(
        json.dumps(
            {
                "account_name_sha256": hashlib.sha256(
                    first.account_name.encode("utf-8")
                ).hexdigest(),
                "account_number_sha256": hashlib.sha256(
                    first.account_number.encode("ascii")
                ).hexdigest(),
                "card_number_sha256": hashlib.sha256(first.card_number.encode("ascii")).hexdigest(),
                "credit_total_minor": credit_total,
                "debit_total_minor": debit_total,
                "monthly_transaction_counts": sorted(months.items()),
                "page_summaries": [
                    (
                        page.summary.page_number,
                        page.summary.row_count,
                        page.summary.debit_total_minor,
                        page.summary.credit_total_minor,
                    )
                    for page in pages
                ],
                "period_end": first.period_end.isoformat(),
                "period_start": first.period_start.isoformat(),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return BankStatement(
        statement_ref=uuid5(_NAMESPACE, f"boc-statement:{source_sha256}"),
        source_sha256=source_sha256,
        source_size=len(raw),
        declared_media_type=BOC_PERSONAL_PDF_V1.declared_media_type,
        currency="CNY",
        institution_code=BOC_PERSONAL_PDF_V1.institution_code,
        account_suffix=managed_account_suffix,
        worksheet_index=0,
        header_row_number=0,
        transactions=generic_transactions,
        parser_profile=BankStatementParserProfile.BOC_PERSONAL_PDF_V1,
        source_system=BOC_PERSONAL_PDF_V1.source_system,
        parser_facts_sha256=parser_facts_sha256,
    )


def _password_for_source(expected_sha256: str) -> str:
    registry_value = os.environ.get(_PASSWORD_REGISTRY_ENV)
    if not registry_value or registry_value != registry_value.strip():
        raise BocStatementError("statement password registry is unavailable")
    registry_path = Path(registry_value)
    raw = _read_regular_file(
        registry_path,
        maximum=_MAX_PASSWORD_REGISTRY_BYTES,
        secret=True,
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BocStatementError("statement password registry is invalid") from None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise BocStatementError("statement password registry is invalid")
    entries = payload.get("entries")
    if not isinstance(entries, list) or len(entries) > 1_000:
        raise BocStatementError("statement password registry is invalid")
    matches = [
        item
        for item in entries
        if isinstance(item, dict) and item.get("attachment_sha256") == expected_sha256
    ]
    if len(matches) != 1 or matches[0].get("status") != "verified":
        raise BocStatementError("statement password registry has no unique verified match")
    password = matches[0].get("password")
    if (
        not isinstance(password, str)
        or not 0 < len(password) <= 200
        or contains_unstorable_text(password)
    ):
        raise BocStatementError("statement password registry match is invalid")
    return password


def _read_regular_file(path: Path, *, maximum: int, secret: bool) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise BocStatementError("statement input path must be absolute")
    try:
        metadata = path.lstat()
    except OSError:
        raise BocStatementError("statement input file is unavailable") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BocStatementError("statement input file must be regular")
    if metadata.st_size <= 0 or metadata.st_size > maximum:
        raise BocStatementError("statement input file size is invalid")
    if secret and os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise BocStatementError("statement password registry permissions are too broad")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise BocStatementError("statement input file cannot be opened") from None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(maximum + 1)
    except OSError:
        raise BocStatementError("statement input file cannot be read") from None
    if len(raw) != metadata.st_size or len(raw) > maximum:
        raise BocStatementError("statement input file changed while reading")
    return raw


def _open_encrypted_pdf(raw: bytes, password: str) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception:
        raise BocStatementError("statement PDF is invalid") from None
    if not bool(reader.is_encrypted):
        raise BocStatementError("statement PDF must be encrypted")
    try:
        decrypted = reader.decrypt(password)
    except Exception:
        raise BocStatementError("statement PDF cannot be decrypted") from None
    if not decrypted:
        raise BocStatementError("statement PDF cannot be decrypted")
    return reader


def _parse_page(
    text: str,
    *,
    physical_page: int,
    document_page_count: int,
    first_sequence: int,
) -> _ParsedPage:
    lines = text.splitlines()
    range_match = _unique_line_match(lines, _PAGE_RANGE_RE, "statement page range is invalid")
    totals_match = _unique_line_match(lines, _PAGE_TOTALS_RE, "statement page totals are invalid")
    account_match = _unique_line_match(lines, _ACCOUNT_RE, "statement page account is invalid")
    page_number = int(range_match.group("page"))
    declared_page_count = int(range_match.group("pages"))
    if page_number != physical_page or declared_page_count != document_page_count:
        raise BocStatementError("statement page number is invalid")
    period_start = _parse_date(range_match.group("start"), "%Y-%m-%d")
    period_end = _parse_date(range_match.group("end"), "%Y-%m-%d")
    if period_start > period_end:
        raise BocStatementError("statement period is invalid")
    _parse_datetime(account_match.group("printed"), "%Y/%m/%d %H:%M:%S")
    account_name = _bounded_text(range_match.group("name"), required=True)
    card_number = totals_match.group("card")
    account_number = account_match.group("account")
    if (
        _ACCOUNT_NUMBER.fullmatch(card_number) is None
        or _ACCOUNT_NUMBER.fullmatch(account_number) is None
    ):
        raise BocStatementError("statement page account identity is invalid")

    header_index, starts = _find_column_header(lines)
    footer_index = _find_footer(lines, header_index + 1)
    body_lines = lines[header_index + 1 : footer_index]
    expected_rows = int(totals_match.group("rows"))
    if expected_rows < 1 or expected_rows > _MAX_ROWS:
        raise BocStatementError("statement page row count is invalid")
    dated_lines = sum(1 for line in body_lines if _MAIN_LINE_RE.match(line) is not None)
    if dated_lines != expected_rows:
        raise BocStatementError("statement page dated-row count is invalid")
    transactions = _parse_transactions(
        body_lines,
        starts=starts,
        page_number=page_number,
        first_sequence=first_sequence,
    )
    if len(transactions) != expected_rows:
        raise BocStatementError("statement page row count is invalid")
    return _ParsedPage(
        page_number=page_number,
        page_count=declared_page_count,
        period_start=period_start,
        period_end=period_end,
        account_name=account_name,
        card_number=card_number,
        account_number=account_number,
        transactions=transactions,
        summary=_PageSummary(
            page_number=page_number,
            row_count=expected_rows,
            debit_total_minor=_money_minor(totals_match.group("debit")),
            credit_total_minor=_money_minor(totals_match.group("credit")),
        ),
    )


def _parse_transactions(
    lines: list[str],
    *,
    starts: tuple[int, ...],
    page_number: int,
    first_sequence: int,
) -> tuple[_ParsedTransaction, ...]:
    records: list[list[str]] = []
    for line in lines:
        if not line.strip():
            continue
        main_match = _MAIN_LINE_RE.match(line)
        if main_match is not None:
            records.append(
                [
                    main_match.group("date"),
                    main_match.group("time"),
                    "人民币",
                    main_match.group("amount"),
                    main_match.group("balance"),
                    *_slice_columns(line, starts[5:]),
                ]
            )
            continue
        if not records:
            raise BocStatementError("statement page body prefix is invalid")
        prefix = line[: starts[5]]
        if _CONTINUATION_FORBIDDEN_RE.search(prefix) is not None:
            raise BocStatementError("statement transaction continuation is invalid")
        text_cells = _slice_columns(line, starts[5:])
        if not any(text_cells):
            continue
        current = records[-1]
        for index, value in enumerate(text_cells, start=5):
            if value:
                current[index] = _join_parts(current[index], value)

    transactions: list[_ParsedTransaction] = []
    for offset, cells in enumerate(records):
        if len(cells) != len(_COLUMN_HEADERS):
            raise BocStatementError("statement transaction width is invalid")
        occurred_at = _parse_datetime(f"{cells[0]} {cells[1]}", "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_SHANGHAI
        )
        text_values = tuple(_bounded_text(value, required=False) for value in cells[5:])
        if not text_values[0]:
            raise BocStatementError("statement transaction name is missing")
        transactions.append(
            _ParsedTransaction(
                sequence=first_sequence + offset,
                occurred_at=occurred_at,
                amount_minor=_money_minor(cells[3]),
                balance_minor=_money_minor(cells[4]),
                transaction_name=text_values[0],
                channel=text_values[1],
                branch_name=text_values[2],
                note=text_values[3],
                counterparty_name=text_values[4],
                counterparty_account=text_values[5],
                counterparty_institution=text_values[6],
                source_page=page_number,
            )
        )
    return tuple(transactions)


def _correct_amount_signs_by_balance(
    transactions: tuple[_ParsedTransaction, ...],
) -> tuple[_ParsedTransaction, ...]:
    """Correct only layout-extraction sign loss proved by the reverse-order balance chain."""

    if not transactions:
        return transactions
    corrected: list[_ParsedTransaction] = []
    for current, older in pairwise(transactions):
        balance_delta = current.balance_minor - older.balance_minor
        if abs(balance_delta) != abs(current.amount_minor):
            raise BocStatementError("statement balance chain does not reconcile")
        corrected.append(
            current
            if current.amount_minor == balance_delta
            else replace(current, amount_minor=balance_delta)
        )
    corrected.append(transactions[-1])
    return tuple(corrected)


def _validate_page_totals(
    transactions: tuple[_ParsedTransaction, ...], pages: list[_ParsedPage]
) -> None:
    by_page: dict[int, list[_ParsedTransaction]] = {}
    for transaction in transactions:
        by_page.setdefault(transaction.source_page, []).append(transaction)
    for page in pages:
        items = by_page.get(page.page_number, [])
        if len(items) != page.summary.row_count:
            raise BocStatementError("statement page row count does not reconcile")
        if sum(-item.amount_minor for item in items if item.amount_minor < 0) != (
            page.summary.debit_total_minor
        ):
            raise BocStatementError("statement page debit total does not reconcile")
        if sum(item.amount_minor for item in items if item.amount_minor > 0) != (
            page.summary.credit_total_minor
        ):
            raise BocStatementError("statement page credit total does not reconcile")


def _validate_transactions(
    transactions: tuple[_ParsedTransaction, ...],
    *,
    period_start: date,
    period_end: date,
) -> None:
    previous: datetime | None = None
    seen: set[tuple[Any, ...]] = set()
    for transaction in transactions:
        if not period_start <= transaction.occurred_at.date() <= period_end:
            raise BocStatementError("statement transaction is outside the declared period")
        if previous is not None and transaction.occurred_at > previous:
            raise BocStatementError("statement transactions are not reverse-date ordered")
        previous = transaction.occurred_at
        identity = (
            transaction.occurred_at.isoformat(),
            transaction.amount_minor,
            transaction.balance_minor,
            transaction.transaction_name,
            transaction.channel,
            transaction.branch_name,
            transaction.note,
            transaction.counterparty_name,
            transaction.counterparty_account,
            transaction.counterparty_institution,
        )
        if identity in seen:
            raise BocStatementError("statement contains a duplicate transaction fact")
        seen.add(identity)


def _to_generic_transaction(
    transaction: _ParsedTransaction, *, source_sha256: str
) -> BankStatementTransaction:
    row_values = (
        transaction.sequence,
        transaction.source_page,
        transaction.occurred_at.isoformat(),
        transaction.amount_minor,
        transaction.balance_minor,
        transaction.transaction_name,
        transaction.channel,
        transaction.branch_name,
        transaction.note,
        transaction.counterparty_name,
        transaction.counterparty_account,
        transaction.counterparty_institution,
    )
    row_sha256 = hashlib.sha256(
        json.dumps(row_values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    fact_sha256 = hashlib.sha256(
        json.dumps(row_values[2:], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    transaction_name = _combined_transaction_name(transaction)
    return BankStatementTransaction(
        source_event_ref=uuid5(
            _NAMESPACE,
            f"boc-event:{source_sha256}:{transaction.sequence}:{row_sha256}",
        ),
        source_row_number=transaction.sequence,
        source_row_sha256=row_sha256,
        occurred_at=transaction.occurred_at,
        amount_minor=transaction.amount_minor,
        balance_minor=transaction.balance_minor,
        counterparty_name=transaction.counterparty_name,
        counterparty_account=transaction.counterparty_account,
        counterparty_institution=transaction.counterparty_institution,
        transaction_serial=f"boc:{fact_sha256}",
        transaction_name=transaction_name,
    )


def _combined_transaction_name(transaction: _ParsedTransaction) -> str:
    values = tuple(
        value
        for value in (
            transaction.transaction_name,
            transaction.channel,
            transaction.branch_name,
            transaction.note,
        )
        if value
    )
    combined = " | ".join(values)
    if not combined or len(combined) > _MAX_TEXT:
        raise BocStatementError("statement transaction description is too long")
    return combined


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
        raise BocStatementError("statement column header is missing or ambiguous")
    return matches[0]


def _slice_columns(line: str, starts: tuple[int, ...]) -> list[str]:
    return [
        line[start : starts[index + 1] if index + 1 < len(starts) else None].strip()
        for index, start in enumerate(starts)
    ]


def _find_footer(lines: list[str], start: int) -> int:
    matches = [
        index
        for index in range(start, len(lines))
        if lines[index].strip() == "END" or lines[index].strip().startswith("温馨提示")
    ]
    if not matches:
        raise BocStatementError("statement page footer is missing")
    return matches[0]


def _unique_line_match(lines: list[str], pattern: re.Pattern[str], message: str) -> re.Match[str]:
    matches = [match for line in lines if (match := pattern.fullmatch(line)) is not None]
    if len(matches) != 1:
        raise BocStatementError(message)
    return matches[0]


def _money_minor(value: str) -> int:
    try:
        amount = Decimal(value.replace(",", ""))
    except (InvalidOperation, ValueError):
        raise BocStatementError("statement amount is invalid") from None
    if not amount.is_finite() or amount.as_tuple().exponent != -2:
        raise BocStatementError("statement amount is invalid")
    minor = amount * 100
    if abs(minor) > 9_007_199_254_740_991:
        raise BocStatementError("statement amount is out of range")
    return int(minor)


def _parse_date(value: str, format_string: str) -> date:
    try:
        return datetime.strptime(value, format_string).date()
    except ValueError:
        raise BocStatementError("statement date is invalid") from None


def _parse_datetime(value: str, format_string: str) -> datetime:
    try:
        return datetime.strptime(value, format_string)
    except ValueError:
        raise BocStatementError("statement timestamp is invalid") from None


def _bounded_text(value: str, *, required: bool) -> str:
    result = " ".join(value.split())
    if (required and not result) or len(result) > _MAX_TEXT or contains_unstorable_text(result):
        raise BocStatementError("statement text field is invalid")
    return result


def _join_parts(left: str, right: str) -> str:
    return right if not left else f"{left} {right}"
