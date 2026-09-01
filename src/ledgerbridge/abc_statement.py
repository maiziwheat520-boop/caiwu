"""Fail-closed parser adapter for unlocked ABC personal-account PDF exports."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Final
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from ledgerbridge.bank_statement_contract import (
    ABC_PERSONAL_PDF_V1,
    BankStatement,
    BankStatementParserProfile,
    BankStatementTransaction,
)
from ledgerbridge.text import contains_unstorable_text

_NAMESPACE: Final = UUID("bf7687d8-7046-46bf-a1a2-ae0f005d153c")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")
_ACCOUNT_NUMBER = re.compile(r"^[0-9]{12,30}$")
_DATE = re.compile(r"^[0-9]{8}$")
_TIME = re.compile(r"^[0-9]{6}$")
_LOG_NUMBER = re.compile(r"^[0-9A-Za-z]{10}$")
_MONEY = re.compile(r"^[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)\.[0-9]{2}$")
_COUNTERPARTY_ACCOUNT = re.compile(r"(?<![0-9])[0-9]{8,30}(?![0-9])")
_OWNER_ACCOUNT_RE = re.compile(
    r"^\s*户名\s*[:\uff1a]\s*(?P<owner>.+?)\s+账户\s*[:\uff1a]\s*"
    r"(?P<account>[0-9]+)\s*$"
)
_CURRENCY_RE = re.compile(
    r"^\s*币种\s*[:\uff1a]\s*(?P<currency>.+?)\s+汇钞标识\s*[:\uff1a]\s*"
    r"(?P<cash>.+?)\s*$"
)
_PERIOD_RE = re.compile(
    r"^\s*起止日期\s*[:\uff1a]\s*(?P<start>[0-9]{8})\s*-\s*(?P<end>[0-9]{8})"
    r"\s+电子流水号\s*[:\uff1a]\s*(?P<serial>[0-9A-Za-z]{8,64})\s*$"
)
_PAGE_MARKER_RE = re.compile(
    r"^\s*第\s*(?P<page>[0-9]+)\s*页\s*[\uff0c,]?\s*共\s*"
    r"(?P<pages>[0-9]+)\s*页\s*$"
)
_EXPECTED_TITLE: Final = "中国农业银行账户活期交易明细清单"
_EXPECTED_HEADERS: Final = (
    "交易日期",
    "交易时间",
    "交易摘要",
    "交易金额",
    "本次余额",
    "对手信息",
    "日 志 号",
    "交易渠道",
    "交易附言",
)
_SHANGHAI: Final = ZoneInfo("Asia/Shanghai")
_MAX_FILE_BYTES = 20 * 1024 * 1024
_MAX_PAGE_TEXT = 4 * 1024 * 1024
_MAX_PAGES = 500
_MAX_ROWS = 100_000
_MAX_TEXT = 300
_MAX_SAFE_MINOR = 9_007_199_254_740_991


class AbcStatementError(RuntimeError):
    """The source file could not prove a valid ABC personal statement."""


@dataclass(frozen=True, slots=True)
class _PageMetadata:
    owner: str
    account_number: str
    currency_marker: str
    cash_marker: str
    period_start: date
    period_end: date
    electronic_serial: str


@dataclass(frozen=True, slots=True)
class _ParsedRow:
    sequence: int
    occurred_at: datetime
    amount_minor: int
    balance_minor: int
    summary: str
    counterparty_name: str
    counterparty_account: str
    log_number: str
    channel: str
    note: str
    source_page: int
    source_line: int
    row_sha256: str
    fact_sha256: str


@dataclass(frozen=True, slots=True)
class _ParsedPage:
    metadata: _PageMetadata
    page_number: int
    page_count: int
    transactions: tuple[_ParsedRow, ...]


def parse_abc_personal_pdf(
    source_path: Path,
    *,
    expected_sha256: str,
    managed_account_suffix: str,
) -> BankStatement:
    """Parse one digest-bound, already-unlocked ABC personal statement PDF."""

    if _DIGEST.fullmatch(expected_sha256) is None:
        raise AbcStatementError("expected source digest is invalid")
    if _ACCOUNT_SUFFIX.fullmatch(managed_account_suffix) is None:
        raise AbcStatementError("managed account suffix is invalid")
    raw = _read_source(source_path)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if source_sha256 != expected_sha256:
        raise AbcStatementError("source digest changed")
    if not raw.startswith(b"%PDF-"):
        raise AbcStatementError("statement is not a PDF")

    reader = _open_unlocked_pdf(raw)
    try:
        page_count = len(reader.pages)
    except Exception:
        raise AbcStatementError("statement pages cannot be read") from None
    if page_count < 1 or page_count > _MAX_PAGES:
        raise AbcStatementError("statement page count is invalid")

    pages: list[_ParsedPage] = []
    sequence = 1
    for page_index in range(page_count):
        try:
            text = reader.pages[page_index].extract_text(extraction_mode="layout")
        except TypeError:
            raise AbcStatementError("PDF layout extraction is unavailable") from None
        except Exception:
            raise AbcStatementError("statement page text cannot be extracted") from None
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > _MAX_PAGE_TEXT
            or contains_unstorable_text(text)
        ):
            raise AbcStatementError("statement page text is invalid")
        page = _parse_page(
            text,
            physical_page=page_index + 1,
            document_page_count=page_count,
            first_sequence=sequence,
        )
        pages.append(page)
        sequence += len(page.transactions)

    first = pages[0]
    account_number = first.metadata.account_number
    if _ACCOUNT_NUMBER.fullmatch(account_number) is None or not account_number.endswith(
        managed_account_suffix
    ):
        raise AbcStatementError("statement does not belong to the managed account")
    for page in pages[1:]:
        if page.metadata != first.metadata or page.page_count != first.page_count:
            raise AbcStatementError("statement page metadata is inconsistent")

    transactions = tuple(item for page in pages for item in page.transactions)
    if not transactions or len(transactions) > _MAX_ROWS:
        raise AbcStatementError("statement transaction count is invalid")
    _validate_transactions(
        transactions,
        period_start=first.metadata.period_start,
        period_end=first.metadata.period_end,
    )

    generic_transactions = tuple(
        _to_generic_transaction(item, source_sha256=source_sha256) for item in transactions
    )
    months = Counter(item.occurred_at.strftime("%Y-%m") for item in transactions)
    log_set_sha256 = _set_digest(
        f"{item.sequence}:{hashlib.sha256(item.log_number.encode('ascii')).hexdigest()}"
        for item in transactions
    )
    parser_facts_sha256 = hashlib.sha256(
        json.dumps(
            {
                "account_number_sha256": hashlib.sha256(account_number.encode("ascii")).hexdigest(),
                "cash_marker_sha256": hashlib.sha256(
                    first.metadata.cash_marker.encode("utf-8")
                ).hexdigest(),
                "currency_marker_sha256": hashlib.sha256(
                    first.metadata.currency_marker.encode("utf-8")
                ).hexdigest(),
                "declared_period_end": first.metadata.period_end.isoformat(),
                "declared_period_start": first.metadata.period_start.isoformat(),
                "document_page_count": page_count,
                "electronic_serial_sha256": hashlib.sha256(
                    first.metadata.electronic_serial.encode("ascii")
                ).hexdigest(),
                "log_set_sha256": log_set_sha256,
                "monthly_transaction_counts": sorted(months.items()),
                "owner_sha256": hashlib.sha256(first.metadata.owner.encode("utf-8")).hexdigest(),
                "page_transaction_counts": [
                    (page.page_number, len(page.transactions)) for page in pages
                ],
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return BankStatement(
        statement_ref=uuid5(
            _NAMESPACE,
            (
                f"abc-statement:{source_sha256}:"
                f"{first.metadata.period_start}:{first.metadata.period_end}"
            ),
        ),
        source_sha256=source_sha256,
        source_size=len(raw),
        declared_media_type=ABC_PERSONAL_PDF_V1.declared_media_type,
        currency="CNY",
        institution_code=ABC_PERSONAL_PDF_V1.institution_code,
        account_suffix=managed_account_suffix,
        worksheet_index=0,
        header_row_number=0,
        transactions=generic_transactions,
        parser_profile=BankStatementParserProfile.ABC_PERSONAL_PDF_V1,
        source_system=ABC_PERSONAL_PDF_V1.source_system,
        parser_facts_sha256=parser_facts_sha256,
    )


def _read_source(path: Path) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise AbcStatementError("statement path must be absolute")
    try:
        metadata = path.lstat()
    except OSError:
        raise AbcStatementError("statement file is unavailable") from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AbcStatementError("statement file must be regular")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_FILE_BYTES:
        raise AbcStatementError("statement file size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise AbcStatementError("statement file cannot be opened") from None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(_MAX_FILE_BYTES + 1)
    except OSError:
        raise AbcStatementError("statement file cannot be read") from None
    if len(raw) != metadata.st_size or len(raw) > _MAX_FILE_BYTES:
        raise AbcStatementError("statement file changed while reading")
    return raw


def _open_unlocked_pdf(raw: bytes) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception:
        raise AbcStatementError("statement PDF is invalid") from None
    if bool(reader.is_encrypted):
        raise AbcStatementError("statement PDF must already be unlocked")
    return reader


def _parse_page(
    text: str,
    *,
    physical_page: int,
    document_page_count: int,
    first_sequence: int,
) -> _ParsedPage:
    lines = text.splitlines()
    if not lines or lines[0].strip() != _EXPECTED_TITLE:
        raise AbcStatementError("statement institution and export type are not proven")
    owner_match = _unique_line_match(lines, _OWNER_ACCOUNT_RE, "statement owner is invalid")
    currency_match = _unique_line_match(lines, _CURRENCY_RE, "statement currency is invalid")
    period_match = _unique_line_match(lines, _PERIOD_RE, "statement period is invalid")
    marker_match = _unique_line_match(lines, _PAGE_MARKER_RE, "statement page marker is invalid")

    page_number = int(marker_match.group("page"))
    page_count = int(marker_match.group("pages"))
    if page_number != physical_page or page_count != document_page_count:
        raise AbcStatementError("statement page number is invalid")
    period_start = _parse_date(period_match.group("start"))
    period_end = _parse_date(period_match.group("end"))
    if period_start > period_end:
        raise AbcStatementError("statement period is invalid")
    currency = _bounded_text(currency_match.group("currency"), required=True)
    if currency not in {"CNY", "人民币", "人民币元"}:
        raise AbcStatementError("statement currency is not CNY")
    metadata = _PageMetadata(
        owner=_bounded_text(owner_match.group("owner"), required=True),
        account_number=owner_match.group("account"),
        currency_marker=currency,
        cash_marker=_bounded_text(currency_match.group("cash"), required=True),
        period_start=period_start,
        period_end=period_end,
        electronic_serial=period_match.group("serial"),
    )

    header_index, starts = _find_header(lines)
    marker_indices = [
        index for index, line in enumerate(lines) if _PAGE_MARKER_RE.fullmatch(line) is not None
    ]
    marker_index = marker_indices[0]
    if marker_index <= header_index:
        raise AbcStatementError("statement page structure is invalid")
    transactions = _parse_rows(
        lines[header_index + 1 : marker_index],
        starts=starts,
        page_number=page_number,
        first_sequence=first_sequence,
        first_source_line=header_index + 2,
    )
    if not transactions:
        raise AbcStatementError("statement page contains no transactions")
    return _ParsedPage(
        metadata=metadata,
        page_number=page_number,
        page_count=page_count,
        transactions=transactions,
    )


def _parse_rows(
    lines: list[str],
    *,
    starts: tuple[int, ...],
    page_number: int,
    first_sequence: int,
    first_source_line: int,
) -> tuple[_ParsedRow, ...]:
    records: list[tuple[int, list[str]]] = []
    footer_started = False
    for offset, line in enumerate(lines):
        if not line.strip():
            continue
        cells = _slice_columns(line, starts)
        is_transaction = _DATE.fullmatch(cells[0]) is not None
        if is_transaction:
            if footer_started:
                raise AbcStatementError("statement transaction appears after footer")
            records.append((first_source_line + offset, list(cells)))
            continue
        populated = tuple(index for index, value in enumerate(cells) if value)
        if records and not footer_started and populated == (5,):
            source_line, current = records[-1]
            current[5] = _join_parts(current[5], cells[5])
            records[-1] = source_line, current
            continue
        footer_started = True

    transactions: list[_ParsedRow] = []
    for offset, (source_line, record_cells) in enumerate(records):
        if len(record_cells) != len(_EXPECTED_HEADERS):
            raise AbcStatementError("statement transaction width is invalid")
        occurred_on = _parse_date(record_cells[0])
        occurred_time = _parse_time(record_cells[1])
        summary = _bounded_text(record_cells[2], required=True)
        amount_minor = _money_minor(record_cells[3], field="amount")
        balance_minor = _money_minor(record_cells[4], field="balance")
        counterparty_name, counterparty_account = _split_counterparty(record_cells[5])
        log_number = record_cells[6]
        if _LOG_NUMBER.fullmatch(log_number) is None:
            raise AbcStatementError("statement transaction log number is invalid")
        channel = _bounded_text(record_cells[7], required=False)
        note = _bounded_text(record_cells[8], required=False)
        occurred_at = datetime.combine(occurred_on, occurred_time, tzinfo=_SHANGHAI)
        canonical_cells = (
            record_cells[0],
            record_cells[1],
            summary,
            amount_minor,
            balance_minor,
            counterparty_name,
            counterparty_account,
            log_number,
            channel,
            note,
        )
        row_sha256 = hashlib.sha256(
            json.dumps(
                (page_number, source_line, canonical_cells),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        fact_sha256 = hashlib.sha256(
            json.dumps(
                (
                    occurred_at.isoformat(),
                    amount_minor,
                    balance_minor,
                    summary,
                    counterparty_name,
                    counterparty_account,
                    log_number,
                    channel,
                    note,
                ),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        transactions.append(
            _ParsedRow(
                sequence=first_sequence + offset,
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                balance_minor=balance_minor,
                summary=summary,
                counterparty_name=counterparty_name,
                counterparty_account=counterparty_account,
                log_number=log_number,
                channel=channel,
                note=note,
                source_page=page_number,
                source_line=source_line,
                row_sha256=row_sha256,
                fact_sha256=fact_sha256,
            )
        )
    return tuple(transactions)


def _validate_transactions(
    transactions: tuple[_ParsedRow, ...],
    *,
    period_start: date,
    period_end: date,
) -> None:
    dates = [item.occurred_at.date() for item in transactions]
    if dates != sorted(dates):
        raise AbcStatementError("statement transactions are not date ordered")
    if dates[0] < period_start or dates[-1] > period_end:
        raise AbcStatementError("statement transaction falls outside declared period")
    if len({item.fact_sha256 for item in transactions}) != len(transactions):
        raise AbcStatementError("statement contains transactions without a unique fact identity")
    for previous, current in pairwise(transactions):
        if previous.balance_minor + current.amount_minor != current.balance_minor:
            raise AbcStatementError("statement balance chain does not reconcile")


def _to_generic_transaction(
    transaction: _ParsedRow,
    *,
    source_sha256: str,
) -> BankStatementTransaction:
    description = " | ".join(
        value for value in (transaction.summary, transaction.channel, transaction.note) if value
    )
    if not description or len(description) > _MAX_TEXT:
        raise AbcStatementError("statement transaction description is invalid")
    return BankStatementTransaction(
        source_event_ref=uuid5(
            _NAMESPACE,
            (f"abc-event:{source_sha256}:{transaction.sequence}:{transaction.row_sha256}"),
        ),
        source_row_number=transaction.sequence,
        source_row_sha256=transaction.row_sha256,
        occurred_at=transaction.occurred_at,
        amount_minor=transaction.amount_minor,
        balance_minor=transaction.balance_minor,
        counterparty_name=transaction.counterparty_name,
        counterparty_account=transaction.counterparty_account,
        counterparty_institution="",
        transaction_serial=f"abc:{transaction.fact_sha256}",
        transaction_name=description,
    )


def _find_header(lines: list[str]) -> tuple[int, tuple[int, ...]]:
    matches: list[tuple[int, tuple[int, ...]]] = []
    for index, line in enumerate(lines):
        starts = tuple(line.find(header) for header in _EXPECTED_HEADERS)
        if all(start >= 0 for start in starts) and tuple(sorted(starts)) == starts:
            cells = _slice_columns(line, starts)
            if cells == _EXPECTED_HEADERS:
                matches.append((index, starts))
    if len(matches) != 1:
        raise AbcStatementError("statement header is missing or ambiguous")
    return matches[0]


def _slice_columns(line: str, starts: tuple[int, ...]) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize(
            "NFKC",
            line[start : starts[index + 1] if index + 1 < len(starts) else None],
        ).strip()
        for index, start in enumerate(starts)
    )


def _split_counterparty(value: str) -> tuple[str, str]:
    normalized = _bounded_text(value, required=False)
    if not normalized or normalized == "-":
        return "", ""
    accounts = list(_COUNTERPARTY_ACCOUNT.finditer(normalized))
    if len(accounts) > 1:
        raise AbcStatementError("statement counterparty account is ambiguous")
    if not accounts:
        return normalized, ""
    account = accounts[0]
    name = _bounded_text(
        f"{normalized[: account.start()]} {normalized[account.end() :]}".strip(" \t|\uff5c/-"),
        required=False,
    )
    return name, account.group(0)


def _unique_line_match(
    lines: list[str],
    pattern: re.Pattern[str],
    message: str,
) -> re.Match[str]:
    matches = [match for line in lines if (match := pattern.fullmatch(line)) is not None]
    if len(matches) != 1:
        raise AbcStatementError(message)
    return matches[0]


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise AbcStatementError("statement date is invalid") from None


def _parse_time(value: str) -> time:
    if not value:
        return time.min
    if _TIME.fullmatch(value) is None:
        raise AbcStatementError("statement transaction time is invalid")
    try:
        return datetime.strptime(value, "%H%M%S").time()
    except ValueError:
        raise AbcStatementError("statement transaction time is invalid") from None


def _money_minor(value: str, *, field: str) -> int:
    if _MONEY.fullmatch(value) is None:
        raise AbcStatementError(f"statement transaction {field} is invalid")
    try:
        decimal_value = Decimal(value.replace(",", ""))
    except InvalidOperation:
        raise AbcStatementError(f"statement transaction {field} is invalid") from None
    minor = decimal_value * 100
    if minor != minor.to_integral_value() or abs(minor) > _MAX_SAFE_MINOR:
        raise AbcStatementError(f"statement transaction {field} is out of range")
    return int(minor)


def _bounded_text(value: str, *, required: bool) -> str:
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()
    if (
        (required and not normalized)
        or len(normalized) > _MAX_TEXT
        or contains_unstorable_text(normalized)
    ):
        raise AbcStatementError("statement text is invalid")
    return normalized


def _join_parts(first: str, second: str) -> str:
    return f"{first} {second}".strip() if first else second


def _set_digest(values: object) -> str:
    return hashlib.sha256("|".join(values).encode("ascii")).hexdigest()  # type: ignore[arg-type]
