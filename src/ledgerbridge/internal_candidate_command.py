"""Synthetic D1 candidate command service and closed HTTP DTOs.

The service deepens the candidate Module: callers submit a bounded human
decision while the implementation owns state-graph steps, idempotency, actor
binding, and append-only event receipts.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.candidate_contract import (
    JSON_SAFE_INTEGER,
    CandidateAction,
    CandidateAggregate,
    CandidateCommand,
    CandidateEvent,
    CandidatePatch,
    CandidateProjection,
    CandidateStatus,
    apply_candidate_command,
    create_candidate_aggregate,
)
from ledgerbridge.internal_read_contract import (
    CANDIDATE_ACTION_CAPABILITIES,
    CandidatePage,
    Capability,
    ResourceNotVisible,
    WorkloadPrincipal,
    authorize_candidate_read,
    authorize_collection_read,
    require_candidate_visible_scope,
    require_capability,
)
from ledgerbridge.internal_read_service import SyntheticInternalReadService


class CandidateDecision(StrEnum):
    CONFIRM = "CONFIRM"
    IGNORE = "IGNORE"
    CORRECT_AND_CONFIRM = "CORRECT_AND_CONFIRM"
    RESOLVE_CONFLICT = "RESOLVE_CONFLICT"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateCorrections(_FrozenModel):
    business_unit: str | None = Field(default=None, min_length=1, max_length=100)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    amount_minor: int | None = Field(
        default=None,
        strict=True,
        ge=-JSON_SAFE_INTEGER,
        le=JSON_SAFE_INTEGER,
    )
    accounting_month: str | None = Field(
        default=None,
        pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$",
    )

    @model_validator(mode="after")
    def contains_a_change(self) -> CandidateCorrections:
        if not self.model_fields_set:
            raise ValueError("corrections must contain at least one field")
        return self


class CandidateDecisionRequest(_FrozenModel):
    decision: CandidateDecision
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1_000)
    corrections: CandidateCorrections | None = None
    conflict_resolution: str | None = Field(default=None, min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def decision_shape(self) -> CandidateDecisionRequest:
        if self.decision == CandidateDecision.CORRECT_AND_CONFIRM:
            if self.corrections is None or self.conflict_resolution is not None:
                raise ValueError("CORRECT_AND_CONFIRM requires only corrections")
        elif self.decision == CandidateDecision.RESOLVE_CONFLICT:
            if self.conflict_resolution is None:
                raise ValueError("RESOLVE_CONFLICT requires a resolution")
        elif self.corrections is not None or self.conflict_resolution is not None:
            raise ValueError("decision does not accept corrections or conflict resolution")
        return self


class CandidateDecisionReceipt(_FrozenModel):
    contract_version: Literal["ledgerbridge.candidate-decision.v1"] = (
        "ledgerbridge.candidate-decision.v1"
    )
    operation_id: UUID
    replayed: bool
    candidate: CandidateProjection
    events: tuple[CandidateEvent, ...] = Field(min_length=1, max_length=2)


class CandidateEventPage(_FrozenModel):
    items: tuple[CandidateEvent, ...] = Field(max_length=100)
    next_cursor: None = None


class CandidateCommandUnavailable(RuntimeError):
    """The command backend is unavailable or not enabled."""


class CandidateCommandRejected(RuntimeError):
    """The bounded public decision cannot be represented by the frozen state graph."""


class CandidateCommandIdempotencyConflict(RuntimeError):
    """An idempotency operation or assertion JTI was reused with different content."""


class SyntheticInternalReviewService(SyntheticInternalReadService):
    """One process-local synthetic read/write adapter for the D1 contract proof."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.RLock()
        self._aggregates: dict[UUID, CandidateAggregate] = {
            candidate.candidate_ref: create_candidate_aggregate(candidate)
            for candidate in self._fixture.candidates
            if candidate.revision == 1
        }
        self._receipts: dict[UUID, tuple[str, str, CandidateDecisionReceipt]] = {}
        self._assertion_jtis: dict[UUID, UUID] = {}

    def list_candidates(
        self,
        principal: WorkloadPrincipal,
        *,
        month: str | None = None,
        status: CandidateStatus | None = None,
        business_unit: str | None = None,
        cursor: str | None = None,
    ) -> CandidatePage:
        authorize_collection_read(principal, Capability.CANDIDATE_READ)
        base = super().list_candidates(principal)
        with self._lock:
            current = []
            for item in base.items:
                aggregate = self._aggregates.get(item.candidate_ref)
                current.append(item if aggregate is None else aggregate.projection)
        visible = [
            item
            for item in current
            if (month is None or item.accounting_month == month)
            and (status is None or item.status == status)
            and (business_unit is None or item.business_unit_ref == business_unit)
        ]
        if cursor is not None:
            raise ValueError("synthetic reader does not accept cursors")
        return CandidatePage(
            items=tuple(
                sorted(visible, key=lambda item: (item.created_at, item.candidate_ref.int))
            ),
            next_cursor=None,
        )

    def get_candidate(
        self,
        principal: WorkloadPrincipal,
        candidate_ref: UUID,
    ) -> CandidateProjection:
        original = super().get_candidate(principal, candidate_ref)
        with self._lock:
            aggregate = self._aggregates.get(candidate_ref)
            return original if aggregate is None else aggregate.projection

    def list_candidate_events(
        self,
        principal: WorkloadPrincipal,
        *,
        candidate_ref: UUID | None = None,
    ) -> CandidateEventPage:
        authorize_collection_read(principal, Capability.CANDIDATE_READ)
        with self._lock:
            aggregates = tuple(self._aggregates.values())
        events: list[CandidateEvent] = []
        for aggregate in aggregates:
            candidate = aggregate.projection
            try:
                authorize_candidate_read(
                    principal,
                    entity_ref=candidate.entity_ref,
                    business_unit_ref=candidate.business_unit_ref,
                )
            except ResourceNotVisible:
                continue
            if candidate_ref is None or candidate.candidate_ref == candidate_ref:
                events.extend(aggregate.events)
        events.sort(key=lambda item: (item.created_at, item.operation_id.int), reverse=True)
        return CandidateEventPage(items=tuple(events[:100]))

    def append_decision(
        self,
        principal: WorkloadPrincipal,
        *,
        candidate_ref: UUID,
        operation_id: UUID,
        assertion_jti: UUID,
        actor_ref: str,
        request: CandidateDecisionRequest,
        decided_at: datetime,
    ) -> CandidateDecisionReceipt:
        fingerprint = _decision_fingerprint(candidate_ref, request)
        with self._lock:
            prior_jti = self._assertion_jtis.get(assertion_jti)
            if prior_jti is not None and prior_jti != operation_id:
                raise CandidateCommandIdempotencyConflict(
                    "assertion JTI was reused for another operation"
                )
            self._assertion_jtis[assertion_jti] = operation_id

            prior = self._receipts.get(operation_id)
            if prior is not None:
                prior_fingerprint, prior_actor, receipt = prior
                if prior_fingerprint != fingerprint or prior_actor != actor_ref:
                    raise CandidateCommandIdempotencyConflict(
                        "operation ID was reused with different content or actor"
                    )
                return receipt.model_copy(update={"replayed": True})

            aggregate = self._aggregates.get(candidate_ref)
            if aggregate is None:
                candidate = super().get_candidate(principal, candidate_ref)
                raise CandidateCommandRejected(
                    f"candidate revision {candidate.revision} lacks a synthetic event history"
                )
            require_candidate_visible_scope(
                principal,
                entity_ref=aggregate.projection.entity_ref,
                business_unit_ref=aggregate.projection.business_unit_ref,
            )
            events: list[CandidateEvent] = []
            for command in _commands_for_decision(
                aggregate,
                operation_id=operation_id,
                request=request,
                decided_at=decided_at,
            ):
                require_capability(
                    principal,
                    CANDIDATE_ACTION_CAPABILITIES[command.action],
                )
                outcome = apply_candidate_command(aggregate, command, actor_ref=actor_ref)
                aggregate = outcome.aggregate
                events.append(aggregate.events[-1])

            self._aggregates[candidate_ref] = aggregate
            receipt = CandidateDecisionReceipt(
                operation_id=operation_id,
                replayed=False,
                candidate=aggregate.projection,
                events=tuple(events),
            )
            self._receipts[operation_id] = (fingerprint, actor_ref, receipt)
            return receipt


