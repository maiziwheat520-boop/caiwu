"""Immutable candidate-intent boundary between triage and Core persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ledgerbridge.hermes_message import HermesPrivateMessage
from ledgerbridge.hermes_triage import (
    HermesTriageAction,
    HermesTriageResult,
)


class CandidateIntentError(ValueError):
    """A triage result or evidence binding cannot become a candidate intent."""


@dataclass(frozen=True, slots=True)
class EvidenceBinding:
    evidence_ref: UUID
    entity_ref: UUID
    business_unit_ref: str | None
    sha256: bytes
    media_type: str

    def __post_init__(self) -> None:
        if len(self.sha256) != 32:
            raise CandidateIntentError("evidence sha256 must be exactly 32 bytes")
        if not self.media_type.strip() or len(self.media_type) > 200:
            raise CandidateIntentError("evidence media_type is invalid")
        if self.business_unit_ref is not None and not self.business_unit_ref.strip():
            raise CandidateIntentError("business_unit_ref cannot be blank")


@dataclass(frozen=True, slots=True)
class CandidateIntent:
    candidate_ref: UUID
    source_message_id: str
    source_event_ref: UUID
    entity_ref: UUID
    evidence: tuple[EvidenceBinding, ...]
    triage_reason: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.source_message_id.strip() or len(self.source_message_id) > 300:
            raise CandidateIntentError("source_message_id is invalid")
        if self.created_at.tzinfo is None:
            raise CandidateIntentError("created_at must be timezone-aware")
        if not self.evidence:
            raise CandidateIntentError("candidate intent requires evidence")
        if any(binding.entity_ref != self.entity_ref for binding in self.evidence):
            raise CandidateIntentError("candidate evidence must share the intent entity")
        if not self.triage_reason.strip() or len(self.triage_reason) > 200:
            raise CandidateIntentError("triage_reason is invalid")


def create_candidate_intent(
    message: HermesPrivateMessage,
    triage: HermesTriageResult,
    *,
    candidate_ref: UUID,
    source_event_ref: UUID,
    entity_ref: UUID,
    evidence: tuple[EvidenceBinding, ...],
    created_at: datetime,
) -> CandidateIntent:
    """Create only a reviewable intent from an explicit financial triage result."""

    if triage.action is not HermesTriageAction.CANDIDATE:
        raise CandidateIntentError("only financial triage results may create a candidate intent")
    return CandidateIntent(
        candidate_ref=candidate_ref,
        source_message_id=message.message_id,
        source_event_ref=source_event_ref,
        entity_ref=entity_ref,
        evidence=evidence,
        triage_reason=triage.reason,
        created_at=created_at,
    )
