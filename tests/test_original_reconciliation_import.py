from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]

from ledgerbridge.controlled_import import ImportBusinessUnit, ImportCategory, ImportEntity
from ledgerbridge.original_reconciliation_import import (
    OriginalReconciliationCellMapping,
    OriginalReconciliationImportError,
    OriginalReconciliationImportPlan,
    OriginalReconciliationScopePlan,
    build_original_reconciliation_manifests,
)

ENTITY_REF = UUID("10000000-0000-4000-8000-000000000001")
BUSINESS_UNIT_ID = UUID("20000000-0000-4000-8000-000000000001")
INCOME_CATEGORY_ID = UUID("30000000-0000-4000-8000-000000000001")
EXPENSE_CATEGORY_ID = UUID("30000000-0000-4000-8000-000000000002")


def _workbook(path: Path) -> None:
    workbook = Workbook()
    june = workbook.active
    june.title = "26.6"
    june["B3"] = 123.45
    june["D24"] = 10
    june["B10"] = "=SUM(B3:B9)"
    july = workbook.create_sheet("26.7")
    july["C3"] = 5
    workbook.save(path)


def _plan(*cells: OriginalReconciliationCellMapping) -> OriginalReconciliationImportPlan:
    return OriginalReconciliationImportPlan(
        schema_version="ledgerbridge.original-reconciliation-import-plan.v1",
        mapping_version="hotel-original-v1",
        source_description="Synthetic original reconciliation workbook",
        evidence_filename="original-reconciliation.xlsx",
        entity=ImportEntity(entity_ref=ENTITY_REF, name="Synthetic entity"),
        scopes=(
            OriginalReconciliationScopePlan(
                business_unit=ImportBusinessUnit(
                    business_unit_ref=BUSINESS_UNIT_ID,
                    ref="hotel-a",
                    label="Hotel A",
                ),
                categories=(
                    ImportCategory(
                        category_ref=INCOME_CATEGORY_ID,
                        code="ROOM_INCOME",
                        label="Room income",
                    ),
                    ImportCategory(
                        category_ref=EXPENSE_CATEGORY_ID,
                        code="OPERATING_EXPENSE",
                        label="Operating expense",
                    ),
                ),
                cells=cells,
            ),
        ),
    )


def test_builds_deterministic_june_and_july_candidate_manifests(tmp_path: Path) -> None:
    workbook_path = tmp_path / "source.xlsx"
    _workbook(workbook_path)
    plan = _plan(
        OriginalReconciliationCellMapping(
            sheet_name="26.6",
            cell="B3",
            accounting_month="2026-06",
            category_code="ROOM_INCOME",
            description="June platform income",
            sign_multiplier=1,
        ),
        OriginalReconciliationCellMapping(
            sheet_name="26.6",
            cell="D24",
            accounting_month="2026-06",
            category_code="OPERATING_EXPENSE",
            description="June operating expense",
            sign_multiplier=-1,
        ),
        OriginalReconciliationCellMapping(
            sheet_name="26.7",
            cell="C3",
            accounting_month="2026-07",
            category_code="ROOM_INCOME",
            description="July platform income",
            sign_multiplier=1,
        ),
    )

    first = build_original_reconciliation_manifests(workbook_path, plan)
    second = build_original_reconciliation_manifests(workbook_path, plan)

    assert first == second
    assert len(first) == 1
    manifest = first[0]
    assert manifest.schema_version == "ledgerbridge.controlled-review-source.v1"
    assert [item.accounting_month for item in manifest.candidates] == [
        "2026-06",
        "2026-06",
        "2026-07",
    ]
    assert [item.amount_minor for item in manifest.candidates] == [12_345, -1_000, 500]
    assert [item.display_label for item in manifest.candidates] == [
        "26.6!B3",
        "26.6!D24",
        "26.7!C3",
    ]
    assert (
        manifest.evidence[0].plaintext_sha256
        == hashlib.sha256(workbook_path.read_bytes()).hexdigest()
    )
    assert all(
        manifest.evidence[0].evidence_ref in item.evidence_refs for item in manifest.candidates
    )


def test_refuses_to_import_a_formula_total_as_a_business_item(tmp_path: Path) -> None:
    workbook_path = tmp_path / "source.xlsx"
    _workbook(workbook_path)
    plan = _plan(
        OriginalReconciliationCellMapping(
            sheet_name="26.6",
            cell="B10",
            accounting_month="2026-06",
            category_code="ROOM_INCOME",
            description="June derived total",
            sign_multiplier=1,
        ),
    )

    with pytest.raises(OriginalReconciliationImportError, match="formula"):
        build_original_reconciliation_manifests(workbook_path, plan)


def test_refuses_to_assign_one_cell_to_multiple_business_units() -> None:
    cell = OriginalReconciliationCellMapping(
        sheet_name="26.6",
        cell="B3",
        accounting_month="2026-06",
        category_code="ROOM_INCOME",
        description="June platform income",
        sign_multiplier=1,
    )
    first_scope = _plan(cell).scopes[0]
    with pytest.raises(ValueError, match="only one business-unit scope"):
        OriginalReconciliationImportPlan(
            schema_version="ledgerbridge.original-reconciliation-import-plan.v1",
            mapping_version="hotel-original-v1",
            source_description="Synthetic original reconciliation workbook",
            evidence_filename="original-reconciliation.xlsx",
            entity=ImportEntity(entity_ref=ENTITY_REF, name="Synthetic entity"),
            scopes=(
                first_scope,
                OriginalReconciliationScopePlan(
                    business_unit=ImportBusinessUnit(
                        business_unit_ref=UUID("20000000-0000-4000-8000-000000000002"),
                        ref="hotel-b",
                        label="Hotel B",
                    ),
                    categories=first_scope.categories,
                    cells=(cell,),
                ),
            ),
        )
