from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from ledgerbridge.company_reporting_contract import (
    CompanyReportBasis,
    CompanyReportBusinessUnit,
    CompanyReportItem,
    CompanyReportMonth,
    CompanyReportPage,
)
from ledgerbridge.internal_read_contract import (
    READ_ROUTE_CAPABILITIES,
    READ_ROUTE_SCOPE_MODES,
    Capability,
    ScopeMode,
)

COMPANY = UUID("10000000-0000-4000-8000-000000000001")


def _balance() -> dict[str, object]:
    return {
        "balance_basis": "UNAVAILABLE",
        "opening_balance_minor": None,
        "closing_balance_minor": None,
        "gap": "AUTHORITATIVE_BALANCE_UNAVAILABLE",
    }


def _metrics(
    basis: CompanyReportBasis = CompanyReportBasis.CONFIRMED_CANDIDATE,
    *,
    zero: bool = False,
) -> dict[str, object]:
    if basis is CompanyReportBasis.CONFIRMED_CANDIDATE:
        return {
            "basis": basis.value,
            "confirmed_positive_minor": 0 if zero else 4500,
            "confirmed_negative_minor": 0 if zero else -1200,
            "confirmed_net_minor": 0 if zero else 3300,
            "confirmed_count": 0 if zero else 2,
            "source_count": 0 if zero else 2,
        }
    if basis is CompanyReportBasis.ACCOUNT_STATEMENT:
        return {
            "basis": basis.value,
            "cash_inflow_minor": 0 if zero else 4500,
            "cash_outflow_minor": 0 if zero else 1200,
            "net_cash_flow_minor": 0 if zero else 3300,
            "confirmed_transaction_count": 0 if zero else 2,
            "statement_count": 0 if zero else 1,
        }
    return {
        "basis": basis.value,
        "revenue_minor": 0 if zero else 4500,
        "expense_minor": 0 if zero else 1200,
        "profit_minor": 0 if zero else 3300,
        "posted_entry_count": 0 if zero else 2,
        "source_count": 0 if zero else 2,
    }


def _business_unit(
    basis: CompanyReportBasis = CompanyReportBasis.CONFIRMED_CANDIDATE,
    **updates: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "business_unit_ref": "unit-hotel-a",
        "business_unit_label": "Hotel A",
        "metrics": _metrics(basis),
        "pending_review_count": 1,
        "attribution_pending_count": 1,
        "missing_material_count": None,
        "taxonomy_version": None,
        "balance": _balance(),
    }
    value.update(updates)
    return value


def _month(
    basis: CompanyReportBasis = CompanyReportBasis.CONFIRMED_CANDIDATE,
    **updates: object,
) -> dict[str, object]:
    statement = basis is CompanyReportBasis.ACCOUNT_STATEMENT
    value: dict[str, object] = {
        "month": "2026-08",
        "metrics": _metrics(basis),
        "pending_review_count": 1,
        "attribution_pending_count": 1,
        "missing_material_count": None,
        "taxonomy_version": None,
        "balance": _balance(),
        "business_unit_breakdown_status": (
            "UNAVAILABLE_ATTRIBUTION_PENDING" if statement else "AVAILABLE"
        ),
        "business_units": None if statement else [_business_unit(basis)],
    }
    value.update(updates)
    return value


def _company(
    basis: CompanyReportBasis = CompanyReportBasis.CONFIRMED_CANDIDATE,
    **updates: object,
) -> dict[str, object]:
    statement = basis is CompanyReportBasis.ACCOUNT_STATEMENT
    value: dict[str, object] = {
        "company_ref": str(COMPANY),
        "company_name": "Example Hotel Company",
        "currency": "CNY",
        "metrics": _metrics(basis),
        "pending_review_count": 1,
        "attribution_pending_count": 1,
        "missing_material_count": None,
        "taxonomy_version": None,
        "balance": _balance(),
        "business_unit_breakdown_status": (
            "UNAVAILABLE_ATTRIBUTION_PENDING" if statement else "AVAILABLE"
        ),
        "months": [_month(basis)],
    }
    value.update(updates)
    return value


