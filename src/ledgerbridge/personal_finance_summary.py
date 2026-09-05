"""Personal finance summary over confirmed candidates.

These rules decide which confirmed candidates represent personal cash movement,
collapse the same movement seen through both a bank and a platform, and total
what is left. They ran in the browser, which meant the page walked every
authorized candidate page before it could show a single number -- nineteen
round trips and about 1.5 s against 1,825 production candidates, growing with
the ledger.

The rules are transliterated from the browser implementation without
behavioural change. One quirk is load-bearing and preserved deliberately: the
direction stated in the summary text wins over the sign of the amount, so a
row that says 收入 counts as income even if its amount is negative. Changing
that is a decision about what the numbers mean, not a refactor.

Only confirmed candidates become entries, and Core will not confirm one that is
missing an amount, a category, a month or a business unit. The null handling
below is therefore defensive rather than reachable -- in particular the web
boundary's habit of projecting a null amount as zero cannot affect a total
here, because such a candidate is excluded one step earlier.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Literal

from pydantic import Field

from ledgerbridge.candidate_contract import CandidateProjection, CandidateStatus
from ledgerbridge.internal_read_contract import _FrozenModel

SourceKind = Literal["PLATFORM", "BANK"]
ScopeStatus = Literal["PERSONAL", "UNASSIGNED"]

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BUSINESS_SCOPE = re.compile(r"公司|酒店|宾馆|门店|company|hotel", re.IGNORECASE)
_PERSONAL_SCOPE = re.compile(r"个人|本人|personal", re.IGNORECASE)
_TRANSFER_TYPE = re.compile(r"转账|提现")
_PLATFORM_SOURCES = frozenset({"微信", "支付宝"})
_REVIEW_PENDING = frozenset(
    {CandidateStatus.PENDING, CandidateStatus.INCOMPLETE, CandidateStatus.CONFLICTED}
)
_UNCLASSIFIED_CATEGORY = "待分类"


class PersonalEntry(_FrozenModel):
    """One personal cash movement, with everything the page renders."""

    candidate_ref: str
    short_id: str
    business_unit_label: str
    category_label: str
    accounting_month: str | None
    summary: str
    cashflow_minor: int
    date: str
    transaction_type: str
    counterparty: str
    source_kind: SourceKind
    scope_status: ScopeStatus


class PendingCandidate(_FrozenModel):
    candidate_ref: str
    short_id: str
    business_unit_label: str
    category_label: str
    accounting_month: str | None
    summary: str
    status: CandidateStatus


class CategoryShare(_FrozenModel):
    category: str
    amount_minor: int
    basis_points: int = Field(ge=0, le=10_000)


class MonthlyTotal(_FrozenModel):
    month: str
    income_minor: int
    expense_minor: int
    net_minor: int


class PersonalFinanceSummary(_FrozenModel):
    contract_version: Literal["ledgerbridge.personal-finance-summary.v1"] = (
        "ledgerbridge.personal-finance-summary.v1"
    )
    candidate_total: int
    pending_total: int
    pending_preview: tuple[PendingCandidate, ...] = Field(max_length=4)
    entry_total: int
    income_minor: int
    expense_minor: int
    net_minor: int
    income_entry_count: int
    expense_entry_count: int
    evidence_count: int
    excluded_count: int
    deduplicated_count: int
    unassigned_entries: tuple[PersonalEntry, ...] = Field(max_length=1000)
    category_shares: tuple[CategoryShare, ...] = Field(max_length=200)
    monthly_totals: tuple[MonthlyTotal, ...] = Field(max_length=200)


def _summary_fields(candidate: CandidateProjection) -> list[str]:
    return [value.strip() for value in candidate.summary.split("|")]


def _cashflow_minor(candidate: CandidateProjection) -> int:
    # The web boundary has always projected an unknown amount as zero.
    amount = candidate.amount_minor if candidate.amount_minor is not None else 0
    fields = _summary_fields(candidate)
    stated = fields[2] if len(fields) > 2 else None
    if stated == "收入":
        return abs(amount)
    if stated == "支出":
        return -abs(amount)
    return amount


def _source_kind(source: str) -> SourceKind | None:
    if source in _PLATFORM_SOURCES:
        return "PLATFORM"
    if "银行" in source:
        return "BANK"
    return None


def _personal_entry(candidate: CandidateProjection) -> PersonalEntry | None:
    if candidate.status != CandidateStatus.CONFIRMED:
        return None
    fields = _summary_fields(candidate)
    if len(fields) < 7 or _ISO_DATE.fullmatch(fields[1]) is None:
        return None
    source_kind = _source_kind(fields[0])
    if source_kind is None:
        return None

    label = candidate.business_unit_label or ""
    ref = candidate.business_unit_ref or ""
    scope = f"{label} {ref}".strip()
    if _BUSINESS_SCOPE.search(scope) is not None:
        return None

    return PersonalEntry(
        candidate_ref=str(candidate.candidate_ref),
        short_id=candidate.short_id,
        business_unit_label=label,
        category_label=candidate.category_label or "",
        accounting_month=candidate.accounting_month,
        summary=candidate.summary,
        cashflow_minor=_cashflow_minor(candidate),
        date=fields[1],
        transaction_type=fields[3],
        counterparty=fields[4],
        source_kind=source_kind,
        scope_status="PERSONAL" if _PERSONAL_SCOPE.search(scope) is not None else "UNASSIGNED",
    )


def _deduplicate(entries: list[PersonalEntry]) -> tuple[list[PersonalEntry], int]:
    """Collapse one movement seen through both a bank and a platform.

    A transfer or withdrawal is authoritative on the bank side; anything else is
    authoritative on the platform side. Only the paired count is dropped, so an
    unmatched surplus on either side survives.
    """
    groups: dict[tuple[str, str, int, str], list[PersonalEntry]] = defaultdict(list)
    for entry in entries:
        direction = "OUT" if entry.cashflow_minor < 0 else "IN"
        groups[(entry.date, direction, abs(entry.cashflow_minor), entry.counterparty)].append(entry)

    deduplicated: list[PersonalEntry] = []
    deduplicated_count = 0
    for group in groups.values():
        is_transfer = any(
            _TRANSFER_TYPE.search(entry.transaction_type) is not None for entry in group
        )
        preferred_kind: SourceKind = "BANK" if is_transfer else "PLATFORM"
        preferred = [entry for entry in group if entry.source_kind == preferred_kind]
        lower_priority = [entry for entry in group if entry.source_kind != preferred_kind]
        if not preferred or not lower_priority:
            deduplicated.extend(group)
            continue
        paired = min(len(preferred), len(lower_priority))
        deduplicated_count += paired
        deduplicated.extend(preferred)
        deduplicated.extend(lower_priority[paired:])
    return deduplicated, deduplicated_count


def _pending_candidate(candidate: CandidateProjection) -> PendingCandidate:
    return PendingCandidate(
        candidate_ref=str(candidate.candidate_ref),
        short_id=candidate.short_id,
        business_unit_label=candidate.business_unit_label or "",
        category_label=candidate.category_label or "",
        accounting_month=candidate.accounting_month,
        summary=candidate.summary,
        status=candidate.status,
    )


def build_personal_finance_summary(
    candidates: tuple[CandidateProjection, ...],
) -> PersonalFinanceSummary:
    eligible = [entry for entry in map(_personal_entry, candidates) if entry is not None]
    excluded_count = len(candidates) - len(eligible)
    deduplicated, deduplicated_count = _deduplicate(eligible)

    entries = [entry for entry in deduplicated if entry.scope_status == "PERSONAL"]
    unassigned = [entry for entry in deduplicated if entry.scope_status == "UNASSIGNED"]
    counted = entries + unassigned

    income_minor = sum(max(entry.cashflow_minor, 0) for entry in counted)
    expense_minor = sum(abs(min(entry.cashflow_minor, 0)) for entry in counted)

    by_ref = {str(candidate.candidate_ref): candidate for candidate in candidates}
    evidence_refs = {
        str(reference.evidence_ref)
        for entry in counted
        for reference in by_ref[entry.candidate_ref].evidence
    }

    category_totals: dict[str, int] = defaultdict(int)
    for entry in counted:
        amount = abs(entry.cashflow_minor)
        if amount == 0:
            continue
        category_totals[entry.category_label.strip() or _UNCLASSIFIED_CATEGORY] += amount
    categorized_total = sum(category_totals.values())
    category_shares = tuple(
        CategoryShare(
            category=category,
            amount_minor=amount,
            # Percentages are formatted for display; carrying basis points
            # keeps the wire free of a float that would round differently.
            basis_points=round(amount * 10_000 / categorized_total) if categorized_total else 0,
        )
        for category, amount in sorted(
            category_totals.items(), key=lambda item: (-item[1], item[0])
        )
    )

    monthly: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for entry in counted:
        if entry.accounting_month is None:
            continue
        if entry.cashflow_minor >= 0:
            monthly[entry.accounting_month][0] += entry.cashflow_minor
        else:
            monthly[entry.accounting_month][1] += abs(entry.cashflow_minor)
    monthly_totals = tuple(
        MonthlyTotal(
            month=month,
            income_minor=totals[0],
            expense_minor=totals[1],
            net_minor=totals[0] - totals[1],
        )
        for month, totals in sorted(monthly.items(), reverse=True)
    )

    pending = [candidate for candidate in candidates if candidate.status in _REVIEW_PENDING]

    return PersonalFinanceSummary(
        candidate_total=len(candidates),
        pending_total=len(pending),
        pending_preview=tuple(_pending_candidate(candidate) for candidate in pending[:4]),
        entry_total=len(counted),
        income_minor=income_minor,
        expense_minor=expense_minor,
        net_minor=income_minor - expense_minor,
        income_entry_count=sum(1 for entry in counted if entry.cashflow_minor >= 0),
        expense_entry_count=sum(1 for entry in counted if entry.cashflow_minor < 0),
        evidence_count=len(evidence_refs),
        excluded_count=excluded_count,
        deduplicated_count=deduplicated_count,
        unassigned_entries=tuple(unassigned),
        category_shares=category_shares,
        monthly_totals=monthly_totals,
    )
