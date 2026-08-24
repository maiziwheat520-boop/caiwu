"""Run the R1 review API contract over one in-memory synthetic review item.

This launcher is a demo-only boundary.  It reuses the production route
handlers and response models, but replaces the database service with a tiny
in-memory fixture.  It binds only to loopback and never enables the real
database-backed review API.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from tempfile import gettempdir
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ledgerbridge.config import Settings
from ledgerbridge.main import (
    ReviewDecisionRequest,
    ReviewResponse,
    decide_review,
    list_reviews,
    require_review_api,
)
from ledgerbridge.models import ReviewItem, ReviewItemKind
from ledgerbridge.review_service import ReviewConflict, ReviewNotFound

DEMO_REVIEW_ID = UUID("40000000-0000-4000-8000-000000000001")
DEMO_ACTOR = "operator:r1-synthetic-demo"


class SyntheticReviewService:
    """Small deterministic service used only by this local demo."""

    def __init__(self) -> None:
        self.items: dict[UUID, ReviewItem] = {}
        self.reset()

    def reset(self) -> None:
        self.items = {
            DEMO_REVIEW_ID: ReviewItem(
                id=DEMO_REVIEW_ID,
                kind=ReviewItemKind.DEDUP.value,
                status="OPEN",
                summary="Synthetic duplicate candidate requires operator review",
                payload={
                    "source_record_ref": "synthetic-bank:tx-001",
                    "conflict": "EXTERNAL_ID_CONFLICT",
                    "amount_minor": 12345,
                    "currency": "CNY",
                },
                candidate_key="a" * 64,
                audit_event_id=uuid4(),
                created_at=datetime(2026, 8, 25, tzinfo=UTC),
            )
        }

    def list_items(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> tuple[ReviewItem, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("review limit must be between 1 and 500")
        values = tuple(
            item
            for item in self.items.values()
            if (status is None or item.status == status) and (kind is None or item.kind == kind)
        )
        return values[:limit]

    def decide(
        self,
        review_id: UUID,
        *,
        actor: str,
        decision: str,
        reason: str,
        resolution_account_id: UUID | None = None,
    ) -> ReviewItem:
        if not actor.strip() or len(actor) > 200:
            raise ValueError("actor must be between 1 and 200 characters")
        if not reason.strip() or len(reason) > 1_000:
            raise ValueError("reason must be between 1 and 1000 characters")
        if decision not in {"RESOLVED", "REJECTED"}:
            raise ValueError("unsupported Review decision")
        item = self.items.get(review_id)
        if item is None:
            raise ReviewNotFound("review item was not found")
        if item.status != "OPEN":
            raise ReviewConflict("Review item is already terminal")
        if item.kind == ReviewItemKind.SUSPENSE.value and resolution_account_id is None:
            raise ReviewConflict("Suspense resolution requires a target account")
        now = datetime.now(UTC)
        item.status = decision
        item.decided_at = now
        item.decision_actor = actor
        item.decision_reason = reason
        return item


SERVICE = SyntheticReviewService()
SETTINGS = Settings(
    env="test",
    runtime_role="api",
    database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
    api_database_url="postgresql+psycopg://synthetic.invalid/ledgerbridge",
    artifact_root=Path(gettempdir()) / "ledgerbridge-r1-synthetic-review-demo",
    enable_review_api=True,
)

app = FastAPI(
    title="LedgerBridge R1 Synthetic Review Demo",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/v1/reviews", response_model=list[ReviewResponse])
def get_reviews(
    review_status: str | None = None,
    kind: ReviewItemKind | None = None,
) -> list[ReviewResponse]:
    require_review_api(SETTINGS)
    return list_reviews(
        principal=DEMO_ACTOR,
        review_service=SERVICE,  # type: ignore[arg-type]
        review_status=review_status,
        kind=kind,
    )


@app.post("/v1/reviews/{review_id}/decision", response_model=ReviewResponse)
def post_review_decision(review_id: UUID, body: ReviewDecisionRequest) -> ReviewResponse:
    require_review_api(SETTINGS)
    return decide_review(
        review_id=review_id,
        body=body,
        principal=DEMO_ACTOR,
        review_service=SERVICE,  # type: ignore[arg-type]
    )


def run_self_check() -> dict[str, Any]:
    """Exercise list -> decide -> terminal conflict over the demo route."""

    SERVICE.reset()
    with TestClient(app) as client:
        opened = client.get("/v1/reviews", params={"review_status": "OPEN"})
        if opened.status_code != 200 or len(opened.json()) != 1:
            raise RuntimeError(f"synthetic review list failed: {opened.status_code}")
        review_id = opened.json()[0]["id"]
        decided = client.post(
            f"/v1/reviews/{review_id}/decision",
            json={"decision": "RESOLVED", "reason": "confirmed duplicate"},
        )
        if decided.status_code != 200 or decided.json()["status"] != "RESOLVED":
            raise RuntimeError(f"synthetic review decision failed: {decided.status_code}")
        terminal = client.post(
            f"/v1/reviews/{review_id}/decision",
            json={"decision": "REJECTED", "reason": "must fail closed"},
        )
        if terminal.status_code != 409:
            raise RuntimeError(f"synthetic terminal conflict failed: {terminal.status_code}")
    return {
        "mode": "synthetic",
        "review_count": 1,
        "initial_status": "OPEN",
        "final_status": SERVICE.items[DEMO_REVIEW_ID].status,
        "terminal_conflict_status": terminal.status_code,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="run the local review self-check")
    args = parser.parse_args()
    if args.check:
        import json

        print(json.dumps(run_self_check(), sort_keys=True))
        raise SystemExit(0)

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8652, log_level="info")
