"""Scoped, read-only website access to original reconciliation month snapshots."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ledgerbridge.db import get_session
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
    require_internal_read_api,
)

_PERIOD = re.compile(r"^20[0-9]{2}-(0[1-9]|1[0-2])$")


class LegacyCell(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    address: str = Field(pattern=r"^[A-Z]{1,3}[1-9][0-9]{0,4}$")
    type: str
    value: str | None
    cached_type: str | None
    cached_value: str | None


class LegacyMonthView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = "ledgerbridge.reconciliation-legacy-month.v1"
    source_ref: UUID
    source_sha256: str
    imported_at: datetime
    period: str
    sheet_name: str
    row_count: int
    cell_count: int
    content_sha256: str
    cells: tuple[LegacyCell, ...]


class LegacySourceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: UUID
    source_sha256: str
    imported_at: datetime
    periods: tuple[str, ...]


class LegacySourceList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = "ledgerbridge.reconciliation-legacy-sources.v1"
    sources: tuple[LegacySourceView, ...]


def require_legacy_read(
    principal: Annotated[WorkloadPrincipal, Depends(get_internal_read_principal)],
) -> WorkloadPrincipal:
    require_capability(principal, Capability.RECONCILIATION_READ)
    return principal


def _may_read(principal: WorkloadPrincipal, required: list[dict[str, str]]) -> bool:
    granted = {
        (str(grant.entity_ref), unit_ref)
        for grant in principal.grants
        for unit_ref in grant.business_unit_refs
    }
    try:
        scopes = {(str(UUID(item["entity_ref"])), item["business_unit_ref"]) for item in required}
    except (KeyError, TypeError, ValueError):
        return False
    return bool(scopes) and len(scopes) == len(required) and scopes.issubset(granted)


router = APIRouter(
    prefix="/internal/v1/reconciliation-legacy",
    tags=["reconciliation-legacy"],
    dependencies=[Depends(require_internal_read_api)],
    route_class=InternalReadRoute,
)


@router.get("/sources", response_model=LegacySourceList)
def list_legacy_sources(
    request: Request,
    principal: Annotated[WorkloadPrincipal, Depends(require_legacy_read)],
    session: Annotated[Session, Depends(get_session)],
) -> LegacySourceList:
    if request.url.query:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    rows = (
        session.execute(
            text(
                """
            SELECT source_ref, source_sha256, scope_pairs, imported_at, period
              FROM reconciliation_legacy.month_read
             ORDER BY imported_at DESC, source_ref, period
             LIMIT 1200
            """
            )
        )
        .mappings()
        .all()
    )
    if len(rows) >= 1200:
        raise InternalReadProblem(
            status.HTTP_503_SERVICE_UNAVAILABLE, "LEGACY_ARCHIVE_INVENTORY_TOO_LARGE"
        )
    grouped: dict[UUID, dict[str, object]] = {}
    for row in rows:
        if not _may_read(principal, row["scope_pairs"]):
            continue
        source_ref = UUID(str(row["source_ref"]))
        entry = grouped.setdefault(
            source_ref,
            {
                "source_ref": source_ref,
                "source_sha256": row["source_sha256"],
                "imported_at": row["imported_at"],
                "periods": [],
            },
        )
        periods = entry["periods"]
        assert isinstance(periods, list)
        periods.append(row["period"])
    return LegacySourceList(
        sources=tuple(LegacySourceView.model_validate(item) for item in grouped.values())
    )


@router.get("/{source_ref}/{period}", response_model=LegacyMonthView)
def get_legacy_month(
    request: Request,
    source_ref: UUID,
    period: str,
    principal: Annotated[WorkloadPrincipal, Depends(require_legacy_read)],
    session: Annotated[Session, Depends(get_session)],
) -> LegacyMonthView:
    if request.url.query or _PERIOD.fullmatch(period) is None:
        raise InternalReadProblem(status.HTTP_400_BAD_REQUEST, "INVALID_QUERY")
    row = (
        session.execute(
            text(
                """
            SELECT source_ref, source_sha256, scope_pairs, imported_at,
                   period, sheet_name, row_count, cell_count, content_sha256, cells
              FROM reconciliation_legacy.month_read
             WHERE source_ref = :source_ref AND period = :period
            """
            ),
            {"source_ref": source_ref, "period": period},
        )
        .mappings()
        .one_or_none()
    )
    if row is None or not _may_read(principal, row["scope_pairs"]):
        raise ResourceNotVisible("historical reconciliation month was not found")
    return LegacyMonthView.model_validate(
        {key: value for key, value in row.items() if key != "scope_pairs"}
    )
