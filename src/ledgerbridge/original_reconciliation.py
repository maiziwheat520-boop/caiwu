"""Versioned, read-only projection for the user's legacy reconciliation grid."""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ledgerbridge.candidate_contract import MoneyMinor
from ledgerbridge.financial_foundation_blocker_taxonomy import (
    FINANCIAL_FOUNDATION_BLOCKER_TAXONOMY_VERSION,
)

ORIGINAL_RECONCILIATION_CONTRACT_VERSION: Literal["ledgerbridge.original-reconciliation.v1"] = (
    "ledgerbridge.original-reconciliation.v1"
)

ColumnLetter = Literal["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M"]
ColumnRole = Literal["MAIN", "SPACER", "DETAIL"]
CellKind = Literal["BLANK", "LABEL", "AMOUNT", "GAP"]
EconomicEffect = Literal["INCOME", "EXPENSE", "EXPENSE_REFUND", "NO_EFFECT", "BALANCE"]
DerivedValueRole = Literal[
    "POSTED_INCOME_TOTAL",
    "POSTED_EXPENSE_TOTAL",
    "POSTED_PROFIT",
    "OPENING_BALANCE",
    "CLOSING_BALANCE",
]
ProjectionGapCode = Literal[
    "MISSING_TIME_GRANULARITY",
    "MISSING_BUSINESS_UNIT_ATTRIBUTION",
]

_COLUMN_LETTERS: tuple[ColumnLetter, ...] = (
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
    "G",
    "H",
    "I",
    "J",
    "K",
    "L",
    "M",
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OriginalReconciliationScope(_FrozenModel):
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)


class OriginalReconciliationColumn(_FrozenModel):
    column: ColumnLetter
    ordinal: int = Field(ge=1, le=13)
    role: ColumnRole


class OriginalReconciliationCell(_FrozenModel):
    coordinate: str = Field(pattern=r"^[A-M](?:[1-9]|[1-3][0-9]|40)$")
    column: ColumnLetter
    row_number: int = Field(ge=1, le=40)
    kind: CellKind = "BLANK"
    label: str | None = Field(default=None, min_length=1, max_length=200)
    amount_minor: MoneyMinor | None = None
    currency: Literal["CNY"] | None = None
    gap_code: (
        Literal[
            "MISSING_LEGACY_SLOT_MAPPING",
            "MISSING_BALANCE_MAPPING",
            "MISSING_ECONOMIC_EFFECT",
            "POSTED_LEDGER_UNAVAILABLE",
        ]
        | None
    ) = None
    source_fact_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def value_shape_matches_kind(self) -> OriginalReconciliationCell:
        if self.kind == "AMOUNT":
            if self.amount_minor is None or self.currency != "CNY" or self.gap_code is not None:
                raise ValueError("AMOUNT cells require integer CNY and no gap")
        elif self.amount_minor is not None or self.currency is not None:
            raise ValueError("non-AMOUNT cells cannot carry money")
        if (self.kind == "GAP") != (self.gap_code is not None):
            raise ValueError("GAP cells and gap_code must appear together")
        if (self.kind == "LABEL") != (self.label is not None):
            raise ValueError("LABEL cells and label must appear together")
        return self


class OriginalReconciliationRow(_FrozenModel):
    row_number: int = Field(ge=1, le=40)
    cells: tuple[OriginalReconciliationCell, ...] = Field(min_length=13, max_length=13)


class OriginalReconciliationTotals(_FrozenModel):
    posted_income_minor: MoneyMinor | None = None
    posted_expense_minor: MoneyMinor | None = None
    posted_profit_minor: MoneyMinor | None = None
    opening_balance_minor: MoneyMinor | None = None
    closing_balance_minor: MoneyMinor | None = None
    mapped_cell_count: int = Field(default=0, ge=0, le=520)
    confirmed_candidate_amount_minor: MoneyMinor = 0
    posted_amount_minor: MoneyMinor | None = None
    currency: Literal["CNY"] = "CNY"

    @model_validator(mode="after")
    def totals_are_signed_and_balanced(self) -> OriginalReconciliationTotals:
        income = self.posted_income_minor
        expense = self.posted_expense_minor
        profit = self.posted_profit_minor
        posted_amount = self.posted_amount_minor
        values = (income, expense, profit, posted_amount)
        if all(value is None for value in values):
            return self
        if income is None or expense is None or profit is None or posted_amount is None:
            raise ValueError("posted ledger totals must be wholly available or wholly unavailable")
        if profit != income - expense:
            raise ValueError(
                "posted_profit_minor must equal posted_income_minor minus posted_expense_minor"
            )
        return self


