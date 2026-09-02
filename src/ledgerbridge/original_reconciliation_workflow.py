"""Closed workflow contract for the user's original reconciliation scope.

The existing :mod:`ledgerbridge.original_reconciliation` module remains a read-only
legacy-grid projection.  This module owns the identities and state transitions that
the browser workflow needs, without adding a database schema or silently posting
ledger facts.  A production adapter must persist the frozen models and perform each
command atomically; the in-memory service below is deliberately synthetic and exists
to prove the contract and its concurrency/idempotency rules.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.candidate_contract import JSON_SAFE_INTEGER, MoneyMinor
from ledgerbridge.original_reconciliation import OriginalReconciliationScope

ORIGINAL_RECONCILIATION_WORKFLOW_CONTRACT_VERSION: Literal[
    "ledgerbridge.original-reconciliation-workflow.v1"
] = "ledgerbridge.original-reconciliation-workflow.v1"

_ITEM_NAMESPACE = UUID("6e7ab334-9778-52e6-9b6f-3eed3a09db19")
_RECEIPT_NAMESPACE = UUID("20cc9d65-00f6-5eab-b2fb-8e320ab20aa3")
_MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
_STABLE_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$"


class OriginalReconciliationFlowKind(StrEnum):
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"
    CURRENT = "CURRENT"


class OriginalReconciliationSourceChannel(StrEnum):
    BANK_STATEMENT = "BANK_STATEMENT"
    WECHAT_TRANSFER = "WECHAT_TRANSFER"
    PAYROLL_PUBLICATION = "PAYROLL_PUBLICATION"


class OriginalReconciliationWorkflowReviewStatus(StrEnum):
    PENDING = "PENDING"
    RETURNED = "RETURNED"
    CONFIRMED = "CONFIRMED"


class OriginalReconciliationWorkflowRejected(RuntimeError):
    """The requested workflow transition is invalid or outside the frozen scope."""


class OriginalReconciliationWorkflowRevisionConflict(
    OriginalReconciliationWorkflowRejected
):
    """A caller tried to overwrite a newer item or month revision."""


class OriginalReconciliationWorkflowIdempotencyConflict(
    OriginalReconciliationWorkflowRejected
):
    """An operation ID was reused with different command content or actor."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def stable_original_reconciliation_item_ref(
    *,
    entity_ref: UUID,
    business_unit_ref: str,
    month: str,
    stable_item_key: str,
) -> UUID:
    """Return an item identity unaffected by money, evidence, or review changes."""

    identity = json.dumps(
        {
            "business_unit_ref": business_unit_ref,
            "entity_ref": str(entity_ref),
            "month": month,
            "stable_item_key": stable_item_key,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid5(_ITEM_NAMESPACE, identity)


class OriginalReconciliationAuthoritativeSource(_FrozenModel):
    """One authoritative upstream fact set and its registered source account."""

    source_channel: OriginalReconciliationSourceChannel
    authority_ref: str = Field(pattern=_STABLE_REF_PATTERN)
    managed_account_ref: UUID | None = None
    source_fact_refs: tuple[str, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def account_and_facts_are_explicit(self) -> OriginalReconciliationAuthoritativeSource:
        if (
            self.source_channel
            in {
                OriginalReconciliationSourceChannel.BANK_STATEMENT,
                OriginalReconciliationSourceChannel.WECHAT_TRANSFER,
            }
            and self.managed_account_ref is None
        ):
            raise ValueError("statement and WeChat sources require a managed account ref")
        if len(self.source_fact_refs) != len(set(self.source_fact_refs)):
            raise ValueError("source fact refs must be unique")
        if any(
            not fact_ref
            or len(fact_ref) > 200
            or fact_ref.strip() != fact_ref
            for fact_ref in self.source_fact_refs
        ):
            raise ValueError("source fact refs must be bounded stable identifiers")
        return self


class OriginalReconciliationWorkflowItem(_FrozenModel):
    item_ref: UUID
    stable_item_key: str = Field(pattern=_STABLE_REF_PATTERN)
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)
    month: str = Field(pattern=_MONTH_PATTERN)
    flow_kind: OriginalReconciliationFlowKind
    signed_amount_minor: MoneyMinor
    review_status: OriginalReconciliationWorkflowReviewStatus
    authoritative_sources: tuple[OriginalReconciliationAuthoritativeSource, ...] = Field(
        min_length=1,
        max_length=100,
    )
    evidence_refs: tuple[UUID, ...] = Field(default=(), max_length=100)
    revision: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def identity_money_and_links_are_consistent(self) -> OriginalReconciliationWorkflowItem:
        expected_ref = stable_original_reconciliation_item_ref(
            entity_ref=self.entity_ref,
            business_unit_ref=self.business_unit_ref,
            month=self.month,
            stable_item_key=self.stable_item_key,
        )
        if self.item_ref != expected_ref:
            raise ValueError("item_ref does not match the stable item identity")
        if self.flow_kind == OriginalReconciliationFlowKind.INCOME and self.signed_amount_minor < 0:
            raise ValueError("income must not carry a negative signed amount")
        if (
            self.flow_kind == OriginalReconciliationFlowKind.EXPENSE
            and self.signed_amount_minor > 0
        ):
            raise ValueError("expense must not carry a positive signed amount")
        source_keys = [
            (source.source_channel, source.authority_ref)
            for source in self.authoritative_sources
        ]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("authoritative source refs must be unique per item")
        source_fact_refs = [
            fact_ref
            for source in self.authoritative_sources
            for fact_ref in source.source_fact_refs
        ]
        if len(source_fact_refs) != len(set(source_fact_refs)):
            raise ValueError("one source fact cannot be attached through multiple authorities")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence refs must be unique per item")
        return self


def create_original_reconciliation_workflow_item(
    *,
    stable_item_key: str,
    entity_ref: UUID,
    business_unit_ref: str,
    month: str,
    flow_kind: OriginalReconciliationFlowKind,
    signed_amount_minor: int,
    review_status: OriginalReconciliationWorkflowReviewStatus,
    authoritative_sources: tuple[OriginalReconciliationAuthoritativeSource, ...],
    evidence_refs: tuple[UUID, ...] = (),
) -> OriginalReconciliationWorkflowItem:
    """Create revision one from an explicit upstream stable key and source mapping."""

    return OriginalReconciliationWorkflowItem(
        item_ref=stable_original_reconciliation_item_ref(
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
            month=month,
            stable_item_key=stable_item_key,
        ),
        stable_item_key=stable_item_key,
        entity_ref=entity_ref,
        business_unit_ref=business_unit_ref,
        month=month,
        flow_kind=flow_kind,
        signed_amount_minor=signed_amount_minor,
        review_status=review_status,
        authoritative_sources=authoritative_sources,
        evidence_refs=evidence_refs,
        revision=1,
    )


class OriginalReconciliationEvidenceRelinkCommand(_FrozenModel):
    expected_month_revision: int = Field(strict=True, ge=1)
    expected_item_revision: int = Field(strict=True, ge=1)
    add_evidence_refs: tuple[UUID, ...] = Field(default=(), max_length=100)
    remove_evidence_refs: tuple[UUID, ...] = Field(default=(), max_length=100)
    reason: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def change_set_is_explicit(self) -> OriginalReconciliationEvidenceRelinkCommand:
        additions = set(self.add_evidence_refs)
        removals = set(self.remove_evidence_refs)
        if not additions and not removals:
            raise ValueError("evidence relink requires at least one add or remove")
        if len(additions) != len(self.add_evidence_refs):
            raise ValueError("evidence additions must be unique")
        if len(removals) != len(self.remove_evidence_refs):
            raise ValueError("evidence removals must be unique")
        if additions & removals:
            raise ValueError("one evidence ref cannot be added and removed together")
        if self.reason.strip() != self.reason:
            raise ValueError("reason cannot contain leading or trailing whitespace")
        return self


class OriginalReconciliationMonthCloseCommand(_FrozenModel):
    expected_month_revision: int = Field(strict=True, ge=1)
    reason: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def reason_is_canonical(self) -> OriginalReconciliationMonthCloseCommand:
        if self.reason.strip() != self.reason:
            raise ValueError("reason cannot contain leading or trailing whitespace")
        return self


class OriginalReconciliationEvidenceRelinkEvent(_FrozenModel):
    event_ref: UUID
    operation_id: UUID
    item_ref: UUID
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)
    month: str = Field(pattern=_MONTH_PATTERN)
    actor_ref: str = Field(pattern=_STABLE_REF_PATTERN)
    reason: str = Field(min_length=1, max_length=1_000)
    from_month_revision: int = Field(strict=True, ge=1)
    to_month_revision: int = Field(strict=True, ge=2)
    from_item_revision: int = Field(strict=True, ge=1)
    to_item_revision: int = Field(strict=True, ge=2)
    added_evidence_refs: tuple[UUID, ...]
    removed_evidence_refs: tuple[UUID, ...]
    resulting_evidence_refs: tuple[UUID, ...] = Field(max_length=100)
    changed_at: datetime

    @model_validator(mode="after")
    def revisions_and_time_are_consistent(self) -> OriginalReconciliationEvidenceRelinkEvent:
        if self.event_ref != uuid5(
            self.operation_id,
            "original-reconciliation:evidence-relink",
        ):
            raise ValueError("event_ref does not match the evidence operation")
        if self.to_month_revision != self.from_month_revision + 1:
            raise ValueError("evidence relink must increment the month revision once")
        if self.to_item_revision != self.from_item_revision + 1:
            raise ValueError("evidence relink must increment the item revision once")
        _require_aware_datetime(self.changed_at, "changed_at")
        return self


class OriginalReconciliationEvidenceRelinkReceipt(_FrozenModel):
    contract_version: Literal["ledgerbridge.original-reconciliation-evidence-relink.v1"] = (
        "ledgerbridge.original-reconciliation-evidence-relink.v1"
    )
    operation_id: UUID
    replayed: bool
    event: OriginalReconciliationEvidenceRelinkEvent
    item: OriginalReconciliationWorkflowItem

    @model_validator(mode="after")
    def operation_and_item_match_event(self) -> OriginalReconciliationEvidenceRelinkReceipt:
        if (
            self.operation_id != self.event.operation_id
            or self.item.item_ref != self.event.item_ref
        ):
            raise ValueError("evidence receipt does not match its event and item")
        return self


class OriginalReconciliationMonthCloseReceipt(_FrozenModel):
    contract_version: Literal["ledgerbridge.original-reconciliation-month-close.v1"] = (
        "ledgerbridge.original-reconciliation-month-close.v1"
    )
    receipt_ref: UUID
    operation_id: UUID
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)
    month: str = Field(pattern=_MONTH_PATTERN)
    closed_revision: int = Field(strict=True, ge=2)
    item_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_count: int = Field(strict=True, ge=1, le=10_000)
    income_minor: MoneyMinor
    expense_minor: MoneyMinor
    current_net_minor: MoneyMinor
    net_minor: MoneyMinor
    actor_ref: str = Field(pattern=_STABLE_REF_PATTERN)
    reason: str = Field(min_length=1, max_length=1_000)
    closed_at: datetime

    @model_validator(mode="after")
    def time_and_totals_are_consistent(self) -> OriginalReconciliationMonthCloseReceipt:
        _require_aware_datetime(self.closed_at, "closed_at")
        if self.net_minor != self.income_minor - self.expense_minor + self.current_net_minor:
            raise ValueError("net_minor must reconcile income, expense, and current totals")
        return self


