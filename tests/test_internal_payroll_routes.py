from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_payroll_routes import (
    get_payroll_publication_source,
    router,
)
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.payroll_integration import PayrollPublication, PayrollPublicationSource

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
OTHER_ENTITY = UUID("10000000-0000-4000-8000-000000000002")
PUBLICATION_ID = "publication_0123456789abcdef01234567"


class _Source:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str, str]] = []

    def pull_publication(
        self,
        *,
        entity_ref: UUID,
        publication_id: str,
        idempotency_key: str,
    ) -> PayrollPublication:
        self.calls.append((entity_ref, publication_id, idempotency_key))
        return PayrollPublication(
            publication_id=publication_id,
            company_id="company_demo_hotel",
            entity_ref=entity_ref,
            batch_id="batch_demo_2026_08",
            pay_period="2026-08",
            employee_account_ids=(("employee_demo_001", "account_demo_001"),),
            payload={
                "schema_version": "payroll-ledgerbridge-publication/v1",
                "publication_id": publication_id,
            },
        )


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        env="test",
        runtime_role="migrate",
        database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
        artifact_root=Path.cwd() / "synthetic-artifacts",
        enable_internal_read_api=enabled,
        internal_read_policy_generation=(1 if enabled else None),
        enable_payroll_integration=enabled,
        payroll_base_url=("http://127.0.0.1:4318" if enabled else None),
        payroll_company_mapping=({"company_demo_hotel": ENTITY} if enabled else {}),
        payroll_bff_user_assertion_key=(SecretStr("b" * 32) if enabled else None),
        payroll_bff_user_assertion_issuer=("web-test" if enabled else None),
        payroll_bff_user_assertion_audience=("core-test" if enabled else None),
    )


def _principal(
    *,
    capability: bool = True,
    multiple_entities: bool = False,
) -> WorkloadPrincipal:
    grants = [EntityGrant(entity_ref=ENTITY, business_unit_refs=frozenset({"unit-a"}))]
    if multiple_entities:
        grants.append(
            EntityGrant(entity_ref=OTHER_ENTITY, business_unit_refs=frozenset({"unit-b"}))
        )
    capabilities = frozenset({Capability.PAYROLL_PUBLICATION_READ}) if capability else frozenset()
    return WorkloadPrincipal(
        principal_ref="workload:payroll-route-test",
        san_uri="spiffe://ledgerbridge.test/payroll-route-test",
        policy_generation=1,
        capabilities=capabilities,
        grants=tuple(grants),
    )


def _client(
    *,
    settings: Settings | None = None,
    principal: WorkloadPrincipal | None = None,
    source: PayrollPublicationSource | None = None,
) -> tuple[TestClient, _Source | PayrollPublicationSource]:
    application = FastAPI()
    application.include_router(router)
    configured = settings or _settings()
    current_source = source or _Source()
    application.dependency_overrides[get_settings] = lambda: configured
    application.dependency_overrides[get_internal_read_principal] = lambda: (
        principal or _principal()
    )
    application.dependency_overrides[get_payroll_publication_source] = lambda: current_source
    return TestClient(application), current_source


def _assert_problem(response: Response, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == code


def test_payroll_router_exposes_only_frozen_reads_and_receipt_verification() -> None:
    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    assert [(route.path, route.methods) for route in routes] == [
        ("/internal/v1/payroll/status", {"GET"}),
        ("/internal/v1/payroll/dashboard", {"GET"}),
        ("/internal/v1/payroll/materials", {"GET"}),
        ("/internal/v1/payroll/batches", {"GET"}),
        ("/internal/v1/payroll/verification", {"GET"}),
        ("/internal/v1/payroll/batches/{batch_id}/verify-receipts", {"POST"}),
        ("/internal/v1/payroll-publications/{publication_id}", {"GET"}),
        ("/internal/v1/payroll/test-workspaces/{test_batch_id}", {"GET"}),
        ("/internal/v1/payroll/test-workspaces", {"POST"}),
        ("/internal/v1/payroll/test-workspaces/{test_batch_id}/clear", {"POST"}),
    ]


def test_gate_precedes_authentication_and_source_construction() -> None:
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_settings] = lambda: _settings(enabled=False)
    calls = 0

    def unexpected_dependency() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("closed payroll route must not resolve later dependencies")

    application.dependency_overrides[get_internal_read_principal] = unexpected_dependency
    application.dependency_overrides[get_payroll_publication_source] = unexpected_dependency

    response = TestClient(application).get(f"/internal/v1/payroll-publications/{PUBLICATION_ID}")

    _assert_problem(response, 404, "PAYROLL_INTEGRATION_DISABLED")
    assert calls == 0


def test_route_derives_entity_only_from_the_verified_principal() -> None:
    client, source = _client()

    response = client.get(
        f"/internal/v1/payroll-publications/{PUBLICATION_ID}",
        headers={"X-Company-Id": "company_attacker", "X-Entity-Ref": str(OTHER_ENTITY)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["entity_ref"] == str(ENTITY)
    assert payload["company_id"] == "company_demo_hotel"
    calls = cast(_Source, source).calls
    assert calls[0][0] == ENTITY
    assert calls[0][1] == PUBLICATION_ID
    assert calls[0][2].startswith("payroll-read-")
    assert response.headers["cache-control"] == "no-store"


def test_route_rejects_query_scope_and_ambiguous_or_ungranted_principal() -> None:
    client, _ = _client()
    query = client.get(
        f"/internal/v1/payroll-publications/{PUBLICATION_ID}?company_id=company_attacker"
    )
    _assert_problem(query, 400, "INVALID_QUERY")

    ambiguous, _ = _client(principal=_principal(multiple_entities=True))
    response = ambiguous.get(f"/internal/v1/payroll-publications/{PUBLICATION_ID}")
    _assert_problem(response, 404, "PAYROLL_COMPANY_SCOPE_UNAVAILABLE")

    denied, _ = _client(principal=_principal(capability=False))
    response = denied.get(f"/internal/v1/payroll-publications/{PUBLICATION_ID}")
    _assert_problem(response, 403, "CAPABILITY_REQUIRED")


def test_settings_require_explicit_provider_and_company_mapping_when_enabled() -> None:
    with pytest.raises(ValueError):
        Settings(
            env="test",
            runtime_role="migrate",
            database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
            enable_internal_read_api=True,
            internal_read_policy_generation=1,
            enable_payroll_integration=True,
        )

    with pytest.raises(ValueError):
        Settings(
            env="test",
            runtime_role="migrate",
            database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
            enable_internal_read_api=True,
            internal_read_policy_generation=1,
            enable_payroll_integration=True,
            payroll_base_url="http://127.0.0.1:4318",
        )