class OriginalReconciliationSourceKind(StrEnum):
    CONFIRMED_CANDIDATE = "CONFIRMED_CANDIDATE"
    ACCOUNT_STATEMENT = "ACCOUNT_STATEMENT"
    POSTED_LEDGER = "POSTED_LEDGER"


class OriginalReconciliationSource(_FrozenModel):
    source_kind: OriginalReconciliationSourceKind
    source_system: str = Field(min_length=1, max_length=100)
    source_label: str | None = Field(default=None, min_length=1, max_length=200)
    fact_count: int = Field(ge=1)
    mapped_fact_count: int = Field(ge=0)
    amount_minor: MoneyMinor

    @model_validator(mode="after")
    def mapped_count_does_not_exceed_facts(self) -> OriginalReconciliationSource:
        if self.mapped_fact_count > self.fact_count:
            raise ValueError("mapped_fact_count cannot exceed fact_count")
        return self


class OriginalReconciliationFact(_FrozenModel):
    fact_ref: str = Field(min_length=1, max_length=200)
    canonical_fact_ref: str = Field(min_length=1, max_length=200)
    source_kind: OriginalReconciliationSourceKind
    source_system: str = Field(min_length=1, max_length=100)
    source_label: str | None = Field(default=None, min_length=1, max_length=200)
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)
    month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    amount_minor: MoneyMinor
    currency: Literal["CNY"] = "CNY"
    legacy_slot_ref: str | None = Field(default=None, min_length=1, max_length=100)


class LegacySlotMapping(_FrozenModel):
    legacy_slot_ref: str = Field(min_length=1, max_length=100)
    coordinate: str = Field(pattern=r"^[A-M](?:[1-9]|[1-3][0-9]|40)$")
    economic_effect: EconomicEffect | None
    balance_position: Literal["OPENING", "CLOSING"] | None = None
    sign_multiplier: Literal[-1, 1] = 1

    @model_validator(mode="after")
    def balance_position_matches_effect(self) -> LegacySlotMapping:
        if (self.economic_effect == "BALANCE") != (self.balance_position is not None):
            raise ValueError("BALANCE mappings alone require a balance_position")
        return self


class LegacyLabelCell(_FrozenModel):
    coordinate: str = Field(pattern=r"^[A-M](?:[1-9]|[1-3][0-9]|40)$")
    label: str = Field(min_length=1, max_length=200)


class LegacyDerivedCellMapping(_FrozenModel):
    coordinate: str = Field(pattern=r"^[A-M](?:[1-9]|[1-3][0-9]|40)$")
    value_role: DerivedValueRole
    sign_multiplier: Literal[-1, 1] = 1


class LegacySourceSlotRule(_FrozenModel):
    source_kind: OriginalReconciliationSourceKind
    source_code: str = Field(min_length=1, max_length=100)
    legacy_slot_ref: str = Field(min_length=1, max_length=100)


