from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_read_audit import (
    EvidenceReadAuditEvent,
    get_internal_read_audit_sink,
)
from ledgerbridge.internal_read_auth import get_internal_read_principal
from ledgerbridge.internal_read_contract import (
    READ_CAPABILITIES,
    Capability,
    EntityGrant,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_routes import (
    InternalReadNoStoreMiddleware,
    get_synthetic_internal_read_service,
    router,
)
from ledgerbridge.internal_read_service import AccountingDimensionsInvalid

ENTITY_A = UUID("10000000-0000-4000-8000-000000000001")
ENTITY_B = UUID("10000000-0000-4000-8000-000000000002")
CANDIDATE_A = UUID("30000000-0000-4000-8000-000000000002")
CANDIDATE_B = UUID("30000000-0000-4000-8000-000000000004")
EVIDENCE_A = UUID("20000000-0000-4000-8000-000000000001")
POLICY_GENERATION = 11


class _MemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[EvidenceReadAuditEvent] = []

    def append(self, event: EvidenceReadAuditEvent) -> None:
        self.events.append(event)


def _settings(*, enabled: bool = True) -> Settings:
    return Settings(
        env="test",
        runtime_role="migrate",
        database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
        artifact_root=Path.cwd() / "synthetic-artifacts",
        enable_internal_read_api=enabled,
        internal_read_policy_generation=(POLICY_GENERATION if enabled else None),
    )


def _principal(
    capabilities: frozenset[Capability] = READ_CAPABILITIES,
    *,
    entity_b: bool = True,
    allow_unassigned: bool = True,
) -> WorkloadPrincipal:
    grants = [
        EntityGrant(
            entity_ref=ENTITY_A,
            business_unit_refs=frozenset({"unit-demo-a"}),
            allow_unassigned_candidates=allow_unassigned,
        )
    ]
    if entity_b:
        grants.append(
            EntityGrant(
                entity_ref=ENTITY_B,
                business_unit_refs=frozenset({"unit-demo-b"}),
            )
        )
    return WorkloadPrincipal(
        principal_ref="workload:r1-route-test",
        san_uri="spiffe://ledgerbridge.test/r1-route-test",
        policy_generation=POLICY_GENERATION,
        capabilities=capabilities,
        grants=tuple(grants),
    )


def _client(
    *,
    settings: Settings | None = None,
    principal: WorkloadPrincipal | None = None,
    override_principal: bool = True,
    audit_sink: _MemoryAuditSink | None = None,
    service_factory: Callable[[], object] | None = None,
) -> TestClient:
    app = FastAPI()
    app.add_middleware(InternalReadNoStoreMiddleware)
    app.include_router(router)
    configured = settings or _settings()
    app.dependency_overrides[get_settings] = lambda: configured
    if override_principal:
        admitted = principal or _principal()
        app.dependency_overrides[get_internal_read_principal] = lambda: admitted
    if audit_sink is not None:
        app.dependency_overrides[get_internal_read_audit_sink] = lambda: audit_sink
    if service_factory is not None:
        app.dependency_overrides[get_synthetic_internal_read_service] = service_factory
    return TestClient(app)


def _assert_problem(response: HttpxResponse, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == {
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


def test_router_topology_matches_the_frozen_contract() -> None:
    assert {route.path for route in router.routes if isinstance(route, APIRoute)} == {
        "/internal/v1/capabilities",
        "/internal/v1/accounting-dimensions",
        "/internal/v1/candidates",
        "/internal/v1/candidates/{id}",
        "/internal/v1/personal-finance-summary",
        "/internal/v1/evidence/{id}/content",
        "/internal/v1/reconciliations/{month}",
        "/internal/v1/ledger-summary",
    }


def test_gate_precedes_authentication_and_hides_route() -> None:
    app = FastAPI()
    app.add_middleware(InternalReadNoStoreMiddleware)
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: _settings(enabled=False)
    authentication_calls = 0

    def unexpected_authentication() -> WorkloadPrincipal:
        nonlocal authentication_calls
        authentication_calls += 1
        raise AssertionError("authentication must not run behind a closed gate")

    app.dependency_overrides[get_internal_read_principal] = unexpected_authentication
    response = TestClient(app).get("/internal/v1/candidates?unknown=value")

    _assert_problem(response, 404, "INTERNAL_READ_DISABLED")
    assert authentication_calls == 0


def test_missing_verified_scope_is_401_and_identity_headers_are_ignored() -> None:
    client = _client(override_principal=False)
    client.cookies.set("principal", "workload:attacker")

    response = client.get(
        "/internal/v1/candidates?unknown=value",
        headers={
            "Authorization": "Bearer attacker",
            "X-Principal": "workload:attacker",
        },
    )

    _assert_problem(response, 401, "AUTH_REQUIRED")


def test_capability_precedes_parameters_and_service_construction() -> None:
    service_calls = 0

    def unexpected_service() -> object:
        nonlocal service_calls
        service_calls += 1
        raise AssertionError("service must not be built before capability authorization")

    principal = _principal(frozenset({Capability.SYSTEM_READ}))
    client = _client(principal=principal, service_factory=unexpected_service)

    response = client.get("/internal/v1/candidates?unknown=value")

    _assert_problem(response, 403, "CAPABILITY_REQUIRED")
    assert service_calls == 0


def test_parameter_validation_precedes_service_construction() -> None:
    service_calls = 0

    def unexpected_service() -> object:
        nonlocal service_calls
        service_calls += 1
        raise AssertionError("service must not be built before parameter validation")

    client = _client(service_factory=unexpected_service)
    response = client.get("/internal/v1/candidates?unknown=value")

    _assert_problem(response, 400, "INVALID_QUERY")
    assert service_calls == 0


@pytest.mark.parametrize(
    "path",
    [
        "/internal/v1/capabilities?unexpected=1",
        "/internal/v1/accounting-dimensions",
        "/internal/v1/accounting-dimensions?entity_ref=not-a-uuid",
        "/internal/v1/candidates?month=2026-08&month=2026-09",
        "/internal/v1/candidates?month=2026-13",
        "/internal/v1/candidates?status=confirmed",
        "/internal/v1/candidates?business_unit=%20unit-demo-a",
        "/internal/v1/candidates?cursor=opaque",
        "/internal/v1/candidates?cursor=",
        ("/internal/v1/reconciliations/2026-08?entity_ref=10000000-0000-4000-8000-000000000001"),
        (
            "/internal/v1/ledger-summary"
            "?entity_ref=10000000-0000-4000-8000-000000000001"
            "&business_unit=unit-demo-a&from_month=2026-09&to_month=2026-08"
        ),
    ],
)
def test_closed_parameter_contract_rejects_unknown_duplicate_or_invalid_values(
    path: str,
) -> None:
    principal = (
        _principal(frozenset({*READ_CAPABILITIES, Capability.CANDIDATE_DECIDE}))
        if path.startswith("/internal/v1/accounting-dimensions")
        else None
    )
    response = _client(principal=principal).get(path)

    _assert_problem(response, 400, "INVALID_QUERY")


def test_parameter_error_precedes_empty_scope_not_found() -> None:
    principal = _principal(
        frozenset({Capability.CANDIDATE_READ}),
        entity_b=False,
    ).model_copy(update={"grants": ()})
    client = _client(principal=principal)

    _assert_problem(client.get("/internal/v1/candidates?unknown=1"), 400, "INVALID_QUERY")
    _assert_problem(client.get("/internal/v1/candidates"), 404, "RESOURCE_NOT_FOUND")


def test_accounting_dimensions_require_decide_and_hide_cross_company_catalogs() -> None:
    decide = frozenset({*READ_CAPABILITIES, Capability.CANDIDATE_DECIDE})
    principal = _principal(decide, entity_b=False)
    client = _client(principal=principal)

    response = client.get(f"/internal/v1/accounting-dimensions?entity_ref={ENTITY_A}")

    assert response.status_code == 200
    assert response.json() == {
        "contract_version": "ledgerbridge.accounting-dimensions.v1",
        "entity_ref": str(ENTITY_A),
        "business_units": [{"ref": "unit-demo-a", "label": "Demo unit A"}],
        "categories": [
            {"code": "SUPPLIES", "label": "Synthetic supplies"},
            {"code": "TRAVEL", "label": "Reviewed travel"},
        ],
    }
    read_only = _client(principal=_principal(READ_CAPABILITIES, entity_b=False))
    _assert_problem(
        read_only.get(f"/internal/v1/accounting-dimensions?entity_ref={ENTITY_A}"),
        403,
        "CAPABILITY_REQUIRED",
    )
    cross_company = _principal(decide).model_copy(
        update={"grants": (_principal(decide).grants[1],)}
    )
    _assert_problem(
        _client(principal=cross_company).get(
            f"/internal/v1/accounting-dimensions?entity_ref={ENTITY_A}"
        ),
        404,
        "RESOURCE_NOT_FOUND",
    )


def test_accounting_dimensions_duplicate_active_labels_request_registry_governance() -> None:
    decide = frozenset({*READ_CAPABILITIES, Capability.CANDIDATE_DECIDE})

    def unavailable(*args: object, **kwargs: object) -> object:
        raise AccountingDimensionsInvalid("active labels require registry governance")

    service = SimpleNamespace(get_accounting_dimensions=unavailable)
    response = _client(
        principal=_principal(decide, entity_b=False),
        service_factory=lambda: service,
    ).get(f"/internal/v1/accounting-dimensions?entity_ref={ENTITY_A}")

    _assert_problem(response, 503, "ACCOUNTING_DIMENSIONS_INVALID")


def test_missing_and_out_of_scope_candidate_are_identical_404() -> None:
    principal = _principal(
        frozenset({Capability.CANDIDATE_READ}),
        entity_b=False,
    )
    client = _client(principal=principal)

    outside = client.get(f"/internal/v1/candidates/{CANDIDATE_B}")
    absent = client.get("/internal/v1/candidates/ffffffff-ffff-4fff-8fff-ffffffffffff")
    malformed = client.get("/internal/v1/candidates/not-a-uuid")

    _assert_problem(outside, 404, "RESOURCE_NOT_FOUND")
    _assert_problem(absent, 404, "RESOURCE_NOT_FOUND")
    _assert_problem(malformed, 404, "RESOURCE_NOT_FOUND")
    assert outside.content == absent.content
    assert absent.content == malformed.content


@pytest.mark.parametrize("method", ["head", "options", "post", "put", "patch", "delete"])
def test_non_get_methods_are_not_enabled_and_never_emit_cors(method: str) -> None:
    client = _client()

    response = getattr(client, method)(
        "/internal/v1/capabilities",
        headers={
            "Origin": "https://browser.invalid",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 405
    assert response.headers["cache-control"] == "no-store"
    assert not any(name.lower().startswith("access-control-") for name in response.headers)


def test_unknown_internal_path_is_not_cached_or_cors_enabled() -> None:
    response = _client().get(
        "/internal/v1/unknown",
        headers={"Origin": "https://browser.invalid"},
    )

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert not any(name.lower().startswith("access-control-") for name in response.headers)


def test_six_read_routes_return_closed_synthetic_projections() -> None:
    sink = _MemoryAuditSink()
    client = _client(audit_sink=sink)

    capabilities = client.get("/internal/v1/capabilities")
    candidates = client.get("/internal/v1/candidates?month=2026-08&business_unit=unit-demo-a")
    candidate = client.get(f"/internal/v1/candidates/{CANDIDATE_A}")
    reconciliation = client.get(
        f"/internal/v1/reconciliations/2026-08?entity_ref={ENTITY_A}&business_unit=unit-demo-a"
    )
    summary = client.get(
        "/internal/v1/ledger-summary"
        f"?entity_ref={ENTITY_A}&business_unit=unit-demo-a"
        "&from_month=2026-08&to_month=2026-08"
    )
    evidence = client.get(f"/internal/v1/evidence/{EVIDENCE_A}/content")

    assert capabilities.status_code == 200
    assert capabilities.json()["data_mode"] == "synthetic"
    assert candidates.status_code == 200
    assert candidates.json()["next_cursor"] is None
    assert {item["business_unit_ref"] for item in candidates.json()["items"]} == {"unit-demo-a"}
    assert candidate.status_code == 200
    assert candidate.json()["candidate_ref"] == str(CANDIDATE_A)
    assert reconciliation.status_code == 200
    assert reconciliation.json()["posted_amount_minor"] == -12345
    assert summary.status_code == 200
    assert summary.json()["posting_status"] == "POSTED"
    assert summary.json()["totals_minor"] == {"SUPPLIES": -12345}
    for response in (capabilities, candidates, candidate, reconciliation, summary, evidence):
        assert response.headers["cache-control"] == "no-store"

    digest = hashlib.sha256(evidence.content).digest()
    assert evidence.status_code == 200
    assert evidence.headers["content-type"] == "application/octet-stream"
    assert evidence.headers["cache-control"] == "no-store"
    assert evidence.headers["x-content-type-options"] == "nosniff"
    assert evidence.headers["content-disposition"].startswith('attachment; filename="evidence-')
    assert evidence.headers["content-disposition"].endswith('.bin"')
    assert evidence.headers["content-digest"] == (
        f"sha-256=:{base64.b64encode(digest).decode('ascii')}:"
    )
    assert len(sink.events) == 1
    assert sink.events[0].model_dump(exclude={"occurred_at"}) == {
        "event_version": "ledgerbridge.internal-read-audit.v1",
        "event_type": "EVIDENCE_CONTENT_READ",
        "principal_ref": "workload:r1-route-test",
        "principal_san_uri": "spiffe://ledgerbridge.test/r1-route-test",
        "policy_generation": POLICY_GENERATION,
        "evidence_ref": EVIDENCE_A,
        "entity_ref": ENTITY_A,
        "business_unit_ref": "unit-demo-a",
        "byte_size": len(evidence.content),
        "sha256": digest.hex(),
        "outcome": "SUCCEEDED",
    }


def test_evidence_default_audit_sink_fails_closed() -> None:
    response = _client().get(f"/internal/v1/evidence/{EVIDENCE_A}/content")

    _assert_problem(response, 503, "AUDIT_SINK_UNAVAILABLE")
    assert response.content != b"Synthetic evidence"


def test_evidence_is_fully_verified_before_audit_or_response() -> None:
    sink = _MemoryAuditSink()

    class TamperedService:
        def get_evidence(self, principal: WorkloadPrincipal, evidence_ref: UUID) -> object:
            _ = (principal, evidence_ref)
            return SimpleNamespace(
                content=b"tampered",
                entity_ref=ENTITY_A,
                business_unit_ref="unit-demo-a",
                media_type="application/octet-stream",
                filename="evidence.bin",
                sha256="0" * 64,
                byte_size=8,
            )

    response = _client(
        audit_sink=sink,
        service_factory=TamperedService,
    ).get(f"/internal/v1/evidence/{EVIDENCE_A}/content")

    _assert_problem(response, 503, "EVIDENCE_INTEGRITY_UNAVAILABLE")
    assert response.content != b"tampered"
    assert sink.events == []
