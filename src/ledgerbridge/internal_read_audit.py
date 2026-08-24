"""Append-only audit seam for synthetic internal evidence downloads.

R1 deliberately does not choose a durable audit backend.  A deployment must
inject a sink whose ``append`` operation is durable and append-only; the default
implementation fails closed so evidence bytes cannot be returned without an
audit record.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ledgerbridge.audit import append_audit_event
from ledgerbridge.config import Settings, get_settings
from ledgerbridge.db import get_session_factory


class EvidenceReadAuditEvent(BaseModel):
    """Allowlisted audit record; request headers and credentials are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_version: Literal["ledgerbridge.internal-read-audit.v1"] = (
        "ledgerbridge.internal-read-audit.v1"
    )
    event_type: Literal["EVIDENCE_CONTENT_READ"] = "EVIDENCE_CONTENT_READ"
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    principal_ref: str = Field(min_length=1, max_length=200)
    principal_san_uri: str = Field(pattern=r"^spiffe://ledgerbridge\.test/[a-z0-9/_-]+$")
    policy_generation: int = Field(ge=1)
    evidence_ref: UUID
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["SUCCEEDED"] = "SUCCEEDED"


class InternalReadAuditSink(Protocol):
    """Minimal append-only contract supplied by the deployment boundary."""

    def append(self, event: EvidenceReadAuditEvent) -> None:
        """Durably append ``event`` or raise without modifying existing events."""


class AuditSinkUnavailable(RuntimeError):
    """No durable append-only audit sink accepted the event."""


class EvidenceReadReceipt(BaseModel):
    """Typed payload for the database reader's immutable evidence receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID = Field(default_factory=uuid4)
    principal_ref: str = Field(min_length=1, max_length=200)
    principal_san_uri: str = Field(pattern=r"^spiffe://ledgerbridge\.test/[a-z0-9/_-]+$")
    key_generation: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    evidence_ref: UUID
    entity_ref: UUID
    business_unit_id: UUID
    blob_ref: UUID
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InternalReadReceiptSink(Protocol):
    """Durable append-only receipt contract for database-backed evidence."""

    def append(self, receipt: EvidenceReadReceipt) -> None:
        """Persist the receipt or raise without returning evidence bytes."""


class UnavailableInternalReadAuditSink:
    """Fail-closed default used until a reviewed durable sink is injected."""

    def append(self, event: EvidenceReadAuditEvent) -> None:
        _ = event
        raise AuditSinkUnavailable("internal read audit sink is unavailable")


class DatabaseInternalReadAuditSink:
    """Append evidence-read events through the existing database hash chain."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def append(self, event: EvidenceReadAuditEvent) -> None:
        try:
            with self._session_factory() as session:
                append_audit_event(
                    session,
                    actor=event.principal_ref,
                    action="internal.read.evidence.content",
                    reason="internal evidence content read",
                    rule_version=event.event_version,
                    payload={
                        "event_type": event.event_type,
                        "principal_san_uri": event.principal_san_uri,
                        "policy_generation": event.policy_generation,
                        "evidence_ref": str(event.evidence_ref),
                        "entity_ref": str(event.entity_ref),
                        "business_unit_ref": event.business_unit_ref,
                        "byte_size": event.byte_size,
                        "sha256": event.sha256,
                        "outcome": event.outcome,
                    },
                )
                session.commit()
        except Exception as exc:
            raise AuditSinkUnavailable("internal read audit append failed") from exc


class DatabaseInternalReadReceiptSink:
    """Append a database reader receipt through the allowlisted internal function."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def append(self, receipt: EvidenceReadReceipt) -> None:
        try:
            with self._session_factory() as session:
                session.execute(
                    text(
                        """
                        SELECT internal_read.append_internal_evidence_read_audit(
                            :operation_id, :principal_ref, :principal_san_uri,
                            :key_generation, :evidence_ref, :entity_ref,
                            :business_unit_id, :blob_ref, :byte_size, :sha256
                        )
                        """
                    ),
                    {
                        "operation_id": receipt.operation_id,
                        "principal_ref": receipt.principal_ref,
                        "principal_san_uri": receipt.principal_san_uri,
                        "key_generation": receipt.key_generation,
                        "evidence_ref": receipt.evidence_ref,
                        "entity_ref": receipt.entity_ref,
                        "business_unit_id": receipt.business_unit_id,
                        "blob_ref": receipt.blob_ref,
                        "byte_size": receipt.byte_size,
                        "sha256": bytes.fromhex(receipt.sha256),
                    },
                ).scalar_one()
                session.commit()
        except Exception as exc:
            raise AuditSinkUnavailable("internal read receipt append failed") from exc


def get_internal_read_audit_sink(
    settings: Annotated[Settings, Depends(get_settings)],
) -> InternalReadAuditSink:
    """Resolve the durable sink only for an explicit non-production test profile."""

    if settings.env == "production" or not settings.enable_internal_read_persistent_audit:
        return UnavailableInternalReadAuditSink()
    try:
        session_factory = get_session_factory(settings.resolved_api_database_url())
    except Exception:
        return UnavailableInternalReadAuditSink()
    return DatabaseInternalReadAuditSink(session_factory)