class LegacyReconciliationLayout(_FrozenModel):
    layout_version: str = Field(min_length=1, max_length=100)
    mapping_version: str = Field(min_length=1, max_length=100)
    label_cells: tuple[LegacyLabelCell, ...] = ()
    slot_mappings: tuple[LegacySlotMapping, ...] = ()
    source_slot_rules: tuple[LegacySourceSlotRule, ...] = ()
    derived_cells: tuple[LegacyDerivedCellMapping, ...] = ()

    @model_validator(mode="after")
    def coordinates_are_unique_and_spacers_remain_blank(self) -> LegacyReconciliationLayout:
        coordinates = [
            *(cell.coordinate for cell in self.label_cells),
            *(mapping.coordinate for mapping in self.slot_mappings),
            *(cell.coordinate for cell in self.derived_cells),
        ]
        if any(coordinate[0] in {"F", "G"} for coordinate in coordinates):
            raise ValueError("legacy layout cannot occupy spacer columns")
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("legacy label, slot, and derived coordinates must be unique")
        slot_refs = [mapping.legacy_slot_ref for mapping in self.slot_mappings]
        if len(slot_refs) != len(set(slot_refs)):
            raise ValueError("legacy slot refs must be unique")
        rule_keys = [(rule.source_kind, rule.source_code) for rule in self.source_slot_rules]
        if len(rule_keys) != len(set(rule_keys)):
            raise ValueError("legacy source-to-slot rules must be unique")
        if any(rule.legacy_slot_ref not in slot_refs for rule in self.source_slot_rules):
            raise ValueError("legacy source-to-slot rules must target a configured slot")
        return self


class OriginalReconciliationReviewItem(_FrozenModel):
    review_ref: str = Field(min_length=1, max_length=200)
    status: Literal["PENDING", "INCOMPLETE", "CONFLICTED"]
    entity_ref: UUID
    business_unit_ref: str = Field(min_length=1, max_length=100)
    month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    missing_material_count: int = Field(default=0, ge=0)


class OriginalReconciliationInput(_FrozenModel):
    month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    scope: OriginalReconciliationScope
    facts: tuple[OriginalReconciliationFact, ...] = ()
    review_items: tuple[OriginalReconciliationReviewItem, ...] = ()
    declared_missing_material_count: int = Field(default=0, ge=0)
    projection_gaps: tuple[ProjectionGapCode, ...] = ()
    # True means the caller supplied the complete immutable posted-fact identity set,
    # including an explicitly empty set. A missing reader is False. Missing future
    # business-unit snapshots belong in a breakdown GAP, never a current-dimension join.
    posted_ledger_complete: bool
    layout: LegacyReconciliationLayout


class OriginalReconciliationProjection(_FrozenModel):
    contract_version: Literal["ledgerbridge.original-reconciliation.v1"] = (
        ORIGINAL_RECONCILIATION_CONTRACT_VERSION
    )
    taxonomy_version: Literal["ledgerbridge.financial-foundation-blocker-taxonomy.v1"] = (
        FINANCIAL_FOUNDATION_BLOCKER_TAXONOMY_VERSION
    )
    month: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    scope: OriginalReconciliationScope
    layout_version: str = Field(min_length=1, max_length=100)
    mapping_version: str = Field(min_length=1, max_length=100)
    columns: tuple[OriginalReconciliationColumn, ...] = Field(min_length=13, max_length=13)
    rows: tuple[OriginalReconciliationRow, ...] = Field(min_length=40, max_length=40)
    totals: OriginalReconciliationTotals
    pending_review_count: int = Field(default=0, ge=0)
    missing_material_count: int = Field(default=0, ge=0)
    unmapped_confirmed_count: int = Field(default=0, ge=0)
    confirmed_pending_posting_count: int = Field(default=0, ge=0)
    posted_ledger_complete: bool
    projection_gaps: tuple[ProjectionGapCode, ...] = ()
    sources: tuple[OriginalReconciliationSource, ...] = ()
    is_complete: bool

    @model_validator(mode="after")
    def fixed_grid_and_completeness_are_consistent(self) -> OriginalReconciliationProjection:
        expected_roles = (
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
        )
        if tuple((column.column, column.ordinal, column.role) for column in self.columns) != tuple(
            (column, ordinal, role)
            for ordinal, (column, role) in enumerate(
                zip(_COLUMN_LETTERS, expected_roles, strict=True), start=1
            )
        ):
            raise ValueError("columns must preserve the fixed A:M order and roles")
        for expected_row, row in enumerate(self.rows, start=1):
            if row.row_number != expected_row:
                raise ValueError("rows must preserve the fixed 1:40 order")
            if tuple(
                (cell.column, cell.row_number, cell.coordinate) for cell in row.cells
            ) != tuple(
                (column, expected_row, f"{column}{expected_row}") for column in _COLUMN_LETTERS
            ):
                raise ValueError("cells must preserve the fixed A:M coordinates")
            if row.cells[5].kind != "BLANK" or row.cells[6].kind != "BLANK":
                raise ValueError("F:G spacer cells must remain blank")

        posted_values = (
            self.totals.posted_income_minor,
            self.totals.posted_expense_minor,
            self.totals.posted_profit_minor,
            self.totals.posted_amount_minor,
        )
        if self.posted_ledger_complete != all(value is not None for value in posted_values):
            raise ValueError("posted totals must match posted_ledger_complete")
        confirmed_source_count = sum(
            source.fact_count
            for source in self.sources
            if source.source_kind == OriginalReconciliationSourceKind.CONFIRMED_CANDIDATE
        )
        if confirmed_source_count != self.confirmed_pending_posting_count:
            raise ValueError("confirmed source count must match pending-posting count")
        if self.unmapped_confirmed_count > confirmed_source_count:
            raise ValueError("unmapped confirmed count cannot exceed confirmed source facts")
        if len(self.projection_gaps) != len(set(self.projection_gaps)):
            raise ValueError("projection gaps must be unique")
        posted_sources_fully_mapped = all(
            source.mapped_fact_count == source.fact_count
            for source in self.sources
            if source.source_kind == OriginalReconciliationSourceKind.POSTED_LEDGER
        )
        has_gap = any(cell.kind == "GAP" for row in self.rows for cell in row.cells)
        if self.is_complete and (
            has_gap
            or self.projection_gaps
            or not self.posted_ledger_complete
            or not posted_sources_fully_mapped
            or self.pending_review_count != 0
            or self.missing_material_count != 0
            or self.unmapped_confirmed_count != 0
            or self.confirmed_pending_posting_count != 0
            or self.totals.opening_balance_minor is None
            or self.totals.closing_balance_minor is None
        ):
            raise ValueError("is_complete cannot hide an unresolved projection gap")
        return self


