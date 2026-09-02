"""Source-bound plan contract for registered-account bank statements."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from ledgerbridge.bank_statement_contract import (
    BankStatement,
    BankStatementParserProfile,
    parser_spec,
)
from ledgerbridge.models import EntityType
from ledgerbridge.text import contains_unstorable_text

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")
_MONTH = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


class ExistingStatementEvidenceMode(StrEnum):
    """Explicit evidence handling for a registered-account statement import."""

    CREATE_NEW = "CREATE_NEW"
    REUSE_EXISTING = "REUSE_EXISTING"


class BankStatementPlanError(RuntimeError):
    """A parsed statement conflicts with its private, operator-bound plan."""


@dataclass(frozen=True, slots=True)
class BankStatementExistingAccountPlan:
    source_path: Path
    expected_sha256: str
    expected_size: int
    parser_profile: BankStatementParserProfile
    evidence_ref: UUID
    evidence_mode: ExistingStatementEvidenceMode
    entity_ref: UUID
    business_unit_ref: UUID
    managed_account_ref: UUID
    institution_code: str
    account_suffix: str
    expected_owner_kind: EntityType
    period_start: date
    period_end: date
    expected_transaction_count: int
    expected_transaction_set_sha256: str
    expected_parser_facts_sha256: str
    expected_monthly_transaction_counts: tuple[tuple[str, int], ...]
    actor: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_path, Path) or not self.source_path.is_absolute():
            raise ValueError("bank statement source path must be absolute")
        if _DIGEST.fullmatch(self.expected_sha256) is None:
            raise ValueError("bank statement source digest is invalid")
        if _DIGEST.fullmatch(self.expected_transaction_set_sha256) is None:
            raise ValueError("bank statement transaction-set digest is invalid")
        if _DIGEST.fullmatch(self.expected_parser_facts_sha256) is None:
            raise ValueError("bank statement parser-facts digest is invalid")
        if type(self.expected_size) is not int or self.expected_size <= 0:
            raise ValueError("bank statement source size is invalid")
        if not isinstance(self.evidence_mode, ExistingStatementEvidenceMode):
            raise ValueError("bank statement evidence mode is invalid")
        spec = parser_spec(self.parser_profile)
        if self.institution_code != spec.institution_code:
            raise ValueError("bank statement institution conflicts with parser profile")
        if self.expected_owner_kind.value not in spec.allowed_owner_kinds:
            required = "/".join(sorted(spec.allowed_owner_kinds))
            raise ValueError(f"bank statement owner must be {required}")
        if _ACCOUNT_SUFFIX.fullmatch(self.account_suffix) is None:
            raise ValueError("bank statement managed-account suffix is invalid")
        if self.period_start > self.period_end:
            raise ValueError("bank statement period is invalid")
        if type(self.expected_transaction_count) is not int or self.expected_transaction_count <= 0:
            raise ValueError("bank statement transaction count is invalid")
        if not self.expected_monthly_transaction_counts or any(
            _MONTH.fullmatch(month) is None or type(count) is not int or count <= 0
            for month, count in self.expected_monthly_transaction_counts
        ):
            raise ValueError("bank statement month summary is invalid")
        if tuple(sorted(self.expected_monthly_transaction_counts)) != (
            self.expected_monthly_transaction_counts
        ) or sum(count for _, count in self.expected_monthly_transaction_counts) != (
            self.expected_transaction_count
        ):
            raise ValueError("bank statement month summary conflicts with transaction count")
        _validate_audit_text("actor", self.actor, 200)
        _validate_audit_text("reason", self.reason, 1_000)

    @classmethod
    def bind(
        cls,
        statement: BankStatement,
        *,
        source_path: Path,
        evidence_ref: UUID,
        entity_ref: UUID,
        business_unit_ref: UUID,
        managed_account_ref: UUID,
        expected_owner_kind: EntityType,
        actor: str,
        reason: str,
    ) -> BankStatementExistingAccountPlan:
        """Bind parser-derived facts to explicit existing Evidence/account scope."""

        return cls(
            source_path=source_path,
            expected_sha256=statement.source_sha256,
            expected_size=statement.source_size,
            parser_profile=statement.parser_profile,
            evidence_ref=evidence_ref,
            evidence_mode=ExistingStatementEvidenceMode.REUSE_EXISTING,
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
            managed_account_ref=managed_account_ref,
            institution_code=statement.institution_code,
            account_suffix=statement.account_suffix,
            expected_owner_kind=expected_owner_kind,
            period_start=statement.period_start,
            period_end=statement.period_end,
            expected_transaction_count=len(statement.transactions),
            expected_transaction_set_sha256=statement.transaction_set_sha256,
            expected_parser_facts_sha256=statement.parser_facts_sha256,
            expected_monthly_transaction_counts=statement.monthly_transaction_counts,
            actor=actor,
            reason=reason,
        )

    def require_statement(self, statement: BankStatement) -> None:
        """Reject source, adapter, period, or parser-fact drift before persistence."""

        spec = parser_spec(self.parser_profile)
        if (
            statement.parser_profile is not self.parser_profile
            or statement.institution_code != self.institution_code
            or statement.source_system != spec.source_system
            or statement.declared_media_type != spec.declared_media_type
            or statement.currency != "CNY"
            or statement.source_sha256 != self.expected_sha256
            or statement.source_size != self.expected_size
            or statement.account_suffix != self.account_suffix
            or statement.period_start != self.period_start
            or statement.period_end != self.period_end
            or len(statement.transactions) != self.expected_transaction_count
            or statement.transaction_set_sha256 != self.expected_transaction_set_sha256
            or statement.parser_facts_sha256 != self.expected_parser_facts_sha256
            or statement.monthly_transaction_counts != self.expected_monthly_transaction_counts
        ):
            raise BankStatementPlanError("parsed bank statement conflicts with private plan")

    @property
    def owner_kind(self) -> EntityType:
        """Expose the owner kind through the legacy cutover's structural seam."""

        return self.expected_owner_kind


def _validate_audit_text(field: str, value: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
        or contains_unstorable_text(value)
    ):
        raise ValueError(f"bank statement {field} is invalid")
