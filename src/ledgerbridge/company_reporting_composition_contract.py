"""Category-composition companion contract for company reporting."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.candidate_contract import JSON_SAFE_INTEGER
from ledgerbridge.company_reporting_contract import (
    MAX_REPORT_COMPANIES,
    CompanyReportBasis,
    validate_report_month_range,
)

COMPANY_REPORT_COMPOSITION_CONTRACT_VERSION = "ledgerbridge.company-report-composition.v1"
MAX_REPORT_CATEGORIES = 100


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


NonNegativeMoney = Annotated[
    int,
    Field(strict=True, ge=0, le=JSON_SAFE_INTEGER),
]
PositiveMoney = Annotated[
    int,
    Field(strict=True, gt=0, le=JSON_SAFE_INTEGER),
]
ReportCount = Annotated[
    int,
    Field(strict=True, ge=0, le=JSON_SAFE_INTEGER),
]
PositiveReportCount = Annotated[
    int,
    Field(strict=True, gt=0, le=JSON_SAFE_INTEGER),
]


class CompanyReportCategorySlice(_FrozenModel):
    category_code: str | None = Field(default=None, min_length=1, max_length=100)
    category_label: str | None = Field(default=None, min_length=1, max_length=200)
    amount_minor: PositiveMoney
    fact_count: PositiveReportCount

    @model_validator(mode="after")
    def validate_category_identity(self) -> CompanyReportCategorySlice:
        if (self.category_code is None) != (self.category_label is None):
            raise ValueError("category code and label must be supplied together")
        return self


class CompanyReportCategoryComposition(_FrozenModel):
    total_minor: NonNegativeMoney
    fact_count: ReportCount
    items: tuple[CompanyReportCategorySlice, ...] = Field(
        default=(), max_length=MAX_REPORT_CATEGORIES
    )

    @model_validator(mode="after")
    def validate_totals_and_order(self) -> CompanyReportCategoryComposition:
        if sum(item.amount_minor for item in self.items) != self.total_minor:
            raise ValueError("category amounts must reconcile to the composition total")
        if sum(item.fact_count for item in self.items) != self.fact_count:
            raise ValueError("category fact counts must reconcile to the composition count")
        identities = [(item.category_code, item.category_label) for item in self.items]
        if len(identities) != len(set(identities)):
            raise ValueError("category composition identities must be unique")
        expected = tuple(
            sorted(
                self.items,
                key=lambda item: (
                    -item.amount_minor,
                    item.category_code is None,
                    item.category_code or "",
                    item.category_label or "",
                ),
            )
        )
        if self.items != expected:
            raise ValueError("category composition must use stable amount order")
        return self


class ConfirmedCandidateComposition(_FrozenModel):
    company_ref: UUID
    company_name: str = Field(min_length=1, max_length=200)
    currency: Literal["CNY"] = "CNY"
    basis: Literal[CompanyReportBasis.CONFIRMED_CANDIDATE]
    positive: CompanyReportCategoryComposition
    negative: CompanyReportCategoryComposition


class PostedLedgerComposition(_FrozenModel):
    company_ref: UUID
    company_name: str = Field(min_length=1, max_length=200)
    currency: Literal["CNY"] = "CNY"
    basis: Literal[CompanyReportBasis.POSTED_LEDGER]
    revenue: CompanyReportCategoryComposition
    expense: CompanyReportCategoryComposition


CompanyReportCompositionItem = Annotated[
    ConfirmedCandidateComposition | PostedLedgerComposition,
    Field(discriminator="basis"),
]


class CompanyReportCompositionPage(_FrozenModel):
    contract_version: Literal["ledgerbridge.company-report-composition.v1"] = (
        "ledgerbridge.company-report-composition.v1"
    )
    basis: Literal[
        CompanyReportBasis.CONFIRMED_CANDIDATE,
        CompanyReportBasis.POSTED_LEDGER,
    ]
    from_month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    to_month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    items: tuple[CompanyReportCompositionItem, ...] = Field(
        default=(), max_length=MAX_REPORT_COMPANIES
    )

    @model_validator(mode="after")
    def validate_page(self) -> CompanyReportCompositionPage:
        validate_report_month_range(self.from_month, self.to_month)
        refs = [item.company_ref for item in self.items]
        if refs != sorted(set(refs), key=lambda value: value.int):
            raise ValueError("composition companies must be unique and stably ordered")
        if any(item.basis is not self.basis for item in self.items):
            raise ValueError("composition page cannot mix financial bases")
        return self
