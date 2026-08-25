from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from ledgerbridge.candidate_intent import (
    CandidateIntentError,
    EvidenceBinding,
    create_candidate_intent,
)
from ledgerbridge.hermes_message import (
    HermesMessageAttachment,
    HermesMessageDisposition,
    HermesMessageError,
    HermesMessageReason,
    HermesPrivateMessage,
    classify_private_message,
)
from ledgerbridge.hermes_triage import (
    HermesTriageAction,
    HermesTriageLabel,
    HermesTriageResult,
    SyntheticKeywordHermesTriageClassifier,
    UnavailableHermesTriageClassifier,
    triage_admitted_message,
)


def test_hermes_admission_and_triage_are_fail_closed() -> None:
    activated = datetime(2026, 8, 25, tzinfo=UTC)
    message = HermesPrivateMessage(
        "message",
        "primary",
        "primary",
        "private",
        "user",
        activated,
        "invoice please",
        (HermesMessageAttachment("receipt.pdf", "application/pdf", b"pdf"),),
    )
    eligible = classify_private_message(
        message, primary_profile_ref="primary", activated_at=activated
    )
    assert eligible.disposition is HermesMessageDisposition.RETAIN_FOR_TRIAGE
    assert eligible.reason is HermesMessageReason.ELIGIBLE_PRIVATE
    assert (
        triage_admitted_message(
            message,
            eligible,
            classifier=SyntheticKeywordHermesTriageClassifier(),
        ).action
        is HermesTriageAction.CANDIDATE
    )
    assert (
        triage_admitted_message(
            replace(message, text="hello"),
            eligible,
            classifier=SyntheticKeywordHermesTriageClassifier(),
        ).action
        is HermesTriageAction.DELETE_TOMBSTONE
    )
    assert (
        triage_admitted_message(
            message,
            eligible,
            classifier=UnavailableHermesTriageClassifier(),
        ).action
        is HermesTriageAction.AMBIGUOUS_RETAIN
    )
    for altered, reason, disposition in (
        (
            replace(message, profile_kind="family"),
            HermesMessageReason.NON_PRIMARY_PROFILE,
            HermesMessageDisposition.DELETE_TOMBSTONE,
        ),
        (
            replace(message, chat_kind="group"),
            HermesMessageReason.NON_PRIVATE_CHAT,
            HermesMessageDisposition.DELETE_TOMBSTONE,
        ),
        (
            replace(message, sender_kind="assistant"),
            HermesMessageReason.NON_USER_SENDER,
            HermesMessageDisposition.DELETE_TOMBSTONE,
        ),
        (
            replace(message, sent_at=activated.replace(year=2025)),
            HermesMessageReason.BEFORE_ACTIVATION,
            HermesMessageDisposition.IGNORE_HISTORY,
        ),
        (
            replace(message, text="", attachments=()),
            HermesMessageReason.EMPTY_MESSAGE,
            HermesMessageDisposition.DELETE_TOMBSTONE,
        ),
    ):
        decision = classify_private_message(
            altered, primary_profile_ref="primary", activated_at=activated
        )
        assert decision.reason is reason
        assert decision.disposition is disposition
    assert (
        triage_admitted_message(
            message,
            classify_private_message(
                replace(message, text="", attachments=()),
                primary_profile_ref="primary",
                activated_at=activated,
            ),
            classifier=UnavailableHermesTriageClassifier(),
        ).action
        is HermesTriageAction.SKIP
    )
    with pytest.raises(HermesMessageError):
        HermesMessageAttachment("../receipt.pdf", "application/pdf", b"pdf")
    with pytest.raises(HermesMessageError):
        HermesPrivateMessage(
            "message", "primary", "primary", "private", "user", datetime.now(), "x"
        )


def test_candidate_intent_accepts_financial_evidence_and_rejects_unsafe_inputs() -> None:
    entity = UUID("10000000-0000-4000-8000-000000000001")
    message = HermesPrivateMessage(
        message_id="candidate-message",
        profile_ref="primary",
        profile_kind="primary",
        chat_kind="private",
        sender_kind="user",
        sent_at=datetime(2026, 8, 25, tzinfo=UTC),
        text="请处理发票",
    )
    evidence = EvidenceBinding(
        evidence_ref=UUID("20000000-0000-4000-8000-000000000001"),
        entity_ref=entity,
        business_unit_ref=None,
        sha256=b"x" * 32,
        media_type="application/pdf",
    )
    intent = create_candidate_intent(
        message,
        HermesTriageResult(HermesTriageLabel.FINANCIAL, HermesTriageAction.CANDIDATE, "invoice"),
        candidate_ref=UUID("30000000-0000-4000-8000-000000000001"),
        source_event_ref=UUID("40000000-0000-4000-8000-000000000001"),
        entity_ref=entity,
        evidence=(evidence,),
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )
    assert intent.source_message_id == "candidate-message"
    assert intent.evidence == (evidence,)

    with pytest.raises(CandidateIntentError, match="only financial"):
        create_candidate_intent(
            message,
            HermesTriageResult(
                HermesTriageLabel.AMBIGUOUS,
                HermesTriageAction.AMBIGUOUS_RETAIN,
                "uncertain",
            ),
            candidate_ref=intent.candidate_ref,
            source_event_ref=intent.source_event_ref,
            entity_ref=entity,
            evidence=(evidence,),
            created_at=intent.created_at,
        )
    with pytest.raises(CandidateIntentError, match="exactly 32"):
        EvidenceBinding(evidence.evidence_ref, entity, None, b"short", "application/pdf")
    with pytest.raises(CandidateIntentError, match="share the intent entity"):
        create_candidate_intent(
            message,
            HermesTriageResult(
                HermesTriageLabel.FINANCIAL, HermesTriageAction.CANDIDATE, "invoice"
            ),
            candidate_ref=intent.candidate_ref,
            source_event_ref=intent.source_event_ref,
            entity_ref=entity,
            evidence=(
                EvidenceBinding(
                    evidence.evidence_ref,
                    UUID("10000000-0000-4000-8000-000000000002"),
                    None,
                    b"x" * 32,
                    "application/pdf",
                ),
            ),
            created_at=intent.created_at,
        )
