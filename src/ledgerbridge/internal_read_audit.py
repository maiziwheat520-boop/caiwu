"""Append-only audit seam for synthetic internal evidence downloads.

R1 deliberately does not choose a durable audit backend.  A deployment must
inject a sink whose ``append`` operation is durable and append-only; the default
implementation fails closed so evidence bytes cannot be returned without an
audit record.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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


class UnavailableInternalReadAuditSink:
    """Fail-closed default used until a reviewed durable sink is injected."""

    def append(self, event: EvidenceReadAuditEvent) -> None:
        _ = event
        raise AuditSinkUnavailable("internal read audit sink is unavailable")


def get_internal_read_audit_sink() -> InternalReadAuditSink:
    """FastAPI dependency seam; evidence reads must override this in R1 tests/runtime."""

    return UnavailableInternalReadAuditSink()
