"""Authenticated read route for direct-from-statement reconciliation."""

from __future__ import annotations

import re
from typing import Annotated, Protocol, cast

from fastapi import APIRouter, Depends

from ledgerbridge.cash_reconciliation import CashReconciliationProjection
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import Capability, WorkloadPrincipal, require_capability
from ledgerbridge.internal_read_routes import (
    InternalReadRoute,
    get_synthetic_internal_read_service,
    require_internal_read_api,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable

_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


class CashReconciliationReadPort(Protocol):
    def get_cash_reconciliation(
        self, principal: WorkloadPrincipal, *, month: str
    ) -> CashReconciliationProjection: ...


def require_cash_reconciliation_read(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.RECONCILIATION_READ)
    require_capability(principal, Capability.LEDGER_READ)
    return principal


router = APIRouter(
    prefix="/internal/v1",
    tags=["cash-reconciliation"],
    dependencies=[Depends(require_internal_read_api)],
    route_class=InternalReadRoute,
)


@router.get("/cash-reconciliations/{month}", response_model=CashReconciliationProjection)
def get_cash_reconciliation(
    month: str,
    principal: Annotated[WorkloadPrincipal, Depends(require_cash_reconciliation_read)],
    source: Annotated[object, Depends(get_synthetic_internal_read_service)],
) -> CashReconciliationProjection:
    if _MONTH.fullmatch(month) is None:
        raise InternalReadBackendUnavailable("cash reconciliation month is invalid")
    if not callable(getattr(source, "get_cash_reconciliation", None)):
        raise InternalReadBackendUnavailable("cash reconciliation projection is unavailable")
    return cast(CashReconciliationReadPort, source).get_cash_reconciliation(principal, month=month)
