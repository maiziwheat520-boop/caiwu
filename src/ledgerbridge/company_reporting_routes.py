"""Closed, read-only internal HTTP route for company reporting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from ledgerbridge.company_reporting_composition_contract import (
    CompanyReportCompositionPage,
)
from ledgerbridge.company_reporting_contract import (
    CompanyReportBasis,
    CompanyReportPage,
    validate_report_month_range,
)
from ledgerbridge.company_reporting_service import DatabaseCompanyReportingService
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.db import get_session_factory
from ledgerbridge.internal_read_contract import (
    Capability,
    WorkloadPrincipal,
    authorize_collection_read,
)
from ledgerbridge.internal_read_routes import (
    InternalReadProblem,
    InternalReadRoute,
    _closed_query,
    _parse_month,
    _parse_uuid,
    require_company_report_read,
    require_internal_read_api,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable


@dataclass(frozen=True, slots=True)
class _CompanyReportParams:
    basis: CompanyReportBasis
    from_month: str
    to_month: str
    company_ref: UUID | None


def _parse_company_report_params(request: Request) -> _CompanyReportParams:
    query = _closed_query(
        request,
        allowed=frozenset({"basis", "from_month", "to_month", "company_ref"}),
        required=frozenset({"basis", "from_month", "to_month"}),
    )
    from_month = _parse_month(query["from_month"])
    to_month = _parse_month(query["to_month"])
    try:
        validate_report_month_range(from_month, to_month)
        basis = CompanyReportBasis(query["basis"])
    except ValueError as exc:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY") from exc
    company_ref = query.get("company_ref")
    return _CompanyReportParams(
        basis=basis,
        from_month=from_month,
        to_month=to_month,
        company_ref=None if company_ref is None else _parse_uuid(company_ref),
    )


def _parse_company_report_composition_params(request: Request) -> _CompanyReportParams:
    params = _parse_company_report_params(request)
    if params.basis is CompanyReportBasis.ACCOUNT_STATEMENT:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    return params


def get_company_reporting_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> DatabaseCompanyReportingService:
    if settings.internal_read_backend != "database":
        raise InternalReadBackendUnavailable("company reporting requires the database read backend")
    return DatabaseCompanyReportingService(
        get_session_factory(settings.resolved_reader_database_url())
    )


router = APIRouter(
    prefix="/internal/v1",
    tags=["internal-company-reporting"],
    dependencies=[Depends(require_internal_read_api)],
    route_class=InternalReadRoute,
)

CompanyReportCapabilityPrincipal = Annotated[
    WorkloadPrincipal,
    Depends(require_company_report_read),
]


def require_company_report_collection(
    principal: CompanyReportCapabilityPrincipal,
) -> WorkloadPrincipal:
    authorize_collection_read(principal, Capability.COMPANY_REPORT_READ)
    return principal


CompanyReportPrincipal = Annotated[
    WorkloadPrincipal,
    Depends(require_company_report_collection),
]
CompanyReportParams = Annotated[_CompanyReportParams, Depends(_parse_company_report_params)]
CompanyReportCompositionParams = Annotated[
    _CompanyReportParams,
    Depends(_parse_company_report_composition_params),
]
CompanyReportingService = Annotated[
    DatabaseCompanyReportingService,
    Depends(get_company_reporting_service),
]


@router.get(
    "/company-reports",
    response_model=CompanyReportPage,
    response_model_exclude_none=False,
)
def get_company_report(
    principal: CompanyReportPrincipal,
    params: CompanyReportParams,
    service: CompanyReportingService,
) -> CompanyReportPage:
    return service.report(
        principal,
        basis=params.basis,
        from_month=params.from_month,
        to_month=params.to_month,
        company_ref=params.company_ref,
    )


@router.get(
    "/company-report-composition",
    response_model=CompanyReportCompositionPage,
    response_model_exclude_none=False,
)
def get_company_report_composition(
    principal: CompanyReportPrincipal,
    params: CompanyReportCompositionParams,
    service: CompanyReportingService,
) -> CompanyReportCompositionPage:
    return service.composition(
        principal,
        basis=params.basis,
        from_month=params.from_month,
        to_month=params.to_month,
        company_ref=params.company_ref,
    )