def build_original_reconciliation(
    projection_input: OriginalReconciliationInput,
) -> OriginalReconciliationProjection:
    """Build the stable 13-column by 40-row projection without inferred values."""

    mapping_by_slot: dict[str, LegacySlotMapping] = {}
    mapping_by_coordinate: dict[str, LegacySlotMapping] = {}
    for mapping in projection_input.layout.slot_mappings:
        if (
            mapping.legacy_slot_ref in mapping_by_slot
            or mapping.coordinate in mapping_by_coordinate
        ):
            raise ValueError("legacy slot refs and coordinates must be unique")
        mapping_by_slot[mapping.legacy_slot_ref] = mapping
        mapping_by_coordinate[mapping.coordinate] = mapping

    facts_by_identity: dict[
        tuple[OriginalReconciliationSourceKind, str], list[OriginalReconciliationFact]
    ] = defaultdict(list)
    for fact in projection_input.facts:
        if (
            fact.entity_ref != projection_input.scope.entity_ref
            or fact.business_unit_ref != projection_input.scope.business_unit_ref
            or fact.month != projection_input.month
        ):
            raise ValueError("original reconciliation fact is outside the requested scope")
        facts_by_identity[(fact.source_kind, fact.canonical_fact_ref)].append(fact)
    if not projection_input.posted_ledger_complete and any(
        fact.source_kind == OriginalReconciliationSourceKind.POSTED_LEDGER
        for fact in projection_input.facts
    ):
        raise ValueError("POSTED_LEDGER facts require a complete posted-ledger read")

    selected_facts: list[OriginalReconciliationFact] = []
    for facts in facts_by_identity.values():
        semantic_values = {
            (
                fact.source_kind,
                fact.source_system,
                fact.source_label,
                fact.amount_minor,
                fact.currency,
                fact.legacy_slot_ref,
            )
            for fact in facts
        }
        if len(semantic_values) != 1:
            raise ValueError("duplicate canonical facts disagree within one source layer")
        selected_facts.append(min(facts, key=lambda fact: fact.fact_ref))

    reviews_by_ref: dict[str, OriginalReconciliationReviewItem] = {}
    for review_item in projection_input.review_items:
        if (
            review_item.entity_ref != projection_input.scope.entity_ref
            or review_item.business_unit_ref != projection_input.scope.business_unit_ref
            or review_item.month != projection_input.month
        ):
            raise ValueError("original reconciliation review item is outside the requested scope")
        existing = reviews_by_ref.get(review_item.review_ref)
        if existing is not None and existing != review_item:
            raise ValueError("duplicate review items disagree")
        reviews_by_ref[review_item.review_ref] = review_item

    mapped_facts_by_coordinate: dict[str, list[OriginalReconciliationFact]] = defaultdict(list)
    mapped_amounts: dict[str, int] = defaultdict(int)
    source_groups: dict[
        tuple[OriginalReconciliationSourceKind, str], list[OriginalReconciliationFact]
    ] = defaultdict(list)
    source_mapped_counts: dict[tuple[OriginalReconciliationSourceKind, str], int] = defaultdict(int)
    posted_income_minor = 0
    posted_expense_minor = 0
    opening_balance_minor: int | None = None
    closing_balance_minor: int | None = None

    for fact in selected_facts:
        source_key = (fact.source_kind, fact.source_system)
        source_groups[source_key].append(fact)
        fact_mapping = (
            mapping_by_slot.get(fact.legacy_slot_ref) if fact.legacy_slot_ref is not None else None
        )
        if fact_mapping is None:
            continue
        if fact_mapping.economic_effect is not None:
            source_mapped_counts[source_key] += 1
        if fact.source_kind != OriginalReconciliationSourceKind.POSTED_LEDGER:
            continue
        if fact_mapping.economic_effect is None:
            continue
        projected_amount = fact.amount_minor * fact_mapping.sign_multiplier
        if fact_mapping.economic_effect == "INCOME":
            posted_income_minor += abs(projected_amount)
        elif fact_mapping.economic_effect == "EXPENSE":
            posted_expense_minor += abs(projected_amount)
        elif fact_mapping.economic_effect == "EXPENSE_REFUND":
            posted_expense_minor -= abs(projected_amount)
        elif (
            fact_mapping.economic_effect == "BALANCE" and fact_mapping.balance_position == "OPENING"
        ):
            if opening_balance_minor is not None:
                raise ValueError("opening balance requires one explicit fact")
            opening_balance_minor = projected_amount
        elif (
            fact_mapping.economic_effect == "BALANCE" and fact_mapping.balance_position == "CLOSING"
        ):
            if closing_balance_minor is not None:
                raise ValueError("closing balance requires one explicit fact")
            closing_balance_minor = projected_amount
        mapped_facts_by_coordinate[fact_mapping.coordinate].append(fact)
        mapped_amounts[fact_mapping.coordinate] += projected_amount

    cell_values: dict[str, OriginalReconciliationCell] = {
        label_cell.coordinate: OriginalReconciliationCell(
            coordinate=label_cell.coordinate,
            column=label_cell.coordinate[0],  # type: ignore[arg-type]
            row_number=int(label_cell.coordinate[1:]),
            kind="LABEL",
            label=label_cell.label,
        )
        for label_cell in projection_input.layout.label_cells
    }
    for coordinate, mapping in mapping_by_coordinate.items():
        facts = mapped_facts_by_coordinate.get(coordinate, [])
        column = coordinate[0]
        row_number = int(coordinate[1:])
        if facts:
            cell_values[coordinate] = OriginalReconciliationCell(
                coordinate=coordinate,
                column=column,  # type: ignore[arg-type]
                row_number=row_number,
                kind="AMOUNT",
                amount_minor=mapped_amounts[coordinate],
                currency="CNY",
                source_fact_refs=tuple(fact.fact_ref for fact in facts),
            )
        elif mapping.economic_effect is None and any(
            fact.source_kind == OriginalReconciliationSourceKind.POSTED_LEDGER
            and fact.legacy_slot_ref == mapping.legacy_slot_ref
            for fact in selected_facts
        ):
            cell_values[coordinate] = OriginalReconciliationCell(
                coordinate=coordinate,
                column=column,  # type: ignore[arg-type]
                row_number=row_number,
                kind="GAP",
                gap_code="MISSING_ECONOMIC_EFFECT",
            )
        elif mapping.economic_effect == "BALANCE":
            cell_values[coordinate] = OriginalReconciliationCell(
                coordinate=coordinate,
                column=column,  # type: ignore[arg-type]
                row_number=row_number,
                kind="GAP",
                gap_code="MISSING_BALANCE_MAPPING",
            )

    def derived_value(
        mapping: LegacyDerivedCellMapping,
    ) -> tuple[int | None, tuple[str, ...]]:
        if not projection_input.posted_ledger_complete and mapping.value_role in {
            "POSTED_INCOME_TOTAL",
            "POSTED_EXPENSE_TOTAL",
            "POSTED_PROFIT",
        }:
            return None, ()
        facts_for_role: list[OriginalReconciliationFact] = []
        for fact in selected_facts:
            if fact.source_kind != OriginalReconciliationSourceKind.POSTED_LEDGER:
                continue
            slot = (
                mapping_by_slot.get(fact.legacy_slot_ref)
                if fact.legacy_slot_ref is not None
                else None
            )
            if slot is None or slot.economic_effect is None:
                continue
            include = (
                (mapping.value_role == "POSTED_INCOME_TOTAL" and slot.economic_effect == "INCOME")
                or (
                    mapping.value_role == "POSTED_EXPENSE_TOTAL"
                    and slot.economic_effect in {"EXPENSE", "EXPENSE_REFUND"}
                )
                or (
                    mapping.value_role == "POSTED_PROFIT"
                    and slot.economic_effect in {"INCOME", "EXPENSE", "EXPENSE_REFUND"}
                )
                or (
                    mapping.value_role == "OPENING_BALANCE"
                    and slot.economic_effect == "BALANCE"
                    and slot.balance_position == "OPENING"
                )
                or (
                    mapping.value_role == "CLOSING_BALANCE"
                    and slot.economic_effect == "BALANCE"
                    and slot.balance_position == "CLOSING"
                )
            )
            if include:
                facts_for_role.append(fact)

        value_by_role = {
            "POSTED_INCOME_TOTAL": posted_income_minor,
            "POSTED_EXPENSE_TOTAL": posted_expense_minor,
            "POSTED_PROFIT": posted_income_minor - posted_expense_minor,
            "OPENING_BALANCE": opening_balance_minor,
            "CLOSING_BALANCE": closing_balance_minor,
        }
        value = value_by_role[mapping.value_role]
        return (
            None if value is None else value * mapping.sign_multiplier,
            tuple(fact.fact_ref for fact in facts_for_role),
        )

    for derived in projection_input.layout.derived_cells:
        value, source_fact_refs = derived_value(derived)
        column = derived.coordinate[0]
        row_number = int(derived.coordinate[1:])
        if value is None:
            cell_values[derived.coordinate] = OriginalReconciliationCell(
                coordinate=derived.coordinate,
                column=column,  # type: ignore[arg-type]
                row_number=row_number,
                kind="GAP",
                gap_code=(
                    "POSTED_LEDGER_UNAVAILABLE"
                    if derived.value_role
                    in {"POSTED_INCOME_TOTAL", "POSTED_EXPENSE_TOTAL", "POSTED_PROFIT"}
                    else "MISSING_BALANCE_MAPPING"
                ),
            )
        else:
            cell_values[derived.coordinate] = OriginalReconciliationCell(
                coordinate=derived.coordinate,
                column=column,  # type: ignore[arg-type]
                row_number=row_number,
                kind="AMOUNT",
                amount_minor=value,
                currency="CNY",
                source_fact_refs=source_fact_refs,
            )

    columns = tuple(
        OriginalReconciliationColumn(
            column=column,
            ordinal=index,
            role=("MAIN" if index <= 5 else "SPACER" if index <= 7 else "DETAIL"),
        )
        for index, column in enumerate(_COLUMN_LETTERS, start=1)
    )
    rows = tuple(
        OriginalReconciliationRow(
            row_number=row_number,
            cells=tuple(
                cell_values.get(
                    f"{column}{row_number}",
                    OriginalReconciliationCell(
                        coordinate=f"{column}{row_number}",
                        column=column,
                        row_number=row_number,
                    ),
                )
                for column in _COLUMN_LETTERS
            ),
        )
        for row_number in range(1, 41)
    )
    sources = tuple(
        OriginalReconciliationSource(
            source_kind=source_kind,
            source_system=source_system,
            source_label=(
                next(iter(labels))
                if len(
                    labels := {fact.source_label for fact in facts if fact.source_label is not None}
                )
                == 1
                else None
            ),
            fact_count=len(facts),
            mapped_fact_count=source_mapped_counts[(source_kind, source_system)],
            amount_minor=sum(fact.amount_minor for fact in facts),
        )
        for (source_kind, source_system), facts in sorted(
            source_groups.items(), key=lambda item: (item[0][0].value, item[0][1])
        )
    )
    posted_sources_fully_mapped = all(
        source.mapped_fact_count == source.fact_count
        for source in sources
        if source.source_kind == OriginalReconciliationSourceKind.POSTED_LEDGER
    )
    confirmed_amount = sum(
        fact.amount_minor
        for fact in selected_facts
        if fact.source_kind == OriginalReconciliationSourceKind.CONFIRMED_CANDIDATE
    )
    posted_amount = (
        sum(
            fact.amount_minor
            for fact in selected_facts
            if fact.source_kind == OriginalReconciliationSourceKind.POSTED_LEDGER
        )
        if projection_input.posted_ledger_complete
        else None
    )
    unmapped_confirmed_count = sum(
        1
        for facts in facts_by_identity.values()
        if any(
            fact.source_kind == OriginalReconciliationSourceKind.CONFIRMED_CANDIDATE
            for fact in facts
        )
        and not any(
            fact.source_kind == OriginalReconciliationSourceKind.CONFIRMED_CANDIDATE
            and fact.legacy_slot_ref is not None
            and fact.legacy_slot_ref in mapping_by_slot
            and mapping_by_slot[fact.legacy_slot_ref].economic_effect is not None
            for fact in facts
        )
    )
    has_gap = any(cell.kind == "GAP" for row in rows for cell in row.cells)
    confirmed_pending_posting_count = sum(
        1
        for fact in selected_facts
        if fact.source_kind == OriginalReconciliationSourceKind.CONFIRMED_CANDIDATE
    )
    pending_review_count = len(reviews_by_ref)
    missing_material_count = projection_input.declared_missing_material_count + sum(
        item.missing_material_count for item in reviews_by_ref.values()
    )
    return OriginalReconciliationProjection(
        month=projection_input.month,
        scope=projection_input.scope,
        layout_version=projection_input.layout.layout_version,
        mapping_version=projection_input.layout.mapping_version,
        columns=columns,
        rows=rows,
        totals=OriginalReconciliationTotals(
            posted_income_minor=(
                posted_income_minor if projection_input.posted_ledger_complete else None
            ),
            posted_expense_minor=(
                posted_expense_minor if projection_input.posted_ledger_complete else None
            ),
            posted_profit_minor=(
                posted_income_minor - posted_expense_minor
                if projection_input.posted_ledger_complete
                else None
            ),
            opening_balance_minor=opening_balance_minor,
            closing_balance_minor=closing_balance_minor,
            mapped_cell_count=len(mapped_facts_by_coordinate),
            confirmed_candidate_amount_minor=confirmed_amount,
            posted_amount_minor=posted_amount,
        ),
        pending_review_count=pending_review_count,
        missing_material_count=missing_material_count,
        unmapped_confirmed_count=unmapped_confirmed_count,
        confirmed_pending_posting_count=confirmed_pending_posting_count,
        posted_ledger_complete=projection_input.posted_ledger_complete,
        projection_gaps=projection_input.projection_gaps,
        sources=sources,
        is_complete=(
            not has_gap
            and not projection_input.projection_gaps
            and unmapped_confirmed_count == 0
            and pending_review_count == 0
            and missing_material_count == 0
            and confirmed_pending_posting_count == 0
            and projection_input.posted_ledger_complete
            and posted_sources_fully_mapped
            and opening_balance_minor is not None
            and closing_balance_minor is not None
        ),
    )
