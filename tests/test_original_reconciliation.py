from uuid import UUID

from ledgerbridge.original_reconciliation import (
    LegacyDerivedCellMapping,
    LegacyLabelCell,
    LegacyReconciliationLayout,
    LegacySlotMapping,
    OriginalReconciliationFact,
    OriginalReconciliationInput,
    OriginalReconciliationReviewItem,
    OriginalReconciliationScope,
    OriginalReconciliationSourceKind,
    build_original_reconciliation,
)

ENTITY_A = UUID("10000000-0000-4000-8000-000000000001")


def test_empty_projection_preserves_the_legacy_grid_without_guessing_balances() -> None:
    projection = build_original_reconciliation(
        OriginalReconciliationInput(
            month="2026-05",
            posted_ledger_complete=False,
            scope=OriginalReconciliationScope(
                entity_ref=ENTITY_A,
                business_unit_ref="unit-a",
            ),
            layout=LegacyReconciliationLayout(
                layout_version="synthetic-layout.v1",
                mapping_version="synthetic-mapping.v1",
                derived_cells=(
                    LegacyDerivedCellMapping(
                        coordinate="D2",
                        value_role="POSTED_PROFIT",
                    ),
                ),
            ),
        )
    )

    assert [column.column for column in projection.columns] == list("ABCDEFGHIJKLM")
    assert [column.role for column in projection.columns] == [
        "MAIN",
        "MAIN",
        "MAIN",
        "MAIN",
        "MAIN",
        "SPACER",
        "SPACER",
        "DETAIL",
        "DETAIL",
        "DETAIL",
        "DETAIL",
        "DETAIL",
        "DETAIL",
    ]
    assert len(projection.rows) == 40
    assert all(len(row.cells) == 13 for row in projection.rows)
    assert [cell.coordinate for cell in projection.rows[0].cells] == [
        f"{column}1" for column in "ABCDEFGHIJKLM"
    ]
    assert all(row.cells[5].kind == "BLANK" for row in projection.rows)
    assert all(row.cells[6].kind == "BLANK" for row in projection.rows)
    assert projection.totals.model_dump() == {
        "posted_income_minor": None,
        "posted_expense_minor": None,
        "posted_profit_minor": None,
        "opening_balance_minor": None,
        "closing_balance_minor": None,
        "mapped_cell_count": 0,
        "confirmed_candidate_amount_minor": 0,
        "posted_amount_minor": None,
        "currency": "CNY",
    }
    assert projection.is_complete is False
    assert projection.posted_ledger_complete is False
    assert projection.rows[1].cells[3].kind == "GAP"
    assert projection.rows[1].cells[3].gap_code == "POSTED_LEDGER_UNAVAILABLE"
    assert projection.taxonomy_version == "ledgerbridge.financial-foundation-blocker-taxonomy.v1"


def test_complete_posted_reader_with_an_explicit_empty_set_returns_zero_totals() -> None:
    projection = build_original_reconciliation(
        OriginalReconciliationInput(
            month="2026-05",
            posted_ledger_complete=True,
            scope=OriginalReconciliationScope(
                entity_ref=ENTITY_A,
                business_unit_ref="unit-a",
            ),
            layout=LegacyReconciliationLayout(
                layout_version="synthetic-layout.v1",
                mapping_version="synthetic-mapping.v1",
            ),
        )
    )

    assert projection.totals.posted_income_minor == 0
    assert projection.totals.posted_expense_minor == 0
    assert projection.totals.posted_profit_minor == 0
    assert projection.totals.posted_amount_minor == 0
    assert projection.is_complete is False  # balances remain unknown


