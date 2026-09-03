"""Read and decide auditable classifications of confirmed company bank transactions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.internal_candidate_command import (
    CandidateCommandIdempotencyConflict,
    CandidateCommandRejected,
    CandidateCommandUnavailable,
)
from ledgerbridge.internal_read_contract import (
    Capability,
    ResourceNotVisible,
    WorkloadPrincipal,
    require_capability,
)


class CompanyTransactionCategory(StrEnum):
    PLATFORM_ROOM_REVENUE = "PLATFORM_ROOM_REVENUE"
    RELATED_PARTY_CURRENT = "RELATED_PARTY_CURRENT"
    PAYROLL = "PAYROLL"
    FINANCING = "FINANCING"
    BOTTLED_WATER = "BOTTLED_WATER"
    INTERNAL_TRANSFER = "INTERNAL_TRANSFER"
    RENT = "RENT"
    BANK_INTEREST = "BANK_INTEREST"
    LINEN_LAUNDRY = "LINEN_LAUNDRY"
    OPERATING_FEE = "OPERATING_FEE"


class CashflowRole(StrEnum):
    OPERATING_INCOME = "OPERATING_INCOME"
    OPERATING_EXPENSE = "OPERATING_EXPENSE"
    NON_OPERATING = "NON_OPERATING"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompanyTransactionClassification(_FrozenModel):
    transaction_ref: UUID
    entity_ref: UUID
    occurred_at: datetime
    amount_minor: int
    currency: Literal["CNY"] = "CNY"
    counterparty_name: str | None = Field(default=None, max_length=300)
    transaction_name: str = Field(min_length=1, max_length=300)
    status: Literal["PENDING", "CONFIRMED"]
    category_code: CompanyTransactionCategory | None
    cashflow_role: CashflowRole | None
    revision: int = Field(strict=True, ge=1)
    source: Literal["AUTO_RULE", "HUMAN_REVIEW"]
    rule_version: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def category_matches_status(self) -> CompanyTransactionClassification:
        if (self.status == "PENDING") != (self.category_code is None):
            raise ValueError("pending classification must be uncategorized")
        if (self.status == "PENDING") != (self.cashflow_role is None):
            raise ValueError("pending classification cannot claim a cashflow role")
        return self


class CompanyTransactionClassificationPage(_FrozenModel):
    contract_version: Literal["ledgerbridge.company-transaction-classification.v1"] = (
        "ledgerbridge.company-transaction-classification.v1"
    )
    items: tuple[CompanyTransactionClassification, ...] = Field(max_length=200)


class CompanyTransactionCategorySummary(_FrozenModel):
    category_code: CompanyTransactionCategory
    cashflow_role: CashflowRole
    transaction_count: int = Field(strict=True, ge=0)
    inflow_minor: int = Field(strict=True, ge=0)
    outflow_minor: int = Field(strict=True, ge=0)
    net_minor: int
    gross_minor: int = Field(strict=True, ge=0)
    transaction_share_ppm: int = Field(strict=True, ge=0, le=1_000_000)
    gross_share_ppm: int = Field(strict=True, ge=0, le=1_000_000)


class CompanyTransactionClassificationSummary(_FrozenModel):
    entity_ref: UUID
    from_date: date
    to_date_exclusive: date
    confirmed_count: int = Field(strict=True, ge=0)
    pending_count: int = Field(strict=True, ge=0)
    confirmed_gross_minor: int = Field(strict=True, ge=0)
    categories: tuple[CompanyTransactionCategorySummary, ...]


class CompanyTransactionClassificationSummaryPage(_FrozenModel):
    contract_version: Literal["ledgerbridge.company-transaction-classification-summary.v1"] = (
        "ledgerbridge.company-transaction-classification-summary.v1"
    )
    items: tuple[CompanyTransactionClassificationSummary, ...]


class CompanyTransactionClassificationReviewRequest(_FrozenModel):
    entity_ref: UUID
    expected_revision: int = Field(strict=True, ge=1)
    category_code: CompanyTransactionCategory
    reason: str = Field(min_length=1, max_length=1000)


class CompanyTransactionClassificationReviewReceipt(_FrozenModel):
    contract_version: Literal["ledgerbridge.company-transaction-classification-review.v1"] = (
        "ledgerbridge.company-transaction-classification-review.v1"
    )
    transaction_ref: UUID
    status: Literal["CONFIRMED"]
    category_code: CompanyTransactionCategory
    revision: int = Field(strict=True, ge=2)
    created: bool


class DatabaseCompanyTransactionClassificationService:
    def __init__(
        self,
        reader_factory: Callable[[], Session],
        api_factory: Callable[[], Session],
    ) -> None:
        self._reader_factory = reader_factory
        self._api_factory = api_factory

    @staticmethod
    def _entity_refs(principal: WorkloadPrincipal, capability: Capability) -> tuple[UUID, ...]:
        require_capability(principal, capability)
        refs = tuple(
            sorted({grant.entity_ref for grant in principal.grants}, key=lambda item: item.int)
        )
        if not refs:
            raise ResourceNotVisible("company transaction classification scope is not visible")
        return refs

    @staticmethod
    def _audit_horizon(session: Session) -> tuple[int, bytes]:
        row = (
            session.execute(
                text("SELECT sequence, hash FROM internal_read.current_audit_horizon()")
            )
            .mappings()
            .first()
        )
        if row is None:
            raise CandidateCommandUnavailable("audit horizon is unavailable")
        sequence = int(row["sequence"])
        horizon_hash = bytes(row["hash"])
        if sequence < 1 or len(horizon_hash) != 32:
            raise CandidateCommandUnavailable("audit horizon is invalid")
        return sequence, horizon_hash

    def list_current(
        self,
        principal: WorkloadPrincipal,
        *,
        status: Literal["PENDING", "CONFIRMED"] = "PENDING",
    ) -> CompanyTransactionClassificationPage:
        entity_refs = self._entity_refs(principal, Capability.BANK_STATEMENT_REVIEW_READ)
        try:
            with self._reader_factory() as session:
                sequence, horizon_hash = self._audit_horizon(session)
                items: list[CompanyTransactionClassification] = []
                for entity_ref in entity_refs:
                    rows = session.execute(
                        text(
                            "SELECT item FROM "
                            "internal_read.list_company_transaction_classifications_as_of("
                            ":entity_ref, :status, :sequence, :horizon_hash, 200)"
                        ),
                        {
                            "entity_ref": entity_ref,
                            "status": status,
                            "sequence": sequence,
                            "horizon_hash": horizon_hash,
                        },
                    ).mappings()
                    items.extend(
                        CompanyTransactionClassification.model_validate(row["item"]) for row in rows
                    )
        except (SQLAlchemyError, TypeError, ValueError, KeyError) as exc:
            raise CandidateCommandUnavailable(
                "company transaction classification reader is unavailable"
            ) from exc
        items.sort(key=lambda item: (item.occurred_at, item.transaction_ref.int), reverse=True)
        return CompanyTransactionClassificationPage(items=tuple(items[:200]))

    def summaries(
        self,
        principal: WorkloadPrincipal,
        *,
        from_date: date,
        to_date_exclusive: date,
    ) -> CompanyTransactionClassificationSummaryPage:
        if from_date >= to_date_exclusive:
            raise ValueError("from_date must precede to_date_exclusive")
        entity_refs = self._entity_refs(principal, Capability.COMPANY_REPORT_READ)
        try:
            with self._reader_factory() as session:
                sequence, horizon_hash = self._audit_horizon(session)
                items = tuple(
                    CompanyTransactionClassificationSummary.model_validate(
                        session.execute(
                            text(
                                "SELECT "
                                "internal_read.get_company_transaction_classification_summary_as_of("
                                ":entity_ref, :from_date, :to_date, :sequence, :horizon_hash)"
                            ),
                            {
                                "entity_ref": entity_ref,
                                "from_date": from_date,
                                "to_date": to_date_exclusive,
                                "sequence": sequence,
                                "horizon_hash": horizon_hash,
                            },
                        ).scalar_one()
                    )
                    for entity_ref in entity_refs
                )
        except (SQLAlchemyError, TypeError, ValueError, KeyError) as exc:
            raise CandidateCommandUnavailable(
                "company transaction classification summary is unavailable"
            ) from exc
        return CompanyTransactionClassificationSummaryPage(items=items)

    def review(
        self,
        principal: WorkloadPrincipal,
        *,
        transaction_ref: UUID,
        operation_id: UUID,
        assertion_jti: UUID,
        actor_ref: str,
        command: CompanyTransactionClassificationReviewRequest,
    ) -> CompanyTransactionClassificationReviewReceipt:
        entity_refs = self._entity_refs(principal, Capability.BANK_STATEMENT_REVIEW_DECIDE)
        if command.entity_ref not in entity_refs:
            raise ResourceNotVisible("company transaction is outside the authorized scope")
        try:
            with self._api_factory() as session:
                raw = session.execute(
                    text(
                        "SELECT internal_command.review_company_transaction_classification("
                        ":transaction_ref, :entity_ref, :operation_id, :assertion_jti, "
                        ":actor_ref, :principal_ref, :expected_revision, :category_code, :reason)"
                    ),
                    {
                        "transaction_ref": transaction_ref,
                        "entity_ref": command.entity_ref,
                        "operation_id": operation_id,
                        "assertion_jti": assertion_jti,
                        "actor_ref": actor_ref,
                        "principal_ref": principal.principal_ref,
                        "expected_revision": command.expected_revision,
                        "category_code": command.category_code.value,
                        "reason": command.reason,
                    },
                ).scalar_one()
                receipt = CompanyTransactionClassificationReviewReceipt.model_validate(raw)
                session.commit()
                return receipt
        except DBAPIError as exc:
            sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            if sqlstate == "LB001":
                raise CandidateCommandIdempotencyConflict(
                    "company transaction review idempotency conflict"
                ) from exc
            if sqlstate == "LB002":
                raise CandidateCommandRejected(
                    "company transaction review revision is stale"
                ) from exc
            if sqlstate == "LB003":
                raise CandidateCommandRejected("company transaction review was rejected") from exc
            if sqlstate == "LB004":
                raise ResourceNotVisible(
                    "company transaction is outside the authorized scope"
                ) from exc
            raise CandidateCommandUnavailable("company transaction review is unavailable") from exc
        except (SQLAlchemyError, TypeError, ValueError) as exc:
            raise CandidateCommandUnavailable("company transaction review is unavailable") from exc
