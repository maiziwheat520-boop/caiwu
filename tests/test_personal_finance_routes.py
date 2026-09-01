from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.internal_read_routes import InternalReadNoStoreMiddleware
from ledgerbridge.personal_finance_contract import PersonalFinancePage
from ledgerbridge.personal_finance_routes import get_personal_finance_service, router

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
STATEMENT = UUID("20000000-0000-4000-8000-000000000001")
ACCOUNT = UUID("30000000-0000-4000-8000-000000000001")


def _page() -> PersonalFinancePage:
    return PersonalFinancePage.model_validate(
        {
            "snapshot_revision": "a" * 64,
            "statement": {
                "statement_ref": STATEMENT,
                "managed_account_ref": ACCOUNT,
                "institution_code": "mybank",
                "account_suffix": "7968",
                "period_start": date(2026, 1, 1),
                "period_end": date(2026, 1, 2),
                "transaction_count": 2,
                "review_status": "CONFIRMED",
                "review_revision": 1,
            },
            "summary": {
                "cash_inflow_minor": 1_500,
                "cash_outflow_minor": 500,
                "net_cash_flow_minor": 1_000,
            },
            "items": [
                {
                    "source_row_number": 1,
                    "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
                    "amount_minor": 1_500,
                    "balance_minor": 5_000,
                    "currency": "CNY",
                    "counterparty_name": "付款方",
                    "counterparty_account_masked": None,
                    "counterparty_institution": None,
                    "transaction_name": "转入",
                },
                {
                    "source_row_number": 2,
                    "occurred_at": datetime(2026, 1, 2, tzinfo=UTC),
                    "amount_minor": -500,
                    "balance_minor": 4_500,
                    "currency": "CNY",
                    "counterparty_name": "收款方",
                    "counterparty_account_masked": "****1234",
                    "counterparty_institution": "示例银行",
                    "transaction_name": "消费",
                },
            ],
        }
    )


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[WorkloadPrincipal, UUID, UUID]] = []

    def statement(
        self,
        principal: WorkloadPrincipal,
        *,
        statement_ref: UUID,
        entity_ref: UUID,
    ) -> PersonalFinancePage:
        self.calls.append((principal, statement_ref, entity_ref))
        return _page()


def _principal(*, allowed: bool = True) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:personal-finance-test",
        san_uri="spiffe://ledgerbridge.test/personal-finance-test",
        policy_generation=1,
        capabilities=(frozenset({Capability.LEDGER_READ}) if allowed else frozenset()),
        grants=(EntityGrant(entity_ref=ENTITY, business_unit_refs=frozenset({"personal"})),),
    )


def _client(service: _Service, *, principal: WorkloadPrincipal | None = None) -> TestClient:
    application = FastAPI()
    application.add_middleware(InternalReadNoStoreMiddleware)
    application.include_router(router)
    application.dependency_overrides[get_settings] = lambda: Settings(
        env="test",
        runtime_role="migrate",
        database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
        artifact_root=Path.cwd() / "synthetic-artifacts",
        enable_internal_read_api=True,
        internal_read_policy_generation=1,
    )
    application.dependency_overrides[get_internal_read_principal] = lambda: (
        principal or _principal()
    )
    application.dependency_overrides[get_personal_finance_service] = lambda: service
    return TestClient(application)


def test_personal_finance_route_returns_the_closed_formal_contract() -> None:
    service = _Service()
    response = _client(service).get(
        f"/internal/v1/personal-finance?statement_ref={STATEMENT}&entity_ref={ENTITY}"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == _page().model_dump(mode="json")
    assert service.calls == [(_principal(), STATEMENT, ENTITY)]


def test_personal_finance_route_rejects_unknown_or_duplicate_query_fields() -> None:
    service = _Service()
    response = _client(service).get(
        f"/internal/v1/personal-finance?statement_ref={STATEMENT}"
        f"&statement_ref={STATEMENT}&entity_ref={ENTITY}&unknown=1"
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_QUERY"
    assert service.calls == []


def test_personal_finance_contract_rejects_incomplete_or_unreconciled_items() -> None:
    payload = _page().model_dump(mode="python")
    payload["items"] = payload["items"][:1]
    with pytest.raises(ValidationError, match="incomplete or unstable"):
        PersonalFinancePage.model_validate(payload)

    payload = _page().model_dump(mode="python")
    payload["summary"]["net_cash_flow_minor"] = 999
    with pytest.raises(ValidationError, match="does not reconcile"):
        PersonalFinancePage.model_validate(payload)