def test_company_report_v1_uses_basis_specific_metrics_and_common_pending_counts() -> None:
    page = CompanyReportPage.model_validate(
        {
            "contract_version": "ledgerbridge.company-report.v1",
            "basis": "CONFIRMED_CANDIDATE",
            "from_month": "2026-08",
            "to_month": "2026-08",
            "items": [_company()],
        }
    )

    assert page.model_dump(mode="json") == {
        "contract_version": "ledgerbridge.company-report.v1",
        "basis": "CONFIRMED_CANDIDATE",
        "from_month": "2026-08",
        "to_month": "2026-08",
        "items": [_company()],
    }
    item = page.items[0]
    metric_values = item.metrics.model_dump()
    assert metric_values["confirmed_positive_minor"] == 4500
    assert metric_values["confirmed_negative_minor"] == -1200
    assert metric_values["confirmed_net_minor"] == 3300
    assert item.pending_review_count == item.attribution_pending_count == 1
    assert item.missing_material_count is None
    assert item.taxonomy_version is None
    assert item.balance.model_dump() == _balance()
    assert isinstance(item.months[0], CompanyReportMonth)
    business_units = item.months[0].business_units
    assert business_units is not None
    assert isinstance(business_units[0], CompanyReportBusinessUnit)


@pytest.mark.parametrize("basis", list(CompanyReportBasis))
def test_company_report_page_selects_exactly_one_financial_basis(
    basis: CompanyReportBasis,
) -> None:
    page = CompanyReportPage.model_validate(
        {
            "basis": basis.value,
            "from_month": "2026-08",
            "to_month": "2026-08",
            "items": [_company(basis)],
        }
    )

    assert page.basis is basis
    assert page.items[0].metrics.basis is basis
    assert page.items[0].months[0].metrics.basis is basis
    business_units = page.items[0].months[0].business_units
    if business_units is not None:
        assert business_units[0].metrics.basis is basis


def test_confirmed_account_statement_can_expose_attributed_business_units() -> None:
    basis = CompanyReportBasis.ACCOUNT_STATEMENT
    month = _month(
        basis,
        business_unit_breakdown_status="AVAILABLE",
        business_units=[_business_unit(basis)],
    )
    item = _company(
        basis,
        business_unit_breakdown_status="AVAILABLE",
        months=[month],
    )

    page = CompanyReportPage.model_validate(
        {
            "basis": basis.value,
            "from_month": "2026-08",
            "to_month": "2026-08",
            "items": [item],
        }
    )

    assert page.items[0].business_unit_breakdown_status.value == "AVAILABLE"
    assert page.items[0].months[0].business_units is not None


def test_company_report_rejects_mixed_metric_bases_in_one_page() -> None:
    for payload in (
        _company(CompanyReportBasis.ACCOUNT_STATEMENT),
        _company(months=[_month(CompanyReportBasis.ACCOUNT_STATEMENT)]),
        _company(
            months=[_month(business_units=[_business_unit(CompanyReportBasis.ACCOUNT_STATEMENT)])],
        ),
    ):
        with pytest.raises(ValidationError):
            CompanyReportPage.model_validate(
                {
                    "basis": "CONFIRMED_CANDIDATE",
                    "from_month": "2026-08",
                    "to_month": "2026-08",
                    "items": [payload],
                }
            )


def test_company_report_route_uses_a_dedicated_collection_capability() -> None:
    key = "GET /internal/v1/company-reports"

    assert READ_ROUTE_CAPABILITIES[key] is Capability.COMPANY_REPORT_READ
    assert READ_ROUTE_SCOPE_MODES[key] is ScopeMode.COLLECTION


