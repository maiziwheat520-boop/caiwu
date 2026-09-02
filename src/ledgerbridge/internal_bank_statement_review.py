"""Authorized append-only decisions for imported bank statements."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.internal_candidate_command import (
    CandidateCommandRejected,
    CandidateCommandUnavailable,
)
from ledgerbridge.internal_read_contract import WorkloadPrincipal
from ledgerbridge.personal_finance_service import DatabasePersonalFinanceService


class BankStatementReviewUnavailable(CandidateCommandUnavailable):
    pass


class BankStatementReviewRejected(CandidateCommandRejected):
    pass


class BankStatementReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entity_ref: UUID
    expected_revision: int = Field(strict=True, ge=1)
    decision: Literal["CONFIRMED", "REJECTED"]
    reason: str = Field(min_length=1, max_length=1000)


class BankStatementReviewReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract_version: Literal["ledgerbridge.bank-statement-review.v1"] = (
        "ledgerbridge.bank-statement-review.v1"
    )
    statement_ref: UUID
    decision: Literal["CONFIRMED", "REJECTED"]
    revision: int = Field(strict=True, ge=2)
    created: bool


class DatabaseBankStatementReviewService:
    def __init__(
        self,
        reader_factory: Callable[[], Session],
        api_factory: Callable[[], Session],
    ) -> None:
        self._reader = DatabasePersonalFinanceService(reader_factory)
        self._api_factory = api_factory

    def review(
        self,
        principal: WorkloadPrincipal,
        *,
        statement_ref: UUID,
        operation_id: UUID,
        assertion_jti: UUID,
        actor_ref: str,
        command: BankStatementReviewRequest,
    ) -> BankStatementReviewReceipt:
        current = self._reader.statement(
            principal,
            statement_ref=statement_ref,
            entity_ref=command.entity_ref,
        )
        if current.statement.review_revision != command.expected_revision:
            raise BankStatementReviewRejected("bank statement review revision is stale")
        try:
            with self._api_factory() as session:
                raw = session.execute(
                    text(
                        "SELECT internal_command.review_bank_statement("
                        ":statement_ref, :operation_id, :assertion_jti, :actor_ref, "
                        ":principal_ref, :expected_revision, :decision, :reason)"
                    ),
                    {
                        "statement_ref": statement_ref,
                        "operation_id": operation_id,
                        "assertion_jti": assertion_jti,
                        "actor_ref": actor_ref,
                        "principal_ref": principal.principal_ref,
                        "expected_revision": command.expected_revision,
                        "decision": command.decision,
                        "reason": command.reason,
                    },
                ).scalar_one()
                receipt = BankStatementReviewReceipt.model_validate(raw)
                session.commit()
                return receipt
        except DBAPIError as exc:
            raise BankStatementReviewRejected("bank statement review was rejected") from exc
        except (SQLAlchemyError, TypeError, ValueError) as exc:
            raise BankStatementReviewUnavailable("bank statement review is unavailable") from exc