class OriginalReconciliationMonthCloseCommandReceipt(_FrozenModel):
    operation_id: UUID
    replayed: bool
    close_receipt: OriginalReconciliationMonthCloseReceipt

    @model_validator(mode="after")
    def operation_matches_receipt(self) -> OriginalReconciliationMonthCloseCommandReceipt:
        if self.operation_id != self.close_receipt.operation_id:
            raise ValueError("close command receipt operation does not match the close receipt")
        return self


class OriginalReconciliationWorkflowMonth(_FrozenModel):
    contract_version: Literal["ledgerbridge.original-reconciliation-workflow.v1"] = (
        ORIGINAL_RECONCILIATION_WORKFLOW_CONTRACT_VERSION
    )
    month: str = Field(pattern=_MONTH_PATTERN)
    scope: OriginalReconciliationScope
    revision: int = Field(strict=True, ge=1)
    items: tuple[OriginalReconciliationWorkflowItem, ...] = Field(max_length=10_000)
    close_receipt: OriginalReconciliationMonthCloseReceipt | None = None

    @model_validator(mode="after")
    def items_and_close_receipt_remain_in_scope(self) -> OriginalReconciliationWorkflowMonth:
        item_refs = [item.item_ref for item in self.items]
        if len(item_refs) != len(set(item_refs)):
            raise ValueError("workflow item refs must be unique within a month")
        if any(
            item.entity_ref != self.scope.entity_ref
            or item.business_unit_ref != self.scope.business_unit_ref
            or item.month != self.month
            for item in self.items
        ):
            raise ValueError("workflow item escaped its month scope")
        receipt = self.close_receipt
        if (
            receipt is not None
            and (
                receipt.entity_ref != self.scope.entity_ref
                or receipt.business_unit_ref != self.scope.business_unit_ref
                or receipt.month != self.month
                or receipt.closed_revision != self.revision
                or receipt.item_count != len(self.items)
                or receipt.item_set_sha256 != original_reconciliation_item_set_sha256(self.items)
                or receipt.receipt_ref
                != _original_reconciliation_close_receipt_ref(
                    entity_ref=self.scope.entity_ref,
                    business_unit_ref=self.scope.business_unit_ref,
                    month=self.month,
                    closed_revision=self.revision,
                    item_set_sha256=receipt.item_set_sha256,
                )
                or (
                    receipt.income_minor,
                    receipt.expense_minor,
                    receipt.current_net_minor,
                    receipt.net_minor,
                )
                != _original_reconciliation_totals(self.items)
            )
        ):
            raise ValueError("close receipt does not bind the exact workflow snapshot")
        return self


