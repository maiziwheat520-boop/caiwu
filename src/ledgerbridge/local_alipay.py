"""Read an Alipay bill export, and check it against what it says of itself.

Alipay ships the same kind of document WeChat does - a payment bill, with no
running balance - so it enters the ledger the same way, as review candidates
rather than as an account's statement. What differs is the clothing: a
GBK-encoded CSV rather than a workbook, a header that arrives twenty-odd lines
in, and 不计收支 where WeChat writes "/". The completeness proof is the same
shape and is checked by ``local_payments``.

Two things this file carries that WeChat's does not. It names the account it
belongs to, in the preamble, which lets a batch refuse a file that turns out to
be a different login's. And it carries the counterparty's own account - an
email address or a phone number - which is read past and never kept: it is
someone else's identifier and the ledger has no use for it.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

from ledgerbridge.local_payments import (
    DIRECTIONLESS,
    EXPENDITURE,
    INCOME,
    MAX_ROWS,
    MAX_TEXT,
    PaymentBillError,
    PaymentExport,
    PaymentRow,
    agrees_with_itself,
    money_minor,
)

#: The name Core's released Alipay import already uses. A second name for the
#: same kind of file would split one platform's rows across two source systems.
ALIPAY_SOURCE_SYSTEM: Final = "alipay_export"

#: Alipay writes its exports in the mainland Windows code page. GB18030 is a
#: superset of GBK, so it reads every file GBK does and does not fail on the
#: rarer characters a counterparty's name can contain.
_ENCODING: Final = "gb18030"

_ZONE: Final = ZoneInfo("Asia/Shanghai")

_HEADER: Final = (
    "交易时间",
    "交易分类",
    "交易对方",
    "对方账号",
    "商品说明",
    "收/支",
    "金额",
    "收/付款方式",
    "交易状态",
    "交易订单号",
    "商家订单号",
    "备注",
)

#: Written by code point so the source stays free of characters that read as
#: ASCII punctuation but are not.
_COLON: Final = chr(0xFF1A)

_ACCOUNT: Final = re.compile(rf"^支付宝账户{_COLON}(?P<account>.+)$")
_PERIOD: Final = re.compile(
    rf"^起始时间{_COLON}\[(?P<start>[0-9: \-]{{19}})\]"
    rf"\s*终止时间{_COLON}\[(?P<end>[0-9: \-]{{19}})\]$"
)
_TOTAL: Final = re.compile(r"^共(?P<count>[0-9]{1,7})笔记录$")
_SUBTOTAL: Final = re.compile(
    rf"^(?P<label>收入|支出|不计收支){_COLON}(?P<count>[0-9]{{1,7}})笔"
    rf"\s*(?P<amount>[0-9]+(?:\.[0-9]+)?)元$"
)

#: Alipay's order numbers are mostly digits, but a refund or a lease carries
#: the originating order plus a suffix, and a few are the merchant's own
#: alphanumeric reference. The form is pinned to what the files were seen to
#: hold rather than left open, because this value becomes the transaction's
#: identity in the ledger.
_SERIAL: Final = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_.*-]{5,127}$")

_NEUTRAL_LABEL: Final = "不计收支"

#: The preamble lines the reader needs, and a bound on how far in to look for
#: the header row so a file that is not one stops quickly.
_MAX_PREAMBLE: Final = 200


class LocalAlipayError(PaymentBillError):
    """An Alipay export could not be read, or disagrees with itself."""


def read_alipay_export(raw: bytes) -> PaymentExport:
    """Read one export, refusing a file that disagrees with its own preamble."""

    try:
        text = raw.decode(_ENCODING)
    except UnicodeDecodeError:
        raise LocalAlipayError("Alipay export is not a readable bill file") from None
    lines = text.splitlines()

    header_at: int | None = None
    for index, line in enumerate(lines[:_MAX_PREAMBLE]):
        if _cells(line)[: len(_HEADER)] == _HEADER:
            header_at = index
            break
    if header_at is None:
        raise LocalAlipayError("Alipay export has no transaction header row")
    preamble = [line.strip() for line in lines[:header_at] if line.strip()]

    period_start, period_end = _period(preamble)
    body = list(csv.reader(io.StringIO("\n".join(lines[header_at + 1 :]))))
    rows = tuple(_row(values) for values in body if any(cell.strip() for cell in values))
    if len(rows) > MAX_ROWS:
        raise LocalAlipayError("Alipay export is implausibly large")

    agrees_with_itself(
        rows,
        _declared_total(preamble),
        _declared_subtotals(preamble),
        neutral_label=_NEUTRAL_LABEL,
        totals_are_binding=False,
        error=LocalAlipayError,
    )
    for row in rows:
        if not period_start <= row.occurred_at <= period_end:
            raise LocalAlipayError("Alipay export holds a row outside its own period")
    if len({row.serial for row in rows}) != len(rows):
        raise LocalAlipayError("Alipay export repeats a transaction serial")

    return PaymentExport(
        period_start=period_start,
        period_end=period_end,
        exported_at=_stated_time(_bracketed(preamble, "导出时间")),
        export_kind=_bracketed(preamble, "导出交易类型"),
        account_hint=_account(preamble),
        rows=rows,
    )


def _cells(line: str) -> tuple[str, ...]:
    return tuple(cell.strip() for cell in next(csv.reader(io.StringIO(line)), []))


def _row(values: list[str]) -> PaymentRow:
    # The trailing header comma gives every row a thirteenth empty cell, and
    # the order-number columns carry a tab Alipay pads them with.
    cells = tuple(cell.strip() for cell in values)
    if len(cells) < len(_HEADER):
        raise LocalAlipayError("Alipay transaction row is too narrow")
    if any(len(cell) > MAX_TEXT for cell in cells):
        raise LocalAlipayError("Alipay transaction row holds an implausibly long field")
    (
        occurred,
        kind,
        counterparty,
        _counterparty_account,
        product,
        direction,
        amount,
        funding,
        status,
        serial,
        merchant_serial,
        note,
    ) = cells[: len(_HEADER)]
    if direction == _NEUTRAL_LABEL:
        direction = DIRECTIONLESS
    if direction not in (INCOME, EXPENDITURE, DIRECTIONLESS):
        raise LocalAlipayError("Alipay transaction direction is invalid")
    if not kind or not status:
        raise LocalAlipayError("Alipay transaction is missing a stated field")
    if _SERIAL.fullmatch(serial) is None:
        raise LocalAlipayError("Alipay transaction serial is invalid")
    return PaymentRow(
        occurred_at=_stated_time(occurred),
        kind=kind,
        counterparty=counterparty,
        product=product,
        direction=direction,
        amount_minor=money_minor(amount, error=LocalAlipayError),
        # Alipay leaves this empty for money that never left the platform -
        # a balance transfer, a refund arriving back. Empty is what the file
        # says, and inventing "余额" here would be inference.
        funding=funding,
        status=status,
        serial=serial,
        merchant_serial=merchant_serial,
        note=note,
    )


def _period(preamble: list[str]) -> tuple[datetime, datetime]:
    for line in preamble:
        match = _PERIOD.fullmatch(line)
        if match is not None:
            return _stated_time(match.group("start")), _stated_time(match.group("end"))
    raise LocalAlipayError("Alipay export does not state its period")


def _declared_total(preamble: list[str]) -> int:
    for line in preamble:
        match = _TOTAL.fullmatch(line)
        if match is not None:
            return int(match.group("count"))
    raise LocalAlipayError("Alipay export does not state its record count")


def _declared_subtotals(preamble: list[str]) -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    for line in preamble:
        match = _SUBTOTAL.fullmatch(line)
        if match is None:
            continue
        label = match.group("label")
        if label in found:
            raise LocalAlipayError(f"Alipay export totals its {label} rows twice")
        found[label] = (
            int(match.group("count")),
            money_minor(match.group("amount"), error=LocalAlipayError),
        )
    return found


def _account(preamble: list[str]) -> str:
    for line in preamble:
        match = _ACCOUNT.fullmatch(line)
        if match is not None:
            return match.group("account").strip()
    raise LocalAlipayError("Alipay export does not name the account it belongs to")


def _bracketed(preamble: list[str], label: str) -> str:
    prefix = f"{label}{_COLON}["
    for line in preamble:
        if line.startswith(prefix) and line.endswith("]"):
            return line[len(prefix) : -1]
    raise LocalAlipayError(f"Alipay export does not state {label}")


def _stated_time(value: str) -> datetime:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ZONE)
    except ValueError:
        raise LocalAlipayError("Alipay export states an invalid time") from None


__all__ = [
    "ALIPAY_SOURCE_SYSTEM",
    "LocalAlipayError",
    "read_alipay_export",
]
