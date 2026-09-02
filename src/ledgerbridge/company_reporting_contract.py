"""Versioned, basis-separated company reporting read contract."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.candidate_contract import JSON_SAFE_INTEGER

COMPANY_REPORT_CONTRACT_VERSION = "ledgerbridge.company-report.v1"
MAX_REPORT_COMPANIES = 50
MAX_REPORT_BUSINESS_UNITS = 50
MAX_REPORT_MONTHS = 24
_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


class CompanyReportBasis(StrEnum):
    CONFIRMED_CANDIDATE = "CONFIRMED_CANDIDATE"
    ACCOUNT_STATEMENT = "ACCOUNT_STATEMENT"
    POSTED_LEDGER = "POSTED_LEDGER"


class BusinessUnitBreakdownStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    EMPTY = "EMPTY"
    UNAVAILABLE_ATTRIBUTION_PENDING = "UNAVAILABLE_ATTRIBUTION_PENDING"
    UNAVAILABLE_MISSING_SNAPSHOT = "UNAVAILABLE_MISSING_SNAPSHOT"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


NonNegativeMoney = Annotated[
    int,
    Field(strict=True, ge=0, le=JSON_SAFE_INTEGER),
]
NegativeMoney = Annotated[
    int,
    Field(strict=True, ge=-JSON_SAFE_INTEGER, le=0),
]
SignedMoney = Annotated[
    int,
    Field(strict=True, ge=-JSON_SAFE_INTEGER, le=JSON_SAFE_INTEGER),
]
ReportCount = Annotated[
    int,
    Field(strict=True, ge=0, le=JSON_SAFE_INTEGER),
]


class ConfirmedCandidateMetrics(_FrozenModel):
    basis: Literal[CompanyReportBasis.CONFIRMED_CANDIDATE]
    confirmed_positive_minor: NonNegativeMoney
    confirmed_negative_minor: NegativeMoney
    confirmed_net_minor: SignedMoney
    confirmed_count: ReportCount
    source_count: ReportCount

    @model_validator(mode="after")
    def validate_metrics(self) -> ConfirmedCandidateMetrics:
        if self.confirmed_net_minor != (
            self.confirmed_positive_minor + self.confirmed_negative_minor
        ):
            raise ValueError("confirmed net must equal positive plus negative")
        if self.source_count > self.confirmed_count:
            raise ValueError("source count cannot exceed confirmed count")
        return self


class AccountStatementMetrics(_FrozenModel):
    basis: Literal[CompanyReportBasis.ACCOUNT_STATEMENT]
    cash_inflow_minor: NonNegativeMoney
    cash_outflow_minor: NonNegativeMoney
    net_cash_flow_minor: SignedMoney
    confirmed_transaction_count: ReportCount
    statement_count: ReportCount

    @model_validator(mode="after")
    def validate_metrics(self) -> AccountStatementMetrics:
        if self.net_cash_flow_minor != self.cash_inflow_minor - self.cash_outflow_minor:
            raise ValueError("net cash flow must equal inflow minus outflow")
        if self.statement_count > self.confirmed_transaction_count:
            raise ValueError("statement count cannot exceed transaction count")
        return self


class PostedLedgerMetrics(_FrozenModel):
    basis: Literal[CompanyReportBasis.POSTED_LEDGER]
    revenue_minor: NonNegativeMoney
    expense_minor: NonNegativeMoney
    profit_minor: SignedMoney
    posted_entry_count: ReportCount
    source_count: ReportCount

    @model_validator(mode="after")
    def validate_metrics(self) -> PostedLedgerMetrics:
        if self.profit_minor != self.revenue_minor - self.expense_minor:
            raise ValueError("profit must equal revenue minus expense")
        if self.source_count > self.posted_entry_count:
            raise ValueError("source count cannot exceed posted entry count")
        return self


CompanyReportMetrics = Annotated[
    ConfirmedCandidateMetrics | AccountStatementMetrics | PostedLedgerMetrics,
    Field(discriminator="basis"),
]


class CompanyReportBalance(_FrozenModel):
    """A deliberately empty v1 balance until an authoritative snapshot exists."""

    balance_basis: Literal["UNAVAILABLE"] = "UNAVAILABLE"
    opening_balance_minor: None = None
    closing_balance_minor: None = None
    gap: Literal["AUTHORITATIVE_BALANCE_UNAVAILABLE"] = "AUTHORITATIVE_BALANCE_UNAVAILABLE"


class _CompanyReportAggregate(_FrozenModel):
    metrics: CompanyReportMetrics
    pending_review_count: ReportCount
    attribution_pending_count: ReportCount
    missing_material_count: ReportCount | None = None
    taxonomy_version: str | None = Field(default=None, min_length=1, max_length=100)
    balance: CompanyReportBalance

    @model_validator(mode="after")
    def validate_taxonomy_pair(self) -> _CompanyReportAggregate:
        if (self.missing_material_count is None) != (self.taxonomy_version is None):
            raise ValueError("missing material count requires its taxonomy version")
        return self


class CompanyReportBusinessUnit(_CompanyReportAggregate):
    business_unit_ref: str = Field(min_length=1, max_length=100)
    business_unit_label: str = Field(min_length=1, max_length=200)


class CompanyReportMonth(_CompanyReportAggregate):
    month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    business_unit_breakdown_status: BusinessUnitBreakdownStatus
    business_units: tuple[CompanyReportBusinessUnit, ...] | None = Field(
        max_length=MAX_REPORT_BUSINESS_UNITS
    )

    @model_validator(mode="after")
    def validate_business_units(self) -> CompanyReportMonth:
        if self.business_unit_breakdown_status is BusinessUnitBreakdownStatus.AVAILABLE:
            if not self.business_units:
                raise ValueError("available business-unit breakdown requires rows")
        elif self.business_unit_breakdown_status is BusinessUnitBreakdownStatus.EMPTY:
            if self.business_units != ():
                raise ValueError("empty business-unit breakdown cannot contain rows")
        elif self.business_units is not None:
            raise ValueError("unavailable business-unit breakdown must use null rows")

        allowed_statuses = {
            CompanyReportBasis.CONFIRMED_CANDIDATE: frozenset(
                {
                    BusinessUnitBreakdownStatus.AVAILABLE,
                    BusinessUnitBreakdownStatus.EMPTY,
                }
            ),
            CompanyReportBasis.ACCOUNT_STATEMENT: frozenset(
                {
                    BusinessUnitBreakdownStatus.AVAILABLE,
                    BusinessUnitBreakdownStatus.EMPTY,
                    BusinessUnitBreakdownStatus.UNAVAILABLE_ATTRIBUTION_PENDING,
                    BusinessUnitBreakdownStatus.UNAVAILABLE_MISSING_SNAPSHOT,
                }
            ),
            CompanyReportBasis.POSTED_LEDGER: frozenset(
                {
                    BusinessUnitBreakdownStatus.AVAILABLE,
                    BusinessUnitBreakdownStatus.EMPTY,
                    BusinessUnitBreakdownStatus.UNAVAILABLE_MISSING_SNAPSHOT,
                }
            ),
        }
        if self.business_unit_breakdown_status not in allowed_statuses[self.metrics.basis]:
            raise ValueError("business-unit breakdown status is invalid for its basis")

        if self.business_units is None:
            return self
        refs = [item.business_unit_ref for item in self.business_units]
        if len(refs) != len(set(refs)):
            raise ValueError("business units must be unique within a month")
        if refs != sorted(refs):
            raise ValueError("business units must be ordered by stable reference")
        _require_basis(self.metrics.basis, self.business_units)
        return self


class CompanyReportItem(_CompanyReportAggregate):
    company_ref: UUID
    company_name: str = Field(min_length=1, max_length=200)
    currency: Literal["CNY"] = "CNY"
    business_unit_breakdown_status: BusinessUnitBreakdownStatus
    months: tuple[CompanyReportMonth, ...] = Field(default=(), max_length=MAX_REPORT_MONTHS)

    @model_validator(mode="after")
    def validate_months(self) -> CompanyReportItem:
        months = [item.month for item in self.months]
        if len(months) != len(set(months)):
            raise ValueError("report months must be unique")
        if months != sorted(months):
            raise ValueError("report months must be ordered")
        _require_basis(self.metrics.basis, self.months)
        expected_breakdown = _item_breakdown_status(self.metrics.basis, self.months)
        if self.business_unit_breakdown_status is not expected_breakdown:
            raise ValueError("company breakdown status must summarize its report months")
        return self


class CompanyReportPage(_FrozenModel):
    contract_version: Literal["ledgerbridge.company-report.v1"] = "ledgerbridge.company-report.v1"
    basis: CompanyReportBasis
    from_month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    to_month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    items: tuple[CompanyReportItem, ...] = Field(default=(), max_length=MAX_REPORT_COMPANIES)

    @model_validator(mode="after")
    def validate_page(self) -> CompanyReportPage:
        if _MONTH.fullmatch(self.from_month) is None or _MONTH.fullmatch(self.to_month) is None:
            raise ValueError("report months must use YYYY-MM")
        start = _month_ordinal(self.from_month)
        end = _month_ordinal(self.to_month)
        if start > end or end - start + 1 > MAX_REPORT_MONTHS:
            raise ValueError("report range must contain 1 to 24 inclusive months")
        refs = [item.company_ref for item in self.items]
        if len(refs) != len(set(refs)):
            raise ValueError("report companies must be unique")
        if refs != sorted(refs, key=lambda value: value.int):
            raise ValueError("report companies must be ordered by stable reference")
        _require_basis(self.basis, self.items)
        for item in self.items:
            if any(
                month.month < self.from_month or month.month > self.to_month
                for month in item.months
            ):
                raise ValueError("report month is outside the requested range")
        return self


def validate_report_month_range(from_month: str, to_month: str) -> None:
    """Validate the public service input before any database access."""

    if _MONTH.fullmatch(from_month) is None or _MONTH.fullmatch(to_month) is None:
        raise ValueError("report months must use YYYY-MM")
    start = _month_ordinal(from_month)
    end = _month_ordinal(to_month)
    if start > end or end - start + 1 > MAX_REPORT_MONTHS:
        raise ValueError("report range must contain 1 to 24 inclusive months")


def _require_basis(
    basis: CompanyReportBasis,
    aggregates: tuple[_CompanyReportAggregate, ...],
) -> None:
    if any(item.metrics.basis is not basis for item in aggregates):
        raise ValueError("report cannot mix financial bases")


def _month_ordinal(value: str) -> int:
    year, month = value.split("-", 1)
    return int(year) * 12 + int(month) - 1


def _item_breakdown_status(
    basis: CompanyReportBasis,
    months: tuple[CompanyReportMonth, ...],
) -> BusinessUnitBreakdownStatus:
    if not months:
        return BusinessUnitBreakdownStatus.EMPTY
    statuses = {month.business_unit_breakdown_status for month in months}
    if (
        basis is CompanyReportBasis.ACCOUNT_STATEMENT
        and BusinessUnitBreakdownStatus.UNAVAILABLE_ATTRIBUTION_PENDING in statuses
    ):
        return BusinessUnitBreakdownStatus.UNAVAILABLE_ATTRIBUTION_PENDING
    if (
        basis is CompanyReportBasis.ACCOUNT_STATEMENT
        and BusinessUnitBreakdownStatus.UNAVAILABLE_MISSING_SNAPSHOT in statuses
    ):
        return BusinessUnitBreakdownStatus.UNAVAILABLE_MISSING_SNAPSHOT
    if (
        basis is CompanyReportBasis.POSTED_LEDGER
        and BusinessUnitBreakdownStatus.UNAVAILABLE_MISSING_SNAPSHOT in statuses
    ):
        return BusinessUnitBreakdownStatus.UNAVAILABLE_MISSING_SNAPSHOT
    if BusinessUnitBreakdownStatus.AVAILABLE in statuses:
        return BusinessUnitBreakdownStatus.AVAILABLE
    return BusinessUnitBreakdownStatus.EMPTY
