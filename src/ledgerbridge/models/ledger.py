"""Core double-entry ledger models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from ledgerbridge.db import Base


class EntityType(StrEnum):
    PERSON = "PERSON"
    COMPANY = "COMPANY"


class AccountClass(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"
    EQUITY = "EQUITY"
    SUSPENSE = "SUSPENSE"


class JournalStatus(StrEnum):
    DRAFT = "DRAFT"
    POSTED = "POSTED"
    REVERSED = "REVERSED"


class Entity(Base):
    __tablename__ = "entity"
    __table_args__ = (CheckConstraint("btrim(name) <> ''", name="entity_name_not_blank"),)

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    entity_type: Mapped[EntityType] = mapped_column(
        Enum(EntityType, name="entity_type"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class Account(Base):
    __tablename__ = "account"
    __table_args__ = (
        CheckConstraint("btrim(identifier) <> ''", name="account_identifier_not_blank"),
        CheckConstraint("btrim(name) <> ''", name="account_name_not_blank"),
        UniqueConstraint("entity_id", "identifier", name="uq_account_entity_identifier"),
        UniqueConstraint("id", "entity_id", name="uq_account_id_entity"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    entity_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("entity.id", ondelete="RESTRICT"), nullable=False
    )
    identifier: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    account_class: Mapped[AccountClass] = mapped_column(
        Enum(AccountClass, name="account_class"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(always=False), nullable=False, unique=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[str | None] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    event_hash: Mapped[bytes] = mapped_column("hash", LargeBinary, nullable=False)


class JournalEntry(Base):
    __tablename__ = "journal_entry"
    __table_args__ = (
        CheckConstraint("btrim(origin) <> ''", name="journal_entry_origin_not_blank"),
        CheckConstraint(
            "adjusts_entry_id IS NULL OR adjusts_entry_id <> id",
            name="journal_entry_adjusts_not_self",
        ),
        CheckConstraint(
            "reverses_entry_id IS NULL OR reverses_entry_id <> id",
            name="journal_entry_reverses_not_self",
        ),
        CheckConstraint(
            "(CASE WHEN adjusts_entry_id IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN reverses_entry_id IS NULL THEN 0 ELSE 1 END) <= 1",
            name="journal_entry_one_correction_relation",
        ),
        ForeignKeyConstraint(
            ["primary_account_id", "entity_id"],
            ["account.id", "account.entity_id"],
            name="fk_journal_entry_primary_account_entity",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    entity_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("entity.id", ondelete="RESTRICT"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    origin: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[JournalStatus] = mapped_column(
        Enum(JournalStatus, name="journal_status"), nullable=False
    )
    source_record_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    adjusts_entry_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("journal_entry.id", ondelete="RESTRICT")
    )
    reverses_entry_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("journal_entry.id", ondelete="RESTRICT")
    )
    primary_account_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    audit_event_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("audit_event.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )


class Posting(Base):
    __tablename__ = "posting"
    __table_args__ = (CheckConstraint("currency = 'CNY'", name="posting_currency_cny_v01"),)

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    entry_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("journal_entry.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'CNY'"))