@pytest.mark.parametrize(
    "metrics",
    [
        _metrics() | {"confirmed_positive_minor": -1, "confirmed_net_minor": -1201},
        _metrics() | {"confirmed_negative_minor": 1, "confirmed_net_minor": 4501},
        _metrics() | {"confirmed_net_minor": 3299},
        _metrics(CompanyReportBasis.ACCOUNT_STATEMENT) | {"net_cash_flow_minor": 3299},
        _metrics(CompanyReportBasis.POSTED_LEDGER) | {"profit_minor": 3299},
        _metrics(CompanyReportBasis.POSTED_LEDGER) | {"revenue_minor": -1},
        _metrics(CompanyReportBasis.CONFIRMED_CANDIDATE) | {"confirmed_count": True},
    ],
)
def test_company_report_rejects_invalid_basis_specific_metrics(
    metrics: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CompanyReportBusinessUnit.model_validate(_business_unit(metrics=metrics))


def test_unavailable_balance_cannot_be_fabricated_from_a_metric_net() -> None:
    with pytest.raises(ValidationError):
        CompanyReportItem.model_validate(
            _company(
                balance={
                    "balance_basis": "UNAVAILABLE",
                    "opening_balance_minor": 0,
                    "closing_balance_minor": 3300,
                    "gap": "AUTHORITATIVE_BALANCE_UNAVAILABLE",
                }
            )
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"business_unit_breakdown_status": "AVAILABLE", "business_units": []},
        {
            "business_unit_breakdown_status": "EMPTY",
            "business_units": [_business_unit()],
        },
        {
            "business_unit_breakdown_status": "UNAVAILABLE_MISSING_SNAPSHOT",
            "business_units": [],
        },
        {
            "business_unit_breakdown_status": "UNAVAILABLE_ATTRIBUTION_PENDING",
            "business_units": [_business_unit()],
        },
    ],
)
def test_business_unit_breakdown_status_cannot_hide_or_invent_rows(
    updates: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        CompanyReportMonth.model_validate(_month(**updates))


def test_statement_and_posted_breakdown_gaps_are_basis_specific() -> None:
    statement = CompanyReportMonth.model_validate(
        _month(
            CompanyReportBasis.ACCOUNT_STATEMENT,
            business_unit_breakdown_status="UNAVAILABLE_ATTRIBUTION_PENDING",
            business_units=None,
        )
    )
    posted = CompanyReportMonth.model_validate(
        _month(
            CompanyReportBasis.POSTED_LEDGER,
            business_unit_breakdown_status="UNAVAILABLE_MISSING_SNAPSHOT",
            business_units=None,
        )
    )

    assert statement.business_units is None
    assert posted.business_units is None


def test_company_report_rejects_unknown_fields_at_every_level() -> None:
    company = _company()
    months = deepcopy(company["months"])
    assert isinstance(months, list)
    month = months[0]
    assert isinstance(month, dict)
    business_units = deepcopy(month["business_units"])
    assert isinstance(business_units, list)
    business_unit = business_units[0]
    assert isinstance(business_unit, dict)

    for model, payload in (
        (CompanyReportBusinessUnit, business_unit | {"bank_name": "must-not-leak"}),
        (CompanyReportMonth, month | {"summary": "must-not-leak"}),
        (CompanyReportItem, company | {"counterparty_name": "must-not-leak"}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_company_business_unit_and_month_bounds_fail_closed() -> None:
    too_many_units = [
        _business_unit(
            business_unit_ref=f"unit-{index:02d}",
            business_unit_label=f"Unit {index:02d}",
        )
        for index in range(51)
    ]
    with pytest.raises(ValidationError):
        CompanyReportMonth.model_validate(_month(business_units=too_many_units))

    too_many_months = [
        _month(month=f"{2025 + index // 12:04d}-{index % 12 + 1:02d}") for index in range(25)
    ]
    with pytest.raises(ValidationError):
        CompanyReportItem.model_validate(_company(months=too_many_months))

    too_many_companies = [
        _company(company_ref=str(UUID(int=index + 1)), company_name=f"Company {index:02d}")
        for index in range(51)
    ]
    with pytest.raises(ValidationError):
        CompanyReportPage.model_validate(
            {
                "basis": "CONFIRMED_CANDIDATE",
                "from_month": "2026-08",
                "to_month": "2026-08",
                "items": too_many_companies,
            }
        )


@pytest.mark.parametrize(
    ("from_month", "to_month"),
    [
        ("2026-09", "2026-08"),
        ("2026-00", "2026-08"),
        ("2025-01", "2027-01"),
    ],
)
def test_company_report_page_rejects_invalid_or_more_than_24_month_ranges(
    from_month: str,
    to_month: str,
) -> None:
    with pytest.raises(ValidationError):
        CompanyReportPage.model_validate(
            {
                "basis": "CONFIRMED_CANDIDATE",
                "from_month": from_month,
                "to_month": to_month,
                "items": [],
            }
        )
