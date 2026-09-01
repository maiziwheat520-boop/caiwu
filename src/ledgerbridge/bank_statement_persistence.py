"""Persist one reviewed bank statement without creating per-row accounting candidates."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.bank_statement_contract import (
    BankStatement,
    BankStatementTransaction,
    parser_spec,
)
from ledgerbridge.internal_read_contract import (
    Capability,
    ResourceNotVisible,
    WorkloadPrincipal,
    require_capability,
)
from ledgerbridge.text import contains_unstorable_text

_ACCOUNT_SUFFIX: Final = re.compile(r"^[0-9]{4,8}$")
_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT: Final = 300

ReviewStatus = Literal["PENDING", "CONFIRMED", "REJECTED"]
_SOURCE_TIME_ZONE: Final = ZoneInfo("Asia/Shanghai")


class BankStatementPersistenceError(RuntimeError):
    """A statement could not be persisted through the controlled database seam."""


@dataclass(frozen=True, slots=True)
class BankStatementImportContext:
    owner_entity_ref: UUID
    managed_account_ref: UUID
    evidence_ref: UUID
    actor: str
    reason: str

    def __post_init__(self) -> None:
        _validate_text("actor", self.actor, 200)
        _validate_text("reason", self.reason, 1_000)


@dataclass(frozen=True, slots=True)
class BankStatementImportResult:
    statement_ref: UUID
    managed_account_ref: UUID
    created: bool
    transaction_count: int
    review_status: ReviewStatus
    statement_review_count: int
    accounting_candidate_count: int


@dataclass(frozen=True, slots=True)
class BankStatementSummary:
    statement_ref: UUID
    managed_account_ref: UUID
    evidence_ref: UUID
    period_start: date
    period_end: date
    transaction_count: int
    review_status: ReviewStatus
    review_revision: int


@dataclass(frozen=True, slots=True)
class BankStatementTransactionItem:
    source_row_number: int
    occurred_at: datetime
    amount_minor: int
    balance_minor: int
    currency: str
    counterparty_ref: str
    counterparty_name: str | None
    counterparty_account_masked: str | None
    counterparty_institution: str | None
    transaction_serial: str
    transaction_name: str


class BankStatementImportService:
    """Import a parsed statement atomically through a write-only database seam."""

    def __init__(self, sessions: Callable[[], Session]) -> None:
        self._sessions = sessions

    def import_statement(
        self,
        statement: BankStatement,
        *,
        context: BankStatementImportContext,
        session: Session | None = None,
    ) -> BankStatementImportResult:
        request = _build_request(statement, context)
        try:
            if session is not None:
                return self._execute_import(session, request)
            with self._sessions() as session:
                result = self._execute_import(session, request)
                session.commit()
                return result
        except BankStatementPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise BankStatementPersistenceError("bank statement import failed") from exc

    def _execute_import(
        self, session: Session, request: dict[str, object]
    ) -> BankStatementImportResult:
        raw = session.execute(
            text("SELECT internal_import.import_bank_statement(CAST(:request AS jsonb))"),
            {
                "request": json.dumps(
                    request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            },
        ).scalar_one()
        return _parse_result(raw)


class BankStatementReadService:
    """Read entity-scoped statement views through a separate read-only session."""

    def __init__(self, sessions: Callable[[], Session]) -> None:
        self._sessions = sessions

    def get_statement_summary(
        self,
        statement_ref: UUID,
        *,
        principal: WorkloadPrincipal,
        entity_ref: UUID,
        audit_horizon_sequence: int,
        audit_horizon_hash: bytes,
    ) -> BankStatementSummary:
        _authorize_statement_entity(principal, entity_ref=entity_ref)
        if (
            isinstance(audit_horizon_sequence, bool)
            or not isinstance(audit_horizon_sequence, int)
            or audit_horizon_sequence <= 0
            or not isinstance(audit_horizon_hash, bytes)
            or len(audit_horizon_hash) != 32
        ):
            raise ValueError("audit horizon is invalid")
        try:
            with self._sessions() as session:
                row = (
                    session.execute(
                        text(
                            "SELECT * FROM internal_read.get_bank_statement_summary("
                            ":statement_ref, :entity_ref, :horizon_sequence, :horizon_hash)"
                        ),
                        {
                            "statement_ref": statement_ref,
                            "entity_ref": entity_ref,
                            "horizon_sequence": audit_horizon_sequence,
                            "horizon_hash": audit_horizon_hash,
                        },
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise BankStatementPersistenceError("bank statement summary was not found")
                return _parse_summary(cast(Mapping[object, object], row))
        except BankStatementPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise BankStatementPersistenceError("bank statement summary read failed") from exc

    def list_statement_transactions(
        self,
        statement_ref: UUID,
        *,
        principal: WorkloadPrincipal,
        entity_ref: UUID,
        audit_horizon_sequence: int,
        audit_horizon_hash: bytes,
        after_row: int,
        limit: int,
    ) -> tuple[BankStatementTransactionItem, ...]:
        _authorize_statement_entity(principal, entity_ref=entity_ref)
        if (
            isinstance(audit_horizon_sequence, bool)
            or not isinstance(audit_horizon_sequence, int)
            or audit_horizon_sequence <= 0
            or not isinstance(audit_horizon_hash, bytes)
            or len(audit_horizon_hash) != 32
            or isinstance(after_row, bool)
            or not isinstance(after_row, int)
            or after_row < 0
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 200
        ):
            raise ValueError("bank statement transaction page is invalid")
        try:
            with self._sessions() as session:
                rows = (
                    session.execute(
                        text(
                            "SELECT * FROM "
                            "internal_read.list_bank_statement_transactions("
                            ":statement_ref, :entity_ref, :horizon_sequence, "
                            ":horizon_hash, :after_row, :limit)"
                        ),
                        {
                            "statement_ref": statement_ref,
                            "entity_ref": entity_ref,
                            "horizon_sequence": audit_horizon_sequence,
                            "horizon_hash": audit_horizon_hash,
                            "after_row": after_row,
                            "limit": limit,
                        },
                    )
                    .mappings()
                    .all()
                )
                return tuple(
                    _parse_transaction_item(cast(Mapping[object, object], row)) for row in rows
                )
        except BankStatementPersistenceError:
            raise
        except SQLAlchemyError as exc:
            raise BankStatementPersistenceError(
                "bank statement transaction page read failed"
            ) from exc


def _build_request(
    statement: BankStatement,
    context: BankStatementImportContext,
) -> dict[str, object]:
    if not statement.transactions:
        raise BankStatementPersistenceError("bank statement has no transactions")
    if statement.currency != "CNY" or _DIGEST.fullmatch(statement.source_sha256) is None:
        raise BankStatementPersistenceError("bank statement identity is invalid")
    try:
        spec = parser_spec(statement.parser_profile)
    except ValueError as exc:
        raise BankStatementPersistenceError("bank statement parser identity is invalid") from exc
    if (
        statement.institution_code != spec.institution_code
        or statement.source_system != spec.source_system
        or statement.declared_media_type != spec.declared_media_type
        or _ACCOUNT_SUFFIX.fullmatch(statement.account_suffix) is None
    ):
        raise BankStatementPersistenceError("managed account identity does not match statement")
    ordered_transactions = tuple(
        sorted(statement.transactions, key=lambda item: item.source_row_number)
    )
    dates = [item.occurred_at.astimezone(_SOURCE_TIME_ZONE).date() for item in ordered_transactions]
    transaction_set_sha256 = hashlib.sha256(
        "|".join(
            f"{item.source_event_ref}:{item.source_row_sha256}:{item.source_row_number}"
            for item in ordered_transactions
        ).encode("ascii")
    ).hexdigest()
    transactions: list[dict[str, object]] = []
    for item in ordered_transactions:
        if item.occurred_at.tzinfo is None or _DIGEST.fullmatch(item.source_row_sha256) is None:
            raise BankStatementPersistenceError("bank statement transaction is invalid")
        transactions.append(
            {
                "source_event_ref": str(item.source_event_ref),
                "source_row_number": item.source_row_number,
                "source_row_sha256": item.source_row_sha256,
                "occurred_at": item.occurred_at.isoformat(),
                "amount_minor": item.amount_minor,
                "balance_minor": item.balance_minor,
                "counterparty_ref": _counterparty_ref(context.owner_entity_ref, item),
                "counterparty_name": item.counterparty_name,
                "counterparty_account": item.counterparty_account,
                "counterparty_institution": item.counterparty_institution,
                "transaction_serial": item.transaction_serial,
                "transaction_name": item.transaction_name,
            }
        )
    request: dict[str, object] = {
        "statement_ref": str(statement.statement_ref),
        "managed_account_ref": str(context.managed_account_ref),
        "owner_entity_ref": str(context.owner_entity_ref),
        "institution_code": statement.institution_code,
        "account_suffix": statement.account_suffix,
        "evidence_ref": str(context.evidence_ref),
        "source_system": statement.source_system,
        "source_sha256": statement.source_sha256,
        "source_size": statement.source_size,
        "declared_media_type": statement.declared_media_type,
        "currency": statement.currency,
        "period_start": min(dates).isoformat(),
        "period_end": max(dates).isoformat(),
        "transaction_count": len(transactions),
        "transaction_set_sha256": transaction_set_sha256,
        "actor": context.actor,
        "reason": context.reason,
        "transactions": transactions,
    }
    if statement.parser_profile.value != "mybank_xlsx_v1":
        request["parser_profile"] = statement.parser_profile.value
    return request


def _counterparty_ref(entity_ref: UUID, transaction: BankStatementTransaction) -> str:
    values = (
        transaction.counterparty_account,
        transaction.counterparty_name,
        transaction.counterparty_institution,
    )
    normalized = "|".join(" ".join(value.split()).casefold() for value in values)
    if not normalized.replace("|", ""):
        normalized = transaction.source_row_sha256
    digest = hashlib.sha256(f"{entity_ref}:{normalized}".encode()).hexdigest()
    return f"cp_{digest[:48]}"


def _authorize_statement_entity(
    principal: WorkloadPrincipal,
    *,
    entity_ref: UUID,
) -> None:
    """Authorize an entity-level statement without widening business-unit grants."""

    require_capability(principal, Capability.LEDGER_READ)
    if not any(grant.entity_ref == entity_ref for grant in principal.grants):
        raise ResourceNotVisible("resource was not found")


def _parse_result(raw: object) -> BankStatementImportResult:
    if not isinstance(raw, Mapping):
        raise BankStatementPersistenceError("bank statement import receipt is invalid")
    try:
        statement_ref = UUID(_required_string(raw, "statement_ref"))
        managed_account_ref = UUID(_required_string(raw, "managed_account_ref"))
        created = raw["created"]
        transaction_count = raw["transaction_count"]
        review_status = _required_string(raw, "review_status")
        statement_review_count = raw["statement_review_count"]
        accounting_candidate_count = raw["accounting_candidate_count"]
    except (KeyError, ValueError, TypeError) as exc:
        raise BankStatementPersistenceError("bank statement import receipt is invalid") from exc
    if not isinstance(created, bool):
        raise BankStatementPersistenceError("bank statement import receipt is invalid")
    counts = (transaction_count, statement_review_count, accounting_candidate_count)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
        raise BankStatementPersistenceError("bank statement import receipt is invalid")
    if (
        transaction_count <= 0
        or statement_review_count != 1
        or accounting_candidate_count != 0
        or review_status not in {"PENDING", "CONFIRMED", "REJECTED"}
    ):
        raise BankStatementPersistenceError("bank statement import receipt violates policy")
    return BankStatementImportResult(
        statement_ref=statement_ref,
        managed_account_ref=managed_account_ref,
        created=created,
        transaction_count=transaction_count,
        review_status=cast(ReviewStatus, review_status),
        statement_review_count=statement_review_count,
        accounting_candidate_count=accounting_candidate_count,
    )


def _parse_summary(raw: Mapping[object, object]) -> BankStatementSummary:
    try:
        statement_ref = UUID(_required_string(raw, "statement_ref"))
        managed_account_ref = UUID(_required_string(raw, "managed_account_ref"))
        evidence_ref = UUID(_required_string(raw, "evidence_ref"))
        period_start = raw["period_start"]
        period_end = raw["period_end"]
        transaction_count = raw["transaction_count"]
        review_status = _required_string(raw, "review_status")
        review_revision = raw["review_revision"]
    except (KeyError, ValueError, TypeError) as exc:
        raise BankStatementPersistenceError("bank statement summary is invalid") from exc
    if (
        not isinstance(period_start, date)
        or isinstance(period_start, datetime)
        or not isinstance(period_end, date)
        or isinstance(period_end, datetime)
        or period_start > period_end
        or isinstance(transaction_count, bool)
        or not isinstance(transaction_count, int)
        or transaction_count <= 0
        or review_status not in {"PENDING", "CONFIRMED", "REJECTED"}
        or isinstance(review_revision, bool)
        or not isinstance(review_revision, int)
        or review_revision <= 0
    ):
        raise BankStatementPersistenceError("bank statement summary is invalid")
    return BankStatementSummary(
        statement_ref=statement_ref,
        managed_account_ref=managed_account_ref,
        evidence_ref=evidence_ref,
        period_start=period_start,
        period_end=period_end,
        transaction_count=transaction_count,
        review_status=cast(ReviewStatus, review_status),
        review_revision=review_revision,
    )


def _parse_transaction_item(
    raw: Mapping[object, object],
) -> BankStatementTransactionItem:
    try:
        source_row_number = _required_int(raw, "source_row_number")
        occurred_at = raw["occurred_at"]
        amount_minor = _required_int(raw, "amount_minor")
        balance_minor = _required_int(raw, "balance_minor")
        currency = _required_string(raw, "currency")
        counterparty_ref = _required_string(raw, "counterparty_ref")
        counterparty_name = _optional_string(raw, "counterparty_name")
        counterparty_account_masked = _optional_string(raw, "counterparty_account_masked")
        counterparty_institution = _optional_string(raw, "counterparty_institution")
        transaction_serial = _required_string(raw, "transaction_serial")
        transaction_name = _required_string(raw, "transaction_name")
    except (KeyError, ValueError, TypeError) as exc:
        raise BankStatementPersistenceError("bank statement transaction item is invalid") from exc
    if (
        source_row_number <= 0
        or not isinstance(occurred_at, datetime)
        or occurred_at.tzinfo is None
        or currency != "CNY"
        or not counterparty_ref.startswith("cp_")
        or not _is_masked_account(counterparty_account_masked)
    ):
        raise BankStatementPersistenceError("bank statement transaction item is invalid")
    return BankStatementTransactionItem(
        source_row_number=source_row_number,
        occurred_at=occurred_at,
        amount_minor=amount_minor,
        balance_minor=balance_minor,
        currency=currency,
        counterparty_ref=counterparty_ref,
        counterparty_name=counterparty_name,
        counterparty_account_masked=counterparty_account_masked,
        counterparty_institution=counterparty_institution,
        transaction_serial=transaction_serial,
        transaction_name=transaction_name,
    )


def _required_string(values: Mapping[object, object], key: str) -> str:
    value = values[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is invalid")
    return value


def _required_int(values: Mapping[object, object], key: str) -> int:
    value = values[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} is invalid")
    return value


def _optional_string(values: Mapping[object, object], key: str) -> str | None:
    value = values[key]
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_TEXT
        or contains_unstorable_text(value)
    ):
        raise ValueError(f"{key} is invalid")
    return value


def _is_masked_account(value: str | None) -> bool:
    if value is None:
        return True
    if len(value) <= 4:
        return set(value) == {"*"}
    return set(value[:-4]) == {"*"}


def _validate_text(field: str, value: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or contains_unstorable_text(value)
    ):
        raise ValueError(f"{field} is invalid")
