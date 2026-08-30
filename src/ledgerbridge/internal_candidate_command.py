"""Synthetic D1 candidate command service and closed HTTP DTOs.

The service deepens the candidate Module: callers submit a bounded human
decision while the implementation owns state-graph steps, idempotency, actor
binding, and append-only event receipts.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Literal, NoReturn
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.candidate_contract import (
    JSON_SAFE_INTEGER,
    CandidateAction,
    CandidateAggregate,
    CandidateCommand,
    CandidateEvent,
    CandidatePatch,
    CandidateProjection,
    CandidateStatus,
    IngestChannel,
    apply_candidate_command,
    create_candidate_aggregate,
)
from ledgerbridge.internal_read_contract import (
    CANDIDATE_ACTION_CAPABILITIES,
    AccountingDimensions,
    CandidatePage,
    Capability,
    ResourceNotVisible,
    WorkloadPrincipal,
    authorize_candidate_read,
    authorize_collection_read,
    require_candidate_visible_scope,
    require_capability,
)
from ledgerbridge.internal_read_cursor import ReadCursorSigner
from ledgerbridge.internal_read_service import (
    DatabaseInternalReadService,
    SyntheticInternalReadService,
    _wire_ingest_channel,
)


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


def _database_candidate_decision_receipt(value: object) -> object:
    """Map database registry IDs in a receipt onto the versioned wire contract."""

    payload = deepcopy(value)
    if not isinstance(payload, dict):
        return payload

    projections: list[object] = [payload.get("candidate")]
    events = payload.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            projections.extend(
                (
                    event.get("prior_projection"),
                    event.get("result_projection"),
                    event.get("result_derived_candidate"),
                )
            )

    for projection in projections:
        if not isinstance(projection, dict):
            continue
        source = projection.get("source")
        if not isinstance(source, dict):
            continue
        channel = source.get("ingest_channel")
        if isinstance(channel, IngestChannel):
            continue
        if isinstance(channel, str):
            try:
                source["ingest_channel"] = IngestChannel(channel)
            except ValueError:
                source["ingest_channel"] = _wire_ingest_channel(channel)
    return payload


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
            corrections = request.corrections
            correction_patch: CandidatePatch | None = None
            if corrections is not None:
                dimensions = self.get_accounting_dimensions(
                    principal,
                    entity_ref=aggregate.projection.entity_ref,
                )
                business_units = {item.ref for item in dimensions.business_units}
                categories = {item.code for item in dimensions.categories}
                final_business_unit = (
                    corrections.business_unit
                    if "business_unit" in corrections.model_fields_set
                    else aggregate.projection.business_unit_ref
                )
                final_category = (
                    corrections.category
                    if "category" in corrections.model_fields_set
                    else aggregate.projection.category_code
                )
                if request.decision == CandidateDecision.CORRECT_AND_CONFIRM:
                    require_candidate_visible_scope(
                        principal,
                        entity_ref=aggregate.projection.entity_ref,
                        business_unit_ref=final_business_unit,
                    )
                    if final_business_unit not in business_units:
                        raise ResourceNotVisible(
                            "final business unit is not an active candidate dimension"
                        )
                    if final_category not in categories:
                        raise ResourceNotVisible(
                            "final category is not an active candidate dimension"
                        )
                else:
                    if "business_unit" in corrections.model_fields_set:
                        require_candidate_visible_scope(
                            principal,
                            entity_ref=aggregate.projection.entity_ref,
                            business_unit_ref=corrections.business_unit,
                        )
                        if corrections.business_unit not in business_units:
                            raise ResourceNotVisible("target business unit is not visible")
                    if (
                        "category" in corrections.model_fields_set
                        and corrections.category not in categories
                    ):
                        raise ResourceNotVisible("target category is not visible")
                correction_patch = _candidate_patch(corrections, dimensions)
            events: list[CandidateEvent] = []
            for command in _commands_for_decision(
                aggregate,
                operation_id=operation_id,
                request=request,
                decided_at=decided_at,
                correction_patch=correction_patch,
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


class DatabaseInternalReviewService(DatabaseInternalReadService):
    """Database-backed D1 adapter with a narrow SECURITY DEFINER write surface."""

    def __init__(
        self,
        read_session_factory: Callable[[], Session],
        command_session_factory: Callable[[], Session],
        cursor_signer: ReadCursorSigner | None = None,
    ) -> None:
        super().__init__(read_session_factory, cursor_signer)
        self._command_session_factory = command_session_factory

    def list_candidate_events(
        self,
        principal: WorkloadPrincipal,
        *,
        candidate_ref: UUID | None = None,
    ) -> CandidateEventPage:
        authorize_collection_read(principal, Capability.CANDIDATE_READ)
        entity_ref, business_unit_id = self._event_scope(principal, candidate_ref)
        try:
            with self._session_factory() as session:
                sequence, horizon_hash = self._audit_horizon(session)
                rows = session.execute(
                    text(
                        "SELECT event FROM internal_read.list_candidate_events_as_of("
                        "CAST(:entity_ref AS uuid), CAST(:business_unit_id AS uuid), "
                        "CAST(:candidate_ref AS uuid), :horizon_sequence, :horizon_hash, 100)"
                    ),
                    {
                        "entity_ref": entity_ref,
                        "business_unit_id": business_unit_id,
                        "candidate_ref": candidate_ref,
                        "horizon_sequence": sequence,
                        "horizon_hash": horizon_hash,
                    },
                ).mappings()
                events = tuple(CandidateEvent.model_validate(row["event"]) for row in rows)
        except SQLAlchemyError as exc:
            raise CandidateCommandUnavailable("candidate event reader is unavailable") from exc
        return CandidateEventPage(items=events)

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
        require_capability(principal, Capability.CANDIDATE_DECIDE)
        candidate = self.get_candidate(principal, candidate_ref)
        current_business_unit_id, target_business_unit_id = self._command_scope(
            principal,
            candidate,
            request,
        )
        corrections = request.corrections
        fields = corrections.model_fields_set if corrections is not None else set()
        month = (
            date.fromisoformat(f"{corrections.accounting_month}-01")
            if corrections is not None and corrections.accounting_month is not None
            else None
        )
        params = {
            "operation_id": operation_id,
            "assertion_jti": assertion_jti,
            "candidate_ref": candidate_ref,
            "actor_ref": actor_ref,
            "workload_principal_ref": principal.principal_ref,
            "verified_san": principal.san_uri,
            "authorized_entity_id": candidate.entity_ref,
            "current_business_unit_id": current_business_unit_id,
            "target_business_unit_id": target_business_unit_id,
            "decision": request.decision.value,
            "expected_revision": request.expected_revision,
            "reason": request.reason,
            "set_business_unit": "business_unit" in fields,
            "business_unit_ref": corrections.business_unit if corrections is not None else None,
            "set_category": "category" in fields,
            "category_code": corrections.category if corrections is not None else None,
            "set_amount": "amount_minor" in fields,
            "amount_minor": corrections.amount_minor if corrections is not None else None,
            "set_month": "accounting_month" in fields,
            "accounting_month": month,
            "conflict_resolution": request.conflict_resolution,
            "decided_at": decided_at,
        }
        sql = text(
            "SELECT internal_command.apply_candidate_decision("
            "CAST(:operation_id AS uuid), CAST(:assertion_jti AS uuid), "
            "CAST(:candidate_ref AS uuid), CAST(:actor_ref AS varchar(200)), "
            "CAST(:workload_principal_ref AS varchar(200)), "
            "CAST(:verified_san AS varchar(200)), CAST(:authorized_entity_id AS uuid), "
            "CAST(:current_business_unit_id AS uuid), CAST(:target_business_unit_id AS uuid), "
            "CAST(:decision AS varchar(32)), :expected_revision, "
            "CAST(:reason AS varchar(1000)), :set_business_unit, "
            "CAST(:business_unit_ref AS varchar(100)), :set_category, "
            "CAST(:category_code AS varchar(100)), :set_amount, :amount_minor, :set_month, "
            "CAST(:accounting_month AS date), CAST(:conflict_resolution AS varchar(1000)), "
            "CAST(:decided_at AS timestamptz)) AS receipt"
        )
        try:
            with self._command_session_factory() as session:
                row = session.execute(sql, params).mappings().first()
                if row is None:
                    raise CandidateCommandUnavailable("candidate command returned no receipt")
                receipt = CandidateDecisionReceipt.model_validate(
                    _database_candidate_decision_receipt(row["receipt"])
                )
                session.commit()
                return receipt
        except SQLAlchemyError as exc:
            self._raise_database_command_error(exc)

    def _event_scope(
        self,
        principal: WorkloadPrincipal,
        candidate_ref: UUID | None,
    ) -> tuple[UUID, UUID | None]:
        if candidate_ref is not None:
            candidate = self.get_candidate(principal, candidate_ref)
            current, _ = self._command_scope(principal, candidate, None)
            return candidate.entity_ref, current
        bindings = [
            (grant.entity_ref, binding_id)
            for grant in principal.grants
            for _, binding_id in grant.business_unit_bindings
        ]
        if len(bindings) != 1:
            raise CandidateCommandUnavailable(
                "database event listing requires exactly one bound business-unit scope"
            )
        return bindings[0]

    @staticmethod
    def _command_scope(
        principal: WorkloadPrincipal,
        candidate: CandidateProjection,
        request: CandidateDecisionRequest | None,
    ) -> tuple[UUID | None, UUID | None]:
        matching = [grant for grant in principal.grants if grant.entity_ref == candidate.entity_ref]
        if len(matching) != 1:
            raise ResourceNotVisible("candidate entity is not visible")
        grant = matching[0]
        bindings = dict(grant.business_unit_bindings)
        if candidate.business_unit_ref is None:
            if not grant.allow_unassigned_candidates:
                raise ResourceNotVisible("unassigned candidate is not visible")
            current_id = None
        else:
            current_id = bindings.get(candidate.business_unit_ref)
            if current_id is None:
                raise ResourceNotVisible("candidate business unit is not visible")
        target_id = current_id
        corrections = request.corrections if request is not None else None
        if corrections is not None and "business_unit" in corrections.model_fields_set:
            if corrections.business_unit is None:
                raise CandidateCommandRejected("business unit correction cannot be null")
            target_id = bindings.get(corrections.business_unit)
            if target_id is None:
                raise ResourceNotVisible("target business unit is not visible")
        return current_id, target_id

    @staticmethod
    def _raise_database_command_error(exc: SQLAlchemyError) -> NoReturn:
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate == "LB001":
            raise CandidateCommandIdempotencyConflict("database idempotency conflict") from exc
        if sqlstate == "LB002":
            from ledgerbridge.candidate_contract import CandidateRevisionConflict

            raise CandidateRevisionConflict("database revision conflict") from exc
        if sqlstate == "LB003":
            raise CandidateCommandRejected("database rejected candidate decision") from exc
        if sqlstate == "LB004":
            raise ResourceNotVisible("candidate is outside the authorized scope") from exc
        raise CandidateCommandUnavailable("candidate command backend is unavailable") from exc


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
    correction_patch: CandidatePatch | None,
) -> tuple[CandidateCommand, ...]:
    current = aggregate.projection
    if request.expected_revision != current.revision:
        from ledgerbridge.candidate_contract import CandidateRevisionConflict

        raise CandidateRevisionConflict("expected_revision does not match current revision")

    if request.decision == CandidateDecision.CONFIRM:
        return (_command(operation_id, "confirm", CandidateAction.CONFIRM, request, decided_at),)
    if request.decision == CandidateDecision.IGNORE:
        return (_command(operation_id, "ignore", CandidateAction.IGNORE, request, decided_at),)
    if request.decision == CandidateDecision.CORRECT_AND_CONFIRM:
        if request.corrections is None:
            raise CandidateCommandRejected("corrections are required")
        if correction_patch is None:
            raise CandidateCommandRejected("correction dimensions were not resolved")
        if current.status == CandidateStatus.PENDING:
            return (
                _command(
                    operation_id,
                    "correct-and-confirm",
                    CandidateAction.CORRECT_AND_CONFIRM,
                    request,
                    decided_at,
                    patch=correction_patch,
                ),
            )
        if current.status != CandidateStatus.INCOMPLETE:
            raise CandidateCommandRejected(
                "corrections are accepted only for open reviewable candidates"
            )
        complete = _command(
            operation_id,
            "complete-fields",
            CandidateAction.COMPLETE_FIELDS,
            request,
            decided_at,
            patch=correction_patch,
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
            blocker.conflict_ref for blocker in current.blockers if blocker.conflict_ref is not None
        }
        resolve = CandidateCommand(
            operation_id=uuid5(operation_id, "resolve-conflict"),
            action=CandidateAction.RESOLVE_CONFLICT,
            expected_revision=request.expected_revision,
            reason=request.reason,
            patch=correction_patch,
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


def _candidate_patch(
    corrections: CandidateCorrections,
    dimensions: AccountingDimensions,
) -> CandidatePatch:
    values: dict[str, str | int | None] = {}
    if "business_unit" in corrections.model_fields_set:
        values["business_unit_ref"] = corrections.business_unit
        values["business_unit_label"] = next(
            item.label
            for item in dimensions.business_units
            if item.ref == corrections.business_unit
        )
    if "category" in corrections.model_fields_set:
        values["category_code"] = corrections.category
        values["category_label"] = next(
            item.label for item in dimensions.categories if item.code == corrections.category
        )
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
