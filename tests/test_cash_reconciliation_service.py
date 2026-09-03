from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from ledgerbridge.internal_read_contract import (
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_service import DatabaseInternalReadService

ENTITY_A = UUID("10000000-0000-4000-8000-000000000001")
ENTITY_B = UUID("10000000-0000-4000-8000-000000000002")
UNIT_A = UUID("11000000-0000-4000-8000-000000000001")
UNIT_B = UUID("11000000-0000-4000-8000-000000000002")


class _Result:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def scalar_one(self) -> dict[str, Any]:
        return self.payload


class _Session:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.statement = ""
        self.parameters: dict[str, Any] = {}

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, parameters: dict[str, Any]) -> _Result:
        self.statement = str(statement)
        self.parameters = parameters
        return _Result(self.payload)


def _payload() -> dict[str, Any]:
    return {
        "contract_version": "ledgerbridge.cash-reconciliation.v2",
        "accounting_month": "2026-08",
        "rules": [],
        "rows": [],
        "issues": [],
        "eligible_fact_count": 0,
        "matched_fact_count": 0,
        "unmatched_fact_count": 0,
        "conflicted_fact_count": 0,
        "issue_count": 0,
        "issues_truncated": False,
        "totals": {"income_minor": 0, "expense_minor": 0, "current_minor": 0},
    }


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:cash-reconciliation-test",
        san_uri="spiffe://ledgerbridge.test/cash-reconciliation-test",
        policy_generation=1,
        capabilities=frozenset({Capability.RECONCILIATION_READ, Capability.LEDGER_READ}),
        grants=(
            EntityGrant(
                entity_ref=ENTITY_B,
                business_unit_refs=frozenset({"unit-b"}),
                business_unit_ids=frozenset({UNIT_B}),
                business_unit_bindings=(("unit-b", UNIT_B),),
            ),
            EntityGrant(
                entity_ref=ENTITY_A,
                business_unit_refs=frozenset({"unit-a"}),
                business_unit_ids=frozenset({UNIT_A}),
                business_unit_bindings=(("unit-a", UNIT_A),),
            ),
        ),
    )


def test_database_cash_reconciliation_binds_sorted_authorized_scope() -> None:
    session = _Session(_payload())
    service = DatabaseInternalReadService(lambda: cast(Session, session))

    projection = service.get_cash_reconciliation(_principal(), month="2026-08")

    assert projection.accounting_month == "2026-08"
    assert "cash_reconciliation_month_v2" in session.statement
    assert "public." not in session.statement
    assert session.parameters == {
        "month": "2026-08",
        "entity_ids": [ENTITY_A, ENTITY_B],
        "business_unit_ids": [UNIT_A, UNIT_B],
    }


def test_database_cash_reconciliation_rejects_empty_entity_scope() -> None:
    principal = WorkloadPrincipal(
        principal_ref="workload:cash-reconciliation-test",
        san_uri="spiffe://ledgerbridge.test/cash-reconciliation-test",
        policy_generation=1,
        capabilities=frozenset({Capability.RECONCILIATION_READ, Capability.LEDGER_READ}),
    )
    service = DatabaseInternalReadService(lambda: cast(Session, _Session(_payload())))

    with pytest.raises(ResourceNotVisible):
        service.get_cash_reconciliation(principal, month="2026-08")
