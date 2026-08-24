from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from ledgerbridge.internal_read_contract import (
    Capability,
    EntityGrant,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_service import (
    DatabaseInternalReadService,
    InternalReadBackendUnavailable,
    SyntheticInternalReadService,
)

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
BUSINESS_UNIT = UUID("11000000-0000-4000-8000-000000000001")


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._rows)


class _Session:
    def __init__(self, candidate_row: dict[str, Any]) -> None:
        self.candidate_row = candidate_row
        self.statements: list[str] = []

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if "current_audit_horizon" in sql:
            return _Result([{"sequence": 7, "hash": b"h" * 32}])
        if "list_candidates_as_of" in sql:
            return _Result([self.candidate_row])
        raise AssertionError(f"unexpected SQL: {sql} / {params}")


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:database-test",
        san_uri="spiffe://ledgerbridge.test/database-test",
        policy_generation=1,
        capabilities=frozenset(
            {Capability.CANDIDATE_READ, Capability.SYSTEM_READ, Capability.EVIDENCE_READ}
        ),
        grants=(
            EntityGrant(
                entity_ref=ENTITY,
                business_unit_refs=frozenset({"unit-demo-a"}),
                business_unit_ids=frozenset({BUSINESS_UNIT}),
                allow_unassigned_candidates=True,
            ),
        ),
    )


def _service(session: _Session) -> DatabaseInternalReadService:
    # The production adapter depends only on the small context-manager/execute
    # surface exercised here; cast the fake to SQLAlchemy's runtime factory
    # type without making the fixture inherit a live database session.
    def factory() -> Session:
        return cast(Session, session)

    return DatabaseInternalReadService(factory)


def test_database_candidate_reader_uses_horizon_and_scoped_function() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    row = candidate.model_dump()
    row["entity_ref"] = ENTITY
    row["business_unit_ref"] = "unit-demo-a"
    session = _Session(row)
    service = _service(session)

    page = service.list_candidates(_principal(), month=candidate.accounting_month)

    assert [item.candidate_ref for item in page.items] == [candidate.candidate_ref]
    assert any("current_audit_horizon" in statement for statement in session.statements)
    assert any("list_candidates_as_of" in statement for statement in session.statements)
    assert all("public." not in statement for statement in session.statements)


def test_database_reader_rejects_ref_only_grants_before_querying_facts() -> None:
    principal = _principal().model_copy(
        update={
            "grants": (
                EntityGrant(
                    entity_ref=ENTITY,
                    business_unit_refs=frozenset({"unit-demo-a"}),
                ),
            )
        }
    )
    session = _Session({})

    with pytest.raises(InternalReadBackendUnavailable, match="immutable business-unit UUIDs"):
        _service(session).list_candidates(principal)
    assert session.statements == []


def test_database_reader_exposes_no_unreviewed_evidence_or_ledger_boundary() -> None:
    service = _service(_Session({}))
    principal = _principal()

    with pytest.raises(InternalReadBackendUnavailable, match="S1 decryptor"):
        service.get_evidence(principal, UUID("20000000-0000-4000-8000-000000000001"))

    ledger_principal = principal.model_copy(
        update={"capabilities": frozenset({Capability.LEDGER_READ})}
    )
    with pytest.raises(InternalReadBackendUnavailable, match="scoped aggregate"):
        service.get_ledger_summary(
            ledger_principal,
            entity_ref=ENTITY,
            business_unit_ref="unit-demo-a",
            from_month="2026-08",
            to_month="2026-08",
        )
