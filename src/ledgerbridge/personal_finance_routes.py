"""Closed read-only internal route for formally imported personal statements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.db import get_session_factory
from ledgerbridge.internal_read_contract import (
    Capability,
    WorkloadPrincipal,
    authorize_collection_read,
)
from ledgerbridge.internal_read_routes import (
    InternalReadRoute,
    _closed_query,
    _parse_uuid,
    require_internal_read_api,
    require_ledger_read,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable
from ledgerbridge.personal_finance_contract import PersonalFinancePage
from ledgerbridge.personal_finance_service import DatabasePersonalFinanceService


@dataclass(frozen=True, slots=True)
class _PersonalFinanceParams:
    statement_ref: UUID
    entity_ref: UUID


def _parse_personal_finance_params(request: Request) -> _PersonalFinanceParams:
    query = _closed_query(
        request,
        allowed=frozenset({"statement_ref", "entity_ref"}),
        required=frozenset({"statement_ref", "entity_ref"}),
    )
    return _PersonalFinanceParams(
        statement_ref=_parse_uuid(query["statement_ref"]),
        entity_ref=_parse_uuid(query["entity_ref"]),
    )


def get_personal_finance_service(
    settings: Annotated[Settings, Depends(get_settings)],
) -> DatabasePersonalFinanceService:
    if settings.internal_read_backend != "database":
        raise InternalReadBackendUnavailable("personal finance requires the database read backend")
    return DatabasePersonalFinanceService(
        get_session_factory(settings.resolved_reader_database_url())
    )


router = APIRouter(
    prefix="/internal/v1",
    tags=["internal-personal-finance"],
    dependencies=[Depends(require_internal_read_api)],
    route_class=InternalReadRoute,
)

LedgerPrincipal = Annotated[WorkloadPrincipal, Depends(require_ledger_read)]


def require_personal_finance_collection(principal: LedgerPrincipal) -> WorkloadPrincipal:
    authorize_collection_read(principal, Capability.LEDGER_READ)
    return principal


PersonalFinancePrincipal = Annotated[
    WorkloadPrincipal,
    Depends(require_personal_finance_collection),
]
PersonalFinanceParams = Annotated[
    _PersonalFinanceParams,
    Depends(_parse_personal_finance_params),
]
PersonalFinanceService = Annotated[
    DatabasePersonalFinanceService,
    Depends(get_personal_finance_service),
]


@router.get(
    "/personal-finance",
    response_model=PersonalFinancePage,
    response_model_exclude_none=False,
)
def get_personal_finance(
    principal: PersonalFinancePrincipal,
    params: PersonalFinanceParams,
    service: PersonalFinanceService,
) -> PersonalFinancePage:
    return service.statement(
        principal,
        statement_ref=params.statement_ref,
        entity_ref=params.entity_ref,
    )
