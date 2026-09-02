from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError

from ledgerbridge.company_reporting_composition_contract import (
    CompanyReportCategoryComposition,
    CompanyReportCompositionPage,
)
from ledgerbridge.internal_read_contract import (
    READ_ROUTE_CAPABILITIES,
    READ_ROUTE_SCOPE_MODES,
    Capability,
    ScopeMode,
)

COMPANY = UUID("10000000-0000-4000-8000-000000000001")


def _candidate_item() -> dict[str, object]:
    return {
        "company_ref": str(COMPANY),
        "company_name": "Example Company",
        "currency": "CNY",
        "basis": "CONFIRMED_CANDIDATE",
        "positive": {
            "total_minor": 10000,
            "fact_count": 3,
            "items": [
                {
                    "category_code": "ROOM",
                    "category_label": "Room revenue",
                    "amount_minor": 7500,
                    "fact_count": 2,
                },
                {
                    "category_code": "OTHER",
                    "category_label": "Other revenue",
                    "amount_minor": 2500,
                    "fact_count": 1,
                },
            ],
        },
        "negative": {
            "total_minor": 3000,
            "fact_count": 1,
            "items": [
                {
                    "category_code": None,
                    "category_label": None,
                    "amount_minor": 3000,
                    "fact_count": 1,
                }
            ],
        },
    }


def test_composition_contract_keeps_candidate_sign_semantics_explicit() -> None:
    page = CompanyReportCompositionPage.model_validate(
        {
            "basis": "CONFIRMED_CANDIDATE",
            "from_month": "2026-01",
            "to_month": "2026-08",
            "items": [_candidate_item()],
        }
    )

    assert page.items[0].positive.total_minor == 10000
    assert page.items[0].negative.total_minor == 3000
    assert page.items[0].negative.items[0].category_label is None


def test_composition_contract_accepts_posted_income_and_expense_categories() -> None:
    item = _candidate_item()
    item["basis"] = "POSTED_LEDGER"
    item["revenue"] = item.pop("positive")
    item["expense"] = item.pop("negative")

    page = CompanyReportCompositionPage.model_validate(
        {
            "basis": "POSTED_LEDGER",
            "from_month": "2026-08",
            "to_month": "2026-08",
            "items": [item],
        }
    )

    assert page.items[0].basis.value == "POSTED_LEDGER"
    assert page.items[0].revenue.total_minor == 10000


@pytest.mark.parametrize(
    "mutation",
    [
        {"total_minor": 9999},
        {"fact_count": 2},
        {
            "items": [
                {
                    "category_code": "OTHER",
                    "category_label": "Other revenue",
                    "amount_minor": 2500,
                    "fact_count": 1,
                },
                {
                    "category_code": "ROOM",
                    "category_label": "Room revenue",
                    "amount_minor": 7500,
                    "fact_count": 2,
                },
            ]
        },
    ],
)
def test_composition_rejects_unreconciled_counts_amounts_or_order(
    mutation: dict[str, object],
) -> None:
    payload = deepcopy(_candidate_item()["positive"])
    assert isinstance(payload, dict)
    payload.update(mutation)

    with pytest.raises(ValidationError):
        CompanyReportCategoryComposition.model_validate(payload)


def test_composition_rejects_half_present_category_identity_and_mixed_basis() -> None:
    item = _candidate_item()
    positive = item["positive"]
    assert isinstance(positive, dict)
    slices = positive["items"]
    assert isinstance(slices, list)
    first = slices[0]
    assert isinstance(first, dict)
    first["category_label"] = None

    with pytest.raises(ValidationError):
        CompanyReportCompositionPage.model_validate(
            {
                "basis": "CONFIRMED_CANDIDATE",
                "from_month": "2026-08",
                "to_month": "2026-08",
                "items": [item],
            }
        )


def test_composition_route_uses_the_company_report_collection_capability() -> None:
    key = "GET /internal/v1/company-report-composition"

    assert READ_ROUTE_CAPABILITIES[key] is Capability.COMPANY_REPORT_READ
    assert READ_ROUTE_SCOPE_MODES[key] is ScopeMode.COLLECTION