def test_explicit_posted_slots_preserve_signs_sources_and_balance_gaps() -> None:
    projection = build_original_reconciliation(
        OriginalReconciliationInput(
            month="2026-05",
            posted_ledger_complete=True,
            scope=OriginalReconciliationScope(
                entity_ref=ENTITY_A,
                business_unit_ref="unit-a",
            ),
            facts=(
                OriginalReconciliationFact(
                    fact_ref="candidate-income",
                    canonical_fact_ref="fact-income",
                    source_kind=OriginalReconciliationSourceKind.POSTED_LEDGER,
                    source_system="ledger-posted-aggregate",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=10_000,
                    legacy_slot_ref="slot-income",
                ),
                OriginalReconciliationFact(
                    fact_ref="candidate-expense",
                    canonical_fact_ref="fact-expense",
                    source_kind=OriginalReconciliationSourceKind.POSTED_LEDGER,
                    source_system="ledger-posted-aggregate",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=-2_500,
                    legacy_slot_ref="slot-expense",
                ),
            ),
            layout=LegacyReconciliationLayout(
                layout_version="synthetic-layout.v1",
                mapping_version="synthetic-mapping.v1",
                slot_mappings=(
                    LegacySlotMapping(
                        legacy_slot_ref="slot-income",
                        coordinate="B5",
                        economic_effect="INCOME",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-expense",
                        coordinate="C5",
                        economic_effect="EXPENSE",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-opening",
                        coordinate="D39",
                        economic_effect="BALANCE",
                        balance_position="OPENING",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-closing",
                        coordinate="D40",
                        economic_effect="BALANCE",
                        balance_position="CLOSING",
                    ),
                ),
            ),
        )
    )

    cells = {cell.coordinate: cell for row in projection.rows for cell in row.cells}
    assert cells["B5"].amount_minor == 10_000
    assert cells["C5"].amount_minor == -2_500
    assert cells["D39"].model_dump() | {"source_fact_refs": ()} == {
        "coordinate": "D39",
        "column": "D",
        "row_number": 39,
        "kind": "GAP",
        "label": None,
        "amount_minor": None,
        "currency": None,
        "gap_code": "MISSING_BALANCE_MAPPING",
        "source_fact_refs": (),
    }
    assert cells["D40"].kind == "GAP"
    assert projection.totals.model_dump() == {
        "posted_income_minor": 10_000,
        "posted_expense_minor": 2_500,
        "posted_profit_minor": 7_500,
        "opening_balance_minor": None,
        "closing_balance_minor": None,
        "mapped_cell_count": 2,
        "confirmed_candidate_amount_minor": 0,
        "posted_amount_minor": 7_500,
        "currency": "CNY",
    }
    assert [source.model_dump() for source in projection.sources] == [
        {
            "source_kind": "POSTED_LEDGER",
            "source_system": "ledger-posted-aggregate",
            "source_label": None,
            "fact_count": 2,
            "mapped_fact_count": 2,
            "amount_minor": 7_500,
        }
    ]


def test_only_posted_layer_enters_formal_totals_without_hiding_confirmed_lineage() -> None:
    def shared_fact(
        fact_ref: str,
        source_kind: OriginalReconciliationSourceKind,
        source_system: str,
        *,
        legacy_slot_ref: str | None = None,
    ) -> OriginalReconciliationFact:
        return OriginalReconciliationFact(
            fact_ref=fact_ref,
            canonical_fact_ref="fact-shared",
            source_kind=source_kind,
            source_system=source_system,
            entity_ref=ENTITY_A,
            business_unit_ref="unit-a",
            month="2026-05",
            amount_minor=5_000,
            legacy_slot_ref=legacy_slot_ref,
        )

    projection = build_original_reconciliation(
        OriginalReconciliationInput(
            month="2026-05",
            posted_ledger_complete=True,
            scope=OriginalReconciliationScope(
                entity_ref=ENTITY_A,
                business_unit_ref="unit-a",
            ),
            facts=(
                shared_fact(
                    "candidate-shared",
                    OriginalReconciliationSourceKind.CONFIRMED_CANDIDATE,
                    "synthetic-inbox",
                ),
                OriginalReconciliationFact(
                    canonical_fact_ref="fact-confirmed-only",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=1_000,
                    fact_ref="candidate-confirmed-only",
                    source_kind=OriginalReconciliationSourceKind.CONFIRMED_CANDIDATE,
                    source_system="synthetic-inbox",
                    legacy_slot_ref="slot-income",
                ),
                shared_fact(
                    "posted-shared-a",
                    OriginalReconciliationSourceKind.POSTED_LEDGER,
                    "ledger-posted-aggregate",
                    legacy_slot_ref="slot-income",
                ),
                shared_fact(
                    "posted-shared-b",
                    OriginalReconciliationSourceKind.POSTED_LEDGER,
                    "ledger-posted-aggregate",
                    legacy_slot_ref="slot-income",
                ),
            ),
            review_items=(
                OriginalReconciliationReviewItem(
                    review_ref="pending-one",
                    status="PENDING",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    missing_material_count=2,
                ),
            ),
            layout=LegacyReconciliationLayout(
                layout_version="synthetic-layout.v1",
                mapping_version="synthetic-mapping.v1",
                slot_mappings=(
                    LegacySlotMapping(
                        legacy_slot_ref="slot-income",
                        coordinate="B5",
                        economic_effect="INCOME",
                    ),
                ),
            ),
        )
    )

    cells = {cell.coordinate: cell for row in projection.rows for cell in row.cells}
    assert cells["B5"].amount_minor == 5_000
    assert cells["B5"].source_fact_refs == ("posted-shared-a",)
    assert projection.totals.posted_income_minor == 5_000
    assert projection.totals.posted_profit_minor == 5_000
    assert projection.totals.confirmed_candidate_amount_minor == 6_000
    assert projection.totals.posted_amount_minor == 5_000
    assert projection.pending_review_count == 1
    assert projection.missing_material_count == 2
    assert projection.unmapped_confirmed_count == 1
    assert projection.is_complete is False
    assert {source.source_kind: source.model_dump() for source in projection.sources} == {
        "CONFIRMED_CANDIDATE": {
            "source_kind": "CONFIRMED_CANDIDATE",
            "source_system": "synthetic-inbox",
            "source_label": None,
            "fact_count": 2,
            "mapped_fact_count": 1,
            "amount_minor": 6_000,
        },
        "POSTED_LEDGER": {
            "source_kind": "POSTED_LEDGER",
            "source_system": "ledger-posted-aggregate",
            "source_label": None,
            "fact_count": 1,
            "mapped_fact_count": 1,
            "amount_minor": 5_000,
        },
    }


