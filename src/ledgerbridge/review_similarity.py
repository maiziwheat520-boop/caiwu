"""Auditable similarity groups for repeated Candidate classification.

Similarity is deliberately narrower than fuzzy merchant matching.  The current
production bridge has no populated counterparty/account registry, so the only
fallback is a versioned, exact seven-field platform summary parser.  The parser
is a provisional batch-review basis; learned rules require registry-backed
counterparty identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date
from enum import StrEnum
from statistics import median
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.candidate_contract import CandidateProjection, CandidateStatus

GROUP_CONTRACT_VERSION: Final[Literal["ledgerbridge.classification-group.v1"]] = (
    "ledgerbridge.classification-group.v1"
)
RULE_CONTRACT_VERSION: Final[Literal["ledgerbridge.classification-rule.v1"]] = (
    "ledgerbridge.classification-rule.v1"
)
KEY_VERSION: Final[Literal["ledgerbridge.classification-key.v1"]] = (
    "ledgerbridge.classification-key.v1"
)
_DATE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
_SPACE = re.compile(r"\s+")
_STRUCTURAL_RISKS = frozenset(
    {
        "FUNDING_STATEMENT_REQUIRED",
        "RELATED_ACCOUNT_STATEMENT_REQUIRED",
        "HOTEL_PAYOUT_STATEMENT_REQUIRED",
        "REVERSAL_MATCH_REQUIRED",
        "UNSETTLED_TRANSACTION",
    }
)
_MANUALLY_RESOLVABLE_GROUP_RISKS = frozenset({"TRANSFER_REVIEW_REQUIRED"})


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClassificationDirection(StrEnum):
    INFLOW = "INFLOW"
    OUTFLOW = "OUTFLOW"
    NEUTRAL = "NEUTRAL"


class SimilarityBasis(StrEnum):
    REGISTRY_COUNTERPARTY = "REGISTRY_COUNTERPARTY"
    EXACT_PLATFORM_SUMMARY_V1 = "EXACT_PLATFORM_SUMMARY_V1"


class GroupExclusionCode(StrEnum):
    NOT_PENDING = "NOT_PENDING"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    BLOCKED = "BLOCKED"
    STRUCTURAL_RISK = "STRUCTURAL_RISK"
    AMOUNT_OUTLIER = "AMOUNT_OUTLIER"


class RuleLearningBlockCode(StrEnum):
    PROVISIONAL_BASIS = "PROVISIONAL_BASIS"
    TERMINAL_DECISION_CONFLICT = "TERMINAL_DECISION_CONFLICT"
    REVIEW_RISK_PRESENT = "REVIEW_RISK_PRESENT"
    AMOUNT_OUTLIER = "AMOUNT_OUTLIER"
    NO_CONFIRMED_SOURCE = "NO_CONFIRMED_SOURCE"


class SimilarityConditions(_FrozenModel):
    key_version: Literal["ledgerbridge.classification-key.v1"] = KEY_VERSION
    entity_ref: UUID
    source_system: str = Field(min_length=1, max_length=100)
    source_kind: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=100)
    direction: ClassificationDirection
    transaction_type: str = Field(min_length=1, max_length=100)
    counterparty_key: str = Field(min_length=1, max_length=200)
    counterparty_label: str = Field(min_length=1, max_length=200)
    counterparty_basis: SimilarityBasis
    funding_instrument: str = Field(min_length=1, max_length=200)
    transaction_status: str = Field(min_length=1, max_length=100)
    currency: Literal["CNY"] = "CNY"
    risk_signature: tuple[str, ...] = ()


class CandidateSimilarity(_FrozenModel):
    group_ref: str = Field(pattern=r"^cg_[0-9a-f]{32}$")
    conditions: SimilarityConditions


class ClassificationGroupMember(_FrozenModel):
    candidate_ref: UUID
    short_id: str
    revision: int = Field(ge=1)
    status: CandidateStatus
    amount_minor: int
    accounting_month: str
    confidence_basis_points: int = Field(ge=0, le=10_000)
    review_risk_codes: tuple[str, ...] = ()
    amount_outlier: bool = False
    batch_eligible: bool
    one_click_eligible: bool
    exclusion_codes: tuple[GroupExclusionCode, ...] = ()


class LearnedClassificationRule(_FrozenModel):
    contract_version: Literal["ledgerbridge.classification-rule.v1"] = RULE_CONTRACT_VERSION
    rule_ref: UUID
    revision: int = Field(ge=1)
    status: Literal["ACTIVE", "DISABLED"]
    group_ref: str = Field(pattern=r"^cg_[0-9a-f]{32}$")
    conditions: SimilarityConditions
    business_unit_ref: str = Field(min_length=1, max_length=100)
    category_code: str = Field(min_length=1, max_length=100)
    source_candidate_ref: UUID
    source_decision_operation_id: UUID
    effective_from: date
    effective_to: date | None = None
    created_at: str
    disabled_at: str | None = None

    @model_validator(mode="after")
    def valid_period_and_status(self) -> LearnedClassificationRule:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("rule effective period is invalid")
        if (self.status == "DISABLED") != (self.disabled_at is not None):
            raise ValueError("disabled rule status requires disabled_at")
        return self


class ClassificationGroup(_FrozenModel):
    contract_version: Literal["ledgerbridge.classification-group.v1"] = GROUP_CONTRACT_VERSION
    group_ref: str = Field(pattern=r"^cg_[0-9a-f]{32}$")
    accounting_month: str
    conditions: SimilarityConditions
    members: tuple[ClassificationGroupMember, ...] = Field(min_length=1)
    batch_member_count: int = Field(ge=0)
    one_click_member_count: int = Field(ge=0)
    terminal_statuses: tuple[CandidateStatus, ...] = ()
    terminal_classifications: tuple[str, ...] = ()
    rule_learning_eligible: bool
    rule_learning_blocks: tuple[RuleLearningBlockCode, ...] = ()
    active_rule: LearnedClassificationRule | None = None


class PlatformSummaryFields(_FrozenModel):
    platform: str
    transaction_date: str
    direction: ClassificationDirection
    transaction_type: str
    counterparty: str
    funding_instrument: str
    transaction_status: str


def parse_exact_platform_summary(summary: str) -> PlatformSummaryFields | None:
    """Parse only the frozen seven-field platform display shape.

    Full-width pipes are accepted as a presentation variant.  Missing fields,
    non-date second fields, and unknown directions are rejected rather than
    treated as fuzzy matches.
    """

    parts = tuple(part.strip() for part in summary.replace("\uff5c", "|").split("|"))
    if len(parts) != 7 or any(not part for part in parts) or _DATE.fullmatch(parts[1]) is None:
        return None
    direction = _direction(parts[2])
    if direction is None:
        return None
    return PlatformSummaryFields(
        platform=_normalize(parts[0]),
        transaction_date=parts[1],
        direction=direction,
        transaction_type=_normalize(parts[3]),
        counterparty=_normalize(parts[4]),
        funding_instrument=_normalize(parts[5]),
        transaction_status=_normalize(parts[6]),
    )


def candidate_similarity(candidate: CandidateProjection) -> CandidateSimilarity | None:
    fields = parse_exact_platform_summary(candidate.summary)
    if (
        fields is None
        or candidate.amount_minor is None
        or candidate.accounting_month is None
        or not _direction_matches_amount(fields.direction, candidate.amount_minor)
    ):
        return None
    if candidate.counterparty_ref is not None:
        basis = SimilarityBasis.REGISTRY_COUNTERPARTY
        counterparty_key = candidate.counterparty_ref
    else:
        basis = SimilarityBasis.EXACT_PLATFORM_SUMMARY_V1
        counterparty_key = f"exact:{fields.counterparty}"
    conditions = SimilarityConditions(
        entity_ref=candidate.entity_ref,
        source_system=_normalize(candidate.source.source_system),
        source_kind=candidate.source.ingest_channel.value,
        platform=fields.platform,
        direction=fields.direction,
        transaction_type=fields.transaction_type,
        counterparty_key=counterparty_key,
        counterparty_label=fields.counterparty,
        counterparty_basis=basis,
        funding_instrument=fields.funding_instrument,
        transaction_status=fields.transaction_status,
        currency=candidate.currency,
        risk_signature=tuple(sorted(risk.code.value for risk in candidate.review_risks)),
    )
    key_payload = conditions.model_dump(mode="json", exclude={"counterparty_label"})
    canonical = json.dumps(key_payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return CandidateSimilarity(group_ref=f"cg_{digest}", conditions=conditions)


def build_classification_groups(
    candidates: tuple[CandidateProjection, ...],
    *,
    active_rules: tuple[LearnedClassificationRule, ...] = (),
) -> tuple[ClassificationGroup, ...]:
    """Build same-month review groups from Core-owned Candidate projections."""

    grouped: defaultdict[tuple[str, str], list[tuple[CandidateProjection, CandidateSimilarity]]]
    grouped = defaultdict(list)
    for candidate in candidates:
        similarity = candidate_similarity(candidate)
        if similarity is None or candidate.accounting_month is None:
            continue
        grouped[(similarity.group_ref, candidate.accounting_month)].append((candidate, similarity))

    active_by_group = {rule.group_ref: rule for rule in active_rules if rule.status == "ACTIVE"}
    result: list[ClassificationGroup] = []
    for (group_ref, accounting_month), values in grouped.items():
        values.sort(key=lambda item: (item[0].created_at, item[0].candidate_ref.int))
        conditions = values[0][1].conditions
        amounts = [abs(item[0].amount_minor or 0) for item in values]
        outlier_indexes = _amount_outlier_indexes(amounts)
        members: list[ClassificationGroupMember] = []
        terminal_statuses: set[CandidateStatus] = set()
        terminal_classifications: set[str] = set()
        for index, (candidate, _) in enumerate(values):
            risks = tuple(sorted(risk.code.value for risk in candidate.review_risks))
            exclusions: list[GroupExclusionCode] = []
            if candidate.status != CandidateStatus.PENDING:
                exclusions.append(GroupExclusionCode.NOT_PENDING)
                terminal_statuses.add(candidate.status)
                terminal_classifications.add(
                    f"{candidate.status.value}:{candidate.business_unit_ref or '-'}:"
                    f"{candidate.category_code or '-'}"
                )
            if candidate.confidence_basis_points < 9_000:
                exclusions.append(GroupExclusionCode.LOW_CONFIDENCE)
            if candidate.blockers:
                exclusions.append(GroupExclusionCode.BLOCKED)
            if set(risks) & _STRUCTURAL_RISKS:
                exclusions.append(GroupExclusionCode.STRUCTURAL_RISK)
            if index in outlier_indexes:
                exclusions.append(GroupExclusionCode.AMOUNT_OUTLIER)
            batch_eligible = not exclusions
            one_click_eligible = batch_eligible and not risks
            # TRANSFER_REVIEW_REQUIRED is intentionally a manually acknowledged
            # classification risk.  It can be resolved by one explicit group
            # action, but never by one-click or learned-rule automation.
            if set(risks) - _MANUALLY_RESOLVABLE_GROUP_RISKS and risks:
                batch_eligible = False
            members.append(
                ClassificationGroupMember(
                    candidate_ref=candidate.candidate_ref,
                    short_id=candidate.short_id,
                    revision=candidate.revision,
                    status=candidate.status,
                    amount_minor=candidate.amount_minor or 0,
                    accounting_month=accounting_month,
                    confidence_basis_points=candidate.confidence_basis_points,
                    review_risk_codes=risks,
                    amount_outlier=index in outlier_indexes,
                    batch_eligible=batch_eligible,
                    one_click_eligible=one_click_eligible,
                    exclusion_codes=tuple(exclusions),
                )
            )

        rule_blocks: list[RuleLearningBlockCode] = []
        if conditions.counterparty_basis != SimilarityBasis.REGISTRY_COUNTERPARTY:
            rule_blocks.append(RuleLearningBlockCode.PROVISIONAL_BASIS)
        if CandidateStatus.IGNORED in terminal_statuses or len(terminal_classifications) > 1:
            rule_blocks.append(RuleLearningBlockCode.TERMINAL_DECISION_CONFLICT)
        if conditions.risk_signature:
            rule_blocks.append(RuleLearningBlockCode.REVIEW_RISK_PRESENT)
        if outlier_indexes:
            rule_blocks.append(RuleLearningBlockCode.AMOUNT_OUTLIER)
        if CandidateStatus.CONFIRMED not in terminal_statuses:
            rule_blocks.append(RuleLearningBlockCode.NO_CONFIRMED_SOURCE)

        batch_count = sum(member.batch_eligible for member in members)
        one_click_count = sum(member.one_click_eligible for member in members)
        result.append(
            ClassificationGroup(
                group_ref=group_ref,
                accounting_month=accounting_month,
                conditions=conditions,
                members=tuple(members),
                batch_member_count=batch_count,
                one_click_member_count=one_click_count,
                terminal_statuses=tuple(sorted(terminal_statuses, key=lambda item: item.value)),
                terminal_classifications=tuple(sorted(terminal_classifications)),
                rule_learning_eligible=not rule_blocks,
                rule_learning_blocks=tuple(rule_blocks),
                active_rule=active_by_group.get(group_ref),
            )
        )
    result.sort(key=lambda group: (group.accounting_month, group.group_ref))
    return tuple(result)


def _normalize(value: str) -> str:
    return _SPACE.sub(" ", value.strip()).casefold()


def _direction(value: str) -> ClassificationDirection | None:
    normalized = _normalize(value)
    if normalized in {"收入", "退款收入", "income", "inflow"}:
        return ClassificationDirection.INFLOW
    if normalized in {"支出", "expense", "outflow"}:
        return ClassificationDirection.OUTFLOW
    if normalized in {"不计收支", "neutral"}:
        return ClassificationDirection.NEUTRAL
    return None


def _direction_matches_amount(direction: ClassificationDirection, amount_minor: int) -> bool:
    if direction == ClassificationDirection.INFLOW:
        return amount_minor >= 0
    if direction == ClassificationDirection.OUTFLOW:
        return amount_minor <= 0
    return amount_minor == 0


def _amount_outlier_indexes(amounts: list[int]) -> frozenset[int]:
    if len(amounts) < 3:
        return frozenset()
    middle = int(median(amounts))
    if middle == 0:
        return frozenset(index for index, amount in enumerate(amounts) if amount != 0)
    threshold = max(100_000, middle * 10)
    return frozenset(index for index, amount in enumerate(amounts) if amount >= threshold)
