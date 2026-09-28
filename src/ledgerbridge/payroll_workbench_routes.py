"""Authenticated read route for the database-owned payroll workbench."""

from __future__ import annotations

import re
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request, status
from sqlalchemy.orm import Session

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.db import get_session
from ledgerbridge.internal_payroll_assertion import PayrollUserAssertionError
from ledgerbridge.internal_payroll_routes import (
    InternalPayrollProblem,
    InternalPayrollRoute,
    PayrollAssertionReplayStore,
    _verify_user_for_grant,
    get_payroll_assertion_replay_store,
)
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    Capability,
    WorkloadPrincipal,
    require_capability,
)
from ledgerbridge.internal_read_routes import require_internal_read_api
from ledgerbridge.payroll_workbench import (
    DatabasePayrollWorkbench,
    PayrollWorkbenchNotFound,
    PayrollWorkbenchView,
)

_PERIOD = re.compile(r"^20[0-9]{2}-(0[1-9]|1[0-2])$")


def require_payroll_workbench(
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if not settings.enable_payroll_workbench:
        raise InternalPayrollProblem(status.HTTP_404_NOT_FOUND, "PAYROLL_WORKBENCH_DISABLED")


def require_payroll_workbench_read(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.PAYROLL_LIVE_READ)
    return principal


class PayrollWorkbenchReadPort(Protocol):
    def get(self, *, entity_ref: UUID, pay_period: str) -> PayrollWorkbenchView: ...


def get_payroll_workbench_service(
    session: Annotated[Session, Depends(get_session)],
) -> DatabasePayrollWorkbench:
    return DatabasePayrollWorkbench(session)


router = APIRouter(
    prefix="/internal/v1",
    tags=["payroll-workbench"],
    dependencies=[Depends(require_internal_read_api), Depends(require_payroll_workbench)],
    route_class=InternalPayrollRoute,
)


@router.get(
    "/payroll/workbench/{entity_ref}/{pay_period}",
    response_model=PayrollWorkbenchView,
    response_model_exclude_none=False,
)
def get_payroll_workbench(
    request: Request,
    entity_ref: Annotated[UUID, Path()],
    pay_period: str,
    assertion: Annotated[str, Header(alias="X-LedgerBridge-User-Assertion")],
    principal: Annotated[WorkloadPrincipal, Depends(require_payroll_workbench_read)],
    settings: Annotated[Settings, Depends(get_settings)],
    replay_store: Annotated[
        PayrollAssertionReplayStore, Depends(get_payroll_assertion_replay_store)
    ],
    service: Annotated[PayrollWorkbenchReadPort, Depends(get_payroll_workbench_service)],
) -> PayrollWorkbenchView:
    if _PERIOD.fullmatch(pay_period) is None:
        raise InternalPayrollProblem(status.HTTP_400_BAD_REQUEST, "PAYROLL_PERIOD_INVALID")
    if not any(grant.entity_ref == entity_ref for grant in principal.grants):
        raise InternalPayrollProblem(status.HTTP_404_NOT_FOUND, "PAYROLL_WORKBENCH_NOT_FOUND")
    if request.url.query:
        raise InternalPayrollProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    claims = _verify_user_for_grant(
        assertion,
        settings=settings,
        principal=principal,
        method="GET",
        path=request.url.path,
        body=b"",
        action="payroll.workbench.read",
        resource_ref=pay_period,
    )
    if claims.entity_ref != entity_ref:
        raise PayrollUserAssertionError("payroll workbench entity does not match the assertion")
    if not replay_store.consume(claims):
        raise PayrollUserAssertionError("payroll assertion replayed")
    try:
        return service.get(entity_ref=entity_ref, pay_period=pay_period)
    except PayrollWorkbenchNotFound as exc:
        raise InternalPayrollProblem(
            status.HTTP_404_NOT_FOUND, "PAYROLL_WORKBENCH_NOT_FOUND"
        ) from exc
