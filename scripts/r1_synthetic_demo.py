"""Run the R1 internal-read contract locally over the packaged synthetic fixture.

This launcher is intentionally a demo-only boundary.  It binds to loopback,
uses no database or real evidence, and installs a fixed principal through the
same typed verifier middleware used by the route contract.  It must not be
used as a production authentication implementation.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import gettempdir
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledgerbridge.config import Settings, get_settings
from ledgerbridge.internal_read_audit import (
    EvidenceReadAuditEvent,
    get_internal_read_audit_sink,
)
from ledgerbridge.internal_read_auth import (
    VerifiedInternalReadPrincipalMiddleware,
    VerifiedMtlsPrincipal,
)
from ledgerbridge.internal_read_contract import (
    READ_CAPABILITIES,
    EntityGrant,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_routes import InternalReadNoStoreMiddleware, router

DEMO_ENTITY_REF = UUID("10000000-0000-4000-8000-000000000001")
DEMO_BUSINESS_UNIT = "unit-demo-a"
DEMO_POLICY_GENERATION = 11

DEMO_PRINCIPAL = WorkloadPrincipal(
    principal_ref="workload:r1-demo",
    san_uri="spiffe://ledgerbridge.test/r1-demo",
    policy_generation=DEMO_POLICY_GENERATION,
    capabilities=READ_CAPABILITIES,
    grants=(
        EntityGrant(
            entity_ref=DEMO_ENTITY_REF,
            business_unit_refs=frozenset({DEMO_BUSINESS_UNIT}),
            allow_unassigned_candidates=True,
        ),
    ),
)


class DemoAuditSink:
    """Process-local audit sink so the evidence route can be demonstrated."""

    def __init__(self) -> None:
        self.events: list[EvidenceReadAuditEvent] = []

    def append(self, event: EvidenceReadAuditEvent) -> None:
        self.events.append(event)


DEMO_AUDIT_SINK = DemoAuditSink()


def _demo_settings() -> Settings:
    return Settings(
        env="test",
        runtime_role="migrate",
        database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
        artifact_root=Path(gettempdir()) / "ledgerbridge-r1-synthetic-demo",
        enable_internal_read_api=True,
        internal_read_backend="synthetic",
        internal_read_policy_generation=DEMO_POLICY_GENERATION,
    )


def _demo_verifier(scope: Mapping[str, object]) -> VerifiedMtlsPrincipal:
    """Install a fixed local identity without reading client-supplied headers."""

    _ = scope
    now = datetime.now(UTC)
    return VerifiedMtlsPrincipal(
        principal=DEMO_PRINCIPAL,
        issued_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        policy_generation=DEMO_POLICY_GENERATION,
    )


app = FastAPI(
    title="LedgerBridge R1 Synthetic Demo",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(InternalReadNoStoreMiddleware)
app.add_middleware(VerifiedInternalReadPrincipalMiddleware, verifier=_demo_verifier)
app.include_router(router)
app.dependency_overrides[get_settings] = _demo_settings
app.dependency_overrides[get_internal_read_audit_sink] = lambda: DEMO_AUDIT_SINK


def run_self_check() -> dict[str, object]:
    """Exercise the complete local route flow and return a small proof record."""

    DEMO_AUDIT_SINK.events.clear()
    with TestClient(app) as client:
        responses = {
            "capabilities": client.get("/internal/v1/capabilities"),
            "candidates": client.get(
                "/internal/v1/candidates",
                params={"month": "2026-08", "business_unit": DEMO_BUSINESS_UNIT},
            ),
            "candidate": client.get("/internal/v1/candidates/30000000-0000-4000-8000-000000000002"),
            "evidence": client.get(
                "/internal/v1/evidence/20000000-0000-4000-8000-000000000001/content"
            ),
            "reconciliation": client.get(
                f"/internal/v1/reconciliations/2026-08?entity_ref={DEMO_ENTITY_REF}"
                f"&business_unit={DEMO_BUSINESS_UNIT}"
            ),
            "ledger_summary": client.get(
                "/internal/v1/ledger-summary",
                params={
                    "entity_ref": str(DEMO_ENTITY_REF),
                    "business_unit": DEMO_BUSINESS_UNIT,
                    "from_month": "2026-08",
                    "to_month": "2026-08",
                },
            ),
        }
    failed = {
        name: response.status_code
        for name, response in responses.items()
        if response.status_code != 200
    }
    if failed:
        raise RuntimeError(f"synthetic demo route failure: {failed}")
    return {
        "mode": "synthetic",
        "routes_checked": len(responses),
        "candidate_count": len(responses["candidates"].json()["items"]),
        "evidence_audit_events": len(DEMO_AUDIT_SINK.events),
        "ledger_totals_minor": responses["ledger_summary"].json()["totals_minor"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the six-route synthetic self-check and exit",
    )
    args = parser.parse_args()
    if args.check:
        import json

        print(json.dumps(run_self_check(), sort_keys=True))
        raise SystemExit(0)

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8651, log_level="info")
