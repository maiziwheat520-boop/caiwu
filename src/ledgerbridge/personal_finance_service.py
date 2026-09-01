"""Read one entity-scoped personal statement through existing R1 read functions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.internal_read_contract import (
    Capability,
    ResourceNotVisible,
    WorkloadPrincipal,
    require_capability,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable
from ledgerbridge.personal_finance_contract import (
    PersonalFinancePage,
    PersonalFinanceStatement,
    PersonalFinanceSummary,
    PersonalFinanceTransaction,
)

_PAGE_SIZE = 200
_MAX_TRANSACTIONS = 10_000
_PUBLIC_TRANSACTION_FIELDS = (
    "source_row_number",
    "occurred_at",
    "amount_minor",
    "balance_minor",
    "currency",
    "counterparty_name",
    "counterparty_account_masked",
    "counterparty_institution",
    "transaction_name",
)


class DatabasePersonalFinanceService:
    """Return a complete personal statement at one immutable audit horizon."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def statement(
        self,
        principal: WorkloadPrincipal,
        *,
        statement_ref: UUID,
        entity_ref: UUID,
    ) -> PersonalFinancePage:
        require_capability(principal, Capability.LEDGER_READ)
        if not any(grant.entity_ref == entity_ref for grant in principal.grants):
            raise ResourceNotVisible("resource was not found")

        try:
            with self._session_factory() as session:
                sequence, horizon_hash = self._audit_horizon(session)
                summary_row = (
                    session.execute(
                        text(
                            "SELECT * FROM internal_read.get_bank_statement_summary("
                            ":statement_ref, :entity_ref, :horizon_sequence, :horizon_hash)"
                        ),
                        {
                            "statement_ref": statement_ref,
                            "entity_ref": entity_ref,
                            "horizon_sequence": sequence,
                            "horizon_hash": horizon_hash,
                        },
                    )
                    .mappings()
                    .one_or_none()
                )
                if summary_row is None:
                    raise ResourceNotVisible("resource was not found")
                summary_values = cast(Mapping[str, Any], summary_row)
                managed_account_ref = self._uuid(summary_values, "managed_account_ref")
                transaction_count = self._positive_int(summary_values, "transaction_count")
                if transaction_count > _MAX_TRANSACTIONS:
                    raise InternalReadBackendUnavailable(
                        "personal statement exceeds the bounded response limit"
                    )

                institution_code, account_suffix = self._account_identity(
                    session,
                    entity_ref=entity_ref,
                    managed_account_ref=managed_account_ref,
                    horizon_sequence=sequence,
                    horizon_hash=horizon_hash,
                )
                items = self._transactions(
                    session,
                    statement_ref=statement_ref,
                    entity_ref=entity_ref,
                    horizon_sequence=sequence,
                    horizon_hash=horizon_hash,
                    transaction_count=transaction_count,
                )

                inflow = sum(item.amount_minor for item in items if item.amount_minor > 0)
                outflow = sum(-item.amount_minor for item in items if item.amount_minor < 0)
                return PersonalFinancePage(
                    snapshot_revision=horizon_hash.hex(),
                    statement=PersonalFinanceStatement.model_validate(
                        {
                            "statement_ref": self._uuid(summary_values, "statement_ref"),
                            "managed_account_ref": managed_account_ref,
                            "institution_code": institution_code,
                            "account_suffix": account_suffix,
                            "period_start": summary_values.get("period_start"),
                            "period_end": summary_values.get("period_end"),
                            "transaction_count": transaction_count,
                            "review_status": summary_values.get("review_status"),
                            "review_revision": summary_values.get("review_revision"),
                        }
                    ),
                    summary=PersonalFinanceSummary(
                        cash_inflow_minor=inflow,
                        cash_outflow_minor=outflow,
                        net_cash_flow_minor=inflow - outflow,
                    ),
                    items=items,
                )
        except (ResourceNotVisible, InternalReadBackendUnavailable):
            raise
        except (SQLAlchemyError, ValidationError, TypeError, ValueError) as exc:
            raise InternalReadBackendUnavailable(
                "personal finance projection is unavailable"
            ) from exc

    @staticmethod
    def _audit_horizon(session: Session) -> tuple[int, bytes]:
        row = (
            session.execute(
                text("SELECT sequence, hash FROM internal_read.current_audit_horizon()")
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise InternalReadBackendUnavailable("database audit horizon is unavailable")
        sequence = row.get("sequence")
        horizon_hash = row.get("hash")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
            or not isinstance(horizon_hash, (bytes, bytearray))
            or len(horizon_hash) != 32
        ):
            raise InternalReadBackendUnavailable("database audit horizon is malformed")
        return sequence, bytes(horizon_hash)

    @classmethod
    def _account_identity(
        cls,
        session: Session,
        *,
        entity_ref: UUID,
        managed_account_ref: UUID,
        horizon_sequence: int,
        horizon_hash: bytes,
    ) -> tuple[str, str]:
        raw = session.execute(
            text(
                "SELECT internal_read.get_account_registry_projection("
                ":entity_ref, :horizon_sequence, :horizon_hash)"
            ),
            {
                "entity_ref": entity_ref,
                "horizon_sequence": horizon_sequence,
                "horizon_hash": horizon_hash,
            },
        ).scalar_one_or_none()
        if not isinstance(raw, Mapping) or raw.get("owner_kind") != "PERSON":
            raise ResourceNotVisible("resource was not found")
        accounts = raw.get("accounts")
        if not isinstance(accounts, list):
            raise InternalReadBackendUnavailable("personal account registry is malformed")
        matches = [
            account
            for account in accounts
            if isinstance(account, Mapping)
            and cls._optional_uuid(account.get("managed_account_ref")) == managed_account_ref
        ]
        if len(matches) != 1:
            raise InternalReadBackendUnavailable("personal statement account is unavailable")
        institution_code = matches[0].get("institution_code")
        account_suffix = matches[0].get("account_suffix")
        if not isinstance(institution_code, str) or not isinstance(account_suffix, str):
            raise InternalReadBackendUnavailable("personal statement account is malformed")
        return institution_code, account_suffix

    @staticmethod
    def _transactions(
        session: Session,
        *,
        statement_ref: UUID,
        entity_ref: UUID,
        horizon_sequence: int,
        horizon_hash: bytes,
        transaction_count: int,
    ) -> tuple[PersonalFinanceTransaction, ...]:
        items: list[PersonalFinanceTransaction] = []
        after_row = 0
        while len(items) < transaction_count:
            rows = (
                session.execute(
                    text(
                        "SELECT * FROM internal_read.list_bank_statement_transactions("
                        ":statement_ref, :entity_ref, :horizon_sequence, :horizon_hash, "
                        ":after_row, :limit)"
                    ),
                    {
                        "statement_ref": statement_ref,
                        "entity_ref": entity_ref,
                        "horizon_sequence": horizon_sequence,
                        "horizon_hash": horizon_hash,
                        "after_row": after_row,
                        "limit": min(_PAGE_SIZE, transaction_count - len(items)),
                    },
                )
                .mappings()
                .all()
            )
            if not rows:
                break
            page = tuple(
                PersonalFinanceTransaction.model_validate(
                    {field: row.get(field) for field in _PUBLIC_TRANSACTION_FIELDS}
                )
                for row in rows
            )
            if page[0].source_row_number <= after_row:
                raise InternalReadBackendUnavailable("personal transaction page did not advance")
            items.extend(page)
            after_row = page[-1].source_row_number
            if len(items) > transaction_count:
                raise InternalReadBackendUnavailable("personal transaction page exceeded summary")
        return tuple(items)

    @staticmethod
    def _uuid(values: Mapping[str, Any], field: str) -> UUID:
        value = DatabasePersonalFinanceService._optional_uuid(values.get(field))
        if value is None:
            raise ValueError(f"{field} is invalid")
        return value

    @staticmethod
    def _optional_uuid(value: object) -> UUID | None:
        if isinstance(value, UUID):
            return value
        if isinstance(value, str):
            try:
                return UUID(value)
            except ValueError:
                return None
        return None

    @staticmethod
    def _positive_int(values: Mapping[str, Any], field: str) -> int:
        value = values.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{field} is invalid")
        return value
