from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import Response

from ledgerbridge.company_reporting_composition_contract import (
    CompanyReportCompositionPage,
)
from ledgerbridge.company_reporting_contract import CompanyReportBasis, CompanyReportPage
from ledgerbridge.company_reporting_routes import get_company_reporting_service, router
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_routes import InternalReadNoStoreMiddleware

COMPANY = UUID("10000000-0000-4000-8000-000000000001")
OTHER_COMPANY = UUID("10000000-0000-4000-8000-000000000002")
UNIT = UUID("11000000-0000-4000-8000-000000000001")


def _page() -> CompanyReportPage:
    return CompanyReportPage.model_validate(
        {
            "basis": "CONFIRMED_CANDIDATE",
            "from_month": "2026-08",
            "to_month": "2026-08",
            "items": [
                {
                    "company_ref": str(COMPANY),
                    "company_name": "Example Company",
                    "currency": "CNY",
                    "metrics": {
                        "basis": "CONFIRMED_CANDIDATE",
                        "confirmed_positive_minor": 9800,
                        "confirmed_negative_minor": -2500,
                        "confirmed_net_minor": 7300,
                        "confirmed_count": 4,
                        "source_count": 3,
                    },
                    "pending_review_count": 2,
                    "attribution_pending_count": 1,
                    "missing_material_count": None,
                    "taxonomy_version": None,
                    "balance": {
                        "balance_basis": "UNAVAILABLE",
                        "opening_balance_minor": None,
                        "closing_balance_minor": None,
                        "gap": "AUTHORITATIVE_BALANCE_UNAVAILABLE",
                    },
                    "business_unit_breakdown_status": "EMPTY",
                    "months": [],
                }
            ],
        }
    )


def _composition_page() -> CompanyReportCompositionPage:
    return CompanyReportCompositionPage.model_validate(
        {
            "basis": "CONFIRMED_CANDIDATE",
            "from_month": "2026-08",
            "to_month": "2026-08",
            "items": [
                {
                    "company_ref": str(COMPANY),
                    "company_name": "Example Company",
                    "currency": "CNY",
                    "basis": "CONFIRMED_CANDIDATE",
                    "positive": {
                        "total_minor": 9800,
                        "fact_count": 2,
                        "items": [
                            {
                                "category_code": "ROOM",
                                "category_label": "Room revenue",
                                "amount_minor": 9800,
                                "fact_count": 2,
                            }
                        ],
                    },
                    "negative": {
                        "total_minor": 2500,
                        "fact_count": 1,
                        "items": [
                            {
                                "category_code": "SUPPLY",
                                "category_label": "Supplies",
                                "amount_minor": 2500,
                                "fact_count": 1,
                            }
                        ],
                    },
                }
            ],
        }
    )


class _Service:
    def __init__(
        self,
        page: CompanyReportPage | None = None,
        *,
        composition_page: CompanyReportCompositionPage | None = None,
        error: Exception | None = None,
    ) -> None:
        self.page = page or _page()
        self.composition_page = composition_page or _composition_page()
        self.error = error
        self.calls: list[tuple[WorkloadPrincipal, CompanyReportBasis, str, str, UUID | None]] = []

    def report(
        self,
        principal: WorkloadPrincipal,
        *,
        basis: CompanyReportBasis,
        from_month: str,
        to_month: str,
        company_ref: UUID | None = None,
    ) -> CompanyReportPage:
        self.calls.append((principal, basis, from_month, to_month, company_ref))
        if self.error is not None:
            raise self.error
        return self.page

    def composition(
        self,
        principal: WorkloadPrincipal,
        *,
        basis: CompanyReportBasis,
        from_month: str,
        to_month: str,
        company_ref: UUID | None = None,
    ) -> CompanyReportCompositionPage:
        self.calls.append((principal, basis, from_month, to_month, company_ref))
        if self.error is not None:
            raise self.error
        return self.composition_page


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        env="test",
        runtime_role="migrate",
        database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
        artifact_root=Path.cwd() / "synthetic-artifacts",
        enable_internal_read_api=enabled,
        internal_read_policy_generation=(7 if enabled else None),
    )


def _principal(*, capability: bool = True) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:company-report-route-test",
        san_uri="spiffe://ledgerbridge.test/company-report-route-test",
        policy_generation=7,
        capabilities=(frozenset({Capability.LEDGER_READ}) if capability else frozenset()),
        grants=(
            EntityGrant(
                entity_ref=COMPANY,
                business_unit_refs=frozenset({"unit-a"}),
                business_unit_ids=frozenset({UNIT}),
                business_unit_bindings=(("unit-a", UNIT),),
                allow_unassigned_candidates=True,
            ),
        ),
    )


def _client(
    *,
    settings: Settings | None = None,
    principal: WorkloadPrincipal | None = None,
    service_factory: Callable[[], object] | None = None,
    override_principal: bool = True,
) -> TestClient:
    application = FastAPI()
    application.add_middleware(InternalReadNoStoreMiddleware)
    application.include_router(router)
    application.dependency_overrides[get_settings] = lambda: settings or _settings()
    if override_principal:
        application.dependency_overrides[get_internal_read_principal] = lambda: (
            principal or _principal()
        )
    if service_factory is not None:
        application.dependency_overrides[get_company_reporting_service] = service_factory
    return TestClient(application)


def _assert_problem(response: Response, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "type": f"urn:ledgerbridge:problem:{code.lower().replace('_', '-')}",
        "title": {
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            503: "Service Unavailable",
        }[status_code],
        "status": status_code,
        "code": code,
    }


def test_company_reporting_router_has_two_read_only_routes() -> None:
    routes = [route for route in router.routes if isinstance(route, APIRoute)]

    assert [(route.path, route.methods) for route in routes] == [
        ("/internal/v1/company-reports", {"GET"}),
        ("/internal/v1/company-report-composition", {"GET"}),
    ]


