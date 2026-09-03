"""Authenticated company transaction classification read and review routes."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request

from ledgerbridge.company_transaction_classification import (
    CompanyTransactionClassificationPage,
    CompanyTransactionClassificationReviewReceipt,
    CompanyTransactionClassificationReviewRequest,
    CompanyTransactionClassificationSummaryPage,
    DatabaseCompanyTransactionClassificationService,
)
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.db import get_session_factory
from ledgerbridge.internal_candidate_command_routes import (
    InternalCandidateCommandProblem,
    InternalCandidateCommandRoute,
    require_internal_candidate_command_api,
)
from ledgerbridge.internal_command_assertion import UserAssertionError, verify_user_assertion
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import Capability, WorkloadPrincipal, require_capability


def get_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> DatabaseCompanyTransactionClassificationService:
    if settings.internal_candidate_command_backend != "database":
        raise InternalCandidateCommandProblem(503, "COMPANY_CLASSIFICATION_UNAVAILABLE")
    return DatabaseCompanyTransactionClassificationService(
        get_session_factory(settings.resolved_reader_database_url()),
        get_session_factory(settings.resolved_api_database_url()),
    )


def require_read(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.BANK_STATEMENT_REVIEW_READ)
    return principal


def require_report_read(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.COMPANY_REPORT_READ)
    return principal


def require_decide(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.BANK_STATEMENT_REVIEW_DECIDE)
    return principal


router = APIRouter(
    prefix="/internal/v1",
    tags=["company-transaction-classification"],
    dependencies=[Depends(require_internal_candidate_command_api)],
    route_class=InternalCandidateCommandRoute,
)


@router.get(
    "/company-transaction-classifications",
    response_model=CompanyTransactionClassificationPage,
)
def list_company_transaction_classifications(
    principal: Annotated[WorkloadPrincipal, Depends(require_read)],
    service: Annotated[DatabaseCompanyTransactionClassificationService, Depends(get_service)],
    classification_status: Annotated[
        Literal["PENDING", "CONFIRMED"], Query(alias="status")
    ] = "PENDING",
) -> CompanyTransactionClassificationPage:
    return service.list_current(principal, status=classification_status)


@router.get(
    "/company-transaction-classification-summary",
    response_model=CompanyTransactionClassificationSummaryPage,
)
def get_company_transaction_classification_summary(
    from_date: date,
    to_date_exclusive: date,
    principal: Annotated[WorkloadPrincipal, Depends(require_report_read)],
    service: Annotated[DatabaseCompanyTransactionClassificationService, Depends(get_service)],
) -> CompanyTransactionClassificationSummaryPage:
    if from_date >= to_date_exclusive:
        raise InternalCandidateCommandProblem(422, "INVALID_COMMAND")
    return service.summaries(
        principal,
        from_date=from_date,
        to_date_exclusive=to_date_exclusive,
    )


@router.post(
    "/company-transaction-classifications/{transaction_ref}/reviews",
    response_model=CompanyTransactionClassificationReviewReceipt,
)
async def review_company_transaction_classification(
    transaction_ref: UUID,
    command: CompanyTransactionClassificationReviewRequest,
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_decide)],
    settings: Annotated[Settings, Depends(get_settings)],
    service: Annotated[DatabaseCompanyTransactionClassificationService, Depends(get_service)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=36, max_length=36)],
    user_assertion: Annotated[
        str,
        Header(alias="X-LedgerBridge-User-Assertion", min_length=1, max_length=4096),
    ],
) -> CompanyTransactionClassificationReviewReceipt:
    if request.url.query:
        raise InternalCandidateCommandProblem(400, "INVALID_QUERY")
    try:
        operation_id = UUID(idempotency_key)
    except ValueError as exc:
        raise InternalCandidateCommandProblem(400, "INVALID_IDEMPOTENCY_KEY") from exc
    if str(operation_id) != idempotency_key.lower():
        raise InternalCandidateCommandProblem(400, "INVALID_IDEMPOTENCY_KEY")
    assertion_key = settings.internal_command_assertion_key
    issuer = settings.internal_command_assertion_issuer
    audience = settings.internal_command_assertion_audience
    if assertion_key is None or issuer is None or audience is None:
        raise UserAssertionError("user assertion verifier is unavailable")
    claims = verify_user_assertion(
        user_assertion,
        key=assertion_key.get_secret_value().encode("utf-8"),
        issuer=issuer,
        audience=audience,
        method="POST",
        canonical_path=request.url.path,
        body=await request.body(),
        resource_ref=transaction_ref,
        expected_revision=command.expected_revision,
        operation_id=operation_id,
        workload_principal=principal,
    )
    return service.review(
        principal,
        transaction_ref=transaction_ref,
        operation_id=operation_id,
        assertion_jti=claims.jti,
        actor_ref=claims.subject,
        command=command,
    )
