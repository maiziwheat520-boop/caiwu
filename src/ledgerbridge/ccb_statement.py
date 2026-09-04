"""Fail-closed parser adapter for CCB personal-account XLS exports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

import xlrd  # type: ignore[import-untyped]

from ledgerbridge.bank_statement_contract import (
    CCB_PERSONAL_XLS_V1,
    BankStatement,
    BankStatementParserProfile,
    BankStatementTransaction,
)
from ledgerbridge.text import contains_unstorable_text

CcbStatement = BankStatement
CcbTransaction = BankStatementTransaction

_OLE_MAGIC: Final = bytes.fromhex("D0CF11E0A1B11AE1")
_NAMESPACE: Final = UUID("d45aadf5-2597-438d-a1e5-37fe862382ef")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")
_ACCOUNT_NUMBER = re.compile(r"^[0-9]{12,30}$")
_MONEY = re.compile(r"^[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?$")
_EXPECTED_TITLE: Final = "中国建设银行个人活期账户全部交易明细"
_EXPECTED_HEADERS: Final = (
    "序号",
    "摘要",
    "币别",
    "钞汇",
    "交易日期",
    "交易金额",
    "账户余额",
    "交易地点/附言",
    "对方账号与户名",
)
_SHANGHAI: Final = ZoneInfo("Asia/Shanghai")
_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_ROWS = 100_000
_MAX_TEXT = 300


class CcbStatementError(RuntimeError):
    """The source file could not prove a valid CCB personal statement."""


def parse_ccb_personal_xls(
    source_path: Path,
    *,
    expected_sha256: str,
    managed_account_suffix: str,
) -> BankStatement:
    """Parse one digest-bound CCB XLS export without exposing private cells."""

    if _DIGEST.fullmatch(expected_sha256) is None:
        raise CcbStatementError("expected source digest is invalid")
    if _ACCOUNT_SUFFIX.fullmatch(managed_account_suffix) is None:
        raise CcbStatementError("managed account suffix is invalid")
    raw = _read_source(source_path)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if source_sha256 != expected_sha256:
        raise CcbStatementError("source digest changed")
    if not raw.startswith(_OLE_MAGIC):
        raise CcbStatementError("statement is not a legacy Excel container")

    rows = _read_workbook_rows(raw)
    if len(rows) < 5:
        raise CcbStatementError("statement contains no transaction rows")
    title = tuple(value for value in rows[0] if value)
    if title != (_EXPECTED_TITLE,):
        raise CcbStatementError("statement institution and export type are not proven")
    account_number = _metadata_value(rows[1][1], "卡号/账号")
    account_holder = _metadata_value(rows[1][3], "客户名称")
    metadata_start = _parse_metadata_date(_metadata_value(rows[1][5], "起始日期"))
    metadata_end = _parse_metadata_date(_metadata_value(rows[1][7], "结束日期"))
    if _ACCOUNT_NUMBER.fullmatch(account_number) is None or not account_number.endswith(
        managed_account_suffix
    ):
        raise CcbStatementError("statement does not belong to the managed account")
    if not account_holder or len(account_holder) > _MAX_TEXT:
        raise CcbStatementError("statement account holder is invalid")
    if rows[3] != _EXPECTED_HEADERS:
        raise CcbStatementError("statement header is missing or ambiguous")

    transactions: list[BankStatementTransaction] = []
    known_serials: set[str] = set()
    summaries: list[tuple[int, str]] = []
    for position, values in enumerate(rows[4:], start=1):
        source_row_number = position + 4
        if not any(values):
            raise CcbStatementError("statement contains an unexpected empty transaction row")
        if len(values) != len(_EXPECTED_HEADERS):
            raise CcbStatementError("statement transaction column count is invalid")
        sequence = _parse_sequence(values[0])
        if sequence != position:
            raise CcbStatementError("statement transaction sequence is not contiguous")
        if values[2] != "人民币元" or values[3] != "钞":
            raise CcbStatementError("statement transaction currency is not proven")
        occurred_on = _parse_transaction_date(values[4])
        amount_minor = _parse_minor(values[5], field="amount")
        balance_minor = _parse_minor(values[6], field="balance")
        summary = _required_text(values[1], field="summary")
        location_note = _optional_text(values[7], field="location note")
        transaction_name = _transaction_name(summary, location_note)
        counterparty_account, counterparty_name = _split_counterparty(values[8])
        row_sha256 = hashlib.sha256(
            json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        fact_sha256 = hashlib.sha256(
            json.dumps(
                (
                    occurred_on.isoformat(),
                    amount_minor,
                    balance_minor,
                    summary,
                    location_note,
                    counterparty_account,
                    counterparty_name,
                ),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        transaction_serial = f"ccb:{fact_sha256}"
        if transaction_serial in known_serials:
            raise CcbStatementError(
                "statement contains transactions without a unique fact identity"
            )
        known_serials.add(transaction_serial)
        summaries.append((source_row_number, summary))
        transactions.append(
            BankStatementTransaction(
                source_event_ref=uuid5(
                    _NAMESPACE,
                    f"ccb-event:{source_sha256}:{source_row_number}:{row_sha256}",
                ),
                source_row_number=source_row_number,
                source_row_sha256=row_sha256,
                occurred_at=datetime.combine(occurred_on, time.min, tzinfo=_SHANGHAI),
                amount_minor=amount_minor,
                balance_minor=balance_minor,
                counterparty_name=counterparty_name,
                counterparty_account=counterparty_account,
                counterparty_institution="",
                transaction_serial=transaction_serial,
                transaction_name=transaction_name,
            )
        )
    if not transactions or len(transactions) > _MAX_ROWS:
        raise CcbStatementError("statement transaction count is invalid")
    dates = [item.occurred_at.date() for item in transactions]
    if dates != sorted(dates):
        raise CcbStatementError("statement transactions are not date ordered")
    if dates[0] < metadata_start or dates[-1] > metadata_end:
        raise CcbStatementError("statement transactions fall outside the metadata period")

    months = Counter(item.strftime("%Y-%m") for item in dates)
    summary_set_sha256 = _set_digest(
        f"{row}:{hashlib.sha256(summary.encode('utf-8')).hexdigest()}" for row, summary in summaries
    )
    account_holder_sha256 = hashlib.sha256(account_holder.encode("utf-8")).hexdigest()
    parser_facts_sha256 = hashlib.sha256(
        json.dumps(
            {
                "account_holder_sha256": account_holder_sha256,
                "monthly_transaction_counts": sorted(months.items()),
                "period_end": metadata_end.isoformat(),
                "period_start": metadata_start.isoformat(),
                "summary_set_sha256": summary_set_sha256,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return BankStatement(
        statement_ref=uuid5(
            _NAMESPACE,
            f"ccb-statement:{source_sha256}:{metadata_start}:{metadata_end}",
        ),
        source_sha256=source_sha256,
        source_size=len(raw),
        declared_media_type=CCB_PERSONAL_XLS_V1.declared_media_type,
        currency="CNY",
        institution_code=CCB_PERSONAL_XLS_V1.institution_code,
        account_suffix=managed_account_suffix,
        worksheet_index=1,
        header_row_number=4,
        transactions=tuple(transactions),
        parser_profile=BankStatementParserProfile.CCB_PERSONAL_XLS_V1,
        source_system=CCB_PERSONAL_XLS_V1.source_system,
        parser_facts_sha256=parser_facts_sha256,
    )


def _read_source(path: Path) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise CcbStatementError("statement path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CcbStatementError("statement file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CcbStatementError("statement file must be regular")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_FILE_BYTES:
        raise CcbStatementError("statement file size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CcbStatementError("statement file cannot be opened") from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(_MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise CcbStatementError("statement file cannot be read") from exc
    if len(raw) != metadata.st_size or len(raw) > _MAX_FILE_BYTES:
        raise CcbStatementError("statement file changed while reading")
    return raw


def _read_workbook_rows(raw: bytes) -> tuple[tuple[str, ...], ...]:
    try:
        workbook = xlrd.open_workbook(
            file_contents=raw,
            formatting_info=False,
            on_demand=True,
            ignore_workbook_corruption=False,
        )
    except (OSError, ValueError, xlrd.XLRDError) as exc:
        raise CcbStatementError("statement workbook is invalid") from exc
    try:
        if workbook.nsheets != 1:
            raise CcbStatementError("statement must contain exactly one worksheet")
        sheet = workbook.sheet_by_index(0)
        if sheet.nrows < 5 or sheet.nrows > _MAX_ROWS + 4 or sheet.ncols != 9:
            raise CcbStatementError("statement worksheet dimensions are invalid")
        return tuple(
            tuple(_cell_text(value) for value in sheet.row_values(index))
            for index in range(sheet.nrows)
        )
    finally:
        workbook.release_resources()


def _cell_text(value: object) -> str:
    if not isinstance(value, str):
        raise CcbStatementError("statement contains a non-text cell")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if len(normalized) > _MAX_TEXT or contains_unstorable_text(normalized):
        raise CcbStatementError("statement cell text is invalid")
    return normalized


def _metadata_value(value: str, label: str) -> str:
    prefix = f"{label}:"
    if not value.startswith(prefix):
        raise CcbStatementError("statement metadata is invalid")
    result = value[len(prefix) :].strip()
    if not result:
        raise CcbStatementError("statement metadata is incomplete")
    return result


def _parse_metadata_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise CcbStatementError("statement metadata date is invalid") from exc


def _parse_transaction_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise CcbStatementError("statement transaction date is invalid") from exc


def _parse_sequence(value: str) -> int:
    if not value.isdigit():
        raise CcbStatementError("statement transaction sequence is invalid")
    result = int(value)
    if result < 1 or result > _MAX_ROWS:
        raise CcbStatementError("statement transaction sequence is invalid")
    return result


def _parse_minor(value: str, *, field: str) -> int:
    if _MONEY.fullmatch(value) is None:
        raise CcbStatementError(f"statement {field} is invalid")
    try:
        decimal_value = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise CcbStatementError(f"statement {field} is invalid") from exc
    minor = decimal_value * 100
    if minor != minor.to_integral_value() or abs(minor) > 9_007_199_254_740_991:
        raise CcbStatementError(f"statement {field} is out of range")
    return int(minor)


def _required_text(value: str, *, field: str) -> str:
    if not value:
        raise CcbStatementError(f"statement {field} is missing")
    return value


def _optional_text(value: str, *, field: str) -> str:
    if len(value) > _MAX_TEXT:
        raise CcbStatementError(f"statement {field} is invalid")
    return value


def _split_counterparty(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    if "/" not in value:
        return (value, "") if value.isdigit() else ("", value)
    account, name = value.split("/", 1)
    return account.strip(), name.strip()


def _transaction_name(summary: str, location_note: str) -> str:
    value = summary if not location_note else f"{summary} | {location_note}"
    if len(value) > _MAX_TEXT:
        raise CcbStatementError("statement summary and location note are too long")
    return value


def _set_digest(values: object) -> str:
    return hashlib.sha256("|".join(values).encode("ascii")).hexdigest()  # type: ignore[arg-type]
