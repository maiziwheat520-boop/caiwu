"""Authenticated, read-only HTTP interface for the legacy reconciliation projection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    Capability,
    ResourceNotVisible,
    WorkloadPrincipal,
    require_capability,
)
from ledgerbridge.internal_read_routes import (
    InternalReadProblem,
    InternalReadRoute,
    get_synthetic_internal_read_service,
    require_internal_read_api,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable
from ledgerbridge.original_reconciliation import (
    LegacyReconciliationLayout,
    OriginalReconciliationProjection,
)
from ledgerbridge.original_reconciliation_reader import (
    CandidateReadPort,
    InternalReadOriginalReconciliationAdapter,
)
from ledgerbridge.text import contains_unstorable_text

_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_UUID = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


def get_original_reconciliation_layout() -> LegacyReconciliationLayout:
    """Fail closed until deployment injects one reviewed private layout."""

    raise InternalReadBackendUnavailable("original reconciliation layout is not configured")


VerifiedPrincipal = Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)]


def require_original_reconciliation_read(
    principal: VerifiedPrincipal,
) -> WorkloadPrincipal:
    require_capability(principal, Capability.RECONCILIATION_READ)
    require_capability(principal, Capability.CANDIDATE_READ)
    return principal


@dataclass(frozen=True, slots=True)
class _OriginalReconciliationParams:
    month: str
    entity_ref: UUID
    business_unit_ref: str


def _parse_original_reconciliation_params(
    month: str,
    request: Request,
) -> _OriginalReconciliationParams:
    if _MONTH.fullmatch(month) is None:
        raise ResourceNotVisible("resource was not found")
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in {"entity_ref", "business_unit"} or key in values:
            raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
        values[key] = value
    if values.keys() != {"entity_ref", "business_unit"}:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    entity_value = values["entity_ref"]
    business_unit = values["business_unit"]
    if _UUID.fullmatch(entity_value) is None:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    if (
        not business_unit
        or len(business_unit) > 100
        or business_unit.strip() != business_unit
        or contains_unstorable_text(business_unit)
    ):
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    try:
        entity_ref = UUID(entity_value)
    except ValueError as exc:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY") from exc
    return _OriginalReconciliationParams(
        month=month,
        entity_ref=entity_ref,
        business_unit_ref=business_unit,
    )


router = APIRouter(
    prefix="/internal/v1",
    tags=["original-reconciliation"],
    dependencies=[Depends(require_internal_read_api)],
    route_class=InternalReadRoute,
)

OriginalReconciliationPrincipal = Annotated[
    WorkloadPrincipal,
    Depends(require_original_reconciliation_read),
]
OriginalReconciliationParams = Annotated[
    _OriginalReconciliationParams,
    Depends(_parse_original_reconciliation_params),
]


@router.get(
    "/original-reconciliations/{month}",
    response_model=OriginalReconciliationProjection,
)
def get_original_reconciliation(
    principal: OriginalReconciliationPrincipal,
    params: OriginalReconciliationParams,
    source: Annotated[object, Depends(get_synthetic_internal_read_service)],
    layout: Annotated[
        LegacyReconciliationLayout,
        Depends(get_original_reconciliation_layout),
    ],
) -> OriginalReconciliationProjection:
    reader = InternalReadOriginalReconciliationAdapter(
        cast(CandidateReadPort, source),
        layout=layout,
    )
    return reader.get(
        principal,
        month=params.month,
        entity_ref=params.entity_ref,
        business_unit_ref=params.business_unit_ref,
    )
