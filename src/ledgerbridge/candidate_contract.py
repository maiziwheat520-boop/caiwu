"""Versioned, side-effect-free candidate contract for the R0 synthetic slice.

This module deliberately has no database, HTTP, artifact, or connector wiring.  It
freezes the anti-corruption projection and candidate state graph that later R1/D1
slices must persist and expose.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = "ledgerbridge.candidate.v1"
STATE_GRAPH_VERSION = "ledgerbridge.candidate-state.v1"
JSON_SAFE_INTEGER = 9_007_199_254_740_991
_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")

MoneyMinor = Annotated[int, Field(strict=True, ge=-JSON_SAFE_INTEGER, le=JSON_SAFE_INTEGER)]


class CandidateStatus(StrEnum):
    INCOMPLETE = "INCOMPLETE"
    CONFLICTED = "CONFLICTED"
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    IGNORED = "IGNORED"
    SUPERSEDED = "SUPERSEDED"


class CandidateAction(StrEnum):
    COMPLETE_FIELDS = "COMPLETE_FIELDS"
    RESOLVE_CONFLICT = "RESOLVE_CONFLICT"
    CONFIRM = "CONFIRM"
    IGNORE = "IGNORE"
    SUPERSEDE = "SUPERSEDE"


class BlockerCode(StrEnum):
    MISSING_BUSINESS_UNIT = "MISSING_BUSINESS_UNIT"
    MISSING_CATEGORY = "MISSING_CATEGORY"
    MISSING_AMOUNT = "MISSING_AMOUNT"
    MISSING_ACCOUNTING_MONTH = "MISSING_ACCOUNTING_MONTH"
    AMBIGUOUS_EXTRACTION = "AMBIGUOUS_EXTRACTION"
    PARSE_FAILED = "PARSE_FAILED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    UNSUPPORTED_ATTACHMENT = "UNSUPPORTED_ATTACHMENT"
    DUPLICATE_MESSAGE = "DUPLICATE_MESSAGE"
    DUPLICATE_ATTACHMENT = "DUPLICATE_ATTACHMENT"
    BUSINESS_KEY_CONFLICT = "BUSINESS_KEY_CONFLICT"
    CROSS_FORMAT_DUPLICATE = "CROSS_FORMAT_DUPLICATE"
    ACCOUNT_UNREGISTERED = "ACCOUNT_UNREGISTERED"
    COUNTERPARTY_STATEMENT_REQUIRED = "COUNTERPARTY_STATEMENT_REQUIRED"


class ReviewRiskCode(StrEnum):
    RELATED_ACCOUNT_STATEMENT_REQUIRED = "RELATED_ACCOUNT_STATEMENT_REQUIRED"
    HOTEL_PAYOUT_STATEMENT_REQUIRED = "HOTEL_PAYOUT_STATEMENT_REQUIRED"
    TRANSFER_REVIEW_REQUIRED = "TRANSFER_REVIEW_REQUIRED"
    REVERSAL_MATCH_REQUIRED = "REVERSAL_MATCH_REQUIRED"
    UNSETTLED_TRANSACTION = "UNSETTLED_TRANSACTION"


class EvidenceKind(StrEnum):
    MESSAGE_ENVELOPE = "MESSAGE_ENVELOPE"
    MAIL_ENVELOPE = "MAIL_ENVELOPE"
    ATTACHMENT = "ATTACHMENT"


class IngestChannel(StrEnum):
    HERMES = "HERMES"
    OUTLOOK = "OUTLOOK"
    CONTROLLED_UPLOAD = "CONTROLLED_UPLOAD"
    SYNTHETIC = "SYNTHETIC"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceProjection(_FrozenModel):
    ingest_channel: IngestChannel
    source_system: str = Field(min_length=1, max_length=100)
    source_event_ref: UUID
    display_label: str = Field(min_length=1, max_length=100)


class EvidenceReference(_FrozenModel):
    evidence_ref: UUID
    kind: EvidenceKind
    media_type: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)
    download_available: bool

    @model_validator(mode="after")
    def safe_display_name(self) -> EvidenceReference:
        if self.display_name is not None and any(
            char in self.display_name for char in ("/", "\\", "\r", "\n", "\x00")
        ):
            raise ValueError("display_name must be a sanitized basename")
        return self


class Blocker(_FrozenModel):
    code: BlockerCode
    message: str = Field(min_length=1, max_length=300)
    field: Literal["business_unit", "category", "amount_minor", "accounting_month"] | None = None
    conflict_ref: UUID | None = None
    evidence_ref: UUID | None = None


class ReviewRisk(_FrozenModel):
    code: ReviewRiskCode
    message: str = Field(min_length=1, max_length=300)


class ReviewSummary(_FrozenModel):
    event_count: int = Field(ge=0)
    last_action: CandidateAction | None = None
    last_decided_at: datetime | None = None
    current_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def decision_time_is_aware(self) -> ReviewSummary:
        if self.last_decided_at is not None and self.last_decided_at.tzinfo is None:
            raise ValueError("review decision timestamp must be timezone-aware")
        if self.event_count == 0 and (
            self.last_action is not None or self.last_decided_at is not None
        ):
            raise ValueError("empty review summary cannot have a last decision")
        if self.event_count > 0 and (self.last_action is None or self.last_decided_at is None):
            raise ValueError("non-empty review summary requires a last decision")
        return self


class CandidateProjection(_FrozenModel):
    contract_version: Literal["ledgerbridge.candidate.v1"] = "ledgerbridge.candidate.v1"
    candidate_ref: UUID
    short_id: str = Field(pattern=r"^C-[A-Z0-9]{4,8}$")
    revision: int = Field(ge=1)
    status: CandidateStatus
    entity_ref: UUID
    business_unit_ref: str | None = Field(default=None, min_length=1, max_length=100)
    business_unit_label: str | None = Field(default=None, min_length=1, max_length=200)
    category_code: str | None = Field(default=None, min_length=1, max_length=100)
    category_label: str | None = Field(default=None, min_length=1, max_length=200)
    amount_minor: MoneyMinor | None = None
    currency: Literal["CNY"] = "CNY"
    accounting_month: str | None = None
    summary: str = Field(min_length=1, max_length=500)
    confidence_basis_points: int = Field(ge=0, le=10_000)
    source: SourceProjection
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)
    blockers: tuple[Blocker, ...] = ()
    review_risks: tuple[ReviewRisk, ...] = ()
    review_summary: ReviewSummary
    created_at: datetime
    updated_at: datetime
    supersedes_candidate_ref: UUID | None = None
    superseded_by_candidate_ref: UUID | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> CandidateProjection:
        if self.accounting_month is not None and _MONTH.fullmatch(self.accounting_month) is None:
            raise ValueError("accounting_month must use YYYY-MM")
        if self.review_summary.current_revision != self.revision:
            raise ValueError("review summary revision must match candidate revision")
        if self.review_summary.event_count != self.revision - 1:
            raise ValueError("candidate revision must equal review event count plus one")
        last_action = self.review_summary.last_action
        if self.status in {CandidateStatus.INCOMPLETE, CandidateStatus.CONFLICTED} and (
            self.revision != 1 or last_action is not None
        ):
            raise ValueError("INCOMPLETE and CONFLICTED are initial candidate states")
        if self.status == CandidateStatus.PENDING:
            if self.revision == 1 and last_action is not None:
                raise ValueError("initial PENDING candidate cannot have a last action")
            if self.revision > 1 and last_action not in {
                CandidateAction.COMPLETE_FIELDS,
                CandidateAction.RESOLVE_CONFLICT,
            }:
                raise ValueError("derived PENDING state requires completion or conflict resolution")
        terminal_actions = {
            CandidateStatus.CONFIRMED: CandidateAction.CONFIRM,
            CandidateStatus.IGNORED: CandidateAction.IGNORE,
            CandidateStatus.SUPERSEDED: CandidateAction.SUPERSEDE,
        }
        if self.status in terminal_actions and last_action != terminal_actions[self.status]:
            raise ValueError("terminal candidate status must match its last action")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("candidate timestamps must be timezone-aware")
        if self.created_at > self.updated_at:
            raise ValueError("candidate timestamps cannot move backward")
        if self.review_summary.event_count == 0 and self.created_at != self.updated_at:
            raise ValueError("initial candidate timestamps must match")
        if (
            self.review_summary.last_decided_at is not None
            and self.review_summary.last_decided_at != self.updated_at
        ):
            raise ValueError("candidate updated_at must equal its last decision time")
        if self.business_unit_ref is None and self.business_unit_label is not None:
            raise ValueError("business unit label requires a reference")
        if self.business_unit_ref is not None and self.business_unit_label is None:
            raise ValueError("business unit reference requires a label")
        if self.category_code is None and self.category_label is not None:
            raise ValueError("category label requires a code")
        if self.category_code is not None and self.category_label is None:
            raise ValueError("category code requires a label")

        missing = _missing_fields(self)
        missing_codes = {
            blocker.code for blocker in self.blockers if blocker.code in _MISSING_CODES
        }
        expected_missing_codes = {_MISSING_CODE_BY_FIELD[field] for field in missing}
        conflict_blockers = [
            blocker for blocker in self.blockers if blocker.code in _CONFLICT_CODES
        ]
        incomplete_blockers = [
            blocker for blocker in self.blockers if blocker.code in _INCOMPLETE_CODES
        ]

        if self.status == CandidateStatus.INCOMPLETE:
            if (
                not missing
                or not self.blockers
                or missing_codes != expected_missing_codes
                or len(incomplete_blockers) != len(self.blockers)
            ):
                raise ValueError(
                    "INCOMPLETE must describe missing fields and only incomplete blockers"
                )
        elif self.status == CandidateStatus.CONFLICTED:
            if (
                missing
                or not conflict_blockers
                or len(conflict_blockers) != len(self.blockers)
                or any(b.conflict_ref is None for b in conflict_blockers)
            ):
                raise ValueError(
                    "CONFLICTED must be complete and contain only opaque conflict blockers"
                )
        elif self.status in {CandidateStatus.PENDING, CandidateStatus.CONFIRMED}:
            if missing or self.blockers:
                raise ValueError(
                    "reviewable and confirmed candidates must be complete and unblocked"
                )
        elif self.status == CandidateStatus.SUPERSEDED and (
            missing or self.blockers or self.superseded_by_candidate_ref is None
        ):
            raise ValueError("SUPERSEDED must link to its derived replacement")

        if (
            self.status != CandidateStatus.SUPERSEDED
            and self.superseded_by_candidate_ref is not None
        ):
            raise ValueError("only SUPERSEDED candidates may link to a replacement")
        return self


class CandidatePatch(_FrozenModel):
    business_unit_ref: str | None = Field(default=None, min_length=1, max_length=100)
    business_unit_label: str | None = Field(default=None, min_length=1, max_length=200)
    category_code: str | None = Field(default=None, min_length=1, max_length=100)
    category_label: str | None = Field(default=None, min_length=1, max_length=200)
    amount_minor: MoneyMinor | None = None
    accounting_month: str | None = None

    @model_validator(mode="after")
    def patch_is_coherent(self) -> CandidatePatch:
        supplied = self.model_fields_set
        if not supplied:
            raise ValueError("candidate patch must change at least one field")
        if ("business_unit_ref" in supplied) != ("business_unit_label" in supplied):
            raise ValueError("business unit reference and label must be changed together")
        if ("category_code" in supplied) != ("category_label" in supplied):
            raise ValueError("category code and label must be changed together")
        if self.accounting_month is not None and _MONTH.fullmatch(self.accounting_month) is None:
            raise ValueError("accounting_month must use YYYY-MM")
        return self


class CandidateCommand(_FrozenModel):
    operation_id: UUID
    action: CandidateAction
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1_000)
    patch: CandidatePatch | None = None
    conflict_resolutions: dict[UUID, str] = Field(default_factory=dict)
    derived_candidate_ref: UUID | None = None
    derived_short_id: str | None = Field(default=None, pattern=r"^C-[A-Z0-9]{4,8}$")
    decided_at: datetime

    @model_validator(mode="after")
    def command_shape(self) -> CandidateCommand:
        if self.decided_at.tzinfo is None:
            raise ValueError("decided_at must be timezone-aware")
        if self.action in {CandidateAction.COMPLETE_FIELDS, CandidateAction.SUPERSEDE}:
            if self.patch is None:
                raise ValueError(f"{self.action} requires a patch")
        elif self.action != CandidateAction.RESOLVE_CONFLICT and self.patch is not None:
            raise ValueError(f"{self.action} does not accept a patch")
        if self.action == CandidateAction.RESOLVE_CONFLICT:
            if not self.conflict_resolutions:
                raise ValueError("RESOLVE_CONFLICT requires resolutions")
        elif self.conflict_resolutions:
            raise ValueError(f"{self.action} does not accept conflict resolutions")
        if self.action == CandidateAction.SUPERSEDE:
            if self.derived_candidate_ref is None or self.derived_short_id is None:
                raise ValueError("SUPERSEDE requires a derived candidate identity")
        elif self.derived_candidate_ref is not None or self.derived_short_id is not None:
            raise ValueError("derived candidate identity is only valid for SUPERSEDE")
        return self


class FieldChange(_FrozenModel):
    field: Literal[
        "business_unit_ref",
        "business_unit_label",
        "category_code",
        "category_label",
        "amount_minor",
        "accounting_month",
        "status",
    ]
    previous_value: str | MoneyMinor | None
    new_value: str | MoneyMinor | None

    @model_validator(mode="after")
    def value_really_changed(self) -> FieldChange:
        if self.previous_value == self.new_value:
            raise ValueError("audit field change must contain different values")
        return self


class ResolvedConflict(_FrozenModel):
    conflict_ref: UUID
    resolution: str = Field(min_length=1, max_length=1_000)


class CandidateEvent(_FrozenModel):
    operation_id: UUID
    command_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_ref: UUID
    action: CandidateAction
    from_revision: int = Field(ge=1)
    to_revision: int = Field(ge=2)
    from_status: CandidateStatus
    to_status: CandidateStatus
    changes: tuple[FieldChange, ...] = Field(min_length=1)
    resolved_conflicts: tuple[ResolvedConflict, ...] = ()
    reason: str
    actor_ref: str = Field(min_length=1, max_length=200)
    created_at: datetime
    derived_candidate_ref: UUID | None = None
    prior_projection: CandidateProjection
    result_projection: CandidateProjection
    result_derived_candidate: CandidateProjection | None = None

    @model_validator(mode="after")
    def legal_state_edge(self) -> CandidateEvent:
        if self.created_at.tzinfo is None:
            raise ValueError("candidate event timestamp must be timezone-aware")
        if self.to_revision != self.from_revision + 1:
            raise ValueError("candidate event must advance exactly one revision")
        legal = {
            CandidateAction.COMPLETE_FIELDS: {
                (CandidateStatus.INCOMPLETE, CandidateStatus.PENDING)
            },
            CandidateAction.RESOLVE_CONFLICT: {
                (CandidateStatus.CONFLICTED, CandidateStatus.PENDING)
            },
            CandidateAction.CONFIRM: {(CandidateStatus.PENDING, CandidateStatus.CONFIRMED)},
            CandidateAction.IGNORE: {
                (CandidateStatus.INCOMPLETE, CandidateStatus.IGNORED),
                (CandidateStatus.CONFLICTED, CandidateStatus.IGNORED),
                (CandidateStatus.PENDING, CandidateStatus.IGNORED),
            },
            CandidateAction.SUPERSEDE: {(CandidateStatus.CONFIRMED, CandidateStatus.SUPERSEDED)},
        }
        if (self.from_status, self.to_status) not in legal[self.action]:
            raise ValueError("candidate event action does not match its state edge")
        if (self.action == CandidateAction.SUPERSEDE) != (self.derived_candidate_ref is not None):
            raise ValueError("only SUPERSEDE events may link a derived candidate")
        if (self.action == CandidateAction.SUPERSEDE) != (
            self.result_derived_candidate is not None
        ):
            raise ValueError("only SUPERSEDE receipts may contain a derived candidate snapshot")
        if (self.action == CandidateAction.RESOLVE_CONFLICT) != bool(self.resolved_conflicts):
            raise ValueError("only RESOLVE_CONFLICT events may record conflict resolutions")
        if len({item.conflict_ref for item in self.resolved_conflicts}) != len(
            self.resolved_conflicts
        ):
            raise ValueError("resolved conflict refs must be unique")
        changed_fields = [change.field for change in self.changes]
        if "status" not in changed_fields or len(set(changed_fields)) != len(changed_fields):
            raise ValueError("candidate event changes must uniquely include status")
        status_change = next(change for change in self.changes if change.field == "status")
        if (
            status_change.previous_value != self.from_status.value
            or status_change.new_value != self.to_status.value
        ):
            raise ValueError("status audit change must match the event state edge")
        normalized_changes = [field for field in changed_fields if field != "status"]
        if self.action in {CandidateAction.CONFIRM, CandidateAction.IGNORE} and normalized_changes:
            raise ValueError("decision-only events cannot change normalized fields")
        if (
            self.action
            in {
                CandidateAction.COMPLETE_FIELDS,
                CandidateAction.SUPERSEDE,
            }
            and not normalized_changes
        ):
            raise ValueError("candidate action requires a normalized field change")
        if self.action == CandidateAction.COMPLETE_FIELDS and any(
            change.previous_value is not None for change in self.changes if change.field != "status"
        ):
            raise ValueError("COMPLETE_FIELDS may only fill previously missing values")
        if (
            self.prior_projection.candidate_ref != self.candidate_ref
            or self.prior_projection.revision != self.from_revision
            or self.prior_projection.status != self.from_status
            or self.prior_projection.updated_at > self.created_at
        ):
            raise ValueError("event prior projection must match its state edge")
        if (
            self.result_projection.candidate_ref != self.candidate_ref
            or self.result_projection.revision != self.to_revision
            or self.result_projection.status != self.to_status
            or self.result_projection.updated_at != self.created_at
            or self.result_projection.review_summary.last_action != self.action
        ):
            raise ValueError("event receipt projection must match its state edge")
        if self.action == CandidateAction.SUPERSEDE:
            changed_projection = self.result_derived_candidate
            if (
                changed_projection is None
                or changed_projection.candidate_ref != self.derived_candidate_ref
            ):
                raise ValueError("supersede receipt must match its derived candidate ref")
        else:
            changed_projection = self.result_projection
        for change in self.changes:
            expected_new = (
                self.to_status.value
                if change.field == "status"
                else _audit_value(getattr(changed_projection, change.field))
            )
            if change.new_value != expected_new:
                raise ValueError("event receipt must match every audited new value")
            expected_previous = (
                self.from_status.value
                if change.field == "status"
                else _audit_value(getattr(self.prior_projection, change.field))
            )
            if change.previous_value != expected_previous:
                raise ValueError("event prior projection must match every audited old value")
        changed_set = set(changed_fields)
        for field in _AUDITED_NORMALIZED_FIELDS:
            prior_value = getattr(self.prior_projection, field)
            if self.action == CandidateAction.SUPERSEDE:
                if getattr(self.result_projection, field) != prior_value:
                    raise ValueError("supersede cannot overwrite its source projection")
            elif field not in changed_set and getattr(self.result_projection, field) != prior_value:
                raise ValueError("unaudited normalized fields cannot change")
        for field in _IMMUTABLE_PROJECTION_FIELDS:
            if getattr(self.result_projection, field) != getattr(self.prior_projection, field):
                raise ValueError("candidate event changed an immutable projection field")
        if self.action in {
            CandidateAction.COMPLETE_FIELDS,
            CandidateAction.RESOLVE_CONFLICT,
        }:
            if self.result_projection.blockers:
                raise ValueError("completion and conflict resolution must clear blockers")
        elif self.result_projection.blockers != self.prior_projection.blockers:
            raise ValueError("candidate event changed blockers without an allowed action")
        if self.action == CandidateAction.SUPERSEDE:
            if self.result_projection.superseded_by_candidate_ref != self.derived_candidate_ref:
                raise ValueError("supersede receipt must link its replacement")
            if changed_projection is None:  # defensive type narrowing
                raise ValueError("supersede receipt is missing its replacement")
            for field in _DERIVED_IMMUTABLE_FIELDS:
                if getattr(changed_projection, field) != getattr(self.prior_projection, field):
                    raise ValueError("derived candidate changed an immutable source field")
        elif (
            self.result_projection.superseded_by_candidate_ref
            != self.prior_projection.superseded_by_candidate_ref
        ):
            raise ValueError("non-supersede event changed the replacement link")
        return self


class CandidateAggregate(_FrozenModel):
    projection: CandidateProjection
    events: tuple[CandidateEvent, ...] = ()
    derived_candidates: tuple[CandidateProjection, ...] = ()

    @model_validator(mode="after")
    def complete_append_only_history(self) -> CandidateAggregate:
        if self.projection.revision != len(self.events) + 1:
            raise ValueError("aggregate must include complete event history from revision 1")
        if self.projection.review_summary.event_count != len(self.events):
            raise ValueError("review summary event count must match aggregate history")
        if not self.events:
            if self.projection.review_summary.last_action is not None:
                raise ValueError("initial aggregate cannot have a last action")
        else:
            expected_revision = 1
            expected_status = self.events[0].from_status
            if expected_status not in {
                CandidateStatus.INCOMPLETE,
                CandidateStatus.CONFLICTED,
                CandidateStatus.PENDING,
            }:
                raise ValueError("event history must start from an open candidate state")
            operation_ids: set[UUID] = set()
            previous_time = self.projection.created_at
            previous_result: CandidateProjection | None = None
            latest_values: dict[str, str | int | None] = {}
            for event in self.events:
                if (
                    event.candidate_ref != self.projection.candidate_ref
                    or event.operation_id in operation_ids
                    or event.from_revision != expected_revision
                    or event.to_revision != expected_revision + 1
                    or event.from_status != expected_status
                ):
                    raise ValueError("candidate events must form one unique contiguous chain")
                if previous_result is None:
                    if event.prior_projection.created_at != self.projection.created_at:
                        raise ValueError("first event must retain the candidate creation time")
                elif event.prior_projection != previous_result:
                    raise ValueError("event receipt projections must form a contiguous chain")
                if event.created_at < previous_time:
                    raise ValueError("candidate event timestamps must be monotonic")
                operation_ids.add(event.operation_id)
                derived_for_event = next(
                    (
                        candidate
                        for candidate in self.derived_candidates
                        if candidate.candidate_ref == event.derived_candidate_ref
                    ),
                    None,
                )
                if derived_for_event != event.result_derived_candidate:
                    raise ValueError("aggregate derived candidate must match the event receipt")
                for change in event.changes:
                    if change.field == "status":
                        continue
                    if event.action == CandidateAction.SUPERSEDE:
                        if (
                            derived_for_event is None
                            or change.previous_value
                            != _audit_value(getattr(self.projection, change.field))
                            or change.new_value
                            != _audit_value(getattr(derived_for_event, change.field))
                        ):
                            raise ValueError(
                                "supersede audit changes must match the derived candidate"
                            )
                        continue
                    prior_value = latest_values.get(change.field, change.previous_value)
                    if change.previous_value != prior_value:
                        raise ValueError("audit field changes must form a contiguous value chain")
                    latest_values[change.field] = change.new_value
                expected_revision = event.to_revision
                expected_status = event.to_status
                previous_time = event.created_at
                previous_result = event.result_projection
            for field, value in latest_values.items():
                if _audit_value(getattr(self.projection, field)) != value:
                    raise ValueError("candidate projection must match the audit field changes")
            last = self.events[-1]
            if (
                expected_revision != self.projection.revision
                or expected_status != self.projection.status
                or self.projection.review_summary.last_action != last.action
                or self.projection.review_summary.last_decided_at != last.created_at
                or self.projection != last.result_projection
            ):
                raise ValueError("candidate projection must equal the event chain tip")

        event_derived_refs = {
            event.derived_candidate_ref
            for event in self.events
            if event.derived_candidate_ref is not None
        }
        candidate_derived_refs = {candidate.candidate_ref for candidate in self.derived_candidates}
        candidate_short_ids = {candidate.short_id for candidate in self.derived_candidates}
        if (
            event_derived_refs != candidate_derived_refs
            or len(candidate_derived_refs) != len(self.derived_candidates)
            or len(candidate_short_ids) != len(self.derived_candidates)
            or any(
                candidate.supersedes_candidate_ref != self.projection.candidate_ref
                or candidate.candidate_ref == self.projection.candidate_ref
                or candidate.short_id == self.projection.short_id
                or candidate.status != CandidateStatus.PENDING
                or candidate.revision != 1
                or candidate.review_summary.event_count != 0
                or candidate.review_summary.last_action is not None
                or candidate.review_summary.last_decided_at is not None
                or candidate.superseded_by_candidate_ref is not None
                for candidate in self.derived_candidates
            )
        ):
            raise ValueError("derived candidates must match unique supersede events")
        return self


class TransitionOutcome(_FrozenModel):
    aggregate: CandidateAggregate
    replayed: bool = False
    derived_candidate: CandidateProjection | None = None


class CandidateContractError(RuntimeError):
    """Base failure for a rejected R0 state transition."""


class CandidateRevisionConflict(CandidateContractError):
    """The command targets a stale candidate revision."""


class CandidateTransitionRejected(CandidateContractError):
    """The requested action is invalid for the current state."""


class CandidateIdempotencyConflict(CandidateContractError):
    """An operation ID was replayed with different command content."""


def create_candidate_aggregate(projection: CandidateProjection) -> CandidateAggregate:
    """Validate the worker-owned creation boundary for a brand-new candidate."""

    if projection.status not in {
        CandidateStatus.INCOMPLETE,
        CandidateStatus.CONFLICTED,
        CandidateStatus.PENDING,
    }:
        raise CandidateTransitionRejected("new candidates must start in an open state")
    if (
        projection.revision != 1
        or projection.review_summary.event_count != 0
        or projection.review_summary.last_action is not None
        or projection.review_summary.last_decided_at is not None
        or projection.supersedes_candidate_ref is not None
        or projection.superseded_by_candidate_ref is not None
    ):
        raise CandidateTransitionRejected("new candidate metadata is not an initial projection")
    return CandidateAggregate(projection=projection)


def apply_candidate_command(
    aggregate: CandidateAggregate,
    command: CandidateCommand,
    *,
    actor_ref: str,
) -> TransitionOutcome:
    """Apply one deterministic append-only command to a synthetic aggregate."""

    if not actor_ref or len(actor_ref) > 200:
        raise CandidateTransitionRejected("trusted actor reference is invalid")
    fingerprint = _command_fingerprint(command)
    prior_index = next(
        (
            index
            for index, event in enumerate(aggregate.events)
            if event.operation_id == command.operation_id
        ),
        None,
    )
    if prior_index is not None:
        prior = aggregate.events[prior_index]
        if prior.command_fingerprint != fingerprint or prior.actor_ref != actor_ref:
            raise CandidateIdempotencyConflict(
                "operation ID was reused with different content or actor"
            )
        historical_events = aggregate.events[: prior_index + 1]
        historical_derived_refs = {
            event.derived_candidate_ref
            for event in historical_events
            if event.derived_candidate_ref is not None
        }
        historical_derived = tuple(
            candidate
            for candidate in aggregate.derived_candidates
            if candidate.candidate_ref in historical_derived_refs
        )
        historical_aggregate = CandidateAggregate(
            projection=prior.result_projection,
            events=historical_events,
            derived_candidates=historical_derived,
        )
        return TransitionOutcome(
            aggregate=historical_aggregate,
            replayed=True,
            derived_candidate=prior.result_derived_candidate,
        )

    current = aggregate.projection
    if command.expected_revision != current.revision:
        raise CandidateRevisionConflict("expected_revision does not match current revision")
    if command.decided_at < current.updated_at:
        raise CandidateTransitionRejected("candidate decision time cannot move backward")

    updated_values = current.model_dump()
    changed: set[str] = set()
    derived: CandidateProjection | None = None
    target_status: CandidateStatus

    if command.action == CandidateAction.COMPLETE_FIELDS:
        _require_status(current, CandidateStatus.INCOMPLETE)
        if command.patch is None:  # defensive guard after model validation
            raise CandidateTransitionRejected("COMPLETE_FIELDS requires a patch")
        for field, value in _patch_values(command.patch).items():
            if getattr(current, field) is not None:
                raise CandidateTransitionRejected(
                    "COMPLETE_FIELDS cannot overwrite existing values"
                )
            updated_values[field] = value
            changed.add(field)
        remaining = _missing_fields_from_values(updated_values)
        if remaining:
            raise CandidateTransitionRejected("all missing required fields must be completed")
        updated_values["blockers"] = ()
        target_status = CandidateStatus.PENDING
    elif command.action == CandidateAction.RESOLVE_CONFLICT:
        _require_status(current, CandidateStatus.CONFLICTED)
        required = {b.conflict_ref for b in current.blockers if b.conflict_ref is not None}
        if set(command.conflict_resolutions) != required or any(
            not value.strip() or len(value) > 1_000
            for value in command.conflict_resolutions.values()
        ):
            raise CandidateTransitionRejected("every conflict requires one bounded resolution")
        if command.patch is not None:
            for field, value in _patch_values(command.patch).items():
                if getattr(current, field) != value:
                    updated_values[field] = value
                    changed.add(field)
        if _missing_fields_from_values(updated_values):
            raise CandidateTransitionRejected("resolved candidate must remain complete")
        updated_values["blockers"] = ()
        target_status = CandidateStatus.PENDING
    elif command.action == CandidateAction.CONFIRM:
        _require_status(current, CandidateStatus.PENDING)
        target_status = CandidateStatus.CONFIRMED
    elif command.action == CandidateAction.IGNORE:
        if current.status not in {
            CandidateStatus.INCOMPLETE,
            CandidateStatus.CONFLICTED,
            CandidateStatus.PENDING,
        }:
            raise CandidateTransitionRejected("only open candidates may be ignored")
        target_status = CandidateStatus.IGNORED
    elif command.action == CandidateAction.SUPERSEDE:
        _require_status(current, CandidateStatus.CONFIRMED)
        if (
            command.patch is None
            or command.derived_candidate_ref is None
            or command.derived_short_id is None
        ):
            raise CandidateTransitionRejected("SUPERSEDE requires a derived candidate identity")
        if command.derived_candidate_ref == current.candidate_ref or any(
            candidate.candidate_ref == command.derived_candidate_ref
            for candidate in aggregate.derived_candidates
        ):
            raise CandidateTransitionRejected("derived candidate reference must be new")
        if command.derived_short_id == current.short_id or any(
            candidate.short_id == command.derived_short_id
            for candidate in aggregate.derived_candidates
        ):
            raise CandidateTransitionRejected("derived candidate short ID must be new")
        derived_values = current.model_dump()
        for field, value in _patch_values(command.patch).items():
            if value is None:
                raise CandidateTransitionRejected(
                    "SUPERSEDE corrections cannot remove required data"
                )
            if getattr(current, field) != value:
                derived_values[field] = value
                changed.add(field)
        if not changed:
            raise CandidateTransitionRejected("SUPERSEDE must change a normalized field")
        derived_values.update(
            candidate_ref=command.derived_candidate_ref,
            short_id=command.derived_short_id,
            revision=1,
            status=CandidateStatus.PENDING,
            blockers=(),
            review_summary=ReviewSummary(event_count=0, current_revision=1),
            created_at=command.decided_at,
            updated_at=command.decided_at,
            supersedes_candidate_ref=current.candidate_ref,
            superseded_by_candidate_ref=None,
        )
        derived = CandidateProjection.model_validate(derived_values)
        updated_values["superseded_by_candidate_ref"] = derived.candidate_ref
        target_status = CandidateStatus.SUPERSEDED
    else:  # pragma: no cover - enum exhaustiveness guard
        raise CandidateTransitionRejected("unsupported candidate action")

    changed.add("status")
    next_revision = current.revision + 1
    updated_values.update(
        revision=next_revision,
        status=target_status,
        updated_at=command.decided_at,
        review_summary=ReviewSummary(
            event_count=current.review_summary.event_count + 1,
            last_action=command.action,
            last_decided_at=command.decided_at,
            current_revision=next_revision,
        ),
    )
    updated = CandidateProjection.model_validate(updated_values)
    changed_projection = derived if command.action == CandidateAction.SUPERSEDE else updated
    if changed_projection is None:  # defensive guard after SUPERSEDE validation
        raise CandidateTransitionRejected("candidate change projection is unavailable")
    event = CandidateEvent(
        operation_id=command.operation_id,
        command_fingerprint=fingerprint,
        candidate_ref=current.candidate_ref,
        action=command.action,
        from_revision=current.revision,
        to_revision=next_revision,
        from_status=current.status,
        to_status=target_status,
        changes=tuple(
            FieldChange.model_validate(
                {
                    "field": field,
                    "previous_value": _audit_value(getattr(current, field)),
                    "new_value": (
                        target_status.value
                        if field == "status"
                        else _audit_value(getattr(changed_projection, field))
                    ),
                }
            )
            for field in sorted(changed)
        ),
        resolved_conflicts=tuple(
            ResolvedConflict(conflict_ref=conflict_ref, resolution=resolution)
            for conflict_ref, resolution in sorted(
                command.conflict_resolutions.items(), key=lambda item: item[0].hex
            )
        ),
        reason=command.reason,
        actor_ref=actor_ref,
        created_at=command.decided_at,
        derived_candidate_ref=derived.candidate_ref if derived is not None else None,
        prior_projection=current,
        result_projection=updated,
        result_derived_candidate=derived,
    )
    result = CandidateAggregate(
        projection=updated,
        events=(*aggregate.events, event),
        derived_candidates=(
            (*aggregate.derived_candidates, derived)
            if derived is not None
            else aggregate.derived_candidates
        ),
    )
    return TransitionOutcome(aggregate=result, derived_candidate=derived)


_AUDITED_NORMALIZED_FIELDS = (
    "business_unit_ref",
    "business_unit_label",
    "category_code",
    "category_label",
    "amount_minor",
    "accounting_month",
)
_IMMUTABLE_PROJECTION_FIELDS = (
    "candidate_ref",
    "short_id",
    "entity_ref",
    "currency",
    "summary",
    "confidence_basis_points",
    "source",
    "evidence",
    "created_at",
    "supersedes_candidate_ref",
)
_DERIVED_IMMUTABLE_FIELDS = (
    "entity_ref",
    "currency",
    "summary",
    "confidence_basis_points",
    "source",
    "evidence",
)
_MISSING_CODE_BY_FIELD = {
    "business_unit_ref": BlockerCode.MISSING_BUSINESS_UNIT,
    "category_code": BlockerCode.MISSING_CATEGORY,
    "amount_minor": BlockerCode.MISSING_AMOUNT,
    "accounting_month": BlockerCode.MISSING_ACCOUNTING_MONTH,
}
_MISSING_CODES = set(_MISSING_CODE_BY_FIELD.values())
_PROCESSING_CODES = {
    BlockerCode.PARSE_FAILED,
    BlockerCode.DEPENDENCY_UNAVAILABLE,
}
_INCOMPLETE_CODES = _MISSING_CODES | _PROCESSING_CODES
_CONFLICT_CODES = {
    BlockerCode.AMBIGUOUS_EXTRACTION,
    BlockerCode.EVIDENCE_INCOMPLETE,
    BlockerCode.UNSUPPORTED_ATTACHMENT,
    BlockerCode.DUPLICATE_MESSAGE,
    BlockerCode.DUPLICATE_ATTACHMENT,
    BlockerCode.BUSINESS_KEY_CONFLICT,
    BlockerCode.CROSS_FORMAT_DUPLICATE,
}


def _missing_fields(candidate: CandidateProjection) -> set[str]:
    return {field for field in _MISSING_CODE_BY_FIELD if getattr(candidate, field) is None}


def _missing_fields_from_values(values: dict[str, object]) -> set[str]:
    return {field for field in _MISSING_CODE_BY_FIELD if values.get(field) is None}


def _patch_values(patch: CandidatePatch) -> dict[str, object]:
    return {field: getattr(patch, field) for field in patch.model_fields_set}


def _require_status(candidate: CandidateProjection, expected: CandidateStatus) -> None:
    if candidate.status != expected:
        raise CandidateTransitionRejected(f"{candidate.status} cannot perform this action")


def _command_fingerprint(command: CandidateCommand) -> str:
    payload = command.model_dump(mode="json", exclude={"operation_id"})
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit_value(value: object) -> str | int | None:
    if isinstance(value, StrEnum):
        return value.value
    if value is None or (isinstance(value, (str, int)) and not isinstance(value, bool)):
        return value
    raise CandidateTransitionRejected("candidate audit value is not allowlisted")
