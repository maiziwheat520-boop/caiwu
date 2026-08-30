from uuid import UUID

import pytest

from ledgerbridge.candidate_contract import (
    CandidateProjection,
    CandidateStatus,
    ReviewRisk,
    ReviewRiskCode,
)
from ledgerbridge.internal_candidate_command import get_synthetic_review_service
from ledgerbridge.internal_read_contract import (
    READ_CAPABILITIES,
    CandidatePage,
    EntityGrant,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable
from ledgerbridge.original_reconciliation import (
    LegacyReconciliationLayout,
    LegacySlotMapping,
    LegacySourceSlotRule,
    OriginalReconciliationSourceKind,
)
from ledgerbridge.original_reconciliation_reader import (
    InternalReadOriginalReconciliationAdapter,
)

ENTITY_B = UUID("10000000-0000-4000-8000-000000000002")
ENTITY_A = UUID("10000000-0000-4000-8000-000000000001")


def test_internal_reader_keeps_confirmed_as_pending_posting_with_strict_scope() -> None:
    principal = WorkloadPrincipal(
        principal_ref="workload:original-reconciliation-test",
        san_uri="spiffe://ledgerbridge.test/original-reconciliation-test",
        policy_generation=11,
        capabilities=READ_CAPABILITIES,
        grants=(
            EntityGrant(
                entity_ref=ENTITY_B,
                business_unit_refs=frozenset({"unit-demo-b"}),
            ),
        ),
    )
    layout = LegacyReconciliationLayout(
        layout_version="synthetic-layout.v1",
        mapping_version="synthetic-mapping.v1",
        slot_mappings=(
            LegacySlotMapping(
                legacy_slot_ref="slot-confirmed-income",
                coordinate="B5",
                economic_effect="INCOME",
            ),
        ),
        source_slot_rules=(
            LegacySourceSlotRule(
                source_kind=OriginalReconciliationSourceKind.CONFIRMED_CANDIDATE,
                source_code="SETTLEMENT",
                legacy_slot_ref="slot-confirmed-income",
            ),
        ),
    )
    reader = InternalReadOriginalReconciliationAdapter(
        get_synthetic_review_service(),
        layout=layout,
    )

    projection = reader.get(
        principal,
        month="2026-08",
        entity_ref=ENTITY_B,
        business_unit_ref="unit-demo-b",
    )

    assert projection.scope.entity_ref == ENTITY_B
    assert projection.scope.business_unit_ref == "unit-demo-b"
    assert projection.confirmed_pending_posting_count == 1
    assert projection.unmapped_confirmed_count == 0
    assert projection.pending_review_count == 0
    assert projection.missing_material_count == 0
    assert projection.totals.confirmed_candidate_amount_minor == 50_000
    assert projection.posted_ledger_complete is False
    assert projection.totals.posted_income_minor is None
    assert projection.totals.posted_expense_minor is None
    assert projection.totals.posted_profit_minor is None
    assert projection.totals.posted_amount_minor is None
    assert projection.projection_gaps == ("MISSING_TIME_GRANULARITY",)
    assert [
        (source.source_kind, source.source_system, source.fact_count, source.mapped_fact_count)
        for source in projection.sources
    ] == [("CONFIRMED_CANDIDATE", "outlook_mail", 1, 1)]
    cells = {cell.coordinate: cell for row in projection.rows for cell in row.cells}
    assert cells["B5"].kind == "BLANK"
    assert projection.is_complete is False


def test_reader_counts_only_shared_taxonomy_material_codes() -> None:
    principal = WorkloadPrincipal(
        principal_ref="workload:original-reconciliation-material-test",
        san_uri="spiffe://ledgerbridge.test/original-reconciliation-material-test",
        policy_generation=11,
        capabilities=READ_CAPABILITIES,
        grants=(
            EntityGrant(
                entity_ref=ENTITY_A,
                business_unit_refs=frozenset({"unit-demo-a"}),
            ),
        ),
    )
    source = get_synthetic_review_service()

    class RiskReader:
        def list_candidates(
            self,
            principal: WorkloadPrincipal,
            *,
            month: str | None = None,
            status: CandidateStatus | None = None,
            business_unit: str | None = None,
            cursor: str | None = None,
        ) -> CandidatePage:
            if status != CandidateStatus.PENDING:
                return CandidatePage(items=(), next_cursor=None)
            page = source.list_candidates(
                principal,
                month=month,
                status=status,
                business_unit=business_unit,
                cursor=cursor,
            )
            candidate = CandidateProjection.model_validate(
                {
                    **page.items[0].model_dump(),
                    "review_risks": (
                        ReviewRisk(
                            code=ReviewRiskCode.FUNDING_STATEMENT_REQUIRED,
                            message="Synthetic statement is required",
                        ),
                    ),
                }
            )
            return CandidatePage(items=(candidate,), next_cursor=None)

    projection = InternalReadOriginalReconciliationAdapter(
        RiskReader(),
        layout=LegacyReconciliationLayout(
            layout_version="synthetic-layout.v1",
            mapping_version="synthetic-mapping.v1",
        ),
    ).get(
        principal,
        month="2026-08",
        entity_ref=ENTITY_A,
        business_unit_ref="unit-demo-a",
    )

    assert projection.pending_review_count == 1
    assert projection.missing_material_count == 1
    assert projection.projection_gaps == ("MISSING_TIME_GRANULARITY",)
    assert projection.taxonomy_version == "ledgerbridge.financial-foundation-blocker-taxonomy.v1"


def test_reader_fails_closed_when_candidate_adapter_returns_another_entity() -> None:
    principal = WorkloadPrincipal(
        principal_ref="workload:original-reconciliation-isolation-test",
        san_uri="spiffe://ledgerbridge.test/original-reconciliation-isolation-test",
        policy_generation=11,
        capabilities=READ_CAPABILITIES,
        grants=(
            EntityGrant(entity_ref=ENTITY_A, business_unit_refs=frozenset({"unit-demo-a"})),
            EntityGrant(entity_ref=ENTITY_B, business_unit_refs=frozenset({"unit-demo-b"})),
        ),
    )
    source = get_synthetic_review_service()
    foreign = source.list_candidates(
        principal,
        month="2026-08",
        status=CandidateStatus.CONFIRMED,
        business_unit="unit-demo-b",
    ).items[0]

    class ForeignReader:
        def list_candidates(
            self,
            principal: WorkloadPrincipal,
            *,
            month: str | None = None,
            status: CandidateStatus | None = None,
            business_unit: str | None = None,
            cursor: str | None = None,
        ) -> CandidatePage:
            _ = principal, month, business_unit, cursor
            return CandidatePage(
                items=((foreign,) if status == CandidateStatus.CONFIRMED else ()),
                next_cursor=None,
            )

    reader = InternalReadOriginalReconciliationAdapter(
        ForeignReader(),
        layout=LegacyReconciliationLayout(
            layout_version="synthetic-layout.v1",
            mapping_version="synthetic-mapping.v1",
        ),
    )

    with pytest.raises(InternalReadBackendUnavailable):
        reader.get(
            principal,
            month="2026-08",
            entity_ref=ENTITY_A,
            business_unit_ref="unit-demo-a",
        )
