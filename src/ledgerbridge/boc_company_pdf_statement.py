"""Fail-closed parser for the BOC company-account statement mailed monthly.

This is a different document from the online-banking export that
:mod:`ledgerbridge.boc_company_statement` reads, which is why it is not folded
into that module:

- The export carries a transaction time, a counterparty account and one signed
  amount column. The mailed statement has no time, no counterparty account, and
  splits the amount into separate debit and credit columns; the counterparty
  survives only as a name in the notes column.
- The mailed statement carries facts the export does not: a per-page opening
  balance, and period debit/credit totals with a closing balance. Together those
  let the document check itself -- the balance chain runs from the opening
  balance to the closing balance, so a row read twice, missed, or read into the
  wrong column breaks it.

The two containers therefore cannot derive an equal ``transaction_serial``: the
mailed statement simply does not carry the time and counterparty account that
the export's identity is built from. **If one month is ever available both as a
mailed statement and as an export, importing both records every transaction
twice.** Check which months an account already covers before importing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from ledgerbridge.bank_statement_contract import (
    BOC_COMPANY_PDF_V1,
    BankStatement,
    BankStatementParserProfile,
    BankStatementTransaction,
)

_NAMESPACE: Final = UUID("6b5f0f02-9c3e-5a71-9d0c-2a6f7b4d1e88")
_PDF_MAGIC: Final = b"%PDF-"
_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SUFFIX: Final = re.compile(r"^[0-9]{4,8}$")
_MAX_ROWS: Final = 100_000
_MAX_TEXT: Final = 300
_SHANGHAI: Final = ZoneInfo("Asia/Shanghai")

# The eleven table columns, in order, as the statement prints them.
_COLUMNS: Final = (
    "序号",
    "记账日",
    "起息日",
    "交易类型",
    "凭证",
    "凭证号码/业务编号/用途/摘要",
    "借方发生额",
    "贷方发生额",
    "余额",
    "机构/柜员/流水",
    "备注",
)
_MONEY: Final = r"-?[0-9,]+\.[0-9]{2}"
_ACCOUNT: Final = re.compile(r"账号\s+([0-9]{8,32})\s")
_HOLDER: Final = re.compile(r"账户名称\s+(\S+?)\s+开户行")
_CURRENCY: Final = re.compile(r"币种\s+人民币\(CNY\)")
_FROM: Final = re.compile(r"起始日期\s*([0-9]{8})")
_TO: Final = re.compile(r"截止日期\s*([0-9]{8})")
_OPENING: Final = re.compile(rf"承前页余额\s+({_MONEY})")
_PAGES: Final = re.compile(r"第\s*([0-9]+)\s*页/共\s*([0-9]+)\s*页")
_FOOTER: Final = re.compile(
    rf"借方合计\s+({_MONEY})\s+贷方合计\s+({_MONEY})\s+"
    rf"本页余额\s+({_MONEY})\s+"
    rf"本对账期末余额\s+({_MONEY})"
)
_BOOKED: Final = re.compile(r"^[0-9]{6}$")
_SEQUENCE: Final = re.compile(r"^[0-9]{1,6}$")


class BocCompanyPdfStatementError(RuntimeError):
    """The source file could not prove a valid BOC company mailed statement."""


@dataclass(frozen=True, slots=True)
class _Row:
    """One printed table row, fields still verbatim."""

    sequence: int
    booked_on: date
    kind: str
    details: str
    debit: str
    credit: str
    balance: str
    reference: str
    note: str


def parse_boc_company_pdf(
    source_path: Path,
    *,
    expected_sha256: str,
    managed_account_suffix: str,
) -> BankStatement:
    if _DIGEST.fullmatch(expected_sha256) is None:
        raise BocCompanyPdfStatementError("expected source digest is invalid")
    if _ACCOUNT_SUFFIX.fullmatch(managed_account_suffix) is None:
        raise BocCompanyPdfStatementError("managed account suffix is invalid")
    if not isinstance(source_path, Path) or not source_path.is_absolute():
        raise BocCompanyPdfStatementError("statement path must be an absolute PDF file")
    if source_path.suffix.lower() != ".pdf":
        raise BocCompanyPdfStatementError("statement path must be an absolute PDF file")
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        raise BocCompanyPdfStatementError("statement could not be read") from exc
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if source_sha256 != expected_sha256:
        raise BocCompanyPdfStatementError("source digest changed")
    if not raw.startswith(_PDF_MAGIC):
        raise BocCompanyPdfStatementError("statement is not a PDF")
    return _statement(
        _page_text(source_path),
        raw=raw,
        source_sha256=source_sha256,
        managed_account_suffix=managed_account_suffix,
    )


def _statement(
    text: str,
    *,
    raw: bytes,
    source_sha256: str,
    managed_account_suffix: str,
) -> BankStatement:
    """Reconcile the statement body into one statement.

    Kept separate from reading the file because this half is the load-bearing
    one: the balance chain, the period totals and the closing balance interlock,
    and all three depend only on the body, not on how the PDF is stored.
    """

    account_number = _one(_ACCOUNT, text, "account number")
    if not account_number.endswith(managed_account_suffix):
        raise BocCompanyPdfStatementError("statement does not belong to the managed account")
    holder = _one(_HOLDER, text, "account holder")
    if _CURRENCY.search(text) is None:
        raise BocCompanyPdfStatementError("statement currency is not proven")
    period_start = _date8(_one(_FROM, text, "period start"))
    period_end = _date8(_one(_TO, text, "period end"))
    if period_start > period_end:
        raise BocCompanyPdfStatementError("statement period is reversed")
    opening_balance = _minor(_one(_OPENING, text, "opening balance"), "opening balance")

    footer = _FOOTER.search(text)
    if footer is None:
        raise BocCompanyPdfStatementError("statement footer totals are missing")
    debit_total = _minor(footer.group(1), "debit total")
    credit_total = _minor(footer.group(2), "credit total")
    closing_balance = _minor(footer.group(4), "closing balance")

    rows = _rows(text)
    if not rows or len(rows) > _MAX_ROWS:
        raise BocCompanyPdfStatementError("statement transaction count is invalid")

    transactions: list[BankStatementTransaction] = []
    serials: set[str] = set()
    running = opening_balance
    debit_seen = credit_seen = 0
    for row in rows:
        if not period_start <= row.booked_on <= period_end:
            raise BocCompanyPdfStatementError("statement transaction falls outside its period")
        if bool(row.debit) == bool(row.credit):
            raise BocCompanyPdfStatementError("statement transaction has no single amount")
        if row.debit:
            amount_minor = -_minor(row.debit, "amount")
            debit_seen += -amount_minor
        else:
            amount_minor = _minor(row.credit, "amount")
            credit_seen += amount_minor
        if amount_minor == 0:
            raise BocCompanyPdfStatementError("statement contains a zero transaction")
        balance_minor = _minor(row.balance, "balance")
        if running + amount_minor != balance_minor:
            raise BocCompanyPdfStatementError("statement balance chain is broken")
        running = balance_minor
        fact_sha256 = hashlib.sha256(
            json.dumps(
                (
                    row.booked_on.isoformat(),
                    amount_minor,
                    balance_minor,
                    row.reference,
                    row.details,
                    row.note,
                ),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        serial = f"boc-company-pdf:{fact_sha256}"
        if serial in serials:
            raise BocCompanyPdfStatementError("statement contains duplicate transaction facts")
        serials.add(serial)
        transactions.append(
            BankStatementTransaction(
                source_event_ref=uuid5(
                    _NAMESPACE, f"boc-company-pdf-event:{source_sha256}:{row.sequence}"
                ),
                source_row_number=row.sequence,
                source_row_sha256=fact_sha256,
                occurred_at=datetime.combine(row.booked_on, time(), tzinfo=_SHANGHAI),
                amount_minor=amount_minor,
                balance_minor=balance_minor,
                counterparty_name=row.note,
                counterparty_account="",
                counterparty_institution="",
                transaction_serial=serial,
                # The transaction type is the only column that always says what
                # the row is; the details column is often an opaque reference
                # and is sometimes empty, so it qualifies the type rather than
                # replacing it.
                transaction_name=f"{row.kind} | {row.details}" if row.details else row.kind,
            )
        )

    if running != closing_balance:
        raise BocCompanyPdfStatementError("statement closing balance does not reconcile")
    if debit_seen != debit_total or credit_seen != credit_total:
        raise BocCompanyPdfStatementError("statement totals do not reconcile")
    booked = [item.occurred_at for item in transactions]
    if booked != sorted(booked):
        raise BocCompanyPdfStatementError("statement transactions are not ordered")

    parser_facts_sha256 = hashlib.sha256(
        json.dumps(
            {
                "account_holder_sha256": hashlib.sha256(holder.encode("utf-8")).hexdigest(),
                "closing_balance_minor": closing_balance,
                "credit_total_minor": credit_total,
                "debit_total_minor": debit_total,
                "opening_balance_minor": opening_balance,
                "period_end": period_end.isoformat(),
                "period_start": period_start.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return BankStatement(
        statement_ref=uuid5(_NAMESPACE, f"boc-company-pdf-statement:{source_sha256}"),
        source_sha256=source_sha256,
        source_size=len(raw),
        declared_media_type=BOC_COMPANY_PDF_V1.declared_media_type,
        currency="CNY",
        institution_code=BOC_COMPANY_PDF_V1.institution_code,
        account_suffix=managed_account_suffix,
        worksheet_index=0,
        header_row_number=0,
        transactions=tuple(transactions),
        parser_profile=BankStatementParserProfile.BOC_COMPANY_PDF_V1,
        source_system=BOC_COMPANY_PDF_V1.source_system,
        parser_facts_sha256=parser_facts_sha256,
    )


def _page_text(path: Path) -> str:
    """Extract the body, accepting single-page statements only.

    Every page of a multi-page statement prints its own opening balance and its
    own footer totals, and no genuine multi-page sample is available to prove
    whether those are per-page or cumulative. Guessing wrong would make the
    balance chain and the totals agree on a wrong number, so refuse until a real
    sample settles it.
    """

    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise BocCompanyPdfStatementError("statement is encrypted")
        pages = reader.pages
        if len(pages) != 1:
            raise BocCompanyPdfStatementError("multi-page statements are not supported yet")
        text = pages[0].extract_text() or ""
    except BocCompanyPdfStatementError:
        raise
    except Exception as exc:  # pypdf raises a wide family of its own errors
        raise BocCompanyPdfStatementError("statement could not be read as a PDF") from exc
    declared = _PAGES.search(text)
    if declared is None or declared.group(1) != "1" or declared.group(2) != "1":
        raise BocCompanyPdfStatementError("statement page count is not proven")
    return text


def _rows(text: str) -> list[_Row]:
    """Read the pipe-delimited table into one row per transaction.

    A row whose sequence cell is empty is a continuation of the row above: an
    over-long summary wrapped, and the wrapped text appends to the summary. A
    continuation before the first transaction means the header was not matched,
    which is a reason to refuse rather than to guess.
    """

    rows: list[_Row] = []
    seen_header = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if not stripped.endswith("|"):
            raise BocCompanyPdfStatementError("statement table row is not closed")
        # Strip exactly one leading and one trailing pipe rather than using
        # strip("|"): the last column is routinely empty, and stripping greedily
        # would swallow that column entirely, leaving ten fields -- which reads
        # as "wrong width" instead of "empty last column".
        fields = [field.strip() for field in stripped[1:-1].split("|")]
        if len(fields) != len(_COLUMNS):
            raise BocCompanyPdfStatementError("statement table width is invalid")
        if fields[0] == _COLUMNS[0]:
            if tuple(fields) != _COLUMNS:
                raise BocCompanyPdfStatementError("statement table header is missing or ambiguous")
            seen_header = True
            continue
        if fields[0] == "No.":
            continue
        if not seen_header:
            raise BocCompanyPdfStatementError("statement table header is missing or ambiguous")
        if not fields[0]:
            if not rows:
                raise BocCompanyPdfStatementError("statement table starts with a continuation")
            if any(fields[index] for index in (1, 2, 3, 6, 7, 8, 9)):
                raise BocCompanyPdfStatementError(
                    "statement continuation row carries its own facts"
                )
            previous = rows[-1]
            rows[-1] = _Row(
                sequence=previous.sequence,
                booked_on=previous.booked_on,
                kind=previous.kind,
                details=f"{previous.details}{fields[5]}",
                debit=previous.debit,
                credit=previous.credit,
                balance=previous.balance,
                reference=previous.reference,
                note=f"{previous.note}{fields[10]}",
            )
            continue
        if _SEQUENCE.fullmatch(fields[0]) is None:
            raise BocCompanyPdfStatementError("statement transaction number is invalid")
        sequence = int(fields[0])
        if sequence != len(rows) + 1:
            raise BocCompanyPdfStatementError("statement transaction numbers skip")
        rows.append(
            _Row(
                sequence=sequence,
                booked_on=_date6(fields[1]),
                kind=_required(fields[3], "transaction type"),
                details=_bounded(fields[5], "details"),
                debit=fields[6],
                credit=fields[7],
                balance=_required(fields[8], "balance"),
                reference=_required(fields[9], "reference"),
                note=_bounded(fields[10], "note"),
            )
        )
    return rows


def _one(pattern: re.Pattern[str], text: str, field: str) -> str:
    found = pattern.findall(text)
    if not found or len(set(found)) != 1:
        raise BocCompanyPdfStatementError(f"statement {field} is missing or ambiguous")
    return str(found[0])


def _required(value: str, field: str) -> str:
    if not value or len(value) > _MAX_TEXT:
        raise BocCompanyPdfStatementError(f"statement {field} is invalid")
    return value


def _bounded(value: str, field: str) -> str:
    if len(value) > _MAX_TEXT:
        raise BocCompanyPdfStatementError(f"statement {field} is invalid")
    return value


def _minor(value: str, field: str) -> int:
    try:
        decimal = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise BocCompanyPdfStatementError(f"statement {field} is invalid") from exc
    minor = decimal * 100
    if minor != minor.to_integral_value():
        raise BocCompanyPdfStatementError(f"statement {field} has excess precision")
    return int(minor)


def _date8(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise BocCompanyPdfStatementError("statement date is invalid") from exc


def _date6(value: str) -> date:
    """The booking date prints as YYMMDD; company accounts have no 1900s rows."""

    if _BOOKED.fullmatch(value) is None:
        raise BocCompanyPdfStatementError("statement date is invalid")
    return _date8(f"20{value}")


__all__ = ["BocCompanyPdfStatementError", "parse_boc_company_pdf"]