def test_economic_effects_drive_posted_profit_and_missing_effect_is_a_gap() -> None:
    scope = OriginalReconciliationScope(
        entity_ref=ENTITY_A,
        business_unit_ref="unit-a",
    )

    def fact(
        fact_ref: str,
        amount_minor: int,
        slot: str,
        *,
        source_kind: OriginalReconciliationSourceKind = (
            OriginalReconciliationSourceKind.POSTED_LEDGER
        ),
    ) -> OriginalReconciliationFact:
        return OriginalReconciliationFact(
            fact_ref=fact_ref,
            canonical_fact_ref=fact_ref,
            source_kind=source_kind,
            source_system=(
                "ledger-posted-aggregate" if source_kind == "POSTED_LEDGER" else "synthetic-inbox"
            ),
            entity_ref=ENTITY_A,
            business_unit_ref="unit-a",
            month="2026-05",
            amount_minor=amount_minor,
            legacy_slot_ref=slot,
        )

    projection = build_original_reconciliation(
        OriginalReconciliationInput(
            month="2026-05",
            posted_ledger_complete=True,
            scope=scope,
            facts=(
                fact("posted-expense", -1_000, "slot-expense"),
                fact("posted-refund", 1_250, "slot-refund"),
                fact("posted-transfer", 999, "slot-transfer"),
                fact("posted-unknown", -400, "slot-unknown"),
                fact(
                    "confirmed-income",
                    10_000,
                    "slot-income",
                    source_kind=OriginalReconciliationSourceKind.CONFIRMED_CANDIDATE,
                ),
            ),
            layout=LegacyReconciliationLayout(
                layout_version="synthetic-layout.v1",
                mapping_version="synthetic-mapping.v1",
                slot_mappings=(
                    LegacySlotMapping(
                        legacy_slot_ref="slot-expense",
                        coordinate="B10",
                        economic_effect="EXPENSE",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-refund",
                        coordinate="B11",
                        economic_effect="EXPENSE_REFUND",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-transfer",
                        coordinate="B12",
                        economic_effect="NO_EFFECT",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-unknown",
                        coordinate="B13",
                        economic_effect=None,
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-income",
                        coordinate="B14",
                        economic_effect="INCOME",
                    ),
                ),
            ),
        )
    )

    cells = {cell.coordinate: cell for row in projection.rows for cell in row.cells}
    assert cells["B10"].amount_minor == -1_000
    assert cells["B11"].amount_minor == 1_250
    assert cells["B12"].amount_minor == 999
    assert cells["B13"].kind == "GAP"
    assert cells["B13"].gap_code == "MISSING_ECONOMIC_EFFECT"
    assert cells["B14"].kind == "BLANK"
    assert projection.totals.posted_income_minor == 0
    assert projection.totals.posted_expense_minor == -250
    assert projection.totals.posted_profit_minor == 250
    assert projection.totals.confirmed_candidate_amount_minor == 10_000
    assert projection.totals.posted_amount_minor == 849
    assert projection.totals.mapped_cell_count == 3


