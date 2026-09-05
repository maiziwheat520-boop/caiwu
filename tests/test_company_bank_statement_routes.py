from __future__ import annotations

from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledgerbridge.company_bank_statement_routes import get_service, router
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from tests.test_personal_finance_routes import ACCOUNT, ENTITY, STATEMENT, _page


class _Service:
    calls: list[tuple[UUID, UUID, Capability, str]]

    def __init__(self) -> None:
        self.calls = []

    def statement(self, principal, *, statement_ref, entity_ref, required_capability, owner_kind):
        self.calls.append((statement_ref, entity_ref, required_capability, owner_kind))
        return _page()


def _principal(capability: Capability) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:company-bank-review-test",
        san_uri="spiffe://ledgerbridge.test/company-bank-review",
        policy_generation=7,
        capabilities=frozenset({capability}),
        grants=(EntityGrant(entity_ref=ENTITY, business_unit_refs=frozenset({"company"})),),
    )


def _client(service: _Service, capability: Capability) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: Settings(
        env="test", runtime_role="migrate",
        database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
        artifact_root=Path.cwd() / "synthetic-artifacts",
        enable_internal_read_api=True, internal_read_policy_generation=7,
    )
    app.dependency_overrides[get_internal_read_principal] = lambda: _principal(capability)
    app.dependency_overrides[get_service] = lambda: service
    return TestClient(app)


def test_company_statement_route_binds_server_principal_to_company_owner() -> None:
    service = _Service()
    response = _client(service, Capability.BANK_STATEMENT_REVIEW_READ).get(
        f"/internal/v1/company-bank-statements/{STATEMENT}?entity_ref={ENTITY}"
    )
    assert response.status_code == 200
    assert response.json()["statement"]["managed_account_ref"] == str(ACCOUNT)
    assert service.calls == [
        (STATEMENT, ENTITY, Capability.BANK_STATEMENT_REVIEW_READ, "COMPANY")
    ]


def test_company_statement_route_rejects_decide_only_identity() -> None:
    service = _Service()
    response = _client(service, Capability.BANK_STATEMENT_REVIEW_DECIDE).get(
        f"/internal/v1/company-bank-statements/{STATEMENT}?entity_ref={ENTITY}"
    )
    assert response.status_code == 403
    assert service.calls == []