def create_original_reconciliation_workflow_month(
    *,
    month: str,
    scope: OriginalReconciliationScope,
    items: tuple[OriginalReconciliationWorkflowItem, ...],
) -> OriginalReconciliationWorkflowMonth:
    return OriginalReconciliationWorkflowMonth(
        month=month,
        scope=scope,
        revision=1,
        items=items,
    )


def original_reconciliation_item_set_sha256(
    items: tuple[OriginalReconciliationWorkflowItem, ...],
) -> str:
    """Hash the complete close-relevant state in an order-independent form."""

    payload = [
        {
            "authoritative_sources": sorted(
                (
                    {
                        "authority_ref": source.authority_ref,
                        "managed_account_ref": (
                            str(source.managed_account_ref)
                            if source.managed_account_ref is not None
                            else None
                        ),
                        "source_channel": source.source_channel.value,
                        "source_fact_refs": sorted(source.source_fact_refs),
                    }
                    for source in item.authoritative_sources
                ),
                key=lambda source: (source["source_channel"], source["authority_ref"]),
            ),
            "business_unit_ref": item.business_unit_ref,
            "entity_ref": str(item.entity_ref),
            "evidence_refs": sorted(str(ref) for ref in item.evidence_refs),
            "flow_kind": item.flow_kind.value,
            "item_ref": str(item.item_ref),
            "month": item.month,
            "review_status": item.review_status.value,
            "revision": item.revision,
            "signed_amount_minor": item.signed_amount_minor,
            "stable_item_key": item.stable_item_key,
        }
        for item in sorted(items, key=lambda item: item.item_ref.int)
    ]
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _original_reconciliation_totals(
    items: tuple[OriginalReconciliationWorkflowItem, ...],
) -> tuple[int, int, int, int]:
    income_minor = sum(
        item.signed_amount_minor
        for item in items
        if item.flow_kind == OriginalReconciliationFlowKind.INCOME
    )
    expense_minor = sum(
        abs(item.signed_amount_minor)
        for item in items
        if item.flow_kind == OriginalReconciliationFlowKind.EXPENSE
    )
    current_net_minor = sum(
        item.signed_amount_minor
        for item in items
        if item.flow_kind == OriginalReconciliationFlowKind.CURRENT
    )
    net_minor = income_minor - expense_minor + current_net_minor
    if any(
        abs(value) > JSON_SAFE_INTEGER
        for value in (income_minor, expense_minor, current_net_minor, net_minor)
    ):
        raise ValueError("month totals exceed the supported range")
    return income_minor, expense_minor, current_net_minor, net_minor


