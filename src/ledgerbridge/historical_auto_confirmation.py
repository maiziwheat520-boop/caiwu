"""Default-disabled historical Candidate confirmation for a disposable test period.

This module advances only the existing Candidate revision/event state machine. It
does not create JournalEntry or Posting rows and never invokes the POSTED ledger path.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

TEST_HISTORICAL_AUTO_IMPORT_ACTOR = "system:test-historical-auto-import"
_TEST_HISTORICAL_WORKLOAD = "workload:test-historical-auto-import"
_TEST_HISTORICAL_SAN = "spiffe://ledgerbridge.local/test-historical-auto-import"
_TEST_HISTORICAL_REASON = "test historical auto-import; disposable test summary only"
_OPERATION_NAMESPACE = UUID("7d07f17e-ce82-4a11-8cce-2d87b1220ee6")


class HistoricalAutoConfirmationSettings(BaseSettings):
    """Operator-owned gate for the disposable historical test workspace."""

    model_config = SettingsConfigDict(
        env_prefix="LEDGERBRIDGE_TEST_HISTORICAL_AUTO_IMPORT_",
        env_file=".env",
        extra="ignore",
    )

    enabled: bool = False
    cutoff_month: str = Field(default="2026-08", pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")

    @field_validator("cutoff_month")
    @classmethod
    def preserves_manual_review_from_september_2026(cls, value: str) -> str:
        if value > "2026-08":
            raise ValueError("historical cutoff must not include 2026-09 or later")
        return value


class HistoricalAutoConfirmationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool
    cutoff_month: str
    selected_count: int = Field(ge=0)
    confirmed_count: int = Field(ge=0)


class _CandidateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_id: UUID
    entity_id: UUID
    revision: int = Field(ge=1)
    status: Literal[
        "INCOMPLETE",
        "CONFLICTED",
        "PENDING",
        "CONFIRMED",
        "IGNORED",
        "SUPERSEDED",
    ]
    business_unit_id: UUID | None
    category_id: UUID | None
    amount_minor: int | None
    accounting_month: date | None


def confirm_test_historical_candidates(
    connection: Connection,
    settings: HistoricalAutoConfirmationSettings,
    *,
    candidate_refs: Iterable[UUID] | None = None,
    decided_at: datetime | None = None,
) -> HistoricalAutoConfirmationResult:
    """Confirm eligible historical Candidates through the existing command semantics."""

    if not settings.enabled:
        return HistoricalAutoConfirmationResult(
            enabled=False,
            cutoff_month=settings.cutoff_month,
            selected_count=0,
            confirmed_count=0,
        )

    effective_decided_at = decided_at or datetime.now(UTC)
    if effective_decided_at.utcoffset() is None:
        raise ValueError("decided_at must include a timezone")
    cutoff = date.fromisoformat(f"{settings.cutoff_month}-01")
    scoped_candidate_ids = (
        None
        if candidate_refs is None
        else tuple(sorted(set(candidate_refs), key=lambda item: item.int))
    )
    if scoped_candidate_ids == ():
        return HistoricalAutoConfirmationResult(
            enabled=True,
            cutoff_month=settings.cutoff_month,
            selected_count=0,
            confirmed_count=0,
        )
    scope_sql = "" if scoped_candidate_ids is None else "WHERE c.id = ANY(:candidate_ids) "
    query_params: dict[str, object] = {}
    if scoped_candidate_ids is not None:
        query_params["candidate_ids"] = list(scoped_candidate_ids)
    rows = (
        connection.execute(
            text(
                "SELECT c.id AS candidate_id, c.entity_id, latest.revision, "
                "latest.status, latest.business_unit_id, latest.category_id, "
                "latest.amount_minor, latest.accounting_month "
                "FROM public.candidate AS c JOIN LATERAL ("
                "SELECT cr.revision, cr.status, cr.business_unit_id, cr.category_id, "
                "cr.amount_minor, cr.accounting_month FROM public.candidate_revision AS cr "
                "WHERE cr.candidate_id = c.id "
                "ORDER BY cr.revision DESC LIMIT 1"
                ") AS latest ON true "
                f"{scope_sql}"
                "ORDER BY c.id"
            ),
            query_params,
        )
        .mappings()
        .all()
    )
    candidates = tuple(_CandidateSnapshot.model_validate(dict(row)) for row in rows)
    eligible: list[_CandidateSnapshot] = []
    for candidate in candidates:
        if scoped_candidate_ids is not None and candidate.candidate_id not in scoped_candidate_ids:
            continue
        if candidate.status != "PENDING":
            continue
        if candidate.accounting_month is None:
            continue
        if candidate.accounting_month > cutoff:
            continue
        if (
            candidate.business_unit_id is None
            or candidate.category_id is None
            or candidate.amount_minor is None
        ):
            continue
        eligible.append(candidate)

    command_sql = text(
        "SELECT internal_command.apply_candidate_decision("
        "CAST(:operation_id AS uuid), CAST(:assertion_jti AS uuid), "
        "CAST(:candidate_id AS uuid), CAST(:actor_ref AS varchar(200)), "
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
    for candidate in eligible:
        operation_id = uuid5(
            _OPERATION_NAMESPACE,
            f"test-historical-auto-import:v1:{candidate.candidate_id}",
        )
        assertion_jti = uuid5(operation_id, "candidate-confirmation-assertion:v1")
        connection.execute(
            command_sql,
            {
                "operation_id": operation_id,
                "assertion_jti": assertion_jti,
                "candidate_id": candidate.candidate_id,
                "actor_ref": TEST_HISTORICAL_AUTO_IMPORT_ACTOR,
                "workload_principal_ref": _TEST_HISTORICAL_WORKLOAD,
                "verified_san": _TEST_HISTORICAL_SAN,
                "authorized_entity_id": candidate.entity_id,
                "current_business_unit_id": candidate.business_unit_id,
                "target_business_unit_id": candidate.business_unit_id,
                "decision": "CONFIRM",
                "expected_revision": candidate.revision,
                "reason": _TEST_HISTORICAL_REASON,
                "set_business_unit": False,
                "business_unit_ref": None,
                "set_category": False,
                "category_code": None,
                "set_amount": False,
                "amount_minor": None,
                "set_month": False,
                "accounting_month": None,
                "conflict_resolution": None,
                "decided_at": effective_decided_at,
            },
        ).mappings().one()
    return HistoricalAutoConfirmationResult(
        enabled=True,
        cutoff_month=settings.cutoff_month,
        selected_count=len(eligible),
        confirmed_count=len(eligible),
    )


def confirm_existing_test_historical_candidates(
    engine: Engine,
    settings: HistoricalAutoConfirmationSettings,
    *,
    decided_at: datetime | None = None,
) -> HistoricalAutoConfirmationResult:
    """Run the idempotent stock-candidate one-shot in one transaction."""

    with engine.begin() as connection:
        return confirm_test_historical_candidates(
            connection,
            settings,
            decided_at=decided_at,
        )
