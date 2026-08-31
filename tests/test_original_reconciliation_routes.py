import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_candidate_command import get_synthetic_review_service
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    READ_CAPABILITIES,
    CandidatePage,
    Capability,
    EntityGrant,
    LedgerSummary,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_routes import (
    InternalReadNoStoreMiddleware,
    get_synthetic_internal_read_service,
)
from ledgerbridge.original_reconciliation import LegacyReconciliationLayout
from ledgerbridge.original_reconciliation_routes import (
    get_original_reconciliation_layout,
    router,
)

ENTITY_B = UUID("10000000-0000-4000-8000-000000000002")


def _settings() -> Settings:
    return Settings(
        env="test",
        runtime_role="migrate",
        database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
        artifact_root=Path.cwd() / "synthetic-artifacts",
        enable_internal_read_api=True,
        internal_read_policy_generation=11,
    )


def _principal(capabilities: frozenset[Capability] = READ_CAPABILITIES) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:original-reconciliation-route-test",
        san_uri="spiffe://ledgerbridge.test/original-reconciliation-route-test",
        policy_generation=11,
        capabilities=capabilities,
        grants=(
            EntityGrant(
                entity_ref=ENTITY_B,
                business_unit_refs=frozenset({"unit-demo-b"}),
            ),
        ),
    )


def _client(
    *,
    principal: WorkloadPrincipal | None = None,
    configured_layout: bool = True,
    source: object | None = None,
) -> TestClient:
    app = FastAPI()
    app.add_middleware(InternalReadNoStoreMiddleware)
    app.include_router(router)
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_internal_read_principal] = lambda: principal or _principal()
    app.dependency_overrides[get_synthetic_internal_read_service] = (
        (lambda: source) if source is not None else get_synthetic_review_service
    )
    if configured_layout:
        app.dependency_overrides[get_original_reconciliation_layout] = lambda: (
            LegacyReconciliationLayout(
                layout_version="synthetic-layout.v1",
                mapping_version="synthetic-mapping.v1",
            )
        )
    return TestClient(app)


class EmptyOriginalReconciliationSource:
    def list_candidates(self, *args: object, **kwargs: object) -> CandidatePage:
        _ = args, kwargs
        return CandidatePage(items=(), next_cursor=None)

    def get_ledger_summary(
        self,
        principal: WorkloadPrincipal,
        *,
        entity_ref: UUID,
        business_unit_ref: str,
        from_month: str,
        to_month: str,
    ) -> LedgerSummary:
        _ = principal
        return LedgerSummary(
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
            from_month=from_month,
            to_month=to_month,
            posting_status="POSTED",
            currency="CNY",
            totals_minor={},
        )


def test_route_loads_hash_pinned_private_layout_and_returns_formal_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        {
            "layout_version": "reviewed-private-layout.v1",
            "mapping_version": "reviewed-private-mapping.v1",
            "label_cells": [{"coordinate": "A1", "label": "Synthetic private title"}],
            "slot_mappings": [],
            "source_slot_rules": [],
            "derived_cells": [
                {"coordinate": "B21", "value_role": "POSTED_INCOME_TOTAL"},
                {"coordinate": "B29", "value_role": "POSTED_EXPENSE_TOTAL"},
            ],
        },
        separators=(",", ":"),
    ).encode()
    layout_path = tmp_path / "original-reconciliation-layout.json"
    layout_path.write_bytes(payload)
    monkeypatch.setenv("LEDGERBRIDGE_ORIGINAL_RECONCILIATION_LAYOUT_FILE", str(layout_path))
    monkeypatch.setenv(
        "LEDGERBRIDGE_ORIGINAL_RECONCILIATION_LAYOUT_SHA256",
        hashlib.sha256(payload).hexdigest(),
    )
    get_original_reconciliation_layout.cache_clear()

    response = _client(
        configured_layout=False,
        source=EmptyOriginalReconciliationSource(),
    ).get(
        "/internal/v1/original-reconciliations/2026-08",
        params={"entity_ref": str(ENTITY_B), "business_unit": "unit-demo-b"},
    )

    get_original_reconciliation_layout.cache_clear()
    assert response.status_code == 200
    body = response.json()
    assert body["layout_version"] == "reviewed-private-layout.v1"
    assert body["posted_ledger_complete"] is True
    assert body["totals"]["posted_income_minor"] == 0
    assert body["totals"]["posted_expense_minor"] == 0
    assert body["rows"][0]["cells"][0]["label"] == "Synthetic private title"


