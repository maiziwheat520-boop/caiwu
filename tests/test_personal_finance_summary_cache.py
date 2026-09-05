"""The personal finance summary may be reused only at the horizon it was built at.

The summary is a pure function of the candidates a principal may see and the
as-of audit horizon, which is what makes reuse exact rather than a guess. These
tests pin the invalidation, because a summary that outlived its horizon would
show the user totals the ledger no longer supports.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.internal_read_cursor import ReadCursorSigner
from ledgerbridge.internal_read_service import (
    DatabaseInternalReadService,
    SyntheticInternalReadService,
    reset_personal_finance_summary_cache,
)

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
UNIT = UUID("11000000-0000-4000-8000-00000000000a")
OTHER_ENTITY = UUID("10000000-0000-4000-8000-000000000002")
CURSOR_KEY = "k" * 48


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
    """Serves one candidate page and a horizon the test can advance."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sequence = 7
        self.hash = b"h" * 32
        self.candidate_queries = 0

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        if "current_audit_horizon" in sql:
            return _Result([{"sequence": self.sequence, "hash": self.hash}])
        if "list_candidates_as_of" in sql:
            self.candidate_queries += 1
            assert params is not None
            # Echo the requested scope so the reader's scope-binding assertions
            # see rows that genuinely belong to the scope it asked for.
            scoped = []
            for row in self.rows:
                item = dict(row)
                item["entity_ref"] = params["entity_id"]
                scoped.append(item)
            return _Result(scoped)
        if "list_candidate_evidence_satisfactions" in sql:
            return _Result([])
        if "list_candidate_counterparty_facts" in sql:
            return _Result([])
        raise AssertionError(f"unexpected SQL: {sql}")


def _rows() -> list[dict[str, Any]]:
    template = SyntheticInternalReadService()._fixture.candidates[1].model_dump()
    row = dict(template)
    row["entity_ref"] = ENTITY
    row["business_unit_ref"] = "unit-a"
    return [row]


def _principal(entity: UUID = ENTITY, unit: UUID = UNIT) -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:summary-cache-test",
        san_uri="spiffe://ledgerbridge.test/summary-cache-test",
        policy_generation=1,
        capabilities=frozenset({Capability.CANDIDATE_READ}),
        grants=(
            EntityGrant(
                entity_ref=entity,
                business_unit_refs=frozenset({"unit-a"}),
                business_unit_ids=frozenset({unit}),
                business_unit_bindings=(("unit-a", unit),),
            ),
        ),
    )


def _service(session: _Session) -> DatabaseInternalReadService:
    factory = cast("Any", lambda: cast(Session, session))
    return DatabaseInternalReadService(factory, ReadCursorSigner(CURSOR_KEY))


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    reset_personal_finance_summary_cache()
    yield
    reset_personal_finance_summary_cache()


def test_repeated_reads_at_one_horizon_walk_the_candidates_once() -> None:
    session = _Session(_rows())
    service = _service(session)
    principal = _principal()

    first = service.personal_finance_summary(principal)
    walked = session.candidate_queries
    second = service.personal_finance_summary(principal)

    assert walked > 0
    assert session.candidate_queries == walked, "a second read must not walk again"
    assert second == first


def test_an_advanced_horizon_invalidates_the_cached_summary() -> None:
    session = _Session(_rows())
    service = _service(session)
    principal = _principal()

    service.personal_finance_summary(principal)
    walked = session.candidate_queries

    # Any write anywhere advances the audit horizon.
    session.sequence += 1
    session.hash = b"i" * 32
    service.personal_finance_summary(principal)

    assert session.candidate_queries > walked, "an advanced horizon must be rebuilt"


def test_a_different_principal_scope_does_not_reuse_the_entry() -> None:
    session = _Session(_rows())
    service = _service(session)

    service.personal_finance_summary(_principal())
    walked = session.candidate_queries
    service.personal_finance_summary(_principal(entity=OTHER_ENTITY))

    assert session.candidate_queries > walked, "another scope must not read a foreign summary"


def test_a_horizon_that_moves_during_the_walk_is_not_cached() -> None:
    class _MovingSession(_Session):
        def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
            result = super().execute(statement, params)
            if "list_candidates_as_of" in str(statement):
                # A write lands while the summary is being built.
                self.sequence += 1
            return result

    session = _MovingSession(_rows())
    service = _service(session)
    principal = _principal()

    service.personal_finance_summary(principal)
    walked = session.candidate_queries
    service.personal_finance_summary(principal)

    assert session.candidate_queries > walked, "a straddled summary must never be cached"
