from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from ledgerbridge.candidate_contract import CandidateAction
from ledgerbridge.internal_read_contract import (
    CANDIDATE_ACTION_CAPABILITIES,
    READ_CAPABILITIES,
    READ_ROUTE_CAPABILITIES,
    READ_ROUTE_SCOPE_MODES,
    AccountingDimensions,
    AuthenticationDenied,
    AuthorizationDenied,
    CandidatePage,
    CapabilitiesResponse,
    Capability,
    EntityGrant,
    LedgerSummary,
    ReconciliationProjection,
    ResourceNotVisible,
    ScopeMode,
    SyntheticPeerEvidence,
    WorkloadPrincipal,
    authorize_collection_read,
    authorize_read,
    filter_visible_scopes,
    require_candidate_workload_scope,
    resolve_synthetic_peer,
)
from ledgerbridge.main import app

ROOT = Path(__file__).parents[1]
OPENAPI = ROOT / "docs" / "contracts" / "internal-read-v1.openapi.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "r0_contract_fixture.json"
ENTITY_A = UUID("10000000-0000-4000-8000-000000000001")
ENTITY_B = UUID("10000000-0000-4000-8000-000000000002")
GENERATION = 7


def _principal(
    name: str,
    capabilities: frozenset[Capability],
    *,
    entity: UUID = ENTITY_A,
    unit: str = "unit-demo-a",
) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref=f"workload:{name}",
        san_uri=f"spiffe://ledgerbridge.test/{name}",
        policy_generation=GENERATION,
        capabilities=capabilities,
        grants=(EntityGrant(entity_ref=entity, business_unit_refs=frozenset({unit})),),
    )


def _peer(san: str = "spiffe://ledgerbridge.test/bff-full") -> SyntheticPeerEvidence:
    return SyntheticPeerEvidence(
        san_uri=san,
        chain_verified=True,
        within_validity=True,
        client_auth_eku=True,
        revoked=False,
        policy_generation=GENERATION,
    )


def test_openapi_is_a_separate_read_only_mutual_tls_contract() -> None:
    document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    assert document["openapi"] == "3.1.0"
    assert set(document["paths"]) == {
        "/internal/v1/capabilities",
        "/internal/v1/accounting-dimensions",
        "/internal/v1/candidates",
        "/internal/v1/candidates/{id}",
        "/internal/v1/evidence/{id}/content",
        "/internal/v1/reconciliations/{month}",
        "/internal/v1/ledger-summary",
    }
    for operations in document["paths"].values():
        assert set(operations) == {"get"}
    assert document["components"]["securitySchemes"] == {
        "workloadMtls": {
            "type": "mutualTLS",
            "description": (
                "Workload identity is derived only from a trusted certificate verifier and "
                "fixed SAN policy; request headers cannot assert a principal."
            ),
        }
    }

    candidate_parameters = document["paths"]["/internal/v1/candidates"]["get"]["parameters"]
    assert {parameter["$ref"].rsplit("/", 1)[-1] for parameter in candidate_parameters} == {
        "Month",
        "Status",
        "BusinessUnit",
    }
    assert "Cursor" not in document["components"]["parameters"]
    expected_statuses = {"200", "400", "401", "403", "404", "503"}
    for operations in document["paths"].values():
        responses = operations["get"]["responses"]
        assert set(responses) == expected_statuses
        assert responses["200"]["headers"]["Cache-Control"] == {
            "$ref": "#/components/headers/NoStore"
        }
    for response in document["components"]["responses"].values():
        assert response["headers"]["Cache-Control"] == {"$ref": "#/components/headers/NoStore"}
    wire = OPENAPI.read_text(encoding="utf-8").lower()
    for forbidden in (
        "sessioncookie",
        "set-cookie",
        "access-control-allow-origin",
        "database_url",
        "storage_key",
        "raw_fields",
        "private_key",
        "oauth",
    ):
        assert forbidden not in wire

    # R1 installs the frozen GET routes behind a default-off gate.
    installed: dict[str, set[str]] = {}
    for route in app.routes:
        nested = getattr(getattr(route, "original_router", None), "routes", (route,))
        for candidate in nested:
            path = getattr(candidate, "path", None)
            methods = getattr(candidate, "methods", None)
            if isinstance(path, str) and path.startswith("/internal/v1"):
                installed[path] = set(methods or ())
    # Later D1 routers may add separately gated internal command paths.  This
    # R0 contract remains authoritative only for its frozen read-only surface.
    assert {path: installed.get(path) for path in document["paths"]} == {
        path: {"GET"} for path in document["paths"]
    }


