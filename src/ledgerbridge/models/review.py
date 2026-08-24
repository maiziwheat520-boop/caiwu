"""Persistent review, reconciliation, and Suspense state models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from ledgerbridge.db import Base


class ReviewItemKind(StrEnum):
    DEDUP = "DEDUP"
    RECONCILIATION = "RECONCILIATION"
    SUSPENSE = "SUSPENSE"


class ReviewItemStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"


class ReconciliationRelation(StrEnum):
    ONE_TO_ONE = "1:1"
    ONE_TO_MANY = "1:N"
    MANY_TO_ONE = "N:1"


class ReconciliationStatus(StrEnum):
    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class SuspenseReason(StrEnum):
    UNKNOWN_COUNTERPARTY = "UNKNOWN_COUNTERPARTY"
    UNMATCHED_TRANSFER = "UNMATCHED_TRANSFER"
    BALANCE_GAP = "BALANCE_GAP"
    LOAN_BREAKDOWN = "LOAN_BREAKDOWN"


class SuspenseStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class ReviewItem(Base):
    __tablename__ = "review_item"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('DEDUP', 'RECONCILIATION', 'SUSPENSE')",
            name="review_item_kind_allowed",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'RESOLVED', 'REJECTED')",
            name="review_item_status_allowed",
        ),
        CheckConstraint("btrim(summary) <> ''", name="review_item_summary_not_blank"),
        CheckConstraint(
            "candidate_key IS NULL OR candidate_key ~ '^[a-f0-9]{64}$'",
            name="review_item_candidate_key_shape",
        ),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="review_item_payload_object"),
        CheckConstraint(
            "(status = 'OPEN' AND decided_at IS NULL AND decision_actor IS NULL "
            "AND decision_reason IS NULL) OR "
            "(status IN ('RESOLVED', 'REJECTED') AND decided_at IS NOT NULL "
            "AND btrim(decision_actor) <> '' AND btrim(decision_reason) <> '')",
            name="review_item_decision_shape",
        ),
        Index("ix_review_item_status_created", "status", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'OPEN'"))
    source_record_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("source_record.id", ondelete="RESTRICT")
    )
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    candidate_key: Mapped[str | None] = mapped_column(String(64))
    audit_event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("audit_event.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_actor: Mapped[str | None] = mapped_column(String(200))
    decision_reason: Mapped[str | None] = mapped_column(String(1_000))


class ReconciliationGroup(Base):
    __tablename__ = "reconciliation_group"
    __table_args__ = (
        CheckConstraint(
            "relation IN ('1:1', '1:N', 'N:1')",
            name="reconciliation_group_relation_allowed",
        ),
        CheckConstraint(
            "status IN ('PROPOSED', 'CONFIRMED', 'REJECTED')",
            name="reconciliation_group_status_allowed",
        ),
        CheckConstraint("currency = 'CNY'", name="reconciliation_group_currency_cny_v01"),
        CheckConstraint(
            "(status = 'PROPOSED' AND decided_at IS NULL AND decision_actor IS NULL "
            "AND decision_reason IS NULL) OR "
            "(status IN ('CONFIRMED', 'REJECTED') AND decided_at IS NOT NULL "
            "AND btrim(decision_actor) <> '' AND btrim(decision_reason) <> '')",
            name="reconciliation_group_decision_shape",
        ),
        UniqueConstraint("review_item_id", name="uq_reconciliation_group_review_item"),
        Index("ix_reconciliation_group_status_created", "status", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    review_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("review_item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    relation: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'PROPOSED'")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'CNY'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_actor: Mapped[str | None] = mapped_column(String(200))
    decision_reason: Mapped[str | None] = mapped_column(String(1_000))


class ReconciliationLeg(Base):
    __tablename__ = "reconciliation_leg"
    __table_args__ = (
        CheckConstraint("amount_minor <> 0", name="reconciliation_leg_amount_nonzero"),
        CheckConstraint("currency = 'CNY'", name="reconciliation_leg_currency_cny_v01"),
        UniqueConstraint(
            "reconciliation_group_id",
            "source_record_id",
            name="uq_reconciliation_leg_group_source_record",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    reconciliation_group_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("reconciliation_group.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_record_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("source_record.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'CNY'"))


class SuspenseItem(Base):
    __tablename__ = "suspense_item"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('UNKNOWN_COUNTERPARTY', 'UNMATCHED_TRANSFER', 'BALANCE_GAP', "
            "'LOAN_BREAKDOWN')",
            name="suspense_item_reason_allowed",
        ),
        CheckConstraint("amount_minor <> 0", name="suspense_item_amount_nonzero"),
        CheckConstraint("currency = 'CNY'", name="suspense_item_currency_cny_v01"),
        CheckConstraint(
            "status IN ('OPEN', 'RESOLVED')",
            name="suspense_item_status_allowed",
        ),
        CheckConstraint(
            "(status = 'OPEN' AND resolved_at IS NULL AND resolution_account_id IS NULL "
            "AND resolution_actor IS NULL AND resolution_reason IS NULL) OR "
            "(status = 'RESOLVED' AND resolved_at IS NOT NULL "
            "AND resolution_account_id IS NOT NULL AND btrim(resolution_actor) <> '' "
            "AND btrim(resolution_reason) <> '')",
            name="suspense_item_resolution_shape",
        ),
        UniqueConstraint("review_item_id", name="uq_suspense_item_review_item"),
        Index("ix_suspense_item_status_created", "status", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    review_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("review_item.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_record_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("source_record.id", ondelete="RESTRICT")
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'CNY'"))
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    suspense_account_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'OPEN'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_account_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("account.id", ondelete="RESTRICT")
    )
    resolution_actor: Mapped[str | None] = mapped_column(String(200))
    resolution_reason: Mapped[str | None] = mapped_column(String(1_000))
