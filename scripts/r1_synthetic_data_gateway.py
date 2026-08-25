"""Loopback-only JSON data gateway for the R1 synthetic workflow.

This launcher is deliberately process-local: it accepts one bounded message
and its evidence bytes, runs the reviewed Hermes admission/triage and candidate
intent boundary, and exposes the resulting candidate projection. It never
writes PostgreSQL, an artifact store, a JournalEntry, or a Posting.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from ledgerbridge.candidate_intent import EvidenceBinding, create_candidate_intent
from ledgerbridge.hermes_message import HermesPrivateMessage, classify_private_message
from ledgerbridge.hermes_triage import (
    HermesTriageAction,
    SyntheticKeywordHermesTriageClassifier,
    triage_admitted_message,
)

MAX_EVIDENCE_BYTES = 1_048_576
PRIMARY_PROFILE = "profile:primary"
ACTIVATED_AT = datetime(2026, 8, 25, tzinfo=UTC)


class IntakeEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_ref: UUID
    media_type: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1, max_length=2_000_000)
    business_unit_ref: str | None = Field(default=None, max_length=200)


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=300)
    source_event_ref: UUID
    profile_ref: str = Field(default=PRIMARY_PROFILE, min_length=1, max_length=200)
    profile_kind: str = "primary"
    chat_kind: str = "private"
    sender_kind: str = "user"
    sent_at: datetime = ACTIVATED_AT
    text: str = Field(default="", max_length=1_000_000)
    entity_ref: UUID
    evidence: tuple[IntakeEvidence, ...] = Field(min_length=1, max_length=32)


class EvidenceOutput(BaseModel):
    evidence_ref: UUID
    media_type: str
    sha256: str
    size_bytes: int
    business_unit_ref: str | None


class IntakeOutput(BaseModel):
    mode: str = "synthetic"
    disposition: str
    triage_label: str
    triage_action: str
    triage_reason: str
    candidate_ref: UUID | None = None
    source_message_id: str
    source_event_ref: UUID
    entity_ref: UUID
    evidence: tuple[EvidenceOutput, ...]
    writes_posting: bool = False


STORE: dict[UUID, IntakeOutput] = {}
app = FastAPI(
    title="LedgerBridge R1 Synthetic Data Gateway",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _decode_evidence(item: IntakeEvidence) -> tuple[bytes, str]:
    try:
        content = base64.b64decode(item.content_base64, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid evidence encoding"
        ) from exc
    if not content or len(content) > MAX_EVIDENCE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "evidence exceeds demo limit")
    return content, hashlib.sha256(content).hexdigest()


@app.post("/v1/intake", response_model=IntakeOutput, status_code=status.HTTP_201_CREATED)
def intake(request: IntakeRequest) -> IntakeOutput:
    try:
        message = HermesPrivateMessage(
            request.message_id,
            request.profile_ref,
            request.profile_kind,  # type: ignore[arg-type]
            request.chat_kind,  # type: ignore[arg-type]
            request.sender_kind,  # type: ignore[arg-type]
            request.sent_at,
            request.text,
        )
        admission = classify_private_message(
            message,
            primary_profile_ref=PRIMARY_PROFILE,
            activated_at=ACTIVATED_AT,
        )
        triage = triage_admitted_message(
            message,
            admission,
            classifier=SyntheticKeywordHermesTriageClassifier(),
        )
        decoded: list[tuple[IntakeEvidence, bytes, str]] = [
            (item, *_decode_evidence(item)) for item in request.evidence
        ]
        bindings = tuple(
            EvidenceBinding(
                item.evidence_ref,
                request.entity_ref,
                item.business_unit_ref,
                bytes.fromhex(digest),
                item.media_type,
            )
            for item, _, digest in decoded
        )
        output_evidence = tuple(
            EvidenceOutput(
                evidence_ref=item.evidence_ref,
                media_type=item.media_type,
                sha256=digest,
                size_bytes=len(content),
                business_unit_ref=item.business_unit_ref,
            )
            for item, content, digest in decoded
        )
        candidate_ref: UUID | None = None
        if triage.action is HermesTriageAction.CANDIDATE:
            candidate = create_candidate_intent(
                message,
                triage,
                candidate_ref=uuid4(),
                source_event_ref=request.source_event_ref,
                entity_ref=request.entity_ref,
                evidence=bindings,
                created_at=request.sent_at,
            )
            candidate_ref = candidate.candidate_ref
        output = IntakeOutput(
            disposition=admission.disposition.value,
            triage_label=triage.label.value,
            triage_action=triage.action.value,
            triage_reason=triage.reason,
            candidate_ref=candidate_ref,
            source_message_id=message.message_id,
            source_event_ref=request.source_event_ref,
            entity_ref=request.entity_ref,
            evidence=output_evidence,
        )
    except HTTPException:
        raise
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if candidate_ref is not None:
        STORE[candidate_ref] = output
    return output


@app.get("/v1/candidates", response_model=list[IntakeOutput])
def candidates() -> list[IntakeOutput]:
    return list(STORE.values())


def run_self_check() -> dict[str, Any]:
    STORE.clear()
    payload = {
        "message_id": "gateway-demo-message",
        "source_event_ref": "40000000-0000-4000-8000-000000000099",
        "entity_ref": "10000000-0000-4000-8000-000000000001",
        "text": "请处理发票",
        "evidence": [
            {
                "evidence_ref": "20000000-0000-4000-8000-000000000099",
                "media_type": "text/plain",
                "content_base64": base64.b64encode(b"synthetic invoice").decode(),
                "business_unit_ref": "unit-demo-a",
            }
        ],
    }
    with TestClient(app) as client:
        created = client.post("/v1/intake", json=payload)
        listed = client.get("/v1/candidates")
    if created.status_code != status.HTTP_201_CREATED or listed.status_code != status.HTTP_200_OK:
        raise RuntimeError(f"synthetic gateway failed: {created.status_code}/{listed.status_code}")
    result = created.json()
    if result["triage_action"] != HermesTriageAction.CANDIDATE.value or result["writes_posting"]:
        raise RuntimeError("synthetic gateway did not preserve the posting boundary")
    return {
        "mode": "synthetic",
        "created_status": created.status_code,
        "candidate_count": len(listed.json()),
        "triage_action": result["triage_action"],
        "evidence_count": len(result["evidence"]),
        "writes_posting": result["writes_posting"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="run the local gateway self-check")
    args = parser.parse_args()
    if args.check:
        print(json.dumps(run_self_check(), sort_keys=True))
        raise SystemExit(0)
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8653, log_level="info")