def test_closed_gate_precedes_authentication_and_service_construction() -> None:
    application = FastAPI()
    application.add_middleware(InternalReadNoStoreMiddleware)
    application.include_router(router)
    application.dependency_overrides[get_settings] = lambda: _settings(enabled=False)
    calls = 0

    def unexpected_dependency() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("closed route must not resolve later dependencies")

    application.dependency_overrides[get_internal_read_principal] = unexpected_dependency
    application.dependency_overrides[get_company_reporting_service] = unexpected_dependency

    response = TestClient(application).get(
        "/internal/v1/company-reports?from_month=2026-08&to_month=2026-08"
    )

    _assert_problem(response, 404, "INTERNAL_READ_DISABLED")
    assert calls == 0


def test_ledger_capability_precedes_query_validation_and_service_construction() -> None:
    calls = 0

    def unexpected_service() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("service must not be built before capability authorization")

    response = _client(
        principal=_principal(capability=False),
        service_factory=unexpected_service,
    ).get("/internal/v1/company-reports?unknown=1")

    _assert_problem(response, 403, "CAPABILITY_REQUIRED")
    assert calls == 0


def test_collection_scope_precedes_query_validation_and_service_construction() -> None:
    calls = 0

    def unexpected_service() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("service must not be built without collection scope")

    principal = WorkloadPrincipal(
        principal_ref="workload:no-company-scope",
        san_uri="spiffe://ledgerbridge.test/no-company-scope",
        policy_generation=7,
        capabilities=frozenset({Capability.LEDGER_READ}),
        grants=(),
    )
    response = _client(
        principal=principal,
        service_factory=unexpected_service,
    ).get("/internal/v1/company-reports?unknown=1")

    _assert_problem(response, 404, "RESOURCE_NOT_FOUND")
    assert calls == 0


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?from_month=2026-08",
        "?to_month=2026-08",
        "?from_month=2026-08&to_month=2026-08&unknown=1",
        "?from_month=2026-08&to_month=2026-08",
        "?from_month=2026-08&from_month=2026-07&to_month=2026-08",
        "?from_month=2026-13&to_month=2026-13",
        "?from_month=2026-09&to_month=2026-08",
        "?from_month=2025-01&to_month=2027-01",
        "?from_month=2026-08&to_month=2026-08&company_ref=not-a-uuid",
        "?from_month=2026-08&to_month=2026-08&basis=confirmed_candidate",
        "?from_month=2026-08&to_month=2026-08&basis=MIXED",
    ],
)
def test_closed_query_contract_requires_a_bounded_month_range(query: str) -> None:
    calls = 0

    def unexpected_service() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("service must not be built before parameter validation")

    response = _client(service_factory=unexpected_service).get(
        f"/internal/v1/company-reports{query}"
    )

    _assert_problem(response, 400, "INVALID_QUERY")
    assert calls == 0


def test_route_returns_real_service_projection_and_ignores_identity_headers() -> None:
    service = _Service()
    client = _client(service_factory=lambda: service)

    response = client.get(
        "/internal/v1/company-reports?from_month=2026-08&to_month=2026-08"
        f"&basis=CONFIRMED_CANDIDATE&company_ref={COMPANY}",
        headers={
            "X-Company-Ref": str(OTHER_COMPANY),
            "X-Entity-Ref": str(OTHER_COMPANY),
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == _page().model_dump(mode="json")
    assert len(service.calls) == 1
    principal, basis, from_month, to_month, company_ref = service.calls[0]
    assert principal == _principal()
    assert basis is CompanyReportBasis.CONFIRMED_CANDIDATE
    assert (from_month, to_month, company_ref) == ("2026-08", "2026-08", COMPANY)


def test_composition_route_returns_category_projection() -> None:
    service = _Service()
    response = _client(service_factory=lambda: service).get(
        "/internal/v1/company-report-composition"
        "?from_month=2026-08&to_month=2026-08&basis=CONFIRMED_CANDIDATE"
        f"&company_ref={COMPANY}"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == _composition_page().model_dump(mode="json")
    assert service.calls[0][1:] == (
        CompanyReportBasis.CONFIRMED_CANDIDATE,
        "2026-08",
        "2026-08",
        COMPANY,
    )


def test_composition_route_rejects_statement_basis_before_service_construction() -> None:
    calls = 0

    def unexpected_service() -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("statement composition must fail before service construction")

    response = _client(service_factory=unexpected_service).get(
        "/internal/v1/company-report-composition"
        "?from_month=2026-08&to_month=2026-08&basis=ACCOUNT_STATEMENT"
    )

    _assert_problem(response, 400, "INVALID_QUERY")
    assert calls == 0


def test_unknown_and_unauthorized_company_filters_share_the_not_found_problem() -> None:
    unknown = _Service(error=ResourceNotVisible("resource was not found"))
    unauthorized = _Service(error=ResourceNotVisible("resource was not found"))

    unknown_response = _client(service_factory=lambda: unknown).get(
        "/internal/v1/company-reports?from_month=2026-08&to_month=2026-08"
        f"&basis=CONFIRMED_CANDIDATE&company_ref={COMPANY}"
    )
    unauthorized_response = _client(service_factory=lambda: unauthorized).get(
        "/internal/v1/company-reports?from_month=2026-08&to_month=2026-08"
        f"&basis=CONFIRMED_CANDIDATE&company_ref={OTHER_COMPANY}"
    )

    _assert_problem(unknown_response, 404, "RESOURCE_NOT_FOUND")
    _assert_problem(unauthorized_response, 404, "RESOURCE_NOT_FOUND")
    assert unknown_response.content == unauthorized_response.content
