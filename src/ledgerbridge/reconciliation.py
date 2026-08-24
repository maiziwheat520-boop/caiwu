"""Deterministic Phase 5 deduplication, reconciliation, and Suspense contracts.

This module is deliberately side-effect free. It produces reviewable decisions
and metadata proposals; it never deletes a source record, mutates a JournalEntry,
or posts a Suspense resolution. A later service can persist these values behind
the existing audit and database state-machine boundaries.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from threading import RLock
from uuid import UUID

from ledgerbridge.connectors import CANONICAL_SOURCE_PATTERN
from ledgerbridge.text import contains_unstorable_text

MAX_IDENTIFIER_TEXT = 300
MAX_DESCRIPTION_TEXT = 1_000
MAX_RECONCILIATION_LEGS = 32
SUSPENSE_ACCOUNT = "Suspense:Unclassified"


class Phase5Error(ValueError):
    """Raised when a Phase 5 value violates a reviewable contract."""


class DedupDecision(StrEnum):
    NEW = "NEW"
    DUPLICATE = "DUPLICATE"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ReconciliationRelation(StrEnum):
    ONE_TO_ONE = "1:1"
    ONE_TO_MANY = "1:N"
    MANY_TO_ONE = "N:1"


class ReconciliationStatus(StrEnum):
    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class SuspenseReason(StrEnum):
    UNKNOWN_COUNTERPARTY = "UNKNOWN_COUNTERPARTY"
    UNMATCHED_TRANSFER = "UNMATCHED_TRANSFER"
    BALANCE_GAP = "BALANCE_GAP"
    LOAN_BREAKDOWN = "LOAN_BREAKDOWN"


class SuspenseStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True, slots=True)
class ExternalTransactionIdentity:
    """Stable source/account/external-ID identity used before fingerprints."""

    source_system: str
    account_key: str
    external_transaction_id: str

    def __post_init__(self) -> None:
        if CANONICAL_SOURCE_PATTERN.fullmatch(self.source_system) is None:
            raise Phase5Error("source_system is not canonical")
        _require_text("account_key", self.account_key, MAX_IDENTIFIER_TEXT)
        _require_text(
            "external_transaction_id",
            self.external_transaction_id,
            MAX_IDENTIFIER_TEXT,
        )


@dataclass(frozen=True, slots=True)
class TransactionFingerprint:
    """Auxiliary fingerprint; a match always requires review, never deletion."""

    occurred_on: date
    amount_minor: int
    counterparty: str | None = None
    description: str | None = None
    balance_minor: int | None = None
    currency: str = "CNY"

    def __post_init__(self) -> None:
        _require_amount("amount_minor", self.amount_minor)
        if self.balance_minor is not None:
            _require_amount("balance_minor", self.balance_minor)
        if self.currency != "CNY":
            raise Phase5Error("currency must be CNY")
        if self.counterparty is not None:
            _require_text("counterparty", self.counterparty, MAX_IDENTIFIER_TEXT)
        if self.description is not None:
            _require_text("description", self.description, MAX_DESCRIPTION_TEXT)

    @property
    def digest_hex(self) -> str:
        payload = {
            "amount_minor": self.amount_minor,
            "balance_minor": self.balance_minor,
            "counterparty": _normalize_optional(self.counterparty),
            "currency": self.currency,
            "description": _normalize_optional(self.description),
            "occurred_on": self.occurred_on.isoformat(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DedupRecord:
    record_locator: str
    fingerprint: TransactionFingerprint
    external_identity: ExternalTransactionIdentity | None = None

    def __post_init__(self) -> None:
        _require_text("record_locator", self.record_locator, MAX_IDENTIFIER_TEXT)


@dataclass(frozen=True, slots=True)
class DedupResult:
    decision: DedupDecision
    reason: str
    matched_record_locator: str | None = None


class DedupIndex:
    """In-memory decision index with no delete or overwrite operation."""

    def __init__(self, records: Iterable[DedupRecord] = ()) -> None:
        self._by_external: dict[ExternalTransactionIdentity, DedupRecord] = {}
        self._by_fingerprint: dict[str, str] = {}
        self._records: dict[str, DedupRecord] = {}
        for record in records:
            self.register(record)

    @property
    def record_count(self) -> int:
        return len(self._records)

    def classify(self, record: DedupRecord) -> DedupResult:
        if record.record_locator in self._records:
            existing = self._records[record.record_locator]
            if existing == record:
                return DedupResult(
                    DedupDecision.DUPLICATE, "RECORD_LOCATOR_MATCH", record.record_locator
                )
            return DedupResult(
                DedupDecision.NEEDS_REVIEW,
                "RECORD_LOCATOR_CONFLICT",
                record.record_locator,
            )
        if record.external_identity is not None:
            existing_by_external = self._by_external.get(record.external_identity)
            if existing_by_external is not None:
                if existing_by_external.fingerprint.digest_hex == record.fingerprint.digest_hex:
                    return DedupResult(
                        DedupDecision.DUPLICATE,
                        "EXTERNAL_ID_MATCH",
                        existing_by_external.record_locator,
                    )
                return DedupResult(
                    DedupDecision.NEEDS_REVIEW,
                    "EXTERNAL_ID_CONFLICT",
                    existing_by_external.record_locator,
                )
        matched_locator = self._by_fingerprint.get(record.fingerprint.digest_hex)
        if matched_locator is not None:
            return DedupResult(
                DedupDecision.NEEDS_REVIEW,
                "FINGERPRINT_MATCH",
                matched_locator,
            )
        return DedupResult(DedupDecision.NEW, "NO_MATCH")

    def register(self, record: DedupRecord) -> DedupResult:
        result = self.classify(record)
        if result.decision is not DedupDecision.NEW:
            raise Phase5Error(f"record requires review: {result.reason}")
        self._records[record.record_locator] = record
        self._by_fingerprint[record.fingerprint.digest_hex] = record.record_locator
        if record.external_identity is not None:
            self._by_external[record.external_identity] = record
        return result


class ConcurrentDedupIndex:
    """Atomic candidate-admission boundary for concurrent importer workers.

    ``DedupIndex.classify()`` and ``register()`` are intentionally separate for
    pure, single-threaded callers.  Import workers need one operation that cannot
    interleave the classify/register pair: exactly one concurrent copy may be
    admitted, while equivalent copies are duplicates and conflicting copies stay
    reviewable.  The lock is process-local; a database unique constraint remains
    the durable cross-process boundary.
    """

    def __init__(self, records: Iterable[DedupRecord] = ()) -> None:
        self._index = DedupIndex(records)
        self._lock = RLock()

    @property
    def record_count(self) -> int:
        with self._lock:
            return self._index.record_count

    def classify(self, record: DedupRecord) -> DedupResult:
        """Return a snapshot decision without admitting the record."""

        with self._lock:
            return self._index.classify(record)

    def admit(self, record: DedupRecord) -> DedupResult:
        """Classify and, only for ``NEW``, register atomically."""

        with self._lock:
            result = self._index.classify(record)
            if result.decision is DedupDecision.NEW:
                self._index.register(record)
            return result

    def admit_many(self, records: Iterable[DedupRecord]) -> tuple[DedupResult, ...]:
        """Admit a bounded caller batch under one lock, preserving input order."""

        with self._lock:
            results: list[DedupResult] = []
            for record in records:
                result = self._index.classify(record)
                if result.decision is DedupDecision.NEW:
                    self._index.register(record)
                results.append(result)
            return tuple(results)


@dataclass(frozen=True, slots=True)
class ReconciliationLeg:
    """One immutable source-record leg; positive and negative amounts must net zero."""

    record_locator: str
    source_system: str
    amount_minor: int
    currency: str = "CNY"

    def __post_init__(self) -> None:
        _require_text("record_locator", self.record_locator, MAX_IDENTIFIER_TEXT)
        if CANONICAL_SOURCE_PATTERN.fullmatch(self.source_system) is None:
            raise Phase5Error("source_system is not canonical")
        _require_amount("amount_minor", self.amount_minor, allow_zero=False)
        if self.currency != "CNY":
            raise Phase5Error("currency must be CNY")


@dataclass(frozen=True, slots=True)
class ReconciliationProposal:
    group_id: UUID
    relation: ReconciliationRelation
    legs: tuple[ReconciliationLeg, ...]
    status: ReconciliationStatus = ReconciliationStatus.PROPOSED
    decision_actor: str | None = None
    decision_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.legs or len(self.legs) > MAX_RECONCILIATION_LEGS:
            raise Phase5Error("reconciliation leg count is out of bounds")
        locators = [leg.record_locator for leg in self.legs]
        if len(set(locators)) != len(locators):
            raise Phase5Error("reconciliation record locators must be unique")
        positive = sum(leg.amount_minor > 0 for leg in self.legs)
        negative = sum(leg.amount_minor < 0 for leg in self.legs)
        if sum(leg.amount_minor for leg in self.legs) != 0 or not positive or not negative:
            raise Phase5Error("reconciliation legs must net to zero")
        cardinality_valid = {
            ReconciliationRelation.ONE_TO_ONE: positive == 1 and negative == 1,
            ReconciliationRelation.ONE_TO_MANY: negative == 1 and positive > 1,
            ReconciliationRelation.MANY_TO_ONE: negative > 1 and positive == 1,
        }[self.relation]
        if not cardinality_valid:
            raise Phase5Error("reconciliation relation cardinality is invalid")
        if self.status is ReconciliationStatus.PROPOSED:
            if self.decision_actor is not None or self.decision_reason is not None:
                raise Phase5Error("proposed reconciliation cannot have a decision")
        else:
            _require_text("decision_actor", self.decision_actor or "", MAX_IDENTIFIER_TEXT)
            _require_text("decision_reason", self.decision_reason or "", MAX_DESCRIPTION_TEXT)

    @classmethod
    def propose(
        cls,
        group_id: UUID,
        relation: ReconciliationRelation,
        legs: Iterable[ReconciliationLeg],
    ) -> ReconciliationProposal:
        return cls(group_id, relation, tuple(legs))

    def confirm(self, *, actor: str, reason: str) -> ReconciliationProposal:
        if self.status is not ReconciliationStatus.PROPOSED:
            raise Phase5Error("only a proposed reconciliation can be confirmed")
        return replace(
            self,
            status=ReconciliationStatus.CONFIRMED,
            decision_actor=actor,
            decision_reason=reason,
        )

    def reject(self, *, actor: str, reason: str) -> ReconciliationProposal:
        if self.status is not ReconciliationStatus.PROPOSED:
            raise Phase5Error("only a proposed reconciliation can be rejected")
        return replace(
            self,
            status=ReconciliationStatus.REJECTED,
            decision_actor=actor,
            decision_reason=reason,
        )


@dataclass(frozen=True, slots=True)
class SuspenseItem:
    item_id: UUID
    record_locator: str
    amount_minor: int
    reason: SuspenseReason
    suspense_account: str = SUSPENSE_ACCOUNT
    status: SuspenseStatus = SuspenseStatus.OPEN
    resolution_account: str | None = None
    resolution_actor: str | None = None
    resolution_reason: str | None = None

    def __post_init__(self) -> None:
        _require_text("record_locator", self.record_locator, MAX_IDENTIFIER_TEXT)
        _require_amount("amount_minor", self.amount_minor, allow_zero=False)
        _require_text("suspense_account", self.suspense_account, MAX_IDENTIFIER_TEXT)
        if self.status is SuspenseStatus.OPEN:
            if any(
                value is not None
                for value in (
                    self.resolution_account,
                    self.resolution_actor,
                    self.resolution_reason,
                )
            ):
                raise Phase5Error("open Suspense item cannot have a resolution")
        else:
            _require_text("resolution_account", self.resolution_account or "", MAX_IDENTIFIER_TEXT)
            _require_text("resolution_actor", self.resolution_actor or "", MAX_IDENTIFIER_TEXT)
            _require_text("resolution_reason", self.resolution_reason or "", MAX_DESCRIPTION_TEXT)
            if self.resolution_account == self.suspense_account:
                raise Phase5Error("Suspense resolution must name a different account")

    def resolve(self, *, account: str, actor: str, reason: str) -> SuspenseItem:
        if self.status is not SuspenseStatus.OPEN:
            raise Phase5Error("only an open Suspense item can be resolved")
        return replace(
            self,
            status=SuspenseStatus.RESOLVED,
            resolution_account=account,
            resolution_actor=actor,
            resolution_reason=reason,
        )


def _require_text(field: str, value: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or contains_unstorable_text(value)
    ):
        raise Phase5Error(f"{field} is invalid")


def _require_amount(field: str, value: int, *, allow_zero: bool = True) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Phase5Error(f"{field} must be an integer minor-unit value")
    if not allow_zero and value == 0:
        raise Phase5Error(f"{field} must not be zero")
    if value < -(2**63) or value > 2**63 - 1:
        raise Phase5Error(f"{field} must fit a signed 64-bit value")


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(value.split()).casefold()