def _original_reconciliation_close_receipt_ref(
    *,
    entity_ref: UUID,
    business_unit_ref: str,
    month: str,
    closed_revision: int,
    item_set_sha256: str,
) -> UUID:
    receipt_identity = json.dumps(
        {
            "business_unit_ref": business_unit_ref,
            "closed_revision": closed_revision,
            "entity_ref": str(entity_ref),
            "item_set_sha256": item_set_sha256,
            "month": month,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid5(_RECEIPT_NAMESPACE, receipt_identity)


def relink_original_reconciliation_evidence(
    state: OriginalReconciliationWorkflowMonth,
    *,
    item_ref: UUID,
    operation_id: UUID,
    actor_ref: str,
    command: OriginalReconciliationEvidenceRelinkCommand,
    changed_at: datetime,
) -> tuple[OriginalReconciliationWorkflowMonth, OriginalReconciliationEvidenceRelinkReceipt]:
    """Apply one bounded evidence-link change to an open month."""

    if state.close_receipt is not None:
        raise OriginalReconciliationWorkflowRejected("closed workflow months are immutable")
    if command.expected_month_revision != state.revision:
        raise OriginalReconciliationWorkflowRevisionConflict(
            "expected_month_revision does not match the current month"
        )
    try:
        item_index, current_item = next(
            (index, item)
            for index, item in enumerate(state.items)
            if item.item_ref == item_ref
        )
    except StopIteration as exc:
        raise OriginalReconciliationWorkflowRejected(
            "workflow item is not part of the requested month"
        ) from exc
    if command.expected_item_revision != current_item.revision:
        raise OriginalReconciliationWorkflowRevisionConflict(
            "expected_item_revision does not match the current item"
        )
    current_refs = set(current_item.evidence_refs)
    additions = set(command.add_evidence_refs)
    removals = set(command.remove_evidence_refs)
    if additions & current_refs:
        raise OriginalReconciliationWorkflowRejected("evidence addition is already linked")
    if not removals <= current_refs:
        raise OriginalReconciliationWorkflowRejected("evidence removal is not currently linked")
    resulting_refs = tuple(sorted((current_refs | additions) - removals, key=lambda ref: ref.int))
    updated_item = OriginalReconciliationWorkflowItem.model_validate(
        {
            **current_item.model_dump(mode="python"),
            "evidence_refs": resulting_refs,
            "revision": current_item.revision + 1,
        }
    )
    updated_items = list(state.items)
    updated_items[item_index] = updated_item
    updated_state = OriginalReconciliationWorkflowMonth.model_validate(
        {
            **state.model_dump(mode="python"),
            "items": tuple(updated_items),
            "revision": state.revision + 1,
        }
    )
    event = OriginalReconciliationEvidenceRelinkEvent(
        event_ref=uuid5(operation_id, "original-reconciliation:evidence-relink"),
        operation_id=operation_id,
        item_ref=item_ref,
        entity_ref=state.scope.entity_ref,
        business_unit_ref=state.scope.business_unit_ref,
        month=state.month,
        actor_ref=actor_ref,
        reason=command.reason,
        from_month_revision=state.revision,
        to_month_revision=updated_state.revision,
        from_item_revision=current_item.revision,
        to_item_revision=updated_item.revision,
        added_evidence_refs=tuple(sorted(additions, key=lambda ref: ref.int)),
        removed_evidence_refs=tuple(sorted(removals, key=lambda ref: ref.int)),
        resulting_evidence_refs=resulting_refs,
        changed_at=changed_at,
    )
    return updated_state, OriginalReconciliationEvidenceRelinkReceipt(
        operation_id=operation_id,
        replayed=False,
        event=event,
        item=updated_item,
    )


def close_original_reconciliation_month(
    state: OriginalReconciliationWorkflowMonth,
    *,
    operation_id: UUID,
    actor_ref: str,
    command: OriginalReconciliationMonthCloseCommand,
    closed_at: datetime,
) -> tuple[OriginalReconciliationWorkflowMonth, OriginalReconciliationMonthCloseCommandReceipt]:
    """Close the exact reviewed item set and bind it to an immutable digest."""

    if state.close_receipt is not None:
        raise OriginalReconciliationWorkflowRejected("workflow month is already closed")
    if command.expected_month_revision != state.revision:
        raise OriginalReconciliationWorkflowRevisionConflict(
            "expected_month_revision does not match the current month"
        )
    if not state.items:
        raise OriginalReconciliationWorkflowRejected("an empty workflow month cannot be closed")
    unconfirmed = [
        item.item_ref
        for item in state.items
        if item.review_status != OriginalReconciliationWorkflowReviewStatus.CONFIRMED
    ]
    if unconfirmed:
        raise OriginalReconciliationWorkflowRejected(
            "every original-reconciliation item must be confirmed before close"
        )
    missing_evidence = [item.item_ref for item in state.items if not item.evidence_refs]
    if missing_evidence:
        raise OriginalReconciliationWorkflowRejected(
            "every original-reconciliation item must have evidence before close"
        )

    try:
        income_minor, expense_minor, current_net_minor, net_minor = (
            _original_reconciliation_totals(state.items)
        )
    except ValueError as exc:
        raise OriginalReconciliationWorkflowRejected(str(exc)) from exc

    item_set_sha256 = original_reconciliation_item_set_sha256(state.items)
    closed_revision = state.revision + 1
    close_receipt = OriginalReconciliationMonthCloseReceipt(
        receipt_ref=_original_reconciliation_close_receipt_ref(
            entity_ref=state.scope.entity_ref,
            business_unit_ref=state.scope.business_unit_ref,
            month=state.month,
            closed_revision=closed_revision,
            item_set_sha256=item_set_sha256,
        ),
        operation_id=operation_id,
        entity_ref=state.scope.entity_ref,
        business_unit_ref=state.scope.business_unit_ref,
        month=state.month,
        closed_revision=closed_revision,
        item_set_sha256=item_set_sha256,
        item_count=len(state.items),
        income_minor=income_minor,
        expense_minor=expense_minor,
        current_net_minor=current_net_minor,
        net_minor=net_minor,
        actor_ref=actor_ref,
        reason=command.reason,
        closed_at=closed_at,
    )
    closed_state = OriginalReconciliationWorkflowMonth.model_validate(
        {
            **state.model_dump(mode="python"),
            "revision": closed_revision,
            "close_receipt": close_receipt,
        }
    )
    return closed_state, OriginalReconciliationMonthCloseCommandReceipt(
        operation_id=operation_id,
        replayed=False,
        close_receipt=close_receipt,
    )


class OriginalReconciliationWorkflowPort(Protocol):
    """Minimal interface the production persistence adapter must implement."""

    def get_month(
        self,
        *,
        entity_ref: UUID,
        business_unit_ref: str,
        month: str,
    ) -> OriginalReconciliationWorkflowMonth: ...

    def relink_evidence(
        self,
        *,
        entity_ref: UUID,
        business_unit_ref: str,
        month: str,
        item_ref: UUID,
        operation_id: UUID,
        actor_ref: str,
        command: OriginalReconciliationEvidenceRelinkCommand,
        changed_at: datetime,
    ) -> OriginalReconciliationEvidenceRelinkReceipt: ...

    def close_month(
        self,
        *,
        entity_ref: UUID,
        business_unit_ref: str,
        month: str,
        operation_id: UUID,
        actor_ref: str,
        command: OriginalReconciliationMonthCloseCommand,
        closed_at: datetime,
    ) -> OriginalReconciliationMonthCloseCommandReceipt: ...


WorkflowCommandReceipt = (
    OriginalReconciliationEvidenceRelinkReceipt
    | OriginalReconciliationMonthCloseCommandReceipt
)


class SyntheticOriginalReconciliationWorkflowService:
    """Process-local contract proof; never a production persistence substitute."""

    def __init__(
        self,
        months: tuple[OriginalReconciliationWorkflowMonth, ...] = (),
    ) -> None:
        self._lock = threading.RLock()
        self._months: dict[tuple[UUID, str, str], OriginalReconciliationWorkflowMonth] = {}
        self._receipts: dict[UUID, tuple[str, str, WorkflowCommandReceipt]] = {}
        for month in months:
            key = _month_key(month.scope.entity_ref, month.scope.business_unit_ref, month.month)
            if key in self._months:
                raise ValueError("synthetic workflow months must have unique scopes")
            self._months[key] = month

    def get_month(
        self,
        *,
        entity_ref: UUID,
        business_unit_ref: str,
        month: str,
    ) -> OriginalReconciliationWorkflowMonth:
        key = _month_key(entity_ref, business_unit_ref, month)
        with self._lock:
            state = self._months.get(key)
            if state is None:
                raise OriginalReconciliationWorkflowRejected("workflow month was not found")
            return state

    def relink_evidence(
        self,
        *,
        entity_ref: UUID,
        business_unit_ref: str,
        month: str,
        item_ref: UUID,
        operation_id: UUID,
        actor_ref: str,
        command: OriginalReconciliationEvidenceRelinkCommand,
        changed_at: datetime,
    ) -> OriginalReconciliationEvidenceRelinkReceipt:
        fingerprint = _command_fingerprint(
            "RELINK_EVIDENCE",
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
            month=month,
            item_ref=item_ref,
            command=command,
        )
        with self._lock:
            replay = self._replay(operation_id, fingerprint, actor_ref)
            if replay is not None:
                if not isinstance(replay, OriginalReconciliationEvidenceRelinkReceipt):
                    raise OriginalReconciliationWorkflowIdempotencyConflict(
                        "operation ID was reused for another workflow command"
                    )
                return replay.model_copy(update={"replayed": True})
            key = _month_key(entity_ref, business_unit_ref, month)
            state = self._months.get(key)
            if state is None:
                raise OriginalReconciliationWorkflowRejected("workflow month was not found")
            updated_state, receipt = relink_original_reconciliation_evidence(
                state,
                item_ref=item_ref,
                operation_id=operation_id,
                actor_ref=actor_ref,
                command=command,
                changed_at=changed_at,
            )
            self._months[key] = updated_state
            self._receipts[operation_id] = (fingerprint, actor_ref, receipt)
            return receipt

    def close_month(
        self,
        *,
        entity_ref: UUID,
        business_unit_ref: str,
        month: str,
        operation_id: UUID,
        actor_ref: str,
        command: OriginalReconciliationMonthCloseCommand,
        closed_at: datetime,
    ) -> OriginalReconciliationMonthCloseCommandReceipt:
        fingerprint = _command_fingerprint(
            "CLOSE_MONTH",
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
            month=month,
            item_ref=None,
            command=command,
        )
        with self._lock:
            replay = self._replay(operation_id, fingerprint, actor_ref)
            if replay is not None:
                if not isinstance(replay, OriginalReconciliationMonthCloseCommandReceipt):
                    raise OriginalReconciliationWorkflowIdempotencyConflict(
                        "operation ID was reused for another workflow command"
                    )
                return replay.model_copy(update={"replayed": True})
            key = _month_key(entity_ref, business_unit_ref, month)
            state = self._months.get(key)
            if state is None:
                raise OriginalReconciliationWorkflowRejected("workflow month was not found")
            closed_state, receipt = close_original_reconciliation_month(
                state,
                operation_id=operation_id,
                actor_ref=actor_ref,
                command=command,
                closed_at=closed_at,
            )
            self._months[key] = closed_state
            self._receipts[operation_id] = (fingerprint, actor_ref, receipt)
            return receipt

    def _replay(
        self,
        operation_id: UUID,
        fingerprint: str,
        actor_ref: str,
    ) -> WorkflowCommandReceipt | None:
        prior = self._receipts.get(operation_id)
        if prior is None:
            return None
        prior_fingerprint, prior_actor, receipt = prior
        if prior_fingerprint != fingerprint or prior_actor != actor_ref:
            raise OriginalReconciliationWorkflowIdempotencyConflict(
                "operation ID was reused with different content or actor"
            )
        return receipt


def _month_key(entity_ref: UUID, business_unit_ref: str, month: str) -> tuple[UUID, str, str]:
    return entity_ref, business_unit_ref, month


def _command_fingerprint(
    command_type: str,
    *,
    entity_ref: UUID,
    business_unit_ref: str,
    month: str,
    item_ref: UUID | None,
    command: OriginalReconciliationEvidenceRelinkCommand
    | OriginalReconciliationMonthCloseCommand,
) -> str:
    payload = {
        "business_unit_ref": business_unit_ref,
        "command": command.model_dump(mode="json"),
        "command_type": command_type,
        "entity_ref": str(entity_ref),
        "item_ref": str(item_ref) if item_ref is not None else None,
        "month": month,
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
