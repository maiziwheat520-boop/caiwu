"""Database boundary for human Review, reconciliation, and Suspense decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ledgerbridge.audit import append_audit_event
from ledgerbridge.models import (
    ReconciliationGroup,
    ReviewItem,
    ReviewItemKind,
    SuspenseItem,
)
from ledgerbridge.text import contains_unstorable_text

ReviewDecision = Literal["RESOLVED", "REJECTED"]


class ReviewServiceError(RuntimeError):
    """Base class for Review service failures."""


class ReviewNotFound(ReviewServiceError):
    """The requested Review item does not exist."""


class ReviewConflict(ReviewServiceError):
    """The requested decision is not valid for the current state."""


class ReviewService:
    """Perform Review reads and explicit decisions in one database transaction."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def list_items(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> tuple[ReviewItem, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("review limit must be between 1 and 500")
        with self._sessions() as session:
            query = select(ReviewItem).order_by(ReviewItem.created_at.asc(), ReviewItem.id.asc())
            if status is not None:
                query = query.where(ReviewItem.status == status)
            if kind is not None:
                query = query.where(ReviewItem.kind == kind)
            return tuple(session.scalars(query.limit(limit)).all())

    def get(self, review_id: UUID) -> ReviewItem:
        with self._sessions() as session:
            item = session.get(ReviewItem, review_id)
            if item is None:
                raise ReviewNotFound("review item was not found")
            return item

    def decide(
        self,
        review_id: UUID,
        *,
        actor: str,
        decision: ReviewDecision,
        reason: str,
        resolution_account_id: UUID | None = None,
    ) -> ReviewItem:
        _validate_text("actor", actor, 200)
        _validate_text("reason", reason, 1_000)
        if decision not in {"RESOLVED", "REJECTED"}:
            raise ValueError("unsupported Review decision")
        with self._sessions() as session, session.begin():
            item = session.scalar(
                select(ReviewItem).where(ReviewItem.id == review_id).with_for_update()
            )
            if item is None:
                raise ReviewNotFound("review item was not found")
            if item.status != "OPEN":
                raise ReviewConflict("Review item is already terminal")
            if item.kind == ReviewItemKind.SUSPENSE and decision == "REJECTED":
                raise ReviewConflict("Suspense items require an explicit resolution account")
            if item.kind == ReviewItemKind.SUSPENSE and resolution_account_id is None:
                raise ReviewConflict("Suspense resolution requires a target account")

            audit_id = append_audit_event(
                session,
                actor=actor,
                action=f"review.{decision.lower()}",
                reason=reason,
                payload={
                    "review_item_id": str(review_id),
                    "kind": item.kind,
                    "resolution_account_id": (
                        str(resolution_account_id) if resolution_account_id is not None else None
                    ),
                },
            )
            item.status = decision
            item.decided_at = datetime.now(UTC)
            item.decision_actor = actor
            item.decision_reason = reason

            if item.kind == ReviewItemKind.RECONCILIATION:
                group = session.scalar(
                    select(ReconciliationGroup)
                    .where(ReconciliationGroup.review_item_id == item.id)
                    .with_for_update()
                )
                if group is None:
                    raise ReviewConflict("reconciliation group is missing")
                group.status = "CONFIRMED" if decision == "RESOLVED" else "REJECTED"
                group.decided_at = item.decided_at
                group.decision_actor = actor
                group.decision_reason = reason
            elif item.kind == ReviewItemKind.SUSPENSE:
                suspense = session.scalar(
                    select(SuspenseItem)
                    .where(SuspenseItem.review_item_id == item.id)
                    .with_for_update()
                )
                if suspense is None:
                    raise ReviewConflict("Suspense item is missing")
                suspense.status = "RESOLVED"
                suspense.resolved_at = item.decided_at
                suspense.resolution_account_id = resolution_account_id
                suspense.resolution_actor = actor
                suspense.resolution_reason = reason

            # The decision audit event is intentionally separate from the immutable
            # creation event referenced by review_item.audit_event_id.
            _ = audit_id
            session.flush()
            return item

    def create_review_item(
        self,
        *,
        kind: ReviewItemKind,
        summary: str,
        payload: dict[str, Any],
        actor: str,
        reason: str,
        source_record_id: UUID | None = None,
    ) -> UUID:
        _validate_text("summary", summary, 500)
        _validate_text("actor", actor, 200)
        _validate_text("reason", reason, 1_000)
        if not isinstance(payload, dict):
            raise ValueError("review payload must be an object")
        with self._sessions() as session, session.begin():
            audit_id = append_audit_event(
                session,
                actor=actor,
                action="review.open",
                reason=reason,
                payload={"kind": kind.value, "summary": summary, **payload},
            )
            item = ReviewItem(
                kind=kind.value,
                summary=summary,
                payload=payload,
                source_record_id=source_record_id,
                audit_event_id=audit_id,
            )
            session.add(item)
            session.flush()
            return item.id

    def create_suspense(
        self,
        *,
        summary: str,
        payload: dict[str, Any],
        amount_minor: int,
        reason: str,
        suspense_reason: str,
        suspense_account_id: UUID,
        actor: str,
        source_record_id: UUID | None = None,
    ) -> UUID:
        """Create a Review item and its open Suspense row atomically for the worker."""

        _validate_text("suspense_reason", suspense_reason, 32)
        _validate_text("reason", reason, 1_000)
        _validate_text("actor", actor, 200)
        if amount_minor == 0 or isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise ValueError("amount_minor must be a non-zero integer")
        with self._sessions() as session, session.begin():
            audit_id = append_audit_event(
                session,
                actor=actor,
                action="review.suspense.open",
                reason=reason,
                payload={"summary": summary, "amount_minor": amount_minor, **payload},
            )
            item = ReviewItem(
                kind=ReviewItemKind.SUSPENSE.value,
                summary=summary,
                payload=payload,
                source_record_id=source_record_id,
                audit_event_id=audit_id,
            )
            session.add(item)
            session.flush()
            session.add(
                SuspenseItem(
                    review_item_id=item.id,
                    source_record_id=source_record_id,
                    amount_minor=amount_minor,
                    reason=suspense_reason,
                    suspense_account_id=suspense_account_id,
                )
            )
            session.flush()
            return item.id


def _validate_text(field: str, value: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or contains_unstorable_text(value)
    ):
        raise ValueError(f"{field} is invalid")
