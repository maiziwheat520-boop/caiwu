"""Read adapter from the authorized Candidate projection into the legacy grid module."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from ledgerbridge.candidate_contract import CandidateProjection, CandidateStatus
from ledgerbridge.financial_foundation_blocker_taxonomy import classify_missing_material
from ledgerbridge.internal_read_contract import (
    CandidatePage,
    Capability,
    WorkloadPrincipal,
    authorize_read,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable
from ledgerbridge.original_reconciliation import (
    LegacyReconciliationLayout,
    OriginalReconciliationFact,
    OriginalReconciliationInput,
    OriginalReconciliationProjection,
    OriginalReconciliationReviewItem,
    OriginalReconciliationScope,
    OriginalReconciliationSourceKind,
    build_original_reconciliation,
)


class CandidateReadPort(Protocol):
    def list_candidates(
        self,
        principal: WorkloadPrincipal,
        *,
        month: str | None = None,
        status: CandidateStatus | None = None,
        business_unit: str | None = None,
        cursor: str | None = None,
    ) -> CandidatePage: ...


class InternalReadOriginalReconciliationAdapter:
    """Consume only scope-checked Candidate projections through one deep interface.

    Existing ledger summaries are intentionally not consumed: their category
    aggregates do not expose the unique primary ``posting.id`` required for a
    POSTED_LEDGER fact identity. A later posted-fact adapter can add those facts
    behind this interface without changing the HTTP projection. The interface keeps
    three states distinct: an unavailable posted reader yields null formal totals;
    an explicitly complete empty identity set yields zero; and a future missing
    business-unit posting snapshot yields a breakdown GAP without joining today's
    dimension labels back onto historical postings.
    """

    def __init__(
        self,
        candidate_reader: CandidateReadPort,
        *,
        layout: LegacyReconciliationLayout,
    ) -> None:
        self._candidate_reader = candidate_reader
        self._layout = layout

    def get(
        self,
        principal: WorkloadPrincipal,
        *,
        month: str,
        entity_ref: UUID,
        business_unit_ref: str,
    ) -> OriginalReconciliationProjection:
        authorize_read(
            principal,
            Capability.RECONCILIATION_READ,
            entity_ref=entity_ref,
            business_unit_ref=business_unit_ref,
        )
        rules = {
            (rule.source_kind, rule.source_code): rule.legacy_slot_ref
            for rule in self._layout.source_slot_rules
        }
        candidates: dict[UUID, CandidateProjection] = {}
        for candidate_status in (
            CandidateStatus.CONFIRMED,
            CandidateStatus.PENDING,
            CandidateStatus.INCOMPLETE,
            CandidateStatus.CONFLICTED,
        ):
            cursor: str | None = None
            seen_cursors: set[str] = set()
            while True:
                page = self._candidate_reader.list_candidates(
                    principal,
                    month=month,
                    status=candidate_status,
                    business_unit=business_unit_ref,
                    cursor=cursor,
                )
                for candidate in page.items:
                    if (
                        candidate.entity_ref != entity_ref
                        or candidate.business_unit_ref != business_unit_ref
                        or candidate.accounting_month != month
                        or candidate.status != candidate_status
                    ):
                        raise InternalReadBackendUnavailable(
                            "candidate projection escaped original-reconciliation scope"
                        )
                    existing = candidates.get(candidate.candidate_ref)
                    if existing is not None and existing != candidate:
                        raise InternalReadBackendUnavailable(
                            "candidate identity changed during original-reconciliation read"
                        )
                    candidates[candidate.candidate_ref] = candidate
                if page.next_cursor is None:
                    break
                if page.next_cursor in seen_cursors:
                    raise InternalReadBackendUnavailable("candidate pagination repeated a cursor")
                seen_cursors.add(page.next_cursor)
                cursor = page.next_cursor

        facts: list[OriginalReconciliationFact] = []
        review_items: list[OriginalReconciliationReviewItem] = []
        missing_material_count = 0
        for candidate in sorted(candidates.values(), key=lambda item: item.candidate_ref.int):
            codes = [blocker.code.value for blocker in candidate.blockers]
            codes.extend(risk.code.value for risk in candidate.review_risks)
            missing_material_count += sum(
                1 for code in codes if classify_missing_material(code) is not None
            )
            if candidate.status == CandidateStatus.CONFIRMED:
                if candidate.amount_minor is None or candidate.category_code is None:
                    raise InternalReadBackendUnavailable(
                        "confirmed candidate lacks a complete amount/category projection"
                    )
                source_kind = OriginalReconciliationSourceKind.CONFIRMED_CANDIDATE
                candidate_ref = str(candidate.candidate_ref)
                facts.append(
                    OriginalReconciliationFact(
                        fact_ref=candidate_ref,
                        canonical_fact_ref=candidate_ref,
                        source_kind=source_kind,
                        source_system=candidate.source.source_system,
                        source_label=candidate.source.display_label,
                        entity_ref=candidate.entity_ref,
                        business_unit_ref=business_unit_ref,
                        month=month,
                        amount_minor=candidate.amount_minor,
                        legacy_slot_ref=rules.get((source_kind, candidate.category_code)),
                    )
                )
                continue
            review_items.append(
                OriginalReconciliationReviewItem(
                    review_ref=str(candidate.candidate_ref),
                    status=candidate.status.value,  # type: ignore[arg-type]
                    entity_ref=candidate.entity_ref,
                    business_unit_ref=business_unit_ref,
                    month=month,
                )
            )

        return build_original_reconciliation(
            OriginalReconciliationInput(
                month=month,
                scope=OriginalReconciliationScope(
                    entity_ref=entity_ref,
                    business_unit_ref=business_unit_ref,
                ),
                facts=tuple(facts),
                review_items=tuple(review_items),
                declared_missing_material_count=missing_material_count,
                projection_gaps=("MISSING_TIME_GRANULARITY",),
                posted_ledger_complete=False,
                layout=self._layout,
            )
        )