@lru_cache(maxsize=1)
def get_synthetic_review_service() -> SyntheticInternalReviewService:
    """Return the one process-local synthetic aggregate store used by reads and writes."""

    return SyntheticInternalReviewService()


def _commands_for_decision(
    aggregate: CandidateAggregate,
    *,
    operation_id: UUID,
    request: CandidateDecisionRequest,
    decided_at: datetime,
) -> tuple[CandidateCommand, ...]:
    current = aggregate.projection
    if request.expected_revision != current.revision:
        from ledgerbridge.candidate_contract import CandidateRevisionConflict

        raise CandidateRevisionConflict("expected_revision does not match current revision")

    if request.decision == CandidateDecision.CONFIRM:
        return (
            _command(operation_id, "confirm", CandidateAction.CONFIRM, request, decided_at),
        )
    if request.decision == CandidateDecision.IGNORE:
        return (
            _command(operation_id, "ignore", CandidateAction.IGNORE, request, decided_at),
        )
    if request.decision == CandidateDecision.CORRECT_AND_CONFIRM:
        if current.status != CandidateStatus.INCOMPLETE or request.corrections is None:
            raise CandidateCommandRejected(
                "corrections are accepted only while completing an incomplete candidate"
            )
        patch = _candidate_patch(request.corrections)
        complete = _command(
            operation_id,
            "complete-fields",
            CandidateAction.COMPLETE_FIELDS,
            request,
            decided_at,
            patch=patch,
        )
        confirm = CandidateCommand(
            operation_id=uuid5(operation_id, "confirm-after-complete"),
            action=CandidateAction.CONFIRM,
            expected_revision=request.expected_revision + 1,
            reason=request.reason,
            decided_at=decided_at,
        )
        return complete, confirm
    if request.decision == CandidateDecision.RESOLVE_CONFLICT:
        if current.status != CandidateStatus.CONFLICTED or request.conflict_resolution is None:
            raise CandidateCommandRejected("candidate has no resolvable conflict")
        conflict_refs = {
            blocker.conflict_ref
            for blocker in current.blockers
            if blocker.conflict_ref is not None
        }
        resolve = CandidateCommand(
            operation_id=uuid5(operation_id, "resolve-conflict"),
            action=CandidateAction.RESOLVE_CONFLICT,
            expected_revision=request.expected_revision,
            reason=request.reason,
            patch=(
                _candidate_patch(request.corrections)
                if request.corrections is not None
                else None
            ),
            conflict_resolutions={
                conflict_ref: request.conflict_resolution for conflict_ref in conflict_refs
            },
            decided_at=decided_at,
        )
        confirm = CandidateCommand(
            operation_id=uuid5(operation_id, "confirm-after-conflict"),
            action=CandidateAction.CONFIRM,
            expected_revision=request.expected_revision + 1,
            reason=request.reason,
            decided_at=decided_at,
        )
        return resolve, confirm
    raise CandidateCommandRejected("unsupported candidate decision")


