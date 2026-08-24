from __future__ import annotations

from fastapi.testclient import TestClient

from scripts.r1_synthetic_demo import (
    DEMO_AUDIT_SINK,
    DEMO_BUSINESS_UNIT,
    DEMO_ENTITY_REF,
    app,
)


def test_local_demo_serves_all_six_routes_through_verified_scope() -> None:
    DEMO_AUDIT_SINK.events.clear()
    with TestClient(app) as client:
        capabilities = client.get("/internal/v1/capabilities")
        candidates = client.get(
            "/internal/v1/candidates",
            params={"month": "2026-08", "business_unit": DEMO_BUSINESS_UNIT},
            headers={"X-Principal": "workload:attacker"},
        )
        candidate = client.get("/internal/v1/candidates/30000000-0000-4000-8000-000000000002")
        evidence = client.get("/internal/v1/evidence/20000000-0000-4000-8000-000000000001/content")
        reconciliation = client.get(
            f"/internal/v1/reconciliations/2026-08?entity_ref={DEMO_ENTITY_REF}"
            f"&business_unit={DEMO_BUSINESS_UNIT}"
        )
        summary = client.get(
            "/internal/v1/ledger-summary",
            params={
                "entity_ref": str(DEMO_ENTITY_REF),
                "business_unit": DEMO_BUSINESS_UNIT,
                "from_month": "2026-08",
                "to_month": "2026-08",
            },
        )

    assert capabilities.status_code == 200
    assert capabilities.json()["data_mode"] == "synthetic"
    assert candidates.status_code == 200
    assert len(candidates.json()["items"]) == 3
    assert {item["business_unit_ref"] for item in candidates.json()["items"]} == {
        DEMO_BUSINESS_UNIT
    }
    assert candidate.status_code == 200
    assert evidence.status_code == 200
    assert evidence.headers["cache-control"] == "no-store"
    assert evidence.headers["x-content-type-options"] == "nosniff"
    assert reconciliation.status_code == 200
    assert reconciliation.json()["posted_amount_minor"] == -12345
    assert summary.status_code == 200
    assert summary.json()["totals_minor"] == {"SUPPLIES": -12345}
    assert len(DEMO_AUDIT_SINK.events) == 1