def test_private_layout_preserves_labels_and_derives_totals_without_double_counting() -> None:
    scope = OriginalReconciliationScope(
        entity_ref=ENTITY_A,
        business_unit_ref="unit-a",
    )
    projection = build_original_reconciliation(
        OriginalReconciliationInput(
            month="2026-05",
            posted_ledger_complete=True,
            scope=scope,
            facts=(
                OriginalReconciliationFact(
                    fact_ref="posted-income",
                    canonical_fact_ref="posting-income",
                    source_kind=OriginalReconciliationSourceKind.POSTED_LEDGER,
                    source_system="synthetic-ledger",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=10_000,
                    legacy_slot_ref="slot-income",
                ),
                OriginalReconciliationFact(
                    fact_ref="posted-expense",
                    canonical_fact_ref="posting-expense",
                    source_kind=OriginalReconciliationSourceKind.POSTED_LEDGER,
                    source_system="synthetic-ledger",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=-2_500,
                    legacy_slot_ref="slot-expense",
                ),
                OriginalReconciliationFact(
                    fact_ref="posted-opening",
                    canonical_fact_ref="posting-opening",
                    source_kind=OriginalReconciliationSourceKind.POSTED_LEDGER,
                    source_system="synthetic-ledger",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=50_000,
                    legacy_slot_ref="slot-opening",
                ),
                OriginalReconciliationFact(
                    fact_ref="posted-closing",
                    canonical_fact_ref="posting-closing",
                    source_kind=OriginalReconciliationSourceKind.POSTED_LEDGER,
                    source_system="synthetic-ledger",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=57_500,
                    legacy_slot_ref="slot-closing",
                ),
            ),
            layout=LegacyReconciliationLayout(
                layout_version="synthetic-layout.v1",
                mapping_version="synthetic-mapping.v1",
                label_cells=(
                    LegacyLabelCell(coordinate="A1", label="Synthetic summary"),
                    LegacyLabelCell(coordinate="H1", label="Synthetic detail"),
                ),
                slot_mappings=(
                    LegacySlotMapping(
                        legacy_slot_ref="slot-income",
                        coordinate="B5",
                        economic_effect="INCOME",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-expense",
                        coordinate="B6",
                        economic_effect="EXPENSE",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-opening",
                        coordinate="B7",
                        economic_effect="BALANCE",
                        balance_position="OPENING",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-closing",
                        coordinate="B8",
                        economic_effect="BALANCE",
                        balance_position="CLOSING",
                    ),
                ),
                derived_cells=(
                    LegacyDerivedCellMapping(
                        coordinate="D2",
                        value_role="POSTED_INCOME_TOTAL",
                    ),
                    LegacyDerivedCellMapping(
                        coordinate="D3",
                        value_role="POSTED_EXPENSE_TOTAL",
                        sign_multiplier=-1,
                    ),
                    LegacyDerivedCellMapping(
                        coordinate="D4",
                        value_role="POSTED_PROFIT",
                    ),
                    LegacyDerivedCellMapping(
                        coordinate="D5",
                        value_role="CLOSING_BALANCE",
                    ),
                ),
            ),
        )
    )

    cells = {cell.coordinate: cell for row in projection.rows for cell in row.cells}
    assert cells["A1"].kind == "LABEL"
    assert cells["A1"].label == "Synthetic summary"
    assert cells["H1"].label == "Synthetic detail"
    assert cells["D2"].amount_minor == 10_000
    assert cells["D3"].amount_minor == -2_500
    assert cells["D4"].amount_minor == 7_500
    assert cells["D5"].amount_minor == 57_500
    assert projection.totals.posted_income_minor == 10_000
    assert projection.totals.posted_expense_minor == 2_500
    assert projection.totals.posted_profit_minor == 7_500
    assert projection.totals.mapped_cell_count == 4
    assert projection.layout_version == "synthetic-layout.v1"
    assert projection.mapping_version == "synthetic-mapping.v1"


