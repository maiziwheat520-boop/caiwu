from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from ledgerbridge.company_reporting_contract import CompanyReportBasis
from ledgerbridge.company_reporting_service import DatabaseCompanyReportingService
from ledgerbridge.internal_read_contract import (
    AuthorizationDenied,
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable

COMPANY_A = UUID("10000000-0000-4000-8000-000000000001")
COMPANY_B = UUID("10000000-0000-4000-8000-000000000002")
COMPANY_UNKNOWN = UUID("10000000-0000-4000-8000-000000000099")
PERSON_WITH_REGISTRY_ACCESS = UUID("10000000-0000-4000-8000-000000000098")
UNIT_A = UUID("11000000-0000-4000-8000-000000000001")
UNIT_B = UUID("11000000-0000-4000-8000-000000000002")


def _balance() -> dict[str, object]:
    return {
        "balance_basis": "UNAVAILABLE",
        "opening_balance_minor": None,
        "closing_balance_minor": None,
        "gap": "AUTHORITATIVE_BALANCE_UNAVAILABLE",
    }


def _metrics(basis: CompanyReportBasis, *, zero: bool) -> dict[str, object]:
    if basis is CompanyReportBasis.CONFIRMED_CANDIDATE:
        return {
            "basis": basis.value,
            "confirmed_positive_minor": 0 if zero else 7300,
            "confirmed_negative_minor": 0 if zero else -2100,
            "confirmed_net_minor": 0 if zero else 5200,
            "confirmed_count": 0 if zero else 3,
            "source_count": 0 if zero else 2,
        }
    if basis is CompanyReportBasis.ACCOUNT_STATEMENT:
        return {
            "basis": basis.value,
            "cash_inflow_minor": 0 if zero else 7300,
            "cash_outflow_minor": 0 if zero else 2100,
            "net_cash_flow_minor": 0 if zero else 5200,
            "confirmed_transaction_count": 0 if zero else 3,
            "statement_count": 0 if zero else 1,
        }
    return {
        "basis": basis.value,
        "revenue_minor": 0 if zero else 7300,
        "expense_minor": 0 if zero else 2100,
        "profit_minor": 0 if zero else 5200,
        "posted_entry_count": 0 if zero else 3,
        "source_count": 0 if zero else 2,
    }


def _common_counts(*, zero: bool) -> dict[str, object]:
    return {
        "pending_review_count": 0 if zero else 1,
        "attribution_pending_count": 0 if zero else 2,
        "missing_material_count": None,
        "taxonomy_version": None,
        "balance": _balance(),
    }


def _report(
    company_ref: UUID = COMPANY_A,
    *,
    basis: CompanyReportBasis = CompanyReportBasis.CONFIRMED_CANDIDATE,
    company_name: str = "Company A",
    business_unit_ref: str = "unit-a",
    business_unit_label: str = "Unit A",
    zero: bool = False,
) -> dict[str, object]:
    business_unit = {
        "business_unit_ref": business_unit_ref,
        "business_unit_label": business_unit_label,
        "metrics": _metrics(basis, zero=zero),
        **_common_counts(zero=zero),
    }
    month = {
        "month": "2026-08",
        "metrics": _metrics(basis, zero=zero),
        **_common_counts(zero=zero),
        "business_unit_breakdown_status": "EMPTY" if zero else "AVAILABLE",
        "business_units": [] if zero else [business_unit],
    }
    return {
        "company_ref": str(company_ref),
        "company_name": company_name,
        "currency": "CNY",
        "metrics": _metrics(basis, zero=zero),
        **_common_counts(zero=zero),
        "business_unit_breakdown_status": "EMPTY" if zero else "AVAILABLE",
        "months": [] if zero else [month],
    }


class _Result:
    def __init__(self, rows: list[Mapping[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def first(self) -> Mapping[str, object] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Mapping[str, object]]:
        return list(self._rows)

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        return iter(self._rows)


class _Session:
    def __init__(
        self,
        reports: Mapping[UUID | tuple[UUID, str], list[Mapping[str, object]]],
    ) -> None:
        self.reports = reports
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        values = dict(params or {})
        self.calls.append((sql, values))
        if "current_audit_horizon" in sql:
            return _Result([{"sequence": 17, "hash": b"h" * 32}])
        if "get_company_report_composition_v1_as_of" in sql:
            entity_ref = values.get("entity_ref", values.get("company_ref"))
            assert isinstance(entity_ref, UUID)
            basis = values.get("basis")
            assert isinstance(basis, str)
            rows = self.reports.get((entity_ref, basis), self.reports.get(entity_ref, []))
            return _Result(list(rows))
        if "company_reporting_read.get_company_report_v1_as_of" in sql:
            entity_ref = values.get("entity_ref", values.get("company_ref"))
            assert isinstance(entity_ref, UUID)
            basis = values.get("basis")
            assert isinstance(basis, str)
            rows = self.reports.get((entity_ref, basis), self.reports.get(entity_ref, []))
            return _Result(list(rows))
        raise AssertionError(f"unexpected SQL: {sql} / {values}")


def _service(session: _Session) -> DatabaseCompanyReportingService:
    def factory() -> Session:
        return cast(Session, session)

    return DatabaseCompanyReportingService(factory)


def _grant(
    entity_ref: UUID,
    ref: str,
    unit_id: UUID,
    *,
    allow_unassigned: bool = True,
) -> EntityGrant:
    return EntityGrant(
        entity_ref=entity_ref,
        business_unit_refs=frozenset({ref}),
        business_unit_ids=frozenset({unit_id}),
        business_unit_bindings=((ref, unit_id),),
        allow_unassigned_candidates=allow_unassigned,
    )


def _principal(
    grants: tuple[EntityGrant, ...] | None = None,
    *,
    capability: bool = True,
) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:company-report-test",
        san_uri="spiffe://ledgerbridge.test/company-report-test",
        policy_generation=3,
        capabilities=(frozenset({Capability.COMPANY_REPORT_READ}) if capability else frozenset()),
        grants=grants
        or (
            _grant(COMPANY_A, "unit-a", UNIT_A),
            _grant(COMPANY_B, "unit-b", UNIT_B),
        ),
    )


def test_database_company_report_reads_each_authorized_company_once() -> None:
    session = _Session(
        {
            COMPANY_A: [{"report": _report()}],
            COMPANY_B: [
                {
                    "report": _report(
                        COMPANY_B,
                        company_name="Company B",
                        business_unit_ref="unit-b",
                        business_unit_label="Unit B",
                    )
                }
            ],
        }
    )

    page = _service(session).report(
        _principal(),
        basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
        from_month="2026-08",
        to_month="2026-08",
    )

    assert page.contract_version == "ledgerbridge.company-report.v1"
    assert page.basis == "CONFIRMED_CANDIDATE"
    assert [item.company_ref for item in page.items] == [COMPANY_A, COMPANY_B]
    metric_values = page.items[0].metrics.model_dump()
    assert metric_values["confirmed_positive_minor"] == 7300
    assert metric_values["confirmed_negative_minor"] == -2100
    assert metric_values["confirmed_net_minor"] == 5200

    horizon_calls = [call for call in session.calls if "current_audit_horizon" in call[0]]
    report_calls = [
        call
        for call in session.calls
        if "company_reporting_read.get_company_report_v1_as_of" in call[0]
    ]
    assert len(horizon_calls) == 1
    assert len(report_calls) == 2
    assert {call[1].get("entity_ref", call[1].get("company_ref")) for call in report_calls} == {
        COMPANY_A,
        COMPANY_B,
    }
    assert {
        frozenset(cast(list[UUID] | tuple[UUID, ...], call[1]["business_unit_ids"]))
        for call in report_calls
    } == {frozenset({UNIT_A}), frozenset({UNIT_B})}
    assert all(call[1]["from_month"] == date(2026, 8, 1) for call in report_calls)
    assert all(call[1]["to_month"] == date(2026, 8, 1) for call in report_calls)
    assert all(call[1]["audit_sequence"] == 17 for call in report_calls)
    assert all(call[1]["audit_hash"] == b"h" * 32 for call in report_calls)
    assert all(call[1]["basis"] == "CONFIRMED_CANDIDATE" for call in report_calls)
    assert all("public." not in sql for sql, _ in session.calls)


def test_valid_company_without_facts_is_returned_as_zero_not_unknown() -> None:
    session = _Session({COMPANY_A: [{"report": _report(zero=True)}]})

    page = _service(session).report(
        _principal((_grant(COMPANY_A, "unit-a", UNIT_A),)),
        basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
        from_month="2026-01",
        to_month="2026-08",
        company_ref=COMPANY_A,
    )

    item = page.items[0]
    assert item.company_ref == COMPANY_A
    metric_values = item.metrics.model_dump()
    assert metric_values["confirmed_positive_minor"] == 0
    assert metric_values["confirmed_negative_minor"] == metric_values["confirmed_net_minor"] == 0
    assert metric_values["confirmed_count"] == metric_values["source_count"] == 0
    assert item.pending_review_count == item.attribution_pending_count == 0
    assert item.balance.model_dump() == _balance()
    assert item.months == ()


def test_database_company_composition_reads_categories_at_one_audit_horizon() -> None:
    composition = {
        "company_ref": str(COMPANY_A),
        "company_name": "Company A",
        "currency": "CNY",
        "basis": "CONFIRMED_CANDIDATE",
        "positive": {
            "total_minor": 7300,
            "fact_count": 2,
            "items": [
                {
                    "category_code": "ROOM",
                    "category_label": "Room revenue",
                    "amount_minor": 7300,
                    "fact_count": 2,
                }
            ],
        },
        "negative": {
            "total_minor": 2100,
            "fact_count": 1,
            "items": [
                {
                    "category_code": None,
                    "category_label": None,
                    "amount_minor": 2100,
                    "fact_count": 1,
                }
            ],
        },
    }
    session = _Session({COMPANY_A: [{"composition": composition}]})

    page = _service(session).composition(
        _principal((_grant(COMPANY_A, "unit-a", UNIT_A),)),
        basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
        from_month="2026-08",
        to_month="2026-08",
        company_ref=COMPANY_A,
    )

    assert page.contract_version == "ledgerbridge.company-report-composition.v1"
    assert page.items[0].positive.total_minor == 7300
    assert page.items[0].negative.items[0].category_code is None
    composition_calls = [
        call for call in session.calls if "get_company_report_composition_v1_as_of" in call[0]
    ]
    assert len(composition_calls) == 1
    assert composition_calls[0][1]["audit_sequence"] == 17
    assert composition_calls[0][1]["audit_hash"] == b"h" * 32


def test_database_company_composition_rejects_statement_basis_before_query() -> None:
    session = _Session({})

    with pytest.raises(ValueError, match="do not define"):
        _service(session).composition(
            _principal((_grant(COMPANY_A, "unit-a", UNIT_A),)),
            basis=CompanyReportBasis.ACCOUNT_STATEMENT,
            from_month="2026-08",
            to_month="2026-08",
        )

    assert session.calls == []


def test_collection_omits_granted_non_company_entities() -> None:
    session = _Session({COMPANY_A: [{"report": _report()}]})

    page = _service(session).report(
        _principal(
            (
                _grant(COMPANY_A, "unit-a", UNIT_A),
                _grant(COMPANY_B, "unit-b", UNIT_B),
            )
        ),
        basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
        from_month="2026-08",
        to_month="2026-08",
    )

    assert [item.company_ref for item in page.items] == [COMPANY_A]
    report_calls = [
        call
        for call in session.calls
        if "company_reporting_read.get_company_report_v1_as_of" in call[0]
    ]
    assert len(report_calls) == 2


def test_collection_ignores_registry_only_grants_without_reporting_scope() -> None:
    session = _Session({COMPANY_A: [{"report": _report()}]})
    principal = _principal(
        (
            _grant(COMPANY_A, "unit-a", UNIT_A),
            EntityGrant(
                entity_ref=PERSON_WITH_REGISTRY_ACCESS,
                allow_account_registry=True,
            ),
        )
    )

    page = _service(session).report(
        principal,
        basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
        from_month="2026-01",
        to_month="2026-09",
    )

    assert [item.company_ref for item in page.items] == [COMPANY_A]
    report_calls = [
        call
        for call in session.calls
        if "company_reporting_read.get_company_report_v1_as_of" in call[0]
    ]
    assert len(report_calls) == 1
    assert report_calls[0][1]["entity_ref"] == COMPANY_A


def test_candidate_statement_and_posted_bases_are_returned_without_cross_layer_mixing() -> None:
    confirmed_candidate = _report()
    candidate_aggregates = [confirmed_candidate]
    candidate_months = confirmed_candidate["months"]
    assert isinstance(candidate_months, list)
    candidate_aggregates.extend(candidate_months)
    candidate_units = candidate_months[0]["business_units"]
    assert isinstance(candidate_units, list)
    candidate_aggregates.extend(candidate_units)
    for aggregate in candidate_aggregates:
        assert isinstance(aggregate, dict)
        metrics = aggregate["metrics"]
        assert isinstance(metrics, dict)
        metrics["confirmed_count"] = 61
        metrics["source_count"] = 61
        aggregate["pending_review_count"] = 146
        aggregate["attribution_pending_count"] = 61
    empty_statement_basis = _report(
        basis=CompanyReportBasis.ACCOUNT_STATEMENT,
        zero=True,
    )
    empty_posted_basis = _report(
        basis=CompanyReportBasis.POSTED_LEDGER,
        zero=True,
    )
    session = _Session(
        {
            (COMPANY_A, "CONFIRMED_CANDIDATE"): [{"report": confirmed_candidate}],
            (COMPANY_A, "ACCOUNT_STATEMENT"): [{"report": empty_statement_basis}],
            (COMPANY_A, "POSTED_LEDGER"): [{"report": empty_posted_basis}],
        }
    )
    service = _service(session)
    principal = _principal((_grant(COMPANY_A, "unit-a", UNIT_A),))

    candidate_page = service.report(
        principal,
        basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
        from_month="2026-01",
        to_month="2026-08",
        company_ref=COMPANY_A,
    )
    statement_page = service.report(
        principal,
        basis=CompanyReportBasis.ACCOUNT_STATEMENT,
        from_month="2026-01",
        to_month="2026-08",
        company_ref=COMPANY_A,
    )
    posted_page = service.report(
        principal,
        basis=CompanyReportBasis.POSTED_LEDGER,
        from_month="2026-01",
        to_month="2026-08",
        company_ref=COMPANY_A,
    )

    assert candidate_page.basis is CompanyReportBasis.CONFIRMED_CANDIDATE
    candidate_metrics = candidate_page.items[0].metrics.model_dump()
    assert candidate_metrics["confirmed_count"] == 61
    assert candidate_metrics["source_count"] == 61
    assert candidate_page.items[0].pending_review_count == 146
    assert candidate_page.items[0].attribution_pending_count == 61
    statement_metrics = statement_page.items[0].metrics.model_dump()
    assert statement_metrics["cash_inflow_minor"] == 0
    assert statement_metrics["cash_outflow_minor"] == 0
    assert statement_metrics["confirmed_transaction_count"] == 0
    posted_metrics = posted_page.items[0].metrics.model_dump()
    assert posted_metrics["revenue_minor"] == 0
    assert posted_metrics["expense_minor"] == 0
    assert posted_metrics["posted_entry_count"] == 0
    assert statement_page.basis is CompanyReportBasis.ACCOUNT_STATEMENT
    assert posted_page.basis is CompanyReportBasis.POSTED_LEDGER


def test_unauthorized_and_unknown_company_are_indistinguishable() -> None:
    principal = _principal((_grant(COMPANY_A, "unit-a", UNIT_A),))
    unauthorized_session = _Session({COMPANY_A: [{"report": _report()}]})
    unknown_session = _Session({})

    with pytest.raises(ResourceNotVisible) as unauthorized:
        _service(unauthorized_session).report(
            principal,
            basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
            from_month="2026-08",
            to_month="2026-08",
            company_ref=COMPANY_UNKNOWN,
        )
    with pytest.raises(ResourceNotVisible) as unknown:
        _service(unknown_session).report(
            principal,
            basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
            from_month="2026-08",
            to_month="2026-08",
            company_ref=COMPANY_A,
        )

    assert str(unauthorized.value) == str(unknown.value) == "resource was not found"
    assert unauthorized_session.calls == []


def test_ledger_capability_is_required_before_database_access() -> None:
    session = _Session({COMPANY_A: [{"report": _report()}]})

    with pytest.raises(AuthorizationDenied):
        _service(session).report(
            _principal((_grant(COMPANY_A, "unit-a", UNIT_A),), capability=False),
            basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
            from_month="2026-08",
            to_month="2026-08",
        )

    assert session.calls == []


@pytest.mark.parametrize(
    "grants",
    [
        (
            EntityGrant(
                entity_ref=COMPANY_A,
                business_unit_refs=frozenset({"unit-a"}),
            ),
        ),
        tuple(
            _grant(
                UUID(int=index + 1),
                f"unit-{index:02d}",
                UUID(int=1000 + index),
                allow_unassigned=False,
            )
            for index in range(51)
        ),
        (
            EntityGrant(
                entity_ref=COMPANY_A,
                business_unit_refs=frozenset(f"unit-{index:02d}" for index in range(51)),
                business_unit_ids=frozenset(UUID(int=1000 + index) for index in range(51)),
                business_unit_bindings=tuple(
                    (f"unit-{index:02d}", UUID(int=1000 + index)) for index in range(51)
                ),
            ),
        ),
    ],
)
def test_database_scope_requires_immutable_bindings_and_quantity_bounds(
    grants: tuple[EntityGrant, ...],
) -> None:
    session = _Session({})

    with pytest.raises(InternalReadBackendUnavailable):
        _service(session).report(
            _principal(grants),
            basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
            from_month="2026-08",
            to_month="2026-08",
        )

    assert session.calls == []


@pytest.mark.parametrize(
    "rows",
    [
        [{"report": _report(COMPANY_B)}],
        [
            {
                "report": _report(
                    business_unit_ref="unit-not-granted",
                    business_unit_label="Not Granted",
                )
            }
        ],
        [{"report": _report()}, {"report": _report()}],
    ],
)
def test_database_response_is_revalidated_against_company_and_unit_scope(
    rows: list[Mapping[str, object]],
) -> None:
    session = _Session({COMPANY_A: rows})

    with pytest.raises(InternalReadBackendUnavailable):
        _service(session).report(
            _principal((_grant(COMPANY_A, "unit-a", UNIT_A),)),
            basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
            from_month="2026-08",
            to_month="2026-08",
        )


@pytest.mark.parametrize(
    ("from_month", "to_month"),
    [("2026-09", "2026-08"), ("2025-01", "2027-01"), ("2026-13", "2026-13")],
)
def test_database_service_rejects_invalid_or_more_than_24_month_ranges_before_query(
    from_month: str,
    to_month: str,
) -> None:
    session = _Session({COMPANY_A: [{"report": _report()}]})

    with pytest.raises(ValueError):
        _service(session).report(
            _principal((_grant(COMPANY_A, "unit-a", UNIT_A),)),
            basis=CompanyReportBasis.CONFIRMED_CANDIDATE,
            from_month=from_month,
            to_month=to_month,
        )

    assert session.calls == []
