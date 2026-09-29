"""PostgreSQL Adapter for atomically persisting and locking validated payroll drafts."""

from __future__ import annotations

from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from ledgerbridge.payroll_module import (
    EmployeeType,
    FixedRemittance,
    LockedPayrollVersion,
    PaymentChannel,
    PayrollComponent,
    PayrollDraft,
    PayrollDraftLine,
    lock_draft,
    normalize_account,
)


class PayrollStoreConflict(RuntimeError):
    """An idempotency key or payroll revision was reused for different content."""


class DatabasePayrollStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def load_locked(self, *, entity_ref: UUID, version_ref: UUID) -> LockedPayrollVersion:
        header = self._session.execute(
            text(
                """
                SELECT batch.company_id, batch.pay_period, version.revision,
                       version.rules_version, version.content_sha256
                  FROM payroll.batch_version AS version
                  JOIN payroll.batch AS batch ON batch.batch_ref = version.batch_ref
                 WHERE batch.entity_ref = :entity_ref
                   AND version.batch_version_ref = :version_ref
                   AND version.status = 'LOCKED'
                """
            ),
            {"entity_ref": entity_ref, "version_ref": version_ref},
        ).mappings().one_or_none()
        if header is None:
            raise PayrollStoreConflict("locked payroll version is unavailable")
        line_rows = self._session.execute(
            text(
                """
                SELECT line.line_ref, employee.employee_code, line.employee_name,
                       line.employee_type, line.payment_channel, line.location,
                       line.job_group, line.attendance_days, line.payee_name,
                       line.account_number, line.cash_amount_minor,
                       line.supplemental_amount_minor, line.memo
                  FROM payroll.line AS line
                  JOIN payroll.employee AS employee ON employee.employee_ref = line.employee_ref
                 WHERE line.batch_version_ref = :version_ref
                 ORDER BY employee.employee_code
                """
            ),
            {"version_ref": version_ref},
        ).mappings().all()
        lines: list[PayrollDraftLine] = []
        for row in line_rows:
            components = tuple(
                PayrollComponent(
                    code=item["component_code"],
                    amount_minor=item["amount_minor"],
                    reason=item["note"] or "",
                )
                for item in self._session.execute(
                    text(
                        """
                        SELECT component_code, amount_minor, note
                          FROM payroll.component
                         WHERE line_ref = :line_ref ORDER BY ordinal
                        """
                    ),
                    {"line_ref": row["line_ref"]},
                ).mappings()
            )
            lines.append(
                PayrollDraftLine(
                    employee_id=row["employee_code"],
                    employee_name=row["employee_name"],
                    payee_name=row["payee_name"] or "",
                    account_number=row["account_number"] or "",
                    employee_type=EmployeeType(row["employee_type"]),
                    payment_channel=PaymentChannel(row["payment_channel"]),
                    location=row["location"],
                    job_group=row["job_group"] or "",
                    attendance_days=row["attendance_days"] or "",
                    components=components,
                    cash_amount_minor=row["cash_amount_minor"],
                    supplemental_amount_minor=row["supplemental_amount_minor"],
                    memo=row["memo"],
                )
            )
        fixed = tuple(
            FixedRemittance(
                payee_name=row["payee_name"],
                account_number=row["account_number"],
                amount_minor=row["amount_minor"],
                memo=row["memo"],
            )
            for row in self._session.execute(
                text(
                    """
                    SELECT payee_name, account_number, amount_minor, memo
                      FROM payroll.fixed_remittance_snapshot
                     WHERE batch_version_ref = :version_ref
                     ORDER BY payee_name, account_number
                    """
                ),
                {"version_ref": version_ref},
            ).mappings()
        )
        draft = PayrollDraft(
            company_id=header["company_id"],
            period=header["pay_period"],
            revision=header["revision"],
            rules_version=header["rules_version"],
            lines=tuple(lines),
            fixed_remittances=fixed,
        )
        locked = lock_draft(draft)
        if bytes.fromhex(locked.content_sha256) != header["content_sha256"]:
            raise PayrollStoreConflict("locked payroll data differs from its content digest")
        return locked

    def persist_locked(
        self,
        *,
        entity_ref: UUID,
        operation_ref: UUID,
        actor_ref: str,
        locked: LockedPayrollVersion,
    ) -> UUID:
        digest = bytes.fromhex(locked.content_sha256)
        prior = self._session.execute(
            text(
                "SELECT request_sha256, result_ref FROM payroll.command_receipt "
                "WHERE operation_ref = :operation_ref"
            ),
            {"operation_ref": operation_ref},
        ).mappings().one_or_none()
        if prior is not None:
            if prior["request_sha256"] != digest:
                raise PayrollStoreConflict("operation_ref was reused for different payroll data")
            return cast(UUID, prior["result_ref"])

        self._session.execute(
            text(
                """
                INSERT INTO payroll.batch(entity_ref, company_id, pay_period, created_by)
                VALUES (:entity_ref, :company_id, :pay_period, :actor_ref)
                ON CONFLICT (entity_ref, pay_period) DO NOTHING
                """
            ),
            {
                "entity_ref": entity_ref,
                "company_id": locked.company_id,
                "pay_period": locked.period,
                "actor_ref": actor_ref,
            },
        )
        batch_ref = self._session.execute(
            text(
                "SELECT batch_ref, company_id FROM payroll.batch "
                "WHERE entity_ref = :entity_ref AND pay_period = :pay_period FOR UPDATE"
            ),
            {"entity_ref": entity_ref, "pay_period": locked.period},
        ).mappings().one()
        if batch_ref["company_id"] != locked.company_id:
            raise PayrollStoreConflict("payroll company identifier does not match entity")
        batch_ref = batch_ref["batch_ref"]
        existing = self._session.execute(
            text(
                """
                SELECT batch_version_ref, content_sha256
                  FROM payroll.batch_version
                 WHERE batch_ref = :batch_ref AND revision = :revision
                """
            ),
            {"batch_ref": batch_ref, "revision": locked.revision},
        ).mappings().one_or_none()
        if existing is not None:
            if existing["content_sha256"] != digest:
                raise PayrollStoreConflict("payroll revision already contains different content")
            self._insert_command_receipt(
                operation_ref=operation_ref,
                entity_ref=entity_ref,
                digest=digest,
                version_ref=existing["batch_version_ref"],
                actor_ref=actor_ref,
            )
            return cast(UUID, existing["batch_version_ref"])

        self._session.execute(
            text(
                "UPDATE payroll.batch_version SET status = 'SUPERSEDED' "
                "WHERE batch_ref = :batch_ref AND status = 'LOCKED'"
            ),
            {"batch_ref": batch_ref},
        )
        version_ref = uuid4()
        self._session.execute(
            text(
                """
                INSERT INTO payroll.batch_version(
                    batch_version_ref, batch_ref, revision, rules_version,
                    reconciliation_month, created_by
                ) VALUES (
                    :version_ref, :batch_ref, :revision, :rules_version,
                    :reconciliation_month, :actor_ref
                )
                """
            ),
            {
                "version_ref": version_ref,
                "batch_ref": batch_ref,
                "revision": locked.revision,
                "rules_version": locked.rules_version,
                "reconciliation_month": locked.period,
                "actor_ref": actor_ref,
            },
        )
        for line in locked.lines:
            employee_ref = self._upsert_employee(entity_ref, line)
            payee_account_ref = None
            if line.payment_channel is not PaymentChannel.CASH:
                payee_account_ref = self._upsert_payee_account(entity_ref, employee_ref, line)
            line_ref = uuid4()
            self._session.execute(
                text(
                    """
                    INSERT INTO payroll.line(
                        line_ref, batch_version_ref, employee_ref, employee_name,
                        employee_type, location, job_group, attendance_days,
                        payment_channel, payee_account_ref, memo,
                        payee_name, account_number,
                        net_amount_minor, cash_amount_minor, supplemental_amount_minor
                    ) VALUES (
                        :line_ref, :version_ref, :employee_ref, :employee_name,
                        :employee_type, :location, :job_group, :attendance_days,
                        :payment_channel, :payee_account_ref, :memo,
                        :payee_name, :account_number,
                        :net_amount_minor, :cash_amount_minor, :supplemental_amount_minor
                    )
                    """
                ),
                {
                    "line_ref": line_ref,
                    "version_ref": version_ref,
                    "employee_ref": employee_ref,
                    "employee_name": line.employee_name,
                    "employee_type": line.employee_type.value,
                    "location": line.location,
                    "job_group": line.job_group or None,
                    "attendance_days": line.attendance_days or None,
                    "payment_channel": line.payment_channel.value,
                    "payee_account_ref": payee_account_ref,
                    "memo": line.memo,
                    "payee_name": line.payee_name,
                    "account_number": normalize_account(line.account_number),
                    "net_amount_minor": line.net_amount_minor,
                    "cash_amount_minor": line.cash_amount_minor,
                    "supplemental_amount_minor": line.supplemental_amount_minor,
                },
            )
            for ordinal, component in enumerate(line.components):
                self._session.execute(
                    text(
                        """
                        INSERT INTO payroll.component(
                            line_ref, ordinal, component_code, amount_minor, note
                        ) VALUES (:line_ref, :ordinal, :code, :amount_minor, :note)
                        """
                    ),
                    {
                        "line_ref": line_ref,
                        "ordinal": ordinal,
                        "code": component.code,
                        "amount_minor": component.amount_minor,
                        "note": component.reason or None,
                    },
                )
        for fixed in locked.fixed_remittances:
            self._session.execute(
                text(
                    """
                    INSERT INTO payroll.fixed_remittance_snapshot(
                        batch_version_ref, payee_name, account_number, amount_minor, memo
                    ) VALUES (:version_ref, :payee_name, :account_number, :amount_minor, :memo)
                    """
                ),
                {
                    "version_ref": version_ref,
                    "payee_name": fixed.payee_name,
                    "account_number": normalize_account(fixed.account_number),
                    "amount_minor": fixed.amount_minor,
                    "memo": fixed.memo,
                },
            )
        self._session.execute(
            text(
                """
                UPDATE payroll.batch_version
                   SET status = 'LOCKED', content_sha256 = :digest,
                       locked_at = CURRENT_TIMESTAMP, locked_by = :actor_ref
                 WHERE batch_version_ref = :version_ref AND status = 'DRAFT'
                """
            ),
            {"version_ref": version_ref, "digest": digest, "actor_ref": actor_ref},
        )
        self._insert_command_receipt(
            operation_ref=operation_ref,
            entity_ref=entity_ref,
            digest=digest,
            version_ref=version_ref,
            actor_ref=actor_ref,
        )
        return version_ref

    def _insert_command_receipt(
        self,
        *,
        operation_ref: UUID,
        entity_ref: UUID,
        digest: bytes,
        version_ref: UUID,
        actor_ref: str,
    ) -> None:
        self._session.execute(
            text(
                """
                INSERT INTO payroll.command_receipt(
                    operation_ref, entity_ref, command_kind, request_sha256,
                    result_ref, actor_ref
                ) VALUES (
                    :operation_ref, :entity_ref, 'LOCK', :digest,
                    :version_ref, :actor_ref
                )
                """
            ),
            {
                "operation_ref": operation_ref,
                "entity_ref": entity_ref,
                "digest": digest,
                "version_ref": version_ref,
                "actor_ref": actor_ref,
            },
        )

    def _upsert_employee(self, entity_ref: UUID, employee: PayrollDraftLine) -> UUID:
        self._session.execute(
            text(
                """
                INSERT INTO payroll.employee(
                    entity_ref, employee_code, display_name, employee_type
                ) VALUES (:entity_ref, :employee_code, :display_name, :employee_type)
                ON CONFLICT (entity_ref, employee_code) DO UPDATE
                    SET display_name = EXCLUDED.display_name,
                        employee_type = EXCLUDED.employee_type,
                        active = true
                """
            ),
            {
                "entity_ref": entity_ref,
                "employee_code": employee.employee_id,
                "display_name": employee.employee_name,
                "employee_type": employee.employee_type.value,
            },
        )
        return cast(UUID, self._session.execute(
            text(
                "SELECT employee_ref FROM payroll.employee "
                "WHERE entity_ref = :entity_ref AND employee_code = :employee_code"
            ),
            {
                "entity_ref": entity_ref,
                "employee_code": employee.employee_id,
            },
        ).scalar_one())

    def _upsert_payee_account(
        self, entity_ref: UUID, employee_ref: UUID, account: PayrollDraftLine
    ) -> UUID:
        self._session.execute(
            text(
                """
                INSERT INTO payroll.payee_account(
                    entity_ref, employee_ref, payee_name, account_number
                ) VALUES (:entity_ref, :employee_ref, :payee_name, :account_number)
                ON CONFLICT (entity_ref, employee_ref, account_number) DO NOTHING
                """
            ),
            {
                "entity_ref": entity_ref,
                "employee_ref": employee_ref,
                "payee_name": account.payee_name,
                "account_number": normalize_account(account.account_number),
            },
        )
        return cast(UUID, self._session.execute(
            text(
                """
                SELECT payee_account_ref FROM payroll.payee_account
                 WHERE entity_ref = :entity_ref AND employee_ref = :employee_ref
                   AND account_number = :account_number
                """
            ),
            {
                "entity_ref": entity_ref,
                "employee_ref": employee_ref,
                "account_number": normalize_account(account.account_number),
            },
        ).scalar_one())


def persist_draft_as_locked(
    session: Session,
    *,
    entity_ref: UUID,
    operation_ref: UUID,
    actor_ref: str,
    draft: PayrollDraft,
) -> UUID:
    """Validate and lock before invoking the database Adapter."""

    locked = lock_draft(draft)
    return DatabasePayrollStore(session).persist_locked(
        entity_ref=entity_ref,
        operation_ref=operation_ref,
        actor_ref=actor_ref,
        locked=locked,
    )
