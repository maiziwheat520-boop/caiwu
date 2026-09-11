"""Fail-closed parser for BOC company-account online-banking exports.

Online banking renders one and the same query as a legacy ``.xls`` workbook and
as a ``.csv`` text file. The two containers carry an identical 38-column layout,
identical self-declared totals and an identical time range; the only differences
are the container itself and a title row that the workbook puts in front of the
metadata block. One shared reader therefore serves both, which is what keeps the
derived ``transaction_serial`` equal across them: importing the same month twice,
once from each container, is recognised as the same transactions rather than
doubling the ledger. Two separately written readers could not guarantee that.
"""

from __future__ import annotations

import csv
import hashlib
import io
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
    BOC_COMPANY_CSV_V1,
    BOC_COMPANY_XLS_V1,
    BankStatement,
    BankStatementParserProfile,
    BankStatementParserSpec,
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
# The first line of the text container always names the account query. It is the
# cheapest proof that a candidate decoding produced Chinese text rather than mojibake.
_CSV_MARKER: Final = "Inquirer account number"
# The metadata block is seven self-declared rows; the column header follows it.
_METADATA_ROWS: Final = 7
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
    """Read the legacy workbook container, whose title row offsets the metadata."""

    raw = _read_source(source_path, ".xls", expected_sha256, managed_account_suffix)
    if not raw.startswith(_OLE_MAGIC):
        raise BocCompanyStatementError("statement is not a legacy Excel container")
    return _statement(
        raw,
        _read_workbook_rows(raw),
        metadata_at=1,
        spec=BOC_COMPANY_XLS_V1,
        profile=BankStatementParserProfile.BOC_COMPANY_XLS_V1,
        worksheet_index=1,
        managed_account_suffix=managed_account_suffix,
    )


def parse_boc_company_csv(
    source_path: Path,
    *,
    expected_sha256: str,
    managed_account_suffix: str,
) -> BankStatement:
    """Read the text container, which has no title row and starts at the metadata."""

    raw = _read_source(source_path, ".csv", expected_sha256, managed_account_suffix)
    return _statement(
        raw,
        _read_csv_rows(raw),
        metadata_at=0,
        spec=BOC_COMPANY_CSV_V1,
        profile=BankStatementParserProfile.BOC_COMPANY_CSV_V1,
        worksheet_index=0,
        managed_account_suffix=managed_account_suffix,
    )


def _statement(
    raw: bytes,
    rows: list[list[object]],
    *,
    metadata_at: int,
    spec: BankStatementParserSpec,
    profile: BankStatementParserProfile,
    worksheet_index: int,
    managed_account_suffix: str,
) -> BankStatement:
    """Reconcile already-tabulated rows into one statement.

    ``metadata_at`` is where the seven self-declared rows begin; the column
    header follows them, and the transactions follow the header.
    """

    source_sha256 = hashlib.sha256(raw).hexdigest()
    header_at = metadata_at + _METADATA_ROWS
    if len(rows) < header_at + 2:
        raise BocCompanyStatementError("statement contains no transaction rows")

    account_number = _metadata(rows[metadata_at], "Inquirer account number")
    if _ACCOUNT_NUMBER.fullmatch(account_number) is None or not account_number.endswith(
        managed_account_suffix
    ):
        raise BocCompanyStatementError("statement does not belong to the managed account")
    declared_count = _integer(_metadata(rows[metadata_at + 1], "Total number"), "transaction count")
    debit_count = _integer(
        _metadata(rows[metadata_at + 2], "Total Numbers of Debited Payments"), "debit count"
    )
    debit_total = _minor(
        _metadata(rows[metadata_at + 3], "Total Debit Amount of Payments"), "debit total"
    )
    credit_count = _integer(
        _metadata(rows[metadata_at + 4], "Total Numbers of Credited Payments"), "credit count"
    )
    credit_total = _minor(
        _metadata(rows[metadata_at + 5], "Total Credit Amount of Payments"), "credit total"
    )
    period_match = _RANGE.fullmatch(_metadata(rows[metadata_at + 6], "Time Range"))
    if period_match is None:
        raise BocCompanyStatementError("statement period is invalid")
    metadata_start = _date(period_match.group(1))
    metadata_end = _date(period_match.group(2))
    if metadata_start > metadata_end:
        raise BocCompanyStatementError("statement period is reversed")
    headers = tuple(_text(value) for value in rows[header_at])
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
    for row_number, values in enumerate(rows[header_at + 1 :], start=header_at + 2):
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
        declared_media_type=spec.declared_media_type,
        currency="CNY",
        institution_code=spec.institution_code,
        account_suffix=managed_account_suffix,
        worksheet_index=worksheet_index,
        header_row_number=header_at + 1,
        transactions=tuple(transactions),
        parser_profile=profile,
        source_system=spec.source_system,
        parser_facts_sha256=parser_facts_sha256,
    )


def _read_source(
    path: Path, suffix: str, expected_sha256: str, managed_account_suffix: str
) -> bytes:
    if _DIGEST.fullmatch(expected_sha256) is None:
        raise BocCompanyStatementError("expected source digest is invalid")
    if _ACCOUNT_SUFFIX.fullmatch(managed_account_suffix) is None:
        raise BocCompanyStatementError("managed account suffix is invalid")
    if not isinstance(path, Path) or not path.is_absolute() or path.suffix.lower() != suffix:
        container = suffix.lstrip(".").upper()
        raise BocCompanyStatementError(f"statement path must be an absolute {container} file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BocCompanyStatementError("statement could not be read") from exc
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise BocCompanyStatementError("source digest changed")
    return raw


def _decode_csv(raw: bytes) -> str:
    """Decode the text container without guessing.

    The same online-banking export has been observed as GBK and as UTF-8 with a
    byte-order mark. Guessing wrong does not raise: it yields mojibake that only
    fails later, at "the header is unrecognisable", one layer away from the real
    cause. So a candidate decoding is accepted only when its first line names the
    account query; if neither candidate does, the file is rejected here.
    """

    ordered = ("utf-8-sig", "gbk") if raw.startswith(b"\xef\xbb\xbf") else ("gbk", "utf-8-sig")
    for encoding in ordered:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _CSV_MARKER in text.partition("\n")[0]:
            return text
    raise BocCompanyStatementError("statement text encoding is invalid")


def _read_csv_rows(raw: bytes) -> list[list[object]]:
    """Tabulate the text container to the same 38-column shape as the workbook.

    Online banking suffixes almost every field with a tab so that Excel does not
    read account numbers as numbers, and pads the seven metadata rows with extra
    empty columns beyond the header width. Dropping trailing blank columns and
    padding back to the known width normalises both, so a transaction yields the
    same field values here as in the workbook, and therefore the same serial.
    """

    text = _decode_csv(raw)
    width = len(_HEADER_MARKERS)
    rows: list[list[object]] = []
    for values in csv.reader(io.StringIO(text, newline="")):
        while values and not values[-1].strip():
            values.pop()
        if not values:
            continue
        if len(values) > width:
            raise BocCompanyStatementError("statement row is wider than the known layout")
        if len(rows) > _MAX_ROWS:
            raise BocCompanyStatementError("statement transaction count is invalid")
        rows.append([*values, *[""] * (width - len(values))])
    return rows


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