@pytest.mark.parametrize(
    ("business_units", "categories", "message"),
    [
        (
            [
                {"ref": "unit-a", "label": "Duplicate label"},
                {"ref": "unit-b", "label": "Duplicate label"},
            ],
            [],
            "active business unit labels",
        ),
        (
            [],
            [
                {"code": "A", "label": "Duplicate label"},
                {"code": "B", "label": "Duplicate label"},
            ],
            "active reporting category labels",
        ),
    ],
)
def test_accounting_dimensions_reject_duplicate_active_labels(
    business_units: list[dict[str, str]],
    categories: list[dict[str, str]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        AccountingDimensions.model_validate(
            {
                "entity_ref": ENTITY_A,
                "business_units": business_units,
                "categories": categories,
            }
        )


def test_route_capability_matrix_is_exact_and_non_transitive() -> None:
    assert set(READ_ROUTE_CAPABILITIES) == {
        "GET /internal/v1/capabilities",
        "GET /internal/v1/accounting-dimensions",
        "GET /internal/v1/candidates",
        "GET /internal/v1/candidates/{id}",
        "GET /internal/v1/evidence/{id}/content",
        "GET /internal/v1/reconciliations/{month}",
        "GET /internal/v1/ledger-summary",
        "GET /internal/v1/company-reports",
        "GET /internal/v1/company-report-composition",
        "GET /internal/v1/personal-finance",
        "GET /internal/v1/company-bank-statements/{id}",
        "GET /internal/v1/company-transaction-classifications",
        "GET /internal/v1/company-transaction-classification-summary",
    }
    assert READ_ROUTE_SCOPE_MODES == {
        "GET /internal/v1/capabilities": ScopeMode.SYSTEM,
        "GET /internal/v1/accounting-dimensions": ScopeMode.OBJECT,
        "GET /internal/v1/candidates": ScopeMode.COLLECTION,
        "GET /internal/v1/candidates/{id}": ScopeMode.OBJECT,
        "GET /internal/v1/evidence/{id}/content": ScopeMode.OBJECT,
        "GET /internal/v1/reconciliations/{month}": ScopeMode.OBJECT,
        "GET /internal/v1/ledger-summary": ScopeMode.OBJECT,
        "GET /internal/v1/company-reports": ScopeMode.COLLECTION,
        "GET /internal/v1/company-report-composition": ScopeMode.COLLECTION,
        "GET /internal/v1/personal-finance": ScopeMode.OBJECT,
        "GET /internal/v1/company-bank-statements/{id}": ScopeMode.OBJECT,
        "GET /internal/v1/company-transaction-classifications": ScopeMode.COLLECTION,
        "GET /internal/v1/company-transaction-classification-summary": ScopeMode.COLLECTION,
    }
    candidate_only = _principal("candidate-only", frozenset({Capability.CANDIDATE_READ}))
    authorize_read(
        candidate_only,
        Capability.CANDIDATE_READ,
        entity_ref=ENTITY_A,
        business_unit_ref="unit-demo-a",
    )
    for denied in (
        Capability.SYSTEM_READ,
        Capability.EVIDENCE_READ,
        Capability.RECONCILIATION_READ,
        Capability.LEDGER_READ,
        Capability.COMPANY_REPORT_READ,
    ):
        with pytest.raises(AuthorizationDenied):
            authorize_read(candidate_only, denied)

    authorize_collection_read(candidate_only, Capability.CANDIDATE_READ)
    ledger_collection = _principal("ledger-collection", frozenset({Capability.LEDGER_READ}))
    authorize_collection_read(ledger_collection, Capability.LEDGER_READ)
    report_collection = _principal(
        "report-collection",
        frozenset({Capability.COMPANY_REPORT_READ}),
    )
    authorize_collection_read(report_collection, Capability.COMPANY_REPORT_READ)
    with pytest.raises(AuthorizationDenied):
        authorize_collection_read(ledger_collection, Capability.COMPANY_REPORT_READ)
    with pytest.raises(AuthorizationDenied):
        authorize_collection_read(ledger_collection, Capability.EVIDENCE_READ)
    no_scope = _principal("no-scope", frozenset({Capability.CANDIDATE_READ})).model_copy(
        update={"grants": ()}
    )
    with pytest.raises(ResourceNotVisible):
        authorize_collection_read(no_scope, Capability.CANDIDATE_READ)
    with pytest.raises(ResourceNotVisible):
        authorize_read(candidate_only, Capability.CANDIDATE_READ)


def test_worker_create_decide_and_supersede_are_separate_capabilities() -> None:
    assert CANDIDATE_ACTION_CAPABILITIES == {
        CandidateAction.COMPLETE_FIELDS: Capability.CANDIDATE_DECIDE,
        CandidateAction.RESOLVE_CONFLICT: Capability.CANDIDATE_DECIDE,
        CandidateAction.CORRECT_AND_CONFIRM: Capability.CANDIDATE_DECIDE,
        CandidateAction.CONFIRM: Capability.CANDIDATE_DECIDE,
        CandidateAction.IGNORE: Capability.CANDIDATE_DECIDE,
        CandidateAction.SUPERSEDE: Capability.CANDIDATE_SUPERSEDE,
    }
    worker = _principal("worker", frozenset({Capability.CANDIDATE_CREATE}))
    reviewer = _principal("reviewer", frozenset({Capability.CANDIDATE_DECIDE}))
    supervisor = _principal("supervisor", frozenset({Capability.CANDIDATE_SUPERSEDE}))

    with pytest.raises(AuthorizationDenied):
        require_candidate_workload_scope(
            worker,
            CandidateAction.CONFIRM,
            entity_ref=ENTITY_A,
            business_unit_ref="unit-demo-a",
        )
    require_candidate_workload_scope(
        reviewer,
        CandidateAction.CONFIRM,
        entity_ref=ENTITY_A,
        business_unit_ref="unit-demo-a",
    )
    with pytest.raises(AuthorizationDenied):
        require_candidate_workload_scope(
            reviewer,
            CandidateAction.SUPERSEDE,
            entity_ref=ENTITY_A,
            business_unit_ref="unit-demo-a",
        )
    require_candidate_workload_scope(
        supervisor,
        CandidateAction.SUPERSEDE,
        entity_ref=ENTITY_A,
        business_unit_ref="unit-demo-a",
    )


def test_synthetic_mtls_identity_is_fixed_policy_and_fails_closed() -> None:
    full = _principal("bff-full", READ_CAPABILITIES)
    assert not full.capabilities & {
        Capability.CANDIDATE_CREATE,
        Capability.CANDIDATE_DECIDE,
        Capability.CANDIDATE_SUPERSEDE,
    }
    policy = {full.san_uri: full}
    assert (
        resolve_synthetic_peer(_peer(), policy=policy, current_policy_generation=GENERATION) == full
    )

    invalid_updates: tuple[dict[str, object], ...] = (
        {"chain_verified": False},
        {"within_validity": False},
        {"client_auth_eku": False},
        {"revoked": True},
        {"policy_generation": GENERATION - 1},
        {"san_uri": "spiffe://ledgerbridge.test/unknown"},
    )
    for update in invalid_updates:
        with pytest.raises(AuthenticationDenied):
            resolve_synthetic_peer(
                _peer().model_copy(update=update),
                policy=policy,
                current_policy_generation=GENERATION,
            )

    lower = _principal("candidate-only", frozenset({Capability.CANDIDATE_READ}))
    with pytest.raises(AuthenticationDenied):
        resolve_synthetic_peer(
            _peer(lower.san_uri),
            policy={lower.san_uri: full},
            current_policy_generation=GENERATION,
        )


def test_entity_and_business_unit_scope_is_applied_before_visibility() -> None:
    candidate_only = _principal("candidate-only", frozenset({Capability.CANDIDATE_READ}))
    for entity, unit in ((ENTITY_B, "unit-demo-b"), (ENTITY_A, "unit-demo-b")):
        with pytest.raises(ResourceNotVisible, match="not found"):
            authorize_read(
                candidate_only,
                Capability.CANDIDATE_READ,
                entity_ref=entity,
                business_unit_ref=unit,
            )

    visible = filter_visible_scopes(
        candidate_only,
        ((ENTITY_A, "unit-demo-a"), (ENTITY_A, "unit-demo-b"), (ENTITY_B, "unit-demo-b")),
    )
    assert visible == ((ENTITY_A, "unit-demo-a"),)


def test_fixture_supports_object_scope_and_posted_only_summary() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    entity_scopes = {
        (UUID(row["entity_ref"]), unit)
        for row in fixture["entities"]
        for unit in row["business_unit_refs"]
    }
    assert entity_scopes == {
        (ENTITY_A, "unit-demo-a"),
        (ENTITY_A, "unit-reviewed"),
        (ENTITY_B, "unit-demo-b"),
    }

    posted_a = sum(
        row["amount_minor"]
        for row in fixture["ledger_entries"]
        if row["entity_ref"] == str(ENTITY_A) and row["status"] == "POSTED"
    )
    posted_b = sum(
        row["amount_minor"]
        for row in fixture["ledger_entries"]
        if row["entity_ref"] == str(ENTITY_B) and row["status"] == "POSTED"
    )
    assert posted_a == -12345
    assert posted_b == 50000
    assert all(type(row["amount_minor"]) is int for row in fixture["ledger_entries"])

    responses = fixture["read_responses"]
    capabilities = CapabilitiesResponse.model_validate(responses["capabilities"])
    candidates = tuple(
        fixture["candidates"][index] for index in responses["candidate_page"]["candidate_indexes"]
    )
    page = CandidatePage(
        items=candidates,
        next_cursor=responses["candidate_page"]["next_cursor"],
    )
    reconciliation = ReconciliationProjection.model_validate(responses["reconciliation"])
    ledger = LedgerSummary.model_validate(responses["ledger_summary"])
    assert capabilities.data_mode == "synthetic"
    assert len(page.items) == 6
    assert reconciliation.proposals[0].amount_minor == -12345
    assert reconciliation.suspense[0].status == "OPEN"
    assert ledger.posting_status == "POSTED"
    assert ledger.totals_minor == {"SUPPLIES": -12345}
    with pytest.raises(ValueError, match="from_month must be less than or equal to to_month"):
        LedgerSummary.model_validate(
            responses["ledger_summary"] | {"from_month": "2026-09", "to_month": "2026-08"}
        )

    document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    evidence_content = document["paths"]["/internal/v1/evidence/{id}/content"]["get"]["responses"][
        "200"
    ]["content"]
    assert set(evidence_content) == {"application/octet-stream"}
    # R1 normalizes every legacy fixture media type to a download-only binary response.
    assert {item["served_media_type"] for item in fixture["evidence_objects"]} == {
        "text/plain",
        "application/octet-stream",
    }

    schemas = document["components"]["schemas"]
    assert schemas["ReconciliationProposal"]["additionalProperties"] is False
    assert schemas["SuspenseProjection"]["additionalProperties"] is False
    ledger_parameters = document["paths"]["/internal/v1/ledger-summary"]["get"]["parameters"]
    assert "must be greater than or equal to from_month" in ledger_parameters[-1]["description"]


def test_capabilities_response_contract_has_no_deployment_details() -> None:
    document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    capabilities = document["components"]["schemas"]["Capabilities"]
    assert set(capabilities["properties"]) == {
        "contract_version",
        "candidate_contract_version",
        "state_graph_version",
        "data_mode",
        "enabled_modules",
    }
    assert capabilities["properties"]["data_mode"] == {"const": "synthetic"}
