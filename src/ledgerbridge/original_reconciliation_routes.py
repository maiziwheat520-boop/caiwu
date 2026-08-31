"""Authenticated, read-only HTTP interface for the legacy reconciliation projection."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import ValidationError

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
    PostedLedgerSummaryReadPort,
)
from ledgerbridge.text import contains_unstorable_text

_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_UUID = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)


@lru_cache(maxsize=1)
def get_original_reconciliation_layout() -> LegacyReconciliationLayout:
    """Load one hash-pinned private layout without exposing it to source control."""

    configured_path = os.environ.get("LEDGERBRIDGE_ORIGINAL_RECONCILIATION_LAYOUT_FILE")
    expected_digest = os.environ.get("LEDGERBRIDGE_ORIGINAL_RECONCILIATION_LAYOUT_SHA256")
    if configured_path is None or expected_digest is None:
        raise InternalReadBackendUnavailable("original reconciliation layout is not configured")
    if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
        raise InternalReadBackendUnavailable("original reconciliation layout digest is invalid")
    path = Path(configured_path)
    if not path.is_absolute():
        raise InternalReadBackendUnavailable("original reconciliation layout path is not absolute")
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size <= 0 or stat.st_size > 65_536:
            raise InternalReadBackendUnavailable("original reconciliation layout file is invalid")
        payload = path.read_bytes()
    except InternalReadBackendUnavailable:
        raise
    except OSError as exc:
        raise InternalReadBackendUnavailable(
            "original reconciliation layout file is unavailable"
        ) from exc
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected_digest):
        raise InternalReadBackendUnavailable("original reconciliation layout digest mismatched")
    try:
        return LegacyReconciliationLayout.model_validate_json(payload)
    except (ValidationError, ValueError) as exc:
        raise InternalReadBackendUnavailable(
            "original reconciliation layout contract is invalid"
        ) from exc


VerifiedPrincipal = Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)]


def require_original_reconciliation_read(
    principal: VerifiedPrincipal,
) -> WorkloadPrincipal:
    require_capability(principal, Capability.RECONCILIATION_READ)
    require_capability(principal, Capability.CANDIDATE_READ)
    require_capability(principal, Capability.LEDGER_READ)
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
    if not callable(getattr(source, "list_candidates", None)) or not callable(
        getattr(source, "get_ledger_summary", None)
    ):
        raise InternalReadBackendUnavailable(
            "original reconciliation read interfaces are unavailable"
        )
    reader = InternalReadOriginalReconciliationAdapter(
        cast(CandidateReadPort, source),
        posted_ledger_reader=cast(PostedLedgerSummaryReadPort, source),
        layout=layout,
    )
    return reader.get(
        principal,
        month=params.month,
        entity_ref=params.entity_ref,
        business_unit_ref=params.business_unit_ref,
    )
