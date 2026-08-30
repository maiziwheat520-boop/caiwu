"""Disabled-by-default read-only PayrollVerification publication route."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    AuthenticationDenied,
    AuthorizationDenied,
    Capability,
    WorkloadPrincipal,
    require_capability,
)
from ledgerbridge.payroll_integration import (
    HttpPayrollPublicationSource,
    PayrollHttpTransport,
    PayrollIntegrationError,
    PayrollPublicationSource,
    UrllibPayrollHttpTransport,
)


class PayrollPublicationReadResponse(BaseModel):
    """Source-faithful accounting projection with no payment operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["ledgerbridge.payroll-publication-read.v1"] = (
        "ledgerbridge.payroll-publication-read.v1"
    )
    entity_ref: UUID
    company_id: str
    publication_id: str
    publication: dict[str, object]


class InternalPayrollProblem(RuntimeError):
    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def _problem_response(status_code: int, code: str) -> JSONResponse:
    titles = {
        status.HTTP_400_BAD_REQUEST: "Bad Request",
        status.HTTP_401_UNAUTHORIZED: "Unauthorized",
        status.HTTP_403_FORBIDDEN: "Forbidden",
        status.HTTP_404_NOT_FOUND: "Not Found",
        status.HTTP_409_CONFLICT: "Conflict",
        status.HTTP_422_UNPROCESSABLE_CONTENT: "Unprocessable Content",
        status.HTTP_503_SERVICE_UNAVAILABLE: "Service Unavailable",
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


class InternalPayrollRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                response = await original(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            except InternalPayrollProblem as exc:
                return _problem_response(exc.status_code, exc.code)
            except AuthenticationDenied:
                return _problem_response(status.HTTP_401_UNAUTHORIZED, "AUTH_REQUIRED")
            except AuthorizationDenied:
                return _problem_response(status.HTTP_403_FORBIDDEN, "CAPABILITY_REQUIRED")
            except PayrollIntegrationError as exc:
                return _payroll_error_response(exc)

        return route_handler


def _payroll_error_response(error: PayrollIntegrationError) -> JSONResponse:
    if error.error_code == "PAYROLL_PUBLICATION_ID_INVALID":
        status_code = status.HTTP_400_BAD_REQUEST
    elif error.error_code == "PAYROLL_PUBLICATION_NOT_FOUND":
        status_code = status.HTTP_404_NOT_FOUND
    elif error.error_code == "PAYROLL_IDEMPOTENCY_CONFLICT":
        status_code = status.HTTP_409_CONFLICT
    elif error.error_code in {
        "PAYROLL_PROVIDER_TIMEOUT",
        "PAYROLL_PROVIDER_UNAVAILABLE",
        "PAYROLL_PROVIDER_REJECTED",
        "PAYROLL_PROVIDER_RESPONSE",
        "PAYROLL_COMPANY_MAPPING_INVALID",
        "PAYROLL_COMPANY_MAPPING_MISSING",
        "PAYROLL_COMPANY_MAPPING_CONFLICT",
    }:
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return _problem_response(status_code, error.error_code)


def require_internal_payroll_api(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not settings.enable_payroll_integration:
        raise InternalPayrollProblem(
            status.HTTP_404_NOT_FOUND,
            "PAYROLL_INTEGRATION_DISABLED",
        )


def require_payroll_publication_read(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.PAYROLL_PUBLICATION_READ)
    return principal


def get_payroll_http_transport() -> PayrollHttpTransport:
    """Deployment composition may replace this with an authenticated transport."""

    return UrllibPayrollHttpTransport()


def get_payroll_publication_source(
    settings: Annotated[Settings, Depends(get_settings)],
    transport: Annotated[PayrollHttpTransport, Depends(get_payroll_http_transport)],
) -> PayrollPublicationSource:
    base_url = settings.payroll_base_url
    if base_url is None:
        raise PayrollIntegrationError(
            "PAYROLL_PROVIDER_UNAVAILABLE",
            "payroll provider configuration is unavailable",
        )
    return HttpPayrollPublicationSource(
        base_url=base_url,
        timeout_seconds=settings.payroll_timeout_seconds,
        company_mapping=settings.payroll_company_mapping,
        enabled=settings.enable_payroll_integration,
        transport=transport,
    )


def _entity_from_principal(principal: WorkloadPrincipal) -> UUID:
    entities = frozenset(grant.entity_ref for grant in principal.grants)
    if len(entities) != 1:
        raise InternalPayrollProblem(
            status.HTTP_404_NOT_FOUND,
            "PAYROLL_COMPANY_SCOPE_UNAVAILABLE",
        )
    return next(iter(entities))


def _server_idempotency_key(entity_ref: UUID, publication_id: str) -> str:
    digest = hashlib.sha256(f"{entity_ref}:{publication_id}".encode("ascii")).hexdigest()
    return f"payroll-read-{digest}"


router = APIRouter(
    prefix="/internal/v1",
    tags=["internal-payroll-read"],
    dependencies=[Depends(require_internal_payroll_api)],
    route_class=InternalPayrollRoute,
)


@router.get(
    "/payroll-publications/{publication_id}",
    response_model=PayrollPublicationReadResponse,
)
def get_payroll_publication(
    publication_id: str,
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_publication_read)],
    source: Annotated[PayrollPublicationSource, Depends(get_payroll_publication_source)],
) -> PayrollPublicationReadResponse:
    if request.url.query:
        raise InternalPayrollProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    entity_ref = _entity_from_principal(principal)
    publication = source.pull_publication(
        entity_ref=entity_ref,
        publication_id=publication_id,
        idempotency_key=_server_idempotency_key(entity_ref, publication_id),
    )
    return PayrollPublicationReadResponse(
        entity_ref=entity_ref,
        company_id=publication.company_id,
        publication_id=publication.publication_id,
        publication=publication.payload_copy(),
    )
