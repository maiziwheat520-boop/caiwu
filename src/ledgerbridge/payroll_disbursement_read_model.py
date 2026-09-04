"""Read-only payroll projection over persisted, classified disbursement facts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.internal_candidate_command import CandidateCommandUnavailable
from ledgerbridge.internal_read_contract import Capability, WorkloadPrincipal, require_capability


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PayrollDisbursementSourceRecord(_FrozenModel):
    record_ref: UUID
    entity_ref: UUID
    company_name: str = Field(min_length=1, max_length=200)
    pay_period: str = Field(pattern=r"^20\d{2}-(0[1-9]|1[0-2])$")
    occurred_at: datetime
    actual_amount_minor: int = Field(strict=True, ge=0)
    direction: Literal["OUTFLOW", "INFLOW", "ZERO"]
    currency: Literal["CNY"] = "CNY"
    source_channel: Literal["MYBANK", "BOC", "BANK"]
    source_system: str = Field(min_length=1, max_length=64)
    source_artifact_ref: UUID
    source_statement_ref: UUID
    source_row_number: int = Field(strict=True, ge=1)
    ingested_at: datetime
    managed_account_ref: UUID
    disbursement_account_masked: str = Field(pattern=r"^\*{4}\d{4,8}$")
    counterparty_name: str | None = Field(default=None, max_length=300)
    counterparty_account_masked: str | None = Field(
        default=None,
        pattern=r"^\*{4}\d{4}$",
    )
    transaction_name: str = Field(min_length=1, max_length=300)
    classification_revision: int = Field(strict=True, ge=1)
    classification_source: Literal["AUTO_RULE", "HUMAN_REVIEW", "BACKFILL"]
    classification_rule_version: str = Field(min_length=1, max_length=100)
    period_assignment_source: Literal["NEXT_MONTH_RULE"] = "NEXT_MONTH_RULE"
    period_assignment_rule_version: Literal[
        "payroll-next-month-disbursement.2026-09.v1"
    ] = "payroll-next-month-disbursement.2026-09.v1"
    parse_status: Literal["PARSED"] = "PARSED"
    link_status: Literal["UNMATCHED", "UNSUPPORTED_DIRECTION"]
    payable: Literal[False] = False
    submission_supported: Literal[False] = False


class PayrollDisbursementRecordPage(_FrozenModel):
    schema_version: Literal["ledgerbridge.payroll-disbursement-records.v1"] = (
        "ledgerbridge.payroll-disbursement-records.v1"
    )
    pay_period: str = Field(pattern=r"^20\d{2}-(0[1-9]|1[0-2])$")
    source_artifact_count: int = Field(strict=True, ge=0)
    record_count: int = Field(strict=True, ge=0)
    unmatched_count: int = Field(strict=True, ge=0)
    records: tuple[PayrollDisbursementSourceRecord, ...] = Field(max_length=500)
    payable: Literal[False] = False
    submission_supported: Literal[False] = False


class DatabasePayrollDisbursementReadModel:
    def __init__(self, reader_factory: Callable[[], Session]) -> None:
        self._reader_factory = reader_factory

    @staticmethod
    def _require_access(principal: WorkloadPrincipal) -> None:
        require_capability(principal, Capability.PAYROLL_LIVE_READ)

    @staticmethod
    def _audit_horizon(session: Session) -> tuple[int, bytes]:
        row = (
            session.execute(
                text("SELECT sequence, hash FROM internal_read.current_audit_horizon()")
            )
            .mappings()
            .first()
        )
        if row is None:
            raise CandidateCommandUnavailable("audit horizon is unavailable")
        sequence = int(row["sequence"])
        horizon_hash = bytes(row["hash"])
        if sequence < 1 or len(horizon_hash) != 32:
            raise CandidateCommandUnavailable("audit horizon is invalid")
        return sequence, horizon_hash

    def list_for_period(
        self,
        principal: WorkloadPrincipal,
        *,
        pay_period: str,
        source_entity_refs: tuple[UUID, ...],
    ) -> PayrollDisbursementRecordPage:
        self._require_access(principal)
        if not source_entity_refs or len(set(source_entity_refs)) != len(source_entity_refs):
            raise CandidateCommandUnavailable("payroll disbursement scope is unavailable")
        try:
            with self._reader_factory() as session:
                sequence, horizon_hash = self._audit_horizon(session)
                parsed: list[PayrollDisbursementSourceRecord] = []
                for entity_ref in source_entity_refs:
                    rows = session.execute(
                        text(
                            "SELECT item FROM "
                            "internal_read.list_payroll_disbursement_records_as_of("
                            ":entity_ref, :pay_period, :sequence, :horizon_hash, 500)"
                        ),
                        {
                            "entity_ref": entity_ref,
                            "pay_period": pay_period,
                            "sequence": sequence,
                            "horizon_hash": horizon_hash,
                        },
                    ).mappings()
                    parsed.extend(
                        PayrollDisbursementSourceRecord.model_validate(row["item"])
                        for row in rows
                    )
        except (SQLAlchemyError, TypeError, ValueError, KeyError) as exc:
            raise CandidateCommandUnavailable(
                "payroll disbursement records are unavailable"
            ) from exc
        if len(parsed) > 500:
            raise CandidateCommandUnavailable("payroll disbursement result is too large")
        records = tuple(sorted(parsed, key=lambda item: (item.occurred_at, item.record_ref.int)))
        allowed_entities = frozenset(source_entity_refs)
        if any(
            record.entity_ref not in allowed_entities or record.pay_period != pay_period
            for record in records
        ):
            raise CandidateCommandUnavailable("payroll disbursement scope is invalid")
        return PayrollDisbursementRecordPage(
            pay_period=pay_period,
            source_artifact_count=len({record.source_artifact_ref for record in records}),
            record_count=len(records),
            unmatched_count=len(records),
            records=records,
        )
