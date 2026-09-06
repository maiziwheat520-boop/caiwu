"""What the company reporting reader refuses when the database misbehaves.

The reader runs SECURITY DEFINER functions as a least-privileged role and then
re-checks everything they return: the audit horizon it pinned, the shape of each
payload, and whether the report it got back is actually about the company the
principal was granted.  A projection that escaped its scope, a horizon that is
missing or malformed, and a statement that simply failed all have to be
distinguishable - a wrong report is worse than no report.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ledgerbridge.company_reporting_contract import CompanyReportBasis
from ledgerbridge.company_reporting_service import DatabaseCompanyReportingService
from ledgerbridge.internal_read_contract import (
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable
from tests.test_company_reporting_database_service import (
    COMPANY_A,
    COMPANY_B,
    COMPANY_UNKNOWN,
    PERSON_WITH_REGISTRY_ACCESS,
    UNIT_A,
    UNIT_B,
    _common_counts,
    _grant,
    _metrics,
    _principal,
    _report,
    _Result,
)

HORIZON: tuple[Mapping[str, object], ...] = ({"sequence": 17, "hash": b"h" * 32},)


class _ScriptedSession:
    """A session whose audit horizon and per-company rows are both scripted."""

    def __init__(
        self,
        rows: Mapping[UUID, list[Mapping[str, object]]] | None = None,
        *,
        horizon: tuple[Mapping[str, object], ...] = HORIZON,
        failure: Exception | None = None,
    ) -> None:
        self._rows = dict(rows or {})
        self._horizon = horizon
        self._failure = failure
        self.calls: list[str] = []

    def __enter__(self) -> _ScriptedSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        self.calls.append(sql)
        if "current_audit_horizon" in sql:
            return _Result(list(self._horizon))
        if self._failure is not None:
            raise self._failure
        entity_ref = (params or {}).get("entity_ref")
        assert isinstance(entity_ref, UUID)
        return _Result(list(self._rows.get(entity_ref, [])))


def _reporting(session: _ScriptedSession) -> DatabaseCompanyReportingService:
    return DatabaseCompanyReportingService(lambda: cast(Session, session))


def _registry_only_principal() -> WorkloadPrincipal:
    return _principal(
        (EntityGrant(entity_ref=PERSON_WITH_REGISTRY_ACCESS, allow_account_registry=True),)
    )


def _database_error() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection lost"))


def _composition(company_ref: UUID = COMPANY_A, *, basis: str = "CONFIRMED_CANDIDATE") -> Any:
    return {
        "company_ref": str(company_ref),
        "company_name": "Company A",
        "currency": "CNY",
        "basis": basis,
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


def _posted_ledger_composition(company_ref: UUID = COMPANY_A) -> Any:
    """A composition that is valid on its own terms, but for the other basis."""
    return {
        "company_ref": str(company_ref),
        "company_name": "Company A",
        "currency": "CNY",
        "basis": "POSTED_LEDGER",
        "revenue": {
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
        "expense": {
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


def _unattributed_statement_report() -> dict[str, object]:
    """A statement report whose month has no business-unit attribution yet."""
    basis = CompanyReportBasis.ACCOUNT_STATEMENT
    month = {
        "month": "2026-08",
        "metrics": _metrics(basis, zero=False),
        **_common_counts(zero=False),
        "business_unit_breakdown_status": "UNAVAILABLE_ATTRIBUTION_PENDING",
        "business_units": None,
    }
    return {
        "company_ref": str(COMPANY_A),
        "company_name": "Company A",
        "currency": "CNY",
        "metrics": _metrics(basis, zero=False),
        **_common_counts(zero=False),
        "business_unit_breakdown_status": "UNAVAILABLE_ATTRIBUTION_PENDING",
        "months": [month],
    }


def _read_report(session: _ScriptedSession, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "basis": CompanyReportBasis.CONFIRMED_CANDIDATE,
        "from_month": "2026-08",
        "to_month": "2026-08",
    }
    principal = overrides.pop("principal", _principal((_grant(COMPANY_A, "unit-a", UNIT_A),)))
    arguments.update(overrides)
    return _reporting(session).report(principal, **arguments)


def _read_composition(session: _ScriptedSession, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "basis": CompanyReportBasis.CONFIRMED_CANDIDATE,
        "from_month": "2026-08",
        "to_month": "2026-08",
    }
    principal = overrides.pop("principal", _principal((_grant(COMPANY_A, "unit-a", UNIT_A),)))
    arguments.update(overrides)
    return _reporting(session).composition(principal, **arguments)


# --- inputs the reader refuses before it queries anything --------------------


def test_a_report_basis_the_contract_does_not_define_is_refused() -> None:
    """The basis names which financial layer is being read; there is no default."""
    session = _ScriptedSession()

    with pytest.raises(ValueError, match="company report basis is invalid"):
        _read_report(session, basis=cast(CompanyReportBasis, "MANAGEMENT_ACCOUNTS"))

    assert session.calls == []


def test_a_composition_basis_the_contract_does_not_define_is_refused() -> None:
    """The composition reader is held to the same closed set of bases."""
    session = _ScriptedSession()

    with pytest.raises(ValueError, match="company report composition basis is invalid"):
        _read_composition(session, basis=cast(CompanyReportBasis, "MANAGEMENT_ACCOUNTS"))

    assert session.calls == []


def test_a_principal_with_no_reporting_scope_sees_no_report() -> None:
    """A registry-only grant carries no reporting scope, so it reads as nothing at all."""
    session = _ScriptedSession()

    with pytest.raises(ResourceNotVisible, match="resource was not found"):
        _read_report(session, principal=_registry_only_principal())

    assert session.calls == []


def test_a_principal_with_no_reporting_scope_sees_no_composition() -> None:
    """The same holds for the composition reader: no scope, no answer."""
    session = _ScriptedSession()

    with pytest.raises(ResourceNotVisible, match="resource was not found"):
        _read_composition(session, principal=_registry_only_principal())

    assert session.calls == []


def test_a_composition_for_a_company_outside_the_grants_is_not_visible() -> None:
    """An ungranted company is reported as absent, not as forbidden."""
    session = _ScriptedSession()

    with pytest.raises(ResourceNotVisible, match="resource was not found"):
        _read_composition(session, company_ref=COMPANY_UNKNOWN)

    assert session.calls == []


def test_grants_that_name_one_company_twice_are_refused() -> None:
    """Two grants for one company would read it twice and double its totals."""
    session = _ScriptedSession()
    principal = _principal(
        (
            _grant(COMPANY_A, "unit-a", UNIT_A),
            _grant(COMPANY_A, "unit-b", UNIT_B),
        )
    )

    with pytest.raises(InternalReadBackendUnavailable, match="duplicate an entity"):
        _read_report(session, principal=principal)


# --- the audit horizon -------------------------------------------------------


def test_a_report_without_an_audit_horizon_is_unavailable() -> None:
    """Every company in one page is read as of one horizon; with none there is no page."""
    session = _ScriptedSession(horizon=())

    with pytest.raises(InternalReadBackendUnavailable, match="audit horizon is unavailable"):
        _read_report(session)


@pytest.mark.parametrize(
    "horizon",
    [
        {"sequence": True, "hash": b"h" * 32},
        {"sequence": "17", "hash": b"h" * 32},
        {"sequence": 0, "hash": b"h" * 32},
        {"sequence": 17, "hash": "h" * 32},
        {"sequence": 17, "hash": b"h" * 31},
        {"sequence": 17, "hash": None},
    ],
)
def test_a_malformed_audit_horizon_makes_the_report_unavailable(
    horizon: dict[str, object],
) -> None:
    """A horizon that is not a whole sequence and a 32-byte hash pins nothing."""
    session = _ScriptedSession(horizon=(horizon,))

    with pytest.raises(InternalReadBackendUnavailable, match="audit horizon is malformed"):
        _read_report(session)


# --- what the reader does when a statement fails -----------------------------


def test_a_report_the_database_could_not_produce_is_unavailable() -> None:
    """A failed statement becomes an unavailable backend, never an empty report."""
    session = _ScriptedSession(failure=_database_error())

    with pytest.raises(InternalReadBackendUnavailable, match="report projection is unavailable"):
        _read_report(session)


def test_a_composition_the_database_could_not_produce_is_unavailable() -> None:
    """The composition reader reports its own failure with its own message."""
    session = _ScriptedSession(failure=_database_error())

    with pytest.raises(InternalReadBackendUnavailable, match="composition is unavailable"):
        _read_composition(session)


def test_a_composition_for_a_company_with_no_rows_is_not_visible() -> None:
    """An explicitly named company that returns nothing is absent, not empty."""
    session = _ScriptedSession()

    with pytest.raises(ResourceNotVisible, match="resource was not found"):
        _read_composition(session, company_ref=COMPANY_A)


def test_a_composition_collection_omits_a_company_that_returns_nothing() -> None:
    """An unfiltered collection may include people, who have no composition at all."""
    session = _ScriptedSession({COMPANY_A: [{"composition": _composition()}]})
    principal = _principal(
        (
            _grant(COMPANY_A, "unit-a", UNIT_A),
            _grant(COMPANY_B, "unit-b", UNIT_B),
        )
    )

    page = _read_composition(session, principal=principal)

    assert [item.company_ref for item in page.items] == [COMPANY_A]


def test_a_composition_returning_more_than_one_row_is_unavailable() -> None:
    """One company at one horizon is one row; two means the function is not what it was."""
    session = _ScriptedSession(
        {COMPANY_A: [{"composition": _composition()}, {"composition": _composition()}]}
    )

    with pytest.raises(InternalReadBackendUnavailable, match="invalid row count"):
        _read_composition(session, company_ref=COMPANY_A)


# --- re-reading what the database returned -----------------------------------


def test_a_report_payload_that_is_not_an_object_is_refused() -> None:
    """The payload is JSON from a SQL function; its type is checked before its fields."""
    session = _ScriptedSession({COMPANY_A: [{"report": "not an object"}]})

    with pytest.raises(InternalReadBackendUnavailable, match="report payload is malformed"):
        _read_report(session)


def test_a_report_payload_that_fails_the_contract_is_refused() -> None:
    """A payload missing its metrics cannot be shown as a report of anything."""
    session = _ScriptedSession({COMPANY_A: [{"report": {"company_ref": str(COMPANY_A)}}]})

    with pytest.raises(InternalReadBackendUnavailable, match="failed contract validation"):
        _read_report(session)


def test_a_composition_payload_that_is_not_an_object_is_refused() -> None:
    """The composition payload is checked the same way."""
    session = _ScriptedSession({COMPANY_A: [{"composition": ["not", "an", "object"]}]})

    with pytest.raises(InternalReadBackendUnavailable, match="composition payload is malformed"):
        _read_composition(session, company_ref=COMPANY_A)


def test_a_composition_payload_that_fails_the_contract_is_refused() -> None:
    """A composition without its positive and negative sides is not a composition."""
    session = _ScriptedSession({COMPANY_A: [{"composition": {"company_ref": str(COMPANY_A)}}]})

    with pytest.raises(InternalReadBackendUnavailable, match="failed contract validation"):
        _read_composition(session, company_ref=COMPANY_A)


def test_a_composition_about_another_company_is_refused() -> None:
    """A payload naming a company the grant did not authorise has escaped its scope."""
    session = _ScriptedSession({COMPANY_A: [{"composition": _composition(COMPANY_B)}]})

    with pytest.raises(InternalReadBackendUnavailable, match="composition escaped its authorized"):
        _read_composition(session, company_ref=COMPANY_A)


def test_a_composition_answering_on_another_basis_is_refused() -> None:
    """A posted-ledger composition is well formed on its own terms, but it answers a
    question about a different financial layer than the one that was asked."""
    session = _ScriptedSession({COMPANY_A: [{"composition": _posted_ledger_composition()}]})

    with pytest.raises(InternalReadBackendUnavailable, match="composition escaped its authorized"):
        _read_composition(session, company_ref=COMPANY_A)


# --- what the reader still accepts -------------------------------------------


def test_a_month_whose_attribution_is_still_pending_is_returned_unattributed() -> None:
    """A statement month with no unit breakdown yet is passed through, not rejected."""
    session = _ScriptedSession({COMPANY_A: [{"report": _unattributed_statement_report()}]})

    page = _read_report(
        session,
        basis=CompanyReportBasis.ACCOUNT_STATEMENT,
        company_ref=COMPANY_A,
    )

    month = page.items[0].months[0]
    assert month.business_units is None
    assert month.business_unit_breakdown_status.value == "UNAVAILABLE_ATTRIBUTION_PENDING"


def test_a_well_formed_report_is_still_read() -> None:
    """None of the refusals above have closed the door on a real report."""
    session = _ScriptedSession({COMPANY_A: [{"report": _report()}]})

    page = _read_report(session, company_ref=COMPANY_A)

    assert [item.company_ref for item in page.items] == [COMPANY_A]
