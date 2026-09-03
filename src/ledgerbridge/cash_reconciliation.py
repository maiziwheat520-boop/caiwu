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


class CashReconciliationRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_key: str = Field(min_length=1, max_length=100)
    source_kind: str = Field(pattern=r"^(BANK_TRANSACTION|CANDIDATE)$")
    source_ref: str = Field(min_length=1, max_length=200)
    flow_kind: str = Field(pattern=r"^(INCOME|EXPENSE|CURRENT)$")
    business_unit_label: str = Field(min_length=1, max_length=100)
    item_label: str = Field(min_length=1, max_length=100)
    match_pattern: str = Field(min_length=1, max_length=300)
    amount_direction: str = Field(pattern=r"^(CREDIT|DEBIT|ANY)$")
    effective_from: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    effective_to: str | None = Field(default=None, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class CashReconciliationRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_key: str = Field(min_length=1, max_length=100)
    flow_kind: str = Field(pattern=r"^(INCOME|EXPENSE|CURRENT)$")
    business_unit_label: str = Field(min_length=1, max_length=100)
    item_label: str = Field(min_length=1, max_length=100)
    source_kind: str = Field(pattern=r"^(BANK_TRANSACTION|CANDIDATE|ADJUSTMENT)$")
    source_ref: str = Field(min_length=1, max_length=200)
    transaction_count: int = Field(ge=0)
    amount_minor: int = Field(ge=0)
    facts: tuple[CashReconciliationFact, ...] = ()

    @model_validator(mode="after")
    def verify_count(self) -> CashReconciliationRow:
        if self.transaction_count != len(self.facts):
            raise ValueError("cash reconciliation fact count mismatched")
        return self


class CashReconciliationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_kind: str = Field(pattern=r"^(UNMATCHED|MULTIPLE_RULES)$")
    source_kind: str = Field(pattern=r"^(BANK_TRANSACTION|CANDIDATE)$")
    fact_ref: str = Field(min_length=1, max_length=120)
    occurred_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    amount_minor: int
    matched_rule_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def verify_matches(self) -> CashReconciliationIssue:
        if self.issue_kind == "UNMATCHED" and self.matched_rule_keys:
            raise ValueError("unmatched cash fact cannot reference a rule")
        if self.issue_kind == "MULTIPLE_RULES" and len(self.matched_rule_keys) < 2:
            raise ValueError("cash reconciliation conflict requires multiple rules")
        if len(set(self.matched_rule_keys)) != len(self.matched_rule_keys):
            raise ValueError("cash reconciliation issue repeats a rule")
        return self


class CashReconciliationTotals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    income_minor: int = Field(ge=0)
    expense_minor: int = Field(ge=0)
    current_minor: int = Field(ge=0)


class CashReconciliationProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = Field(pattern=r"^ledgerbridge\.cash-reconciliation\.v2$")
    accounting_month: str
    rules: tuple[CashReconciliationRule, ...]
    rows: tuple[CashReconciliationRow, ...]
    issues: tuple[CashReconciliationIssue, ...]
    eligible_fact_count: int = Field(ge=0)
    matched_fact_count: int = Field(ge=0)
    unmatched_fact_count: int = Field(ge=0)
    conflicted_fact_count: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    issues_truncated: bool
    totals: CashReconciliationTotals

    @model_validator(mode="after")
    def verify_projection(self) -> CashReconciliationProjection:
        if _MONTH.fullmatch(self.accounting_month) is None:
            raise ValueError("cash reconciliation month is invalid")
        if len({rule.rule_key for rule in self.rules}) != len(self.rules):
            raise ValueError("cash reconciliation repeats a rule")
        if self.eligible_fact_count != (
            self.matched_fact_count + self.unmatched_fact_count + self.conflicted_fact_count
        ):
            raise ValueError("cash reconciliation fact coverage mismatched")
        if self.issue_count != self.unmatched_fact_count + self.conflicted_fact_count:
            raise ValueError("cash reconciliation issue count mismatched")
        if len(self.issues) > self.issue_count:
            raise ValueError("cash reconciliation issue sample exceeds total")
        if self.issues_truncated is not (len(self.issues) < self.issue_count):
            raise ValueError("cash reconciliation issue truncation flag mismatched")
        if sum(row.transaction_count for row in self.rows if row.source_kind != "ADJUSTMENT") != (
            self.matched_fact_count
        ):
            raise ValueError("cash reconciliation matched fact count mismatched")
        visible_unmatched = sum(issue.issue_kind == "UNMATCHED" for issue in self.issues)
        visible_conflicted = sum(issue.issue_kind == "MULTIPLE_RULES" for issue in self.issues)
        if not self.issues_truncated and (
            visible_unmatched != self.unmatched_fact_count
            or visible_conflicted != self.conflicted_fact_count
        ):
            raise ValueError("cash reconciliation visible issues mismatched")
        expected = {
            "INCOME": self.totals.income_minor,
            "EXPENSE": self.totals.expense_minor,
            "CURRENT": self.totals.current_minor,
        }
        for flow_kind, total in expected.items():
            if sum(row.amount_minor for row in self.rows if row.flow_kind == flow_kind) != total:
                raise ValueError("cash reconciliation total mismatched")
        return self
