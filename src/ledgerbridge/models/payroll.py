"""Database models for the versioned payroll workbench."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from ledgerbridge.db import Base


class PayrollEmployee(Base):
    __tablename__ = "employee"
    __table_args__ = ({"schema": "payroll"},)

    employee_ref: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    entity_ref: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    employee_code: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    employee_type: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PayrollPayeeAccount(Base):
    __tablename__ = "payee_account"
    __table_args__ = ({"schema": "payroll"},)

    payee_account_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    entity_ref: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    employee_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("payroll.employee.employee_ref"), nullable=False
    )
    payee_name: Mapped[str] = mapped_column(Text, nullable=False)
    account_number: Mapped[str] = mapped_column(Text, nullable=False)
    account_suffix: Mapped[str] = mapped_column(
        Text, Computed("right(account_number, 4)"), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PayrollBatch(Base):
    __tablename__ = "batch"
    __table_args__ = ({"schema": "payroll"},)

    batch_ref: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    entity_ref: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    company_id: Mapped[str] = mapped_column(Text, nullable=False)
    pay_period: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)


class PayrollFixedRemittance(Base):
    __tablename__ = "fixed_remittance_snapshot"
    __table_args__ = ({"schema": "payroll"},)

    fixed_remittance_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    batch_version_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("payroll.batch_version.batch_version_ref"),
        nullable=False,
    )
    payee_name: Mapped[str] = mapped_column(Text, nullable=False)
    account_number: Mapped[str] = mapped_column(Text, nullable=False)
    account_suffix: Mapped[str] = mapped_column(
        Text, Computed("right(account_number, 4)"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    memo: Mapped[str] = mapped_column(Text, nullable=False)


class PayrollBatchVersion(Base):
    __tablename__ = "batch_version"
    __table_args__ = ({"schema": "payroll"},)

    batch_version_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    batch_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("payroll.batch.batch_ref"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    rules_version: Mapped[str] = mapped_column(Text, nullable=False)
    reconciliation_month: Mapped[str] = mapped_column(Text, nullable=False)
    source_artifact_ref: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    content_sha256: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)


class PayrollLine(Base):
    __tablename__ = "line"
    __table_args__ = ({"schema": "payroll"},)

    line_ref: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    batch_version_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("payroll.batch_version.batch_version_ref"),
        nullable=False,
    )
    employee_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("payroll.employee.employee_ref"), nullable=False
    )
    employee_name: Mapped[str] = mapped_column(Text, nullable=False)
    employee_type: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False)
    job_group: Mapped[str | None] = mapped_column(Text)
    attendance_days: Mapped[str | None] = mapped_column(Text)
    payment_channel: Mapped[str] = mapped_column(Text, nullable=False)
    payee_account_ref: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("payroll.payee_account.payee_account_ref")
    )
    payee_name: Mapped[str | None] = mapped_column(Text)
    account_number: Mapped[str | None] = mapped_column(Text)
    memo: Mapped[str] = mapped_column(Text, nullable=False)
    net_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cash_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    supplemental_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bank_amount_minor: Mapped[int] = mapped_column(
        BigInteger, Computed("net_amount_minor - cash_amount_minor"), nullable=False
    )


class PayrollComponent(Base):
    __tablename__ = "component"
    __table_args__ = ({"schema": "payroll"},)

    component_ref: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    line_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("payroll.line.line_ref"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    component_code: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class PayrollBlockingIssue(Base):
    __tablename__ = "blocking_issue"
    __table_args__ = ({"schema": "payroll"},)

    issue_ref: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    batch_version_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("payroll.batch_version.batch_version_ref"),
        nullable=False,
    )
    line_ref: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    issue_code: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)


class PayrollExportReceipt(Base):
    __tablename__ = "export_receipt"
    __table_args__ = ({"schema": "payroll"},)

    export_receipt_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    batch_version_ref: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("payroll.batch_version.batch_version_ref"),
        nullable=False,
    )
    export_group: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    exported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exported_by: Mapped[str] = mapped_column(Text, nullable=False)


class PayrollCommandReceipt(Base):
    __tablename__ = "command_receipt"
    __table_args__ = ({"schema": "payroll"},)

    operation_ref: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    entity_ref: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    command_kind: Mapped[str] = mapped_column(Text, nullable=False)
    request_sha256: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    result_ref: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    actor_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
