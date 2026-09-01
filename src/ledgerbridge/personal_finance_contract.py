"""Closed response contract for one formally imported personal bank statement."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.candidate_contract import JSON_SAFE_INTEGER

MoneyMinor = Annotated[
    int,
    Field(strict=True, ge=-JSON_SAFE_INTEGER, le=JSON_SAFE_INTEGER),
]
NonnegativeMoneyMinor = Annotated[
    int,
    Field(strict=True, ge=0, le=JSON_SAFE_INTEGER),
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PersonalFinanceStatement(_FrozenModel):
    statement_ref: UUID
    managed_account_ref: UUID
    institution_code: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{0,31}$")
    account_suffix: str = Field(pattern=r"^[0-9]{4,8}$")
    period_start: date
    period_end: date
    transaction_count: int = Field(strict=True, ge=1, le=10_000)
    review_status: Literal["PENDING", "CONFIRMED", "REJECTED"]
    review_revision: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def period_is_ordered(self) -> PersonalFinanceStatement:
        if self.period_end < self.period_start:
            raise ValueError("personal statement period is invalid")
        return self


class PersonalFinanceSummary(_FrozenModel):
    currency: Literal["CNY"] = "CNY"
    cash_inflow_minor: NonnegativeMoneyMinor
    cash_outflow_minor: NonnegativeMoneyMinor
    net_cash_flow_minor: MoneyMinor


class PersonalFinanceTransaction(_FrozenModel):
    source_row_number: int = Field(strict=True, ge=1)
    occurred_at: datetime
    amount_minor: MoneyMinor
    balance_minor: MoneyMinor
    currency: Literal["CNY"]
    counterparty_name: str | None = Field(default=None, min_length=1, max_length=300)
    counterparty_account_masked: str | None = Field(default=None, min_length=1, max_length=300)
    counterparty_institution: str | None = Field(default=None, min_length=1, max_length=300)
    transaction_name: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def timestamp_is_timezone_aware(self) -> PersonalFinanceTransaction:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("personal transaction timestamp must be timezone-aware")
        return self


class PersonalFinancePage(_FrozenModel):
    contract_version: Literal["ledgerbridge.personal-finance.v1"] = (
        "ledgerbridge.personal-finance.v1"
    )
    snapshot_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_kind: Literal["PERSON"] = "PERSON"
    statement: PersonalFinanceStatement
    summary: PersonalFinanceSummary
    items: tuple[PersonalFinanceTransaction, ...] = Field(max_length=10_000)

    @model_validator(mode="after")
    def page_is_complete_and_reconciled(self) -> PersonalFinancePage:
        row_numbers = [item.source_row_number for item in self.items]
        if len(self.items) != self.statement.transaction_count or row_numbers != sorted(
            set(row_numbers)
        ):
            raise ValueError("personal transaction page is incomplete or unstable")
        inflow = sum(item.amount_minor for item in self.items if item.amount_minor > 0)
        outflow = sum(-item.amount_minor for item in self.items if item.amount_minor < 0)
        if (
            self.summary.cash_inflow_minor != inflow
            or self.summary.cash_outflow_minor != outflow
            or self.summary.net_cash_flow_minor != inflow - outflow
        ):
            raise ValueError("personal transaction summary does not reconcile")
        return self
