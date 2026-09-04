"""Fail-closed parser for BOC company-account legacy XLS exports."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

import xlrd  # type: ignore[import-untyped]

from ledgerbridge.bank_statement_contract import (
    BOC_COMPANY_XLS_V1,
    BankStatement,
    BankStatementParserProfile,
    BankStatementTransaction,
)

_NAMESPACE: Final = UUID("3e2033bf-dfd4-5ccd-b040-6fada9cbbfb1")
_OLE_MAGIC: Final = bytes.fromhex("D0CF11E0A1B11AE1")
_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SUFFIX: Final = re.compile(r"^[0-9]{4,8}$")
_ACCOUNT_NUMBER: Final = re.compile(r"^[0-9]{8,32}$")
_DATE: Final = re.compile(r"^[0-9]{8}$")
_TIME: Final = re.compile(r"^[0-9]{2}:[0-9]{2}:[0-9]{2}$")
_RANGE: Final = re.compile(r"^([0-9]{8})-([0-9]{8})$")
_MAX_ROWS: Final = 100_000
_MAX_TEXT: Final = 300
_SHANGHAI: Final = ZoneInfo("Asia/Shanghai")
_HEADER_MARKERS: Final = (
    "Transaction Type",
    "Business type",
    "Account holding bank number of payer",
    "Payer account bank",
    "Debit Account No.",
    "Payer's Name",
    "Account holding bank number of beneficiary",
    "Beneficiary account bank",
    "Payee's Account Number",
    "Payee's Name",
    "Transaction Date",
    "Transaction time",
    "Trade Currency",
    "Trade Amount",
    "After-transaction balance",
    "Value Date",
    "Exchange rate",
    "Transaction reference number",
    "Online Banking Transaction Ref.(Bank Ref.)",
    "Customer Transaction Ref.(Customer Ref.)",
    "Voucher type",
    "Voucher number",
    "Record ID",
    "Reference",
    "Purpose",
    "Remark",
    "Remarks",
    "Reserve1",
    "Reserve2",
    "Reserve3",
    "Opening bank number of nominal payer",
    "Opening bank name of nominal payer",
    "Payment A/C No.",
    "Name of nominal payer",
    "Opening bank number of nominal payee",
    "Opening bank name of nominal payee",
    "Account number of nominal payee",
    "Name of nominal payee",
)


class BocCompanyStatementError(RuntimeError):
    """The source file could not prove a valid BOC company statement."""


def parse_boc_company_xls(
    source_path: Path,
    *,
    expected_sha256: str,
    managed_account_suffix: str,
) -> BankStatement:
    if _DIGEST.fullmatch(expected_sha256) is None:
        raise BocCompanyStatementError("expected source digest is invalid")
    if _ACCOUNT_SUFFIX.fullmatch(managed_account_suffix) is None:
        raise BocCompanyStatementError("managed account suffix is invalid")
    raw = _read_source(source_path)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if source_sha256 != expected_sha256:
        raise BocCompanyStatementError("source digest changed")
    if not raw.startswith(_OLE_MAGIC):
        raise BocCompanyStatementError("statement is not a legacy Excel container")
    rows = _read_workbook_rows(raw)
    if len(rows) < 10:
        raise BocCompanyStatementError("statement contains no transaction rows")

    account_number = _metadata(rows[1], "Inquirer account number")
    if _ACCOUNT_NUMBER.fullmatch(account_number) is None or not account_number.endswith(
        managed_account_suffix
    ):
        raise BocCompanyStatementError("statement does not belong to the managed account")
    declared_count = _integer(_metadata(rows[2], "Total number"), "transaction count")
    debit_count = _integer(_metadata(rows[3], "Total Numbers of Debited Payments"), "debit count")
    debit_total = _minor(_metadata(rows[4], "Total Debit Amount of Payments"), "debit total")
    credit_count = _integer(
        _metadata(rows[5], "Total Numbers of Credited Payments"), "credit count"
    )
    credit_total = _minor(_metadata(rows[6], "Total Credit Amount of Payments"), "credit total")
    period_match = _RANGE.fullmatch(_metadata(rows[7], "Time Range"))
    if period_match is None:
        raise BocCompanyStatementError("statement period is invalid")
    metadata_start = _date(period_match.group(1))
    metadata_end = _date(period_match.group(2))
    if metadata_start > metadata_end:
        raise BocCompanyStatementError("statement period is reversed")
    headers = tuple(_text(value) for value in rows[8])
    if len(headers) != len(_HEADER_MARKERS) or any(
        marker not in header for marker, header in zip(_HEADER_MARKERS, headers, strict=True)
    ):
        raise BocCompanyStatementError("statement header is missing or ambiguous")

    transactions: list[BankStatementTransaction] = []
    fact_ids: set[str] = set()
    owner_names: set[str] = set()
    negative_count = positive_count = 0
    negative_total = positive_total = 0
    previous_balance: int | None = None
    for row_number, values in enumerate(rows[9:], start=10):
        if len(values) != len(_HEADER_MARKERS) or not any(values):
            raise BocCompanyStatementError("statement transaction row is invalid")
        occurred_on = _date(_text(values[10]))
        occurred_time = _clock(_text(values[11]))
        if not metadata_start <= occurred_on <= metadata_end:
            raise BocCompanyStatementError("statement transaction falls outside its period")
        if _text(values[12]) != "CNY":
            raise BocCompanyStatementError("statement transaction currency is not proven")
        amount_minor = _minor(values[13], "amount")
        balance_minor = _minor(values[14], "balance")
        if amount_minor == 0:
            raise BocCompanyStatementError("statement contains a zero transaction")
        if previous_balance is not None and previous_balance + amount_minor != balance_minor:
            raise BocCompanyStatementError("statement balance chain is broken")
        previous_balance = balance_minor
        if amount_minor < 0:
            own_account, own_name = _text(values[4]), _required(values[5], "account holder")
            counterparty_account, counterparty_name = _text(values[8]), _text(values[9])
            counterparty_institution = _text(values[7])
            negative_count += 1
            negative_total += -amount_minor
        else:
            own_account, own_name = _text(values[8]), _required(values[9], "account holder")
            counterparty_account, counterparty_name = _text(values[4]), _text(values[5])
            counterparty_institution = _text(values[3])
            positive_count += 1
            positive_total += amount_minor
        if own_account != account_number:
            raise BocCompanyStatementError("transaction account conflicts with statement identity")
        owner_names.add(own_name)
        transaction_name = " | ".join(
            value
            for value in (
                _text(values[23]),
                _text(values[24]),
                _text(values[25]),
                _text(values[26]),
            )
            if value
        )
        if not transaction_name:
            transaction_name = _required(values[1], "business type")
        canonical = tuple(_text(value) for value in values)
        row_sha256 = hashlib.sha256(
            json.dumps(canonical, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        fact_sha256 = hashlib.sha256(
            json.dumps(
                (
                    occurred_on.isoformat(),
                    occurred_time.isoformat(),
                    amount_minor,
                    balance_minor,
                    _text(values[17]),
                    _text(values[22]),
                    counterparty_account,
                    counterparty_name,
                    transaction_name,
                ),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        serial = f"boc-company:{fact_sha256}"
        if serial in fact_ids:
            raise BocCompanyStatementError("statement contains duplicate transaction facts")
        fact_ids.add(serial)
        transactions.append(
            BankStatementTransaction(
                source_event_ref=uuid5(
                    _NAMESPACE, f"boc-company-event:{source_sha256}:{row_number}:{row_sha256}"
                ),
                source_row_number=row_number,
                source_row_sha256=row_sha256,
                occurred_at=datetime.combine(occurred_on, occurred_time, tzinfo=_SHANGHAI),
                amount_minor=amount_minor,
                balance_minor=balance_minor,
                counterparty_name=counterparty_name,
                counterparty_account=counterparty_account,
                counterparty_institution=counterparty_institution,
                transaction_serial=serial,
                transaction_name=transaction_name,
            )
        )
    if not transactions or len(transactions) > _MAX_ROWS:
        raise BocCompanyStatementError("statement transaction count is invalid")
    if len(owner_names) != 1:
        raise BocCompanyStatementError("statement account holder is inconsistent")
    if (
        len(transactions) != declared_count
        or negative_count != debit_count
        or positive_count != credit_count
        or negative_total != debit_total
        or positive_total != credit_total
    ):
        raise BocCompanyStatementError("statement totals do not reconcile")
    occurred = [item.occurred_at for item in transactions]
    if occurred != sorted(occurred):
        raise BocCompanyStatementError("statement transactions are not ordered")
    owner_hash = hashlib.sha256(next(iter(owner_names)).encode("utf-8")).hexdigest()
    parser_facts_sha256 = hashlib.sha256(
        json.dumps(
            {
                "account_holder_sha256": owner_hash,
                "credit_count": credit_count,
                "credit_total_minor": credit_total,
                "debit_count": debit_count,
                "debit_total_minor": debit_total,
                "period_end": metadata_end.isoformat(),
                "period_start": metadata_start.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return BankStatement(
        statement_ref=uuid5(_NAMESPACE, f"boc-company-statement:{source_sha256}"),
        source_sha256=source_sha256,
        source_size=len(raw),
        declared_media_type=BOC_COMPANY_XLS_V1.declared_media_type,
        currency="CNY",
        institution_code=BOC_COMPANY_XLS_V1.institution_code,
        account_suffix=managed_account_suffix,
        worksheet_index=1,
        header_row_number=9,
        transactions=tuple(transactions),
        parser_profile=BankStatementParserProfile.BOC_COMPANY_XLS_V1,
        source_system=BOC_COMPANY_XLS_V1.source_system,
        parser_facts_sha256=parser_facts_sha256,
    )


def _read_source(path: Path) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute() or path.suffix.lower() != ".xls":
        raise BocCompanyStatementError("statement path must be an absolute XLS file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise BocCompanyStatementError("statement could not be read") from exc


def _read_workbook_rows(raw: bytes) -> list[list[object]]:
    try:
        book = xlrd.open_workbook(file_contents=raw, on_demand=True)
        try:
            if book.nsheets != 1:
                raise BocCompanyStatementError("statement workbook shape is invalid")
            sheet = book.sheet_by_index(0)
            if sheet.ncols != len(_HEADER_MARKERS):
                raise BocCompanyStatementError("statement workbook width is invalid")
            return [sheet.row_values(index) for index in range(sheet.nrows)]
        finally:
            book.release_resources()
    except BocCompanyStatementError:
        raise
    except (xlrd.XLRDError, IndexError, ValueError) as exc:
        raise BocCompanyStatementError("statement workbook is invalid") from exc


def _metadata(row: list[object], marker: str) -> str:
    if len(row) != len(_HEADER_MARKERS) or marker not in _text(row[0]):
        raise BocCompanyStatementError("statement metadata is missing or ambiguous")
    return _required(row[1], marker)


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _required(value: object, field: str) -> str:
    text = _text(value)
    if not text or len(text) > _MAX_TEXT:
        raise BocCompanyStatementError(f"statement {field} is invalid")
    return text


def _integer(value: object, field: str) -> int:
    try:
        parsed = int(Decimal(_text(value).replace(",", "")))
    except (InvalidOperation, ValueError) as exc:
        raise BocCompanyStatementError(f"statement {field} is invalid") from exc
    if parsed < 0:
        raise BocCompanyStatementError(f"statement {field} is invalid")
    return parsed


def _minor(value: object, field: str) -> int:
    try:
        decimal = Decimal(_text(value).replace(",", ""))
    except InvalidOperation as exc:
        raise BocCompanyStatementError(f"statement {field} is invalid") from exc
    minor = decimal * 100
    if minor != minor.to_integral_value():
        raise BocCompanyStatementError(f"statement {field} has excess precision")
    return int(minor)


def _date(value: str) -> date:
    if _DATE.fullmatch(value) is None:
        raise BocCompanyStatementError("statement date is invalid")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise BocCompanyStatementError("statement date is invalid") from exc


def _clock(value: str) -> time:
    if _TIME.fullmatch(value) is None:
        raise BocCompanyStatementError("statement time is invalid")
    try:
        return datetime.strptime(value, "%H:%M:%S").time()
    except ValueError as exc:
        raise BocCompanyStatementError("statement time is invalid") from exc
