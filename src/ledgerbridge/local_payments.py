"""What a payment bill is, independent of which platform wrote it.

WeChat and Alipay ship the same kind of document in different clothes: an
export of what was paid to whom, with no running balance, and a preamble that
states how many rows it holds and what they add up to. That preamble is the
only completeness proof either file carries, and it is what makes a
balance-free import trustworthy at all - so it is checked here, once, rather
than in each reader.

Nothing in this module decides what a row *means*. A row keeps the direction
its file states, including the platforms' own way of saying a row has no
direction, and classification is left to review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

#: The three directions, in the shape both readers normalise to. The platforms
#: spell the third one differently - WeChat writes "/", Alipay writes
#: 不计收支 - but they mean the same thing: the file declines to call this
#: money coming in or going out, and totals it separately and unsigned.
INCOME: Final = "收入"
EXPENDITURE: Final = "支出"
DIRECTIONLESS: Final = "/"

MAX_TEXT: Final = 500
MAX_ROWS: Final = 100_000

_AMOUNT: Final = re.compile(r"^[0-9]+(?:\.[0-9]{1,2})?$")


class PaymentBillError(RuntimeError):
    """A payment bill could not be read, or disagrees with itself."""


@dataclass(frozen=True, slots=True)
class PaymentRow:
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
class PaymentExport:
    """One export, checked against the counts it asserts about itself.

    ``account_hint`` is whatever the file says about which account it belongs
    to - Alipay names the login in its preamble, WeChat names nothing - and it
    is deliberately not an account identifier the ledger stores. It exists so a
    batch can refuse files that turn out to be two different people's.
    """

    period_start: datetime
    period_end: datetime
    exported_at: datetime
    export_kind: str
    account_hint: str
    rows: tuple[PaymentRow, ...]


def money_minor(value: str, *, error: type[PaymentBillError]) -> int:
    """Cents from a printed yuan amount, refusing anything finer than a cent."""

    if _AMOUNT.fullmatch(value) is None:
        raise error("payment bill amount is invalid")
    minor = Decimal(value) * 100
    if minor != minor.to_integral_value():
        raise error("payment bill amount is not a whole number of cents")
    return int(minor)


def agrees_with_itself(
    rows: tuple[PaymentRow, ...],
    declared_total: int,
    declared: dict[str, tuple[int, int]],
    *,
    neutral_label: str,
    totals_are_binding: bool,
    error: type[PaymentBillError],
) -> None:
    """Check the rows against the statements the preamble makes.

    This is the file's own evidence that nothing was dropped between the
    platform and here, and it is the role a balance chain plays for a bank
    statement, filled by a different mechanism.

    The counts always bind: a row that went missing changes one of them, and
    that is the loss worth refusing. The amount totals bind only where the
    platform stands behind them. WeChat's reconcile exactly. Alipay's export
    says in its own footnotes that summing the detail amounts may not match the
    totals it prints, because the two are computed differently - so holding it
    to them would be enforcing a claim it explicitly declines to make, and
    every Alipay file would be refused for being what it says it is.
    """

    if len(rows) != declared_total:
        raise error(f"payment bill declares {declared_total} records but holds {len(rows)}")
    for label, direction in (
        (INCOME, INCOME),
        (EXPENDITURE, EXPENDITURE),
        (neutral_label, DIRECTIONLESS),
    ):
        if label not in declared:
            raise error(f"payment bill does not total its {label} rows")
        count, amount_minor = declared[label]
        found = [row for row in rows if row.direction == direction]
        if len(found) != count:
            raise error(f"payment bill declares {count} {label} rows but holds {len(found)}")
        if totals_are_binding and sum(row.amount_minor for row in found) != amount_minor:
            raise error(f"payment bill {label} rows do not add up to its own total")


__all__ = [
    "DIRECTIONLESS",
    "EXPENDITURE",
    "INCOME",
    "MAX_ROWS",
    "MAX_TEXT",
    "PaymentBillError",
    "PaymentExport",
    "PaymentRow",
    "agrees_with_itself",
    "money_minor",
]