def test_confirmed_candidate_with_a_slot_is_still_pending_posting_and_incomplete() -> None:
    scope = OriginalReconciliationScope(
        entity_ref=ENTITY_A,
        business_unit_ref="unit-a",
    )
    projection = build_original_reconciliation(
        OriginalReconciliationInput(
            month="2026-05",
            posted_ledger_complete=True,
            scope=scope,
            facts=(
                OriginalReconciliationFact(
                    fact_ref="confirmed-income",
                    canonical_fact_ref="candidate-confirmed-income",
                    source_kind=OriginalReconciliationSourceKind.CONFIRMED_CANDIDATE,
                    source_system="synthetic-inbox",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=10_000,
                    legacy_slot_ref="slot-income",
                ),
                OriginalReconciliationFact(
                    fact_ref="posted-opening",
                    canonical_fact_ref="posting-opening",
                    source_kind=OriginalReconciliationSourceKind.POSTED_LEDGER,
                    source_system="synthetic-ledger",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=50_000,
                    legacy_slot_ref="slot-opening",
                ),
                OriginalReconciliationFact(
                    fact_ref="posted-closing",
                    canonical_fact_ref="posting-closing",
                    source_kind=OriginalReconciliationSourceKind.POSTED_LEDGER,
                    source_system="synthetic-ledger",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=50_000,
                    legacy_slot_ref="slot-closing",
                ),
            ),
            layout=LegacyReconciliationLayout(
                layout_version="synthetic-layout.v1",
                mapping_version="synthetic-mapping.v1",
                slot_mappings=(
                    LegacySlotMapping(
                        legacy_slot_ref="slot-income",
                        coordinate="B5",
                        economic_effect="INCOME",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-opening",
                        coordinate="B7",
                        economic_effect="BALANCE",
                        balance_position="OPENING",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-closing",
                        coordinate="B8",
                        economic_effect="BALANCE",
                        balance_position="CLOSING",
                    ),
                ),
            ),
        )
    )

    assert projection.unmapped_confirmed_count == 0
    assert projection.confirmed_pending_posting_count == 1
    assert projection.is_complete is False
    assert projection.totals.posted_income_minor == 0


def test_unmapped_posted_fact_keeps_an_otherwise_complete_projection_incomplete() -> None:
    scope = OriginalReconciliationScope(
        entity_ref=ENTITY_A,
        business_unit_ref="unit-a",
    )
    projection = build_original_reconciliation(
        OriginalReconciliationInput(
            month="2026-05",
            posted_ledger_complete=True,
            scope=scope,
            facts=(
                OriginalReconciliationFact(
                    fact_ref="posted-unmapped",
                    canonical_fact_ref="posting-unmapped",
                    source_kind=OriginalReconciliationSourceKind.POSTED_LEDGER,
                    source_system="synthetic-ledger",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=1_000,
                ),
                OriginalReconciliationFact(
                    fact_ref="posted-opening",
                    canonical_fact_ref="posting-opening",
                    source_kind=OriginalReconciliationSourceKind.POSTED_LEDGER,
                    source_system="synthetic-ledger",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=50_000,
                    legacy_slot_ref="slot-opening",
                ),
                OriginalReconciliationFact(
                    fact_ref="posted-closing",
                    canonical_fact_ref="posting-closing",
                    source_kind=OriginalReconciliationSourceKind.POSTED_LEDGER,
                    source_system="synthetic-ledger",
                    entity_ref=ENTITY_A,
                    business_unit_ref="unit-a",
                    month="2026-05",
                    amount_minor=50_000,
                    legacy_slot_ref="slot-closing",
                ),
            ),
            layout=LegacyReconciliationLayout(
                layout_version="synthetic-layout.v1",
                mapping_version="synthetic-mapping.v1",
                slot_mappings=(
                    LegacySlotMapping(
                        legacy_slot_ref="slot-opening",
                        coordinate="B7",
                        economic_effect="BALANCE",
                        balance_position="OPENING",
                    ),
                    LegacySlotMapping(
                        legacy_slot_ref="slot-closing",
                        coordinate="B8",
                        economic_effect="BALANCE",
                        balance_position="CLOSING",
                    ),
                ),
            ),
        )
    )

    assert projection.sources[0].source_kind == "POSTED_LEDGER"
    assert projection.sources[0].mapped_fact_count == 2
    assert projection.sources[0].fact_count == 3
    assert projection.is_complete is False
