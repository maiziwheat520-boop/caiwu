"""Closed authenticated route for bank statement review decisions."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.db import get_session_factory
from ledgerbridge.internal_bank_statement_review import (
    BankStatementReviewReceipt,
    BankStatementReviewRequest,
    DatabaseBankStatementReviewService,
)
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
) -> DatabaseBankStatementReviewService:
    if settings.internal_candidate_command_backend != "database":
        raise InternalCandidateCommandProblem(503, "BANK_STATEMENT_REVIEW_UNAVAILABLE")
    return DatabaseBankStatementReviewService(
        get_session_factory(settings.resolved_reader_database_url()),
        get_session_factory(settings.resolved_api_database_url()),
    )


def require_review(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.CANDIDATE_DECIDE)
    return principal


router = APIRouter(
    prefix="/internal/v1",
    tags=["internal-bank-statement-review"],
    dependencies=[Depends(require_internal_candidate_command_api)],
    route_class=InternalCandidateCommandRoute,
)


@router.post("/bank-statements/{statement_ref}/reviews", response_model=BankStatementReviewReceipt)
async def review_bank_statement(
    statement_ref: UUID,
    command: BankStatementReviewRequest,
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_review)],
    settings: Annotated[Settings, Depends(get_settings)],
    service: Annotated[DatabaseBankStatementReviewService, Depends(get_service)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=36, max_length=36)],
    user_assertion: Annotated[
        str,
        Header(
            alias="X-LedgerBridge-User-Assertion",
            min_length=1,
            max_length=4096,
        ),
    ],
) -> BankStatementReviewReceipt:
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
        resource_ref=statement_ref,
        expected_revision=command.expected_revision,
        operation_id=operation_id,
        workload_principal=principal,
    )
    return service.review(
        principal,
        statement_ref=statement_ref,
        operation_id=operation_id,
        assertion_jti=claims.jti,
        actor_ref=claims.subject,
        command=command,
    )
