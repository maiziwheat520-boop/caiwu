"""Cash-basis reconciliation projection generated directly from imported transactions."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


class CashReconciliationFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_ref: str = Field(min_length=1, max_length=100)
    occurred_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    amount_minor: int


class CashReconciliationRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_key: str = Field(min_length=1, max_length=100)
    flow_kind: str = Field(pattern=r"^(INCOME|EXPENSE|CURRENT)$")
    business_unit_label: str = Field(min_length=1, max_length=100)
    item_label: str = Field(min_length=1, max_length=100)
    source_kind: str = Field(pattern=r"^(BANK_TRANSACTION|CANDIDATE|ADJUSTMENT)$")
    transaction_count: int = Field(ge=0)
    amount_minor: int = Field(ge=0)
    facts: tuple[CashReconciliationFact, ...] = ()

    @model_validator(mode="after")
    def verify_count(self) -> CashReconciliationRow:
        if self.transaction_count != len(self.facts):
            raise ValueError("cash reconciliation fact count mismatched")
        return self


class CashReconciliationTotals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    income_minor: int = Field(ge=0)
    expense_minor: int = Field(ge=0)
    current_minor: int = Field(ge=0)


class CashReconciliationProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = Field(pattern=r"^ledgerbridge\.cash-reconciliation\.v1$")
    accounting_month: str
    rows: tuple[CashReconciliationRow, ...]
    totals: CashReconciliationTotals

    @model_validator(mode="after")
    def verify_projection(self) -> CashReconciliationProjection:
        if _MONTH.fullmatch(self.accounting_month) is None:
            raise ValueError("cash reconciliation month is invalid")
        expected = {
            "INCOME": self.totals.income_minor,
            "EXPENSE": self.totals.expense_minor,
            "CURRENT": self.totals.current_minor,
        }
        for flow_kind, total in expected.items():
            if sum(row.amount_minor for row in self.rows if row.flow_kind == flow_kind) != total:
                raise ValueError("cash reconciliation total mismatched")
        return self
