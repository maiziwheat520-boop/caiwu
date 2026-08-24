"""Replay the fail-closed synthetic Hermes triage boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ledgerbridge.hermes_message import HermesPrivateMessage, classify_private_message
from ledgerbridge.hermes_triage import (
    SyntheticKeywordHermesTriageClassifier,
    UnavailableHermesTriageClassifier,
    triage_admitted_message,
)

PRIMARY_PROFILE = "profile:primary"
ACTIVATED_AT = datetime(2026, 8, 25, tzinfo=UTC)


def run_self_check() -> dict[str, object]:
    admitted = HermesPrivateMessage(
        "msg-triage",
        PRIMARY_PROFILE,
        "primary",
        "private",
        "user",
        ACTIVATED_AT,
        "请处理这张发票",
    )
    admission = classify_private_message(
        admitted,
        primary_profile_ref=PRIMARY_PROFILE,
        activated_at=ACTIVATED_AT,
    )
    fixture = triage_admitted_message(
        admitted,
        admission,
        classifier=SyntheticKeywordHermesTriageClassifier(),
    )
    unavailable = triage_admitted_message(
        admitted,
        admission,
        classifier=UnavailableHermesTriageClassifier(),
    )
    return {
        "mode": "synthetic",
        "fixture_action": fixture.action,
        "fixture_label": fixture.label,
        "unavailable_action": unavailable.action,
        "unavailable_label": unavailable.label,
    }


if __name__ == "__main__":
    print(json.dumps(run_self_check(), ensure_ascii=False, sort_keys=True))
