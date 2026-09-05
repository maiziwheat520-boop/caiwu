"""Least-privilege object reader for company bank-statement review."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.db import get_session_factory
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import Capability, WorkloadPrincipal, require_capability
from ledgerbridge.internal_read_routes import (
    InternalReadRoute,
    _closed_query,
    _parse_uuid,
    require_internal_read_api,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable
from ledgerbridge.personal_finance_contract import PersonalFinancePage
from ledgerbridge.personal_finance_service import DatabasePersonalFinanceService


def get_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> DatabasePersonalFinanceService:
    if settings.internal_read_backend != "database":
        raise InternalReadBackendUnavailable("company statement review requires database reads")
    return DatabasePersonalFinanceService(
        get_session_factory(settings.resolved_reader_database_url())
    )


def require_company_statement_review(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.BANK_STATEMENT_REVIEW_READ)
    return principal


router = APIRouter(
    prefix="/internal/v1",
    tags=["internal-company-bank-statement-review"],
    dependencies=[Depends(require_internal_read_api)],
    route_class=InternalReadRoute,
)


@router.get("/company-bank-statements/{statement_ref}", response_model=PersonalFinancePage)
def company_bank_statement(
    statement_ref: UUID,
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_company_statement_review)],
    service: Annotated[DatabasePersonalFinanceService, Depends(get_service)],
) -> PersonalFinancePage:
    query = _closed_query(
        request,
        allowed=frozenset({"entity_ref"}),
        required=frozenset({"entity_ref"}),
    )
    return service.statement(
        principal,
        statement_ref=statement_ref,
        entity_ref=_parse_uuid(query["entity_ref"]),
        required_capability=Capability.BANK_STATEMENT_REVIEW_READ,
        owner_kind="COMPANY",
    )
