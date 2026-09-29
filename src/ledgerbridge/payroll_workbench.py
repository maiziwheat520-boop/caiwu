"""Database-owned payroll workbench read Interface."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session


class PayrollWorkbenchNotFound(LookupError):
    """No payroll batch exists in the requested entity and period."""


MoneyMinor = Annotated[int, Field(strict=True, ge=0, le=(2**63) - 1)]


class PayrollWorkbenchLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    line_ref: UUID
    employee_ref: UUID
    employee_name: str
    employee_type: Literal["REGULAR", "TEMPORARY"]
    location: str
    job_group: str | None
    attendance_days: str | None
    payment_channel: Literal["MYBANK", "BOC", "WECHAT", "CASH"]
    payee_name: str | None
    account_masked: str | None
    memo: str
    net_amount_minor: MoneyMinor
    cash_amount_minor: MoneyMinor
    supplemental_amount_minor: MoneyMinor
    bank_amount_minor: MoneyMinor


class PayrollWorkbenchIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_code: str
    message: str
    line_ref: UUID | None
    resolved: bool


class PayrollWorkbenchView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["ledgerbridge.payroll-workbench.v1"] = (
        "ledgerbridge.payroll-workbench.v1"
    )
    entity_ref: UUID
    batch_ref: UUID
    batch_version_ref: UUID
    pay_period: str
    reconciliation_month: str
    revision: int
    status: Literal["DRAFT", "LOCKED", "SUPERSEDED"]
    rules_version: str
    content_sha256: str | None
    line_count: int
    net_amount_minor: MoneyMinor
    cash_amount_minor: MoneyMinor
    supplemental_amount_minor: MoneyMinor
    bank_amount_minor: MoneyMinor
    lines: tuple[PayrollWorkbenchLine, ...]
    issues: tuple[PayrollWorkbenchIssue, ...]


_HEADER_SQL = text(
    """
    SELECT batch.entity_ref, batch.batch_ref, version.batch_version_ref,
           batch.pay_period, version.reconciliation_month, version.revision,
           version.status, version.rules_version, encode(version.content_sha256, 'hex') AS digest
      FROM payroll.batch AS batch
      JOIN payroll.batch_version AS version ON version.batch_ref = batch.batch_ref
     WHERE batch.entity_ref = :entity_ref AND batch.pay_period = :pay_period
     ORDER BY version.revision DESC
     LIMIT 1
    """
)
_LINES_SQL = text(
    """
    SELECT line_ref, employee_ref, employee_name, employee_type, location, job_group,
           attendance_days, payment_channel, payee_name, account_masked, memo,
           net_amount_minor, cash_amount_minor, supplemental_amount_minor,
           bank_amount_minor
      FROM payroll.workbench_line
     WHERE batch_version_ref = :batch_version_ref
     ORDER BY location, employee_name, employee_ref
    """
)
_ISSUES_SQL = text(
    """
    SELECT issue_code, message, line_ref, resolved_at IS NOT NULL AS resolved
      FROM payroll.blocking_issue
     WHERE batch_version_ref = :batch_version_ref
     ORDER BY resolved_at NULLS FIRST, issue_code, issue_ref
    """
)


class DatabasePayrollWorkbench:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, *, entity_ref: UUID, pay_period: str) -> PayrollWorkbenchView:
        header = self._session.execute(
            _HEADER_SQL, {"entity_ref": entity_ref, "pay_period": pay_period}
        ).mappings().one_or_none()
        if header is None:
            raise PayrollWorkbenchNotFound
        version_ref = header["batch_version_ref"]
        lines = tuple(
            PayrollWorkbenchLine.model_validate(row)
            for row in self._session.execute(
                _LINES_SQL, {"batch_version_ref": version_ref}
            ).mappings()
        )
        issues = tuple(
            PayrollWorkbenchIssue.model_validate(row)
            for row in self._session.execute(
                _ISSUES_SQL, {"batch_version_ref": version_ref}
            ).mappings()
        )
        return PayrollWorkbenchView(
            entity_ref=header["entity_ref"],
            batch_ref=header["batch_ref"],
            batch_version_ref=version_ref,
            pay_period=header["pay_period"],
            reconciliation_month=header["reconciliation_month"],
            revision=header["revision"],
            status=header["status"],
            rules_version=header["rules_version"],
            content_sha256=header["digest"],
            line_count=len(lines),
            net_amount_minor=sum(item.net_amount_minor for item in lines),
            cash_amount_minor=sum(item.cash_amount_minor for item in lines),
            supplemental_amount_minor=sum(item.supplemental_amount_minor for item in lines),
            bank_amount_minor=sum(item.bank_amount_minor for item in lines),
            lines=lines,
            issues=issues,
        )