def _command(
    operation_id: UUID,
    step: str,
    action: CandidateAction,
    request: CandidateDecisionRequest,
    decided_at: datetime,
    *,
    patch: CandidatePatch | None = None,
) -> CandidateCommand:
    return CandidateCommand(
        operation_id=uuid5(operation_id, step),
        action=action,
        expected_revision=request.expected_revision,
        reason=request.reason,
        patch=patch,
        decided_at=decided_at,
    )


def _candidate_patch(corrections: CandidateCorrections) -> CandidatePatch:
    values: dict[str, str | int | None] = {}
    if "business_unit" in corrections.model_fields_set:
        values["business_unit_ref"] = corrections.business_unit
        values["business_unit_label"] = corrections.business_unit
    if "category" in corrections.model_fields_set:
        values["category_code"] = corrections.category
        values["category_label"] = corrections.category
    if "amount_minor" in corrections.model_fields_set:
        values["amount_minor"] = corrections.amount_minor
    if "accounting_month" in corrections.model_fields_set:
        values["accounting_month"] = corrections.accounting_month
    return CandidatePatch.model_validate(values)


def _decision_fingerprint(
    candidate_ref: UUID,
    request: CandidateDecisionRequest,
) -> str:
    payload = {
        "candidate_ref": str(candidate_ref),
        "request": request.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
