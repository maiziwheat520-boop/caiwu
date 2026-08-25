"""Replay triage -> candidate-intent creation without database writes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from ledgerbridge.candidate_intent import EvidenceBinding, create_candidate_intent
from ledgerbridge.hermes_message import HermesPrivateMessage, classify_private_message
from ledgerbridge.hermes_triage import (
    SyntheticKeywordHermesTriageClassifier,
    triage_admitted_message,
)

PROFILE = "profile:primary"
ACTIVATED_AT = datetime(2026, 8, 25, tzinfo=UTC)
ENTITY = UUID("10000000-0000-4000-8000-000000000001")


def run_self_check() -> dict[str, object]:
    message = HermesPrivateMessage(
        "msg-candidate",
        PROFILE,
        "primary",
        "private",
        "user",
        ACTIVATED_AT,
        "请处理发票",
    )
    admission = classify_private_message(
        message,
        primary_profile_ref=PROFILE,
        activated_at=ACTIVATED_AT,
    )
    triage = triage_admitted_message(
        message,
        admission,
        classifier=SyntheticKeywordHermesTriageClassifier(),
    )
    intent = create_candidate_intent(
        message,
        triage,
        candidate_ref=UUID("30000000-0000-4000-8000-000000000010"),
        source_event_ref=UUID("40000000-0000-4000-8000-000000000010"),
        entity_ref=ENTITY,
        evidence=(
            EvidenceBinding(
                UUID("20000000-0000-4000-8000-000000000010"),
                ENTITY,
                "unit-demo-a",
                bytes.fromhex("11" * 32),
                "image/jpeg",
            ),
        ),
        created_at=ACTIVATED_AT,
    )
    return {
        "mode": "synthetic",
        "candidate_ref": str(intent.candidate_ref),
        "source_message_id": intent.source_message_id,
        "evidence_count": len(intent.evidence),
        "entity_ref": str(intent.entity_ref),
        "writes_posting": False,
    }


if __name__ == "__main__":
    print(json.dumps(run_self_check(), sort_keys=True))
