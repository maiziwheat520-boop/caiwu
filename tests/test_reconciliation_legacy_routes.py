from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledgerbridge.db import get_session
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.internal_read_routes import require_internal_read_api
from ledgerbridge.reconciliation_legacy_routes import require_legacy_read, router

SOURCE = UUID("99999999-9999-4999-8999-999999999999")
PERSONAL = UUID("11111111-1111-4111-8111-111111111111")
COMPANY = UUID("22222222-2222-4222-8222-222222222222")


def _principal(*entities: UUID, units: frozenset[str] = frozenset({"hotel"})) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="reconciliation-web",
        san_uri="spiffe://ledgerbridge.test/reconciliation",
        policy_generation=1,
        capabilities=frozenset({Capability.RECONCILIATION_READ}),
        grants=tuple(
            EntityGrant(entity_ref=entity, business_unit_refs=units) for entity in entities
        ),
    )


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows

    def one_or_none(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class _Session:
    def execute(self, statement: object, params: object = None) -> _Result:
        row: dict[str, object] = {
            "source_ref": SOURCE,
            "source_sha256": "a" * 64,
            "scope_pairs": [
                {"entity_ref": str(PERSONAL), "business_unit_ref": "hotel"},
                {"entity_ref": str(COMPANY), "business_unit_ref": "hotel"},
            ],
            "imported_at": datetime(2026, 9, 29, tzinfo=UTC),
            "period": "2024-01",
            "sheet_name": "24.01",
            "row_count": 2,
            "cell_count": 1,
            "content_sha256": "b" * 64,
            "cells": [
                {
                    "address": "A1",
                    "type": "text",
                    "value": "历史值",
                    "cached_type": None,
                    "cached_value": None,
                }
            ],
        }
        if params is not None and params != {"source_ref": SOURCE, "period": "2024-01"}:
            return _Result([])
        return _Result([row])


def _client(principal: WorkloadPrincipal) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_internal_read_api] = lambda: None
    app.dependency_overrides[require_legacy_read] = lambda: principal
    app.dependency_overrides[get_session] = lambda: _Session()
    return TestClient(app)


def test_full_scope_can_read_original_month_without_recalculation() -> None:
    client = _client(_principal(PERSONAL, COMPANY))

    sources = client.get("/internal/v1/reconciliation-legacy/sources")
    month = client.get(f"/internal/v1/reconciliation-legacy/{SOURCE}/2024-01")

    assert sources.status_code == 200
    assert sources.json()["sources"][0]["periods"] == ["2024-01"]
    assert month.status_code == 200
    assert month.json()["cells"][0]["value"] == "历史值"
    assert month.headers["cache-control"] == "no-store"


def test_partial_scope_cannot_read_cross_entity_workbook() -> None:
    client = _client(_principal(PERSONAL))

    assert client.get("/internal/v1/reconciliation-legacy/sources").json()["sources"] == []
    response = client.get(f"/internal/v1/reconciliation-legacy/{SOURCE}/2024-01")
    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


def test_same_entity_without_business_unit_cannot_read_workbook() -> None:
    client = _client(_principal(PERSONAL, COMPANY, units=frozenset({"other-hotel"})))

    assert client.get("/internal/v1/reconciliation-legacy/sources").json()["sources"] == []
    assert client.get(f"/internal/v1/reconciliation-legacy/{SOURCE}/2024-01").status_code == 404


def test_closed_period_and_query() -> None:
    client = _client(_principal(PERSONAL, COMPANY))

    assert client.get(f"/internal/v1/reconciliation-legacy/{SOURCE}/2024-13").status_code == 400
    assert client.get("/internal/v1/reconciliation-legacy/sources?x=1").status_code == 400
