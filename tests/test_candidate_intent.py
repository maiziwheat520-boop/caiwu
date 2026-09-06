"""What may become a candidate intent, and what the boundary refuses.

A candidate intent is the last thing built before Core persistence, so it has
to carry its own evidence, stay inside one entity, and exist only because a
triage result explicitly said the message was financial.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from ledgerbridge.candidate_intent import (
    CandidateIntent,
    CandidateIntentError,
    EvidenceBinding,
    create_candidate_intent,
)
from ledgerbridge.hermes_message import HermesPrivateMessage
from ledgerbridge.hermes_triage import (
    HermesTriageAction,
    HermesTriageLabel,
    HermesTriageResult,
)

CANDIDATE = UUID("30000000-0000-4000-8000-000000000001")
ENTITY = UUID("30000000-0000-4000-8000-000000000002")
OTHER_ENTITY = UUID("30000000-0000-4000-8000-000000000003")
EVIDENCE = UUID("30000000-0000-4000-8000-000000000004")
EVENT = UUID("30000000-0000-4000-8000-000000000005")
SENT = datetime(2026, 8, 25, 1, 0, tzinfo=UTC)


def _binding(**overrides: Any) -> EvidenceBinding:
    fields: dict[str, Any] = {
        "evidence_ref": EVIDENCE,
        "entity_ref": ENTITY,
        "business_unit_ref": "hotel-a",
        "sha256": bytes(32),
        "media_type": "image/png",
    }
    fields.update(overrides)
    return EvidenceBinding(**fields)


def _intent(**overrides: Any) -> CandidateIntent:
    fields: dict[str, Any] = {
        "candidate_ref": CANDIDATE,
        "source_message_id": "m-1",
        "source_event_ref": EVENT,
        "entity_ref": ENTITY,
        "evidence": (_binding(),),
        "triage_reason": "classifier_financial",
        "created_at": SENT,
    }
    fields.update(overrides)
    return CandidateIntent(**fields)


def _message() -> HermesPrivateMessage:
    return HermesPrivateMessage(
        message_id="m-1",
        profile_ref="telegram-8906289598",
        profile_kind="primary",
        chat_kind="private",
        sender_kind="user",
        sent_at=SENT,
        text="这是一张发票",
    )


def _triage(action: HermesTriageAction) -> HermesTriageResult:
    return HermesTriageResult(
        label=HermesTriageLabel.FINANCIAL,
        action=action,
        reason="classifier_financial",
    )


def test_a_financial_triage_result_becomes_a_reviewable_intent() -> None:
    intent = create_candidate_intent(
        _message(),
        _triage(HermesTriageAction.CANDIDATE),
        candidate_ref=CANDIDATE,
        source_event_ref=EVENT,
        entity_ref=ENTITY,
        evidence=(_binding(),),
        created_at=SENT,
    )

    assert intent.candidate_ref == CANDIDATE
    assert intent.source_message_id == "m-1"
    assert intent.source_event_ref == EVENT
    assert intent.triage_reason == "classifier_financial"
    assert intent.evidence[0].evidence_ref == EVIDENCE


@pytest.mark.parametrize(
    "action",
    [
        HermesTriageAction.DELETE_TOMBSTONE,
        HermesTriageAction.AMBIGUOUS_RETAIN,
        HermesTriageAction.SKIP,
    ],
)
def test_only_a_candidate_action_may_create_an_intent(action: HermesTriageAction) -> None:
    with pytest.raises(CandidateIntentError, match="only financial triage results"):
        create_candidate_intent(
            _message(),
            _triage(action),
            candidate_ref=CANDIDATE,
            source_event_ref=EVENT,
            entity_ref=ENTITY,
            evidence=(_binding(),),
            created_at=SENT,
        )


@pytest.mark.parametrize("digest", [b"", bytes(31), bytes(33)])
def test_evidence_that_is_not_a_sha256_digest_is_refused(digest: bytes) -> None:
    with pytest.raises(CandidateIntentError, match="sha256 must be exactly 32 bytes"):
        _binding(sha256=digest)


@pytest.mark.parametrize("media_type", ["", "   ", "a" * 201])
def test_an_evidence_media_type_outside_its_bounds_is_refused(media_type: str) -> None:
    with pytest.raises(CandidateIntentError, match="media_type is invalid"):
        _binding(media_type=media_type)


def test_a_blank_business_unit_ref_is_refused_rather_than_read_as_unassigned() -> None:
    with pytest.raises(CandidateIntentError, match="business_unit_ref cannot be blank"):
        _binding(business_unit_ref="   ")


def test_evidence_may_be_unassigned_but_only_explicitly() -> None:
    assert _binding(business_unit_ref=None).business_unit_ref is None


@pytest.mark.parametrize("message_id", ["", "  ", "a" * 301])
def test_a_source_message_id_outside_its_bounds_is_refused(message_id: str) -> None:
    with pytest.raises(CandidateIntentError, match="source_message_id is invalid"):
        _intent(source_message_id=message_id)


def test_a_naive_creation_instant_is_refused() -> None:
    with pytest.raises(CandidateIntentError, match="created_at must be timezone-aware"):
        _intent(created_at=datetime(2026, 8, 25, 1, 0))


def test_an_intent_without_evidence_is_refused() -> None:
    with pytest.raises(CandidateIntentError, match="requires evidence"):
        _intent(evidence=())


def test_evidence_from_another_entity_is_refused() -> None:
    with pytest.raises(CandidateIntentError, match="must share the intent entity"):
        _intent(evidence=(_binding(), _binding(entity_ref=OTHER_ENTITY)))


@pytest.mark.parametrize("reason", ["", "  ", "a" * 201])
def test_a_triage_reason_outside_its_bounds_is_refused(reason: str) -> None:
    with pytest.raises(CandidateIntentError, match="triage_reason is invalid"):
        _intent(triage_reason=reason)
