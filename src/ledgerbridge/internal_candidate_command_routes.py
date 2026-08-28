"""Disabled-by-default synthetic D1 candidate command routes."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from ledgerbridge.candidate_contract import (
    CandidateContractError,
    CandidateRevisionConflict,
)
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_candidate_command import (
    CandidateCommandIdempotencyConflict,
    CandidateCommandRejected,
    CandidateDecisionReceipt,
    CandidateDecisionRequest,
    CandidateEventPage,
    SyntheticInternalReviewService,
    get_synthetic_review_service,
)
from ledgerbridge.internal_command_assertion import (
    UserAssertionError,
    verify_user_assertion,
)
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    AuthenticationDenied,
    AuthorizationDenied,
    Capability,
    ResourceNotVisible,
    WorkloadPrincipal,
    require_capability,
)


class InternalCandidateCommandProblem(RuntimeError):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def _problem_response(status_code: int, code: str) -> JSONResponse:
    titles = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        409: "Conflict",
        422: "Unprocessable Content",
        503: "Service Unavailable",
    }
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store"},
        content={
            "type": f"urn:ledgerbridge:problem:{code.lower().replace('_', '-')}",
            "title": titles[status_code],
            "status": status_code,
            "code": code,
        },
    )


class InternalCandidateCommandRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except InternalCandidateCommandProblem as exc:
                return _problem_response(exc.status_code, exc.code)
            except RequestValidationError:
                return _problem_response(status.HTTP_422_UNPROCESSABLE_CONTENT, "INVALID_COMMAND")
            except AuthenticationDenied:
                return _problem_response(status.HTTP_401_UNAUTHORIZED, "AUTH_REQUIRED")
            except UserAssertionError:
                return _problem_response(status.HTTP_401_UNAUTHORIZED, "USER_ASSERTION_INVALID")
            except AuthorizationDenied:
                return _problem_response(status.HTTP_403_FORBIDDEN, "CAPABILITY_REQUIRED")
            except ResourceNotVisible:
                return _problem_response(status.HTTP_404_NOT_FOUND, "RESOURCE_NOT_FOUND")
            except CandidateCommandIdempotencyConflict:
                return _problem_response(status.HTTP_409_CONFLICT, "IDEMPOTENCY_CONFLICT")
            except CandidateRevisionConflict:
                return _problem_response(status.HTTP_409_CONFLICT, "STALE_REVISION")
            except CandidateCommandRejected:
                return _problem_response(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "COMMAND_REJECTED",
                )
            except CandidateContractError:
                return _problem_response(status.HTTP_409_CONFLICT, "INVALID_TRANSITION")
        return route_handler


def require_internal_candidate_command_api(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if settings.env == "production" or not settings.enable_internal_candidate_command_api:
        raise InternalCandidateCommandProblem(
            status.HTTP_404_NOT_FOUND,
            "CANDIDATE_COMMAND_DISABLED",
        )


def get_candidate_command_service() -> SyntheticInternalReviewService:
    return get_synthetic_review_service()


def require_candidate_decide(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.CANDIDATE_DECIDE)
    return principal


def require_candidate_events_read(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.CANDIDATE_READ)
    return principal


router = APIRouter(
    prefix="/internal/v1",
    tags=["internal-candidate-command"],
    dependencies=[Depends(require_internal_candidate_command_api)],
    route_class=InternalCandidateCommandRoute,
)


@router.get("/candidate-events", response_model=CandidateEventPage)
def list_candidate_events(
    principal: Annotated[WorkloadPrincipal, Depends(require_candidate_events_read)],
    service: Annotated[SyntheticInternalReviewService, Depends(get_candidate_command_service)],
    candidate_ref: UUID | None = None,
) -> CandidateEventPage:
    return service.list_candidate_events(principal, candidate_ref=candidate_ref)


@router.post(
    "/candidates/{candidate_ref}/decisions",
    response_model=CandidateDecisionReceipt,
)
async def append_candidate_decision(
    candidate_ref: UUID,
    command: CandidateDecisionRequest,
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_candidate_decide)],
    settings: Annotated[Settings, Depends(get_settings)],
    service: Annotated[SyntheticInternalReviewService, Depends(get_candidate_command_service)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=36, max_length=36)],
    user_assertion: Annotated[
        str,
        Header(alias="X-LedgerBridge-User-Assertion", min_length=1, max_length=4096),
    ],
) -> CandidateDecisionReceipt:
    if request.url.query:
        raise InternalCandidateCommandProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    try:
        operation_id = UUID(idempotency_key)
    except ValueError as exc:
        raise InternalCandidateCommandProblem(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_IDEMPOTENCY_KEY",
        ) from exc
    if str(operation_id) != idempotency_key.lower():
        raise InternalCandidateCommandProblem(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_IDEMPOTENCY_KEY",
        )
    assertion_key = settings.internal_command_assertion_key
    issuer = settings.internal_command_assertion_issuer
    audience = settings.internal_command_assertion_audience
    if assertion_key is None or issuer is None or audience is None:
        raise UserAssertionError("user assertion verifier is unavailable")
    body = await request.body()
    claims = verify_user_assertion(
        user_assertion,
        key=assertion_key.get_secret_value().encode("utf-8"),
        issuer=issuer,
        audience=audience,
        method="POST",
        canonical_path=request.url.path,
        body=body,
        resource_ref=candidate_ref,
        expected_revision=command.expected_revision,
        operation_id=operation_id,
        workload_principal=principal,
    )
    return service.append_decision(
        principal,
        candidate_ref=candidate_ref,
        operation_id=operation_id,
        assertion_jti=claims.jti,
        actor_ref=claims.subject,
        request=command,
        decided_at=datetime.now(UTC),
    )