def test_route_fails_closed_when_private_layout_digest_mismatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout_path = tmp_path / "original-reconciliation-layout.json"
    layout_path.write_text(
        '{"layout_version":"private.v1","mapping_version":"private.v1"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("LEDGERBRIDGE_ORIGINAL_RECONCILIATION_LAYOUT_FILE", str(layout_path))
    monkeypatch.setenv("LEDGERBRIDGE_ORIGINAL_RECONCILIATION_LAYOUT_SHA256", "0" * 64)
    get_original_reconciliation_layout.cache_clear()

    response = _client(
        configured_layout=False,
        source=EmptyOriginalReconciliationSource(),
    ).get(
        "/internal/v1/original-reconciliations/2026-08",
        params={"entity_ref": str(ENTITY_B), "business_unit": "unit-demo-b"},
    )

    get_original_reconciliation_layout.cache_clear()
    assert response.status_code == 503
    assert response.json()["code"] == "INTERNAL_READ_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"


def test_original_reconciliation_route_returns_versioned_no_store_projection() -> None:
    response = _client(source=EmptyOriginalReconciliationSource()).get(
        "/internal/v1/original-reconciliations/2026-08",
        params={"entity_ref": str(ENTITY_B), "business_unit": "unit-demo-b"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["contract_version"] == "ledgerbridge.original-reconciliation.v1"
    assert body["taxonomy_version"] == ("ledgerbridge.financial-foundation-blocker-taxonomy.v1")
    assert body["layout_version"] == "synthetic-layout.v1"
    assert body["mapping_version"] == "synthetic-mapping.v1"
    assert body["confirmed_pending_posting_count"] == 0
    assert body["posted_ledger_complete"] is True
    assert body["projection_gaps"] == ["MISSING_TIME_GRANULARITY"]
    assert body["totals"]["posted_profit_minor"] == 0
    assert body["is_complete"] is False
    assert len(body["columns"]) == 13
    assert len(body["rows"]) == 40


def test_original_reconciliation_route_fails_closed_without_private_layout() -> None:
    response = _client(configured_layout=False).get(
        "/internal/v1/original-reconciliations/2026-08",
        params={"entity_ref": str(ENTITY_B), "business_unit": "unit-demo-b"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "INTERNAL_READ_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"


def test_original_reconciliation_route_fails_closed_without_ledger_interface() -> None:
    class CandidateOnlySource:
        def list_candidates(self, *args: object, **kwargs: object) -> CandidatePage:
            _ = args, kwargs
            return CandidatePage(items=(), next_cursor=None)

    response = _client(source=CandidateOnlySource()).get(
        "/internal/v1/original-reconciliations/2026-08",
        params={"entity_ref": str(ENTITY_B), "business_unit": "unit-demo-b"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "INTERNAL_READ_UNAVAILABLE"
    assert response.headers["cache-control"] == "no-store"


def test_original_reconciliation_route_requires_ledger_read_capability() -> None:
    principal = _principal(frozenset({Capability.RECONCILIATION_READ, Capability.CANDIDATE_READ}))
    response = _client(
        principal=principal,
        source=EmptyOriginalReconciliationSource(),
    ).get(
        "/internal/v1/original-reconciliations/2026-08?"
        f"entity_ref={ENTITY_B}&business_unit=unit-demo-b"
    )

    assert response.status_code == 403
    assert response.json()["code"] == "CAPABILITY_REQUIRED"
    assert response.headers["cache-control"] == "no-store"


def test_original_reconciliation_route_rejects_unknown_query_and_cross_scope() -> None:
    client = _client()
    invalid = client.get(
        "/internal/v1/original-reconciliations/2026-08?"
        f"entity_ref={ENTITY_B}&business_unit=unit-demo-b&layout_version=attacker"
    )
    hidden = client.get(
        "/internal/v1/original-reconciliations/2026-08?"
        "entity_ref=10000000-0000-4000-8000-000000000001&business_unit=unit-demo-a"
    )

    assert invalid.status_code == 400
    assert invalid.json()["code"] == "INVALID_QUERY"
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "RESOURCE_NOT_FOUND"
