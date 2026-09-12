"""Read a WeChat Pay bill export, and check it against what it says of itself.

WeChat keeps 零钱 and 零钱通 as ordinary accounts with a running balance, but it
does not put that balance in this file. The bill export is a *payment* bill: it
records what was paid to whom, not how an account's balance moved. The sweeps
from 零钱 into 零钱通 and the daily 零钱通 interest are both absent from it, so
neither account's balance chain can be rebuilt here - which is why this is not a
``BankStatementParserProfile`` and cannot become one. Inventing a balance to
satisfy that contract would put a false fact in the ledger, and the continuity
check would then be verifying our own arithmetic against itself.

What the file does carry is its own proof of completeness. The preamble states
the record count, and states the count and total of income, expenditure and
direction-less rows separately. Those are WeChat's assertions, not ours, and a
file whose rows disagree with its own preamble is refused here. That is the
role the balance chain plays for a bank statement, filled by a different
mechanism.

Nothing here decides what a row *means*. A direction-less row keeps the
unsigned magnitude the preamble itself totals it by, and classification is left
to review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final
from zoneinfo import ZoneInfo

# The workbook reader is shared rather than copied. It carries the archive
# entry-count, compression and size limits that make opening an untrusted XLSX
# safe, and a second copy of those limits is a second thing to keep right.
from ledgerbridge.mybank_statement import MyBankStatementError
from ledgerbridge.mybank_statement import _read_workbook_rows as _read_xlsx_rows

WECHAT_SOURCE_SYSTEM: Final = "wechat_pay_export"

_ZONE: Final = ZoneInfo("Asia/Shanghai")
_EXCEL_EPOCH: Final = datetime(1899, 12, 30)

_HEADER: Final = (
    "交易时间",
    "交易类型",
    "交易对方",
    "商品",
    "收/支",
    "金额(元)",
    "支付方式",
    "当前状态",
    "交易单号",
    "商户单号",
    "备注",
)

#: The preamble note that fixes the timezone. Without it the serial numbers in
#: the time column are a wall clock with no zone, and this reader would be
#: choosing one rather than reading it.
_TIMEZONE_NOTE: Final = "本账单中所有时间均为UTC+08:00时间"

#: The bill writes its labels with a full-width colon, which is the bank's
#: spelling and not ours; it is written by code point here so that the source
#: stays free of characters that read as ASCII punctuation but are not.
_COLON: Final = chr(0xFF1A)

_PERIOD: Final = re.compile(
    rf"^起始时间{_COLON}\[(?P<start>[0-9: \-]{{19}})\]"
    rf"\s*终止时间{_COLON}\[(?P<end>[0-9: \-]{{19}})\]$"
)
_TOTAL: Final = re.compile(r"^共(?P<count>[0-9]{1,7})笔记录$")
_SUBTOTAL: Final = re.compile(
    rf"^(?P<label>收入|支出|中性交易){_COLON}(?P<count>[0-9]{{1,7}})笔"
    rf"\s*(?P<amount>[0-9]+(?:\.[0-9]+)?)元$"
)
_SERIAL: Final = re.compile(r"^[0-9]{16,64}$")
_AMOUNT: Final = re.compile(r"^[0-9]+(?:\.[0-9]{1,2})?$")

#: The direction column as the file spells it. The last of the three is
#: WeChat's own way of saying that a row has no direction - top-ups,
#: withdrawals, 零钱通 movements, card repayments - and the preamble totals
#: those separately, by magnitude.
INCOME: Final = "收入"
EXPENDITURE: Final = "支出"
DIRECTIONLESS: Final = "/"

_NEUTRAL_LABEL: Final = "中性交易"

_MAX_TEXT: Final = 500
_MAX_ROWS: Final = 100_000


class LocalWeChatError(RuntimeError):
    """A WeChat export could not be read, or disagrees with itself."""


@dataclass(frozen=True, slots=True)
class WeChatRow:
    """One row, as the file states it. No field here is derived."""

    occurred_at: datetime
    kind: str
    counterparty: str
    product: str
    direction: str
    #: Magnitude in cents, always positive. The sign belongs to ``direction``,
    #: and a direction-less row genuinely has none.
    amount_minor: int
    funding: str
    status: str
    serial: str
    merchant_serial: str
    note: str

    @property
    def signed_amount_minor(self) -> int:
        """Signed where the file states a direction, unsigned where it does not."""

        return -self.amount_minor if self.direction == EXPENDITURE else self.amount_minor


@dataclass(frozen=True, slots=True)
class WeChatExport:
    """One export, checked against the four counts it asserts about itself."""

    period_start: datetime
    period_end: datetime
    exported_at: datetime
    export_kind: str
    rows: tuple[WeChatRow, ...]


def read_wechat_export(raw: bytes) -> WeChatExport:
    """Read one export, refusing a file that disagrees with its own preamble."""

    try:
        workbook = _read_xlsx_rows(raw)
    except MyBankStatementError as error:
        raise LocalWeChatError(f"WeChat export is not a readable workbook: {error}") from None

    preamble: list[str] = []
    header_at: int | None = None
    for index, (_, cells) in enumerate(workbook):
        values = tuple(str(cell).strip() for cell in cells)
        if values[: len(_HEADER)] == _HEADER:
            header_at = index
            break
        if values and values[0]:
            preamble.append(values[0][:_MAX_TEXT])
    if header_at is None:
        raise LocalWeChatError("WeChat export has no transaction header row")
    # The note is numbered in the file ("4. ..."), so the declaration is looked
    # for inside a line rather than as one.
    if not any(_TIMEZONE_NOTE in line for line in preamble):
        raise LocalWeChatError("WeChat export does not declare its timezone")

    period_start, period_end = _period(preamble)
    rows = tuple(
        _row(tuple(str(cell).strip() for cell in cells))
        for _, cells in workbook[header_at + 1 :]
        if any(str(cell).strip() for cell in cells)
    )
    if len(rows) > _MAX_ROWS:
        raise LocalWeChatError("WeChat export is implausibly large")

    _agrees_with_itself(rows, _declared_total(preamble), _declared_subtotals(preamble))
    for row in rows:
        if not period_start <= row.occurred_at <= period_end:
            raise LocalWeChatError("WeChat export holds a row outside its own period")
    if len({row.serial for row in rows}) != len(rows):
        raise LocalWeChatError("WeChat export repeats a transaction serial")

    return WeChatExport(
        period_start=period_start,
        period_end=period_end,
        exported_at=_stated_time(_bracketed(preamble, "导出时间")),
        export_kind=_bracketed(preamble, "导出类型"),
        rows=rows,
    )


def _row(values: tuple[str, ...]) -> WeChatRow:
    if len(values) < len(_HEADER):
        raise LocalWeChatError("WeChat transaction row is too narrow")
    if any(len(value) > _MAX_TEXT for value in values):
        raise LocalWeChatError("WeChat transaction row holds an implausibly long field")
    (
        occurred,
        kind,
        counterparty,
        product,
        direction,
        amount,
        funding,
        status,
        serial,
        merchant_serial,
        note,
    ) = values[: len(_HEADER)]
    if direction not in (INCOME, EXPENDITURE, DIRECTIONLESS):
        raise LocalWeChatError("WeChat transaction direction is invalid")
    if not kind or not funding or not status:
        raise LocalWeChatError("WeChat transaction is missing a stated field")
    if _SERIAL.fullmatch(serial) is None:
        raise LocalWeChatError("WeChat transaction serial is invalid")
    return WeChatRow(
        occurred_at=_timestamp(occurred),
        kind=kind,
        counterparty=counterparty,
        product=product,
        direction=direction,
        amount_minor=_money_minor(amount),
        funding=funding,
        status=status,
        serial=serial,
        merchant_serial=merchant_serial,
        note=note,
    )


def _timestamp(value: str) -> datetime:
    """Turn one spreadsheet serial number into a moment in the declared zone."""

    try:
        serial = Decimal(value)
    except InvalidOperation:
        raise LocalWeChatError("WeChat transaction time is invalid") from None
    if not 1 <= serial < 100_000:
        raise LocalWeChatError("WeChat transaction time is out of range")
    moment = _EXCEL_EPOCH + timedelta(days=float(serial))
    # The serial carries float noise the source never had; the export prints to
    # the second, so that is the resolution kept. Rounding rather than
    # truncating, because a time stored as x.999999 is x+1 second in the file.
    moment += timedelta(microseconds=500_000)
    return moment.replace(microsecond=0, tzinfo=_ZONE)


def _money_minor(value: str) -> int:
    if _AMOUNT.fullmatch(value) is None:
        raise LocalWeChatError("WeChat transaction amount is invalid")
    minor = Decimal(value) * 100
    if minor != minor.to_integral_value():
        raise LocalWeChatError("WeChat transaction amount is not a whole number of cents")
    return int(minor)


def _agrees_with_itself(
    rows: tuple[WeChatRow, ...],
    declared_total: int,
    declared: dict[str, tuple[int, int]],
) -> None:
    """Check the rows against the four statements the preamble makes.

    This is the file's own evidence that nothing was dropped between WeChat and
    here, and it is why a balance-free export can be trusted at all.
    """

    if len(rows) != declared_total:
        raise LocalWeChatError(
            f"WeChat export declares {declared_total} records but holds {len(rows)}"
        )
    for label, direction in (
        (INCOME, INCOME),
        (EXPENDITURE, EXPENDITURE),
        (_NEUTRAL_LABEL, DIRECTIONLESS),
    ):
        if label not in declared:
            raise LocalWeChatError(f"WeChat export does not total its {label} rows")
        count, amount_minor = declared[label]
        found = [row for row in rows if row.direction == direction]
        if len(found) != count:
            raise LocalWeChatError(
                f"WeChat export declares {count} {label} rows but holds {len(found)}"
            )
        if sum(row.amount_minor for row in found) != amount_minor:
            raise LocalWeChatError(f"WeChat export {label} rows do not add up to its own total")


def _period(preamble: list[str]) -> tuple[datetime, datetime]:
    for line in preamble:
        match = _PERIOD.fullmatch(line)
        if match is not None:
            return _stated_time(match.group("start")), _stated_time(match.group("end"))
    raise LocalWeChatError("WeChat export does not state its period")


def _declared_total(preamble: list[str]) -> int:
    for line in preamble:
        match = _TOTAL.fullmatch(line)
        if match is not None:
            return int(match.group("count"))
    raise LocalWeChatError("WeChat export does not state its record count")


def _declared_subtotals(preamble: list[str]) -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    for line in preamble:
        match = _SUBTOTAL.fullmatch(line)
        if match is None:
            continue
        label = match.group("label")
        if label in found:
            raise LocalWeChatError(f"WeChat export totals its {label} rows twice")
        found[label] = (int(match.group("count")), _money_minor(match.group("amount")))
    return found


def _bracketed(preamble: list[str], label: str) -> str:
    prefix = f"{label}{_COLON}["
    for line in preamble:
        if line.startswith(prefix) and line.endswith("]"):
            return line[len(prefix) : -1]
    raise LocalWeChatError(f"WeChat export does not state {label}")


def _stated_time(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ZONE)
    except ValueError:
        raise LocalWeChatError("WeChat export states an invalid time") from None


__all__ = [
    "DIRECTIONLESS",
    "EXPENDITURE",
    "INCOME",
    "WECHAT_SOURCE_SYSTEM",
    "LocalWeChatError",
    "WeChatExport",
    "WeChatRow",
    "read_wechat_export",
]
