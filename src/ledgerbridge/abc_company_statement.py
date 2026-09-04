"""Fail-closed parser for Agricultural Bank company account-detail XLS exports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Final
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

import xlrd  # type: ignore[import-untyped]

from ledgerbridge.bank_statement_contract import (
    ABC_COMPANY_XLS_V1,
    BankStatement,
    BankStatementParserProfile,
    BankStatementTransaction,
)
from ledgerbridge.text import contains_unstorable_text

_OLE_MAGIC: Final = bytes.fromhex("D0CF11E0A1B11AE1")
_NAMESPACE: Final = UUID("d671e7d2-3660-4bc0-b7f8-8cc1d3ef731d")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")
_ACCOUNT_NUMBER = re.compile(r"^[0-9]{2}-[0-9]{14,24}$")
_TIME = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$")
_EXPECTED_HEADERS: Final = (
    "交易时间",
    "收入金额",
    "支出金额",
    "账户余额",
    "对方账号",
    "对方户名",
    "对方开户行",
    "摘要",
)
_SHANGHAI: Final = ZoneInfo("Asia/Shanghai")
_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_ROWS = 100_000
_MAX_TEXT = 300


class AbcCompanyStatementError(RuntimeError):
    """The source file could not prove a valid ABC company statement."""


def parse_abc_company_xls(
    source_path: Path, *, expected_sha256: str, managed_account_suffix: str
) -> BankStatement:
    if _DIGEST.fullmatch(expected_sha256) is None:
        raise AbcCompanyStatementError("expected source digest is invalid")
    if _ACCOUNT_SUFFIX.fullmatch(managed_account_suffix) is None:
        raise AbcCompanyStatementError("managed account suffix is invalid")
    raw = _read_source(source_path)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if source_sha256 != expected_sha256:
        raise AbcCompanyStatementError("source digest changed")
    if not raw.startswith(_OLE_MAGIC):
        raise AbcCompanyStatementError("statement is not a legacy Excel container")
    rows = _read_rows(raw)
    if len(rows) < 6 or rows[0][0] != "账户明细" or rows[2] != _EXPECTED_HEADERS:
        raise AbcCompanyStatementError("statement export shape is not proven")
    account_number = _metadata(rows[1][0], "账号")
    account_holder = _metadata(rows[1][1], "户名")
    if _metadata(rows[1][2], "币种") != "人民币":
        raise AbcCompanyStatementError("statement currency is not proven")
    period_start, period_end = _period(_metadata(rows[1][5], "起止日期"))
    if _ACCOUNT_NUMBER.fullmatch(account_number) is None or not account_number.replace(
        "-", ""
    ).endswith(managed_account_suffix):
        raise AbcCompanyStatementError("statement does not belong to the managed account")
    if not account_holder or len(account_holder) > _MAX_TEXT:
        raise AbcCompanyStatementError("statement account holder is invalid")

    transaction_rows: list[tuple[int, tuple[object, ...]]] = []
    footer_index = 3
    while footer_index < len(rows) and _TIME.fullmatch(str(rows[footer_index][0])):
        transaction_rows.append((footer_index + 1, rows[footer_index]))
        footer_index += 1
    if not transaction_rows or footer_index + 1 != len(rows) - 1:
        raise AbcCompanyStatementError("statement footer or transaction range is invalid")
    if rows[footer_index][:4] != ("总收入笔数", "总收入金额", "总支出笔数", "总支出金额"):
        raise AbcCompanyStatementError("statement totals footer is missing")
    totals = rows[footer_index + 1]

    transactions: list[BankStatementTransaction] = []
    serials: set[str] = set()
    incomes = expenses = 0
    for source_row_number, values in transaction_rows:
        occurred_at = datetime.strptime(str(values[0]), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_SHANGHAI
        )
        income = _optional_minor(values[1], field="income")
        expense = _optional_minor(values[2], field="expense")
        if (income is None) == (expense is None):
            raise AbcCompanyStatementError("statement transaction direction is ambiguous")
        if (income is not None and income <= 0) or (expense is not None and expense <= 0):
            raise AbcCompanyStatementError("statement transaction amount must be positive")
        amount_minor = income if income is not None else -expense  # type: ignore[operator]
        incomes += int(income is not None)
        expenses += int(expense is not None)
        balance_minor = _minor(values[3], field="balance")
        counterparty_account = _text(values[4])
        counterparty_name = _text(values[5])
        counterparty_institution = _text(values[6])
        summary = _required_text(values[7], field="summary")
        normalized = tuple(_stable_cell(value) for value in values)
        row_sha256 = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        fact_sha256 = hashlib.sha256(
            json.dumps(
                (occurred_at.isoformat(), amount_minor, balance_minor, *normalized[4:]),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        serial = f"abc-company:{fact_sha256}"
        if serial in serials:
            raise AbcCompanyStatementError("statement contains duplicate fact identity")
        serials.add(serial)
        transactions.append(
            BankStatementTransaction(
                source_event_ref=uuid5(
                    _NAMESPACE, f"event:{source_sha256}:{source_row_number}:{row_sha256}"
                ),
                source_row_number=source_row_number,
                source_row_sha256=row_sha256,
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                balance_minor=balance_minor,
                counterparty_name=counterparty_name,
                counterparty_account=counterparty_account,
                counterparty_institution=counterparty_institution,
                transaction_serial=serial,
                transaction_name=summary,
            )
        )
    if len(transactions) > _MAX_ROWS:
        raise AbcCompanyStatementError("statement transaction count is invalid")
    if transactions != sorted(transactions, key=lambda item: item.occurred_at, reverse=True):
        raise AbcCompanyStatementError("statement transactions are not reverse chronological")
    if (
        min(item.occurred_at.date() for item in transactions) < period_start
        or max(item.occurred_at.date() for item in transactions) > period_end
    ):
        raise AbcCompanyStatementError("statement transactions exceed declared period")
    for newer, older in pairwise(transactions):
        if older.balance_minor + newer.amount_minor != newer.balance_minor:
            raise AbcCompanyStatementError("statement balance chain is invalid")
    if (
        _integer(totals[0]) != incomes
        or _minor(totals[1], field="total income")
        != sum(max(item.amount_minor, 0) for item in transactions)
        or _integer(totals[2]) != expenses
        or _minor(totals[3], field="total expense")
        != sum(max(-item.amount_minor, 0) for item in transactions)
    ):
        raise AbcCompanyStatementError("statement footer totals do not reconcile")
    months = Counter(item.occurred_at.strftime("%Y-%m") for item in transactions)
    parser_facts_sha256 = hashlib.sha256(
        json.dumps(
            {
                "account_holder_sha256": hashlib.sha256(account_holder.encode()).hexdigest(),
                "account_number_sha256": hashlib.sha256(account_number.encode()).hexdigest(),
                "monthly_transaction_counts": sorted(months.items()),
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return BankStatement(
        statement_ref=uuid5(_NAMESPACE, f"statement:{source_sha256}:{period_start}:{period_end}"),
        source_sha256=source_sha256,
        source_size=len(raw),
        declared_media_type=ABC_COMPANY_XLS_V1.declared_media_type,
        currency="CNY",
        institution_code="abc",
        account_suffix=managed_account_suffix,
        worksheet_index=1,
        header_row_number=3,
        transactions=tuple(transactions),
        parser_profile=BankStatementParserProfile.ABC_COMPANY_XLS_V1,
        source_system=ABC_COMPANY_XLS_V1.source_system,
        parser_facts_sha256=parser_facts_sha256,
    )


def _read_source(path: Path) -> bytes:
    if not path.is_absolute():
        raise AbcCompanyStatementError("statement path must be absolute")
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not 0 < metadata.st_size <= _MAX_FILE_BYTES
    ):
        raise AbcCompanyStatementError("statement file is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(_MAX_FILE_BYTES + 1)
    if len(raw) != metadata.st_size:
        raise AbcCompanyStatementError("statement file changed while reading")
    return raw


def _read_rows(raw: bytes) -> tuple[tuple[object, ...], ...]:
    try:
        workbook = xlrd.open_workbook(file_contents=raw, on_demand=True)
    except (OSError, ValueError, xlrd.XLRDError) as exc:
        raise AbcCompanyStatementError("statement workbook is invalid") from exc
    try:
        if workbook.nsheets != 1:
            raise AbcCompanyStatementError("statement must contain one worksheet")
        sheet = workbook.sheet_by_index(0)
        if sheet.ncols != 8 or not 6 <= sheet.nrows <= _MAX_ROWS + 5:
            raise AbcCompanyStatementError("statement dimensions are invalid")
        return tuple(
            tuple(_cell(value) for value in sheet.row_values(index)) for index in range(sheet.nrows)
        )
    finally:
        workbook.release_resources()


def _cell(value: object) -> object:
    if isinstance(value, float):
        return value
    if not isinstance(value, str):
        raise AbcCompanyStatementError("statement cell type is invalid")
    return _text(value)


def _text(value: object) -> str:
    result = unicodedata.normalize("NFKC", str(value)).strip()
    if len(result) > _MAX_TEXT or contains_unstorable_text(result):
        raise AbcCompanyStatementError("statement text is invalid")
    return result


def _required_text(value: object, *, field: str) -> str:
    result = _text(value)
    if not result:
        raise AbcCompanyStatementError(f"statement {field} is missing")
    return result


def _metadata(value: object, label: str) -> str:
    text = _text(value)
    prefix = f"{label}:"
    if not text.startswith(prefix) or not text[len(prefix) :].strip():
        raise AbcCompanyStatementError("statement metadata is invalid")
    return text[len(prefix) :].strip()


def _period(value: str) -> tuple[date, date]:
    match = re.fullmatch(
        r"([0-9]{4})年([0-9]{2})月([0-9]{2})日\s*-\s*([0-9]{4})年([0-9]{2})月([0-9]{2})日", value
    )
    if match is None:
        raise AbcCompanyStatementError("statement period is invalid")
    parts = [int(item) for item in match.groups()]
    return date(*parts[:3]), date(*parts[3:])


def _optional_minor(value: object, *, field: str) -> int | None:
    return None if value == "" else _minor(value, field=field)


def _minor(value: object, *, field: str) -> int:
    try:
        decimal_value = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise AbcCompanyStatementError(f"statement {field} is invalid") from exc
    minor = decimal_value * 100
    if minor != minor.to_integral_value() or abs(minor) > 9_007_199_254_740_991:
        raise AbcCompanyStatementError(f"statement {field} is invalid")
    return int(minor)


def _integer(value: object) -> int:
    result = _minor(value, field="count")
    if result % 100:
        raise AbcCompanyStatementError("statement count is invalid")
    return result // 100


def _stable_cell(value: object) -> str:
    if isinstance(value, float):
        return format(Decimal(str(value)), "f")
    return str(value)
