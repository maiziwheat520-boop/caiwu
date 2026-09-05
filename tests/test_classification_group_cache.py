"""Classification groups may be reused only at the horizon they were built at.

A cache that outlives its horizon would show reviewers a grouping that no
longer matches the ledger, so the invalidation is the part that matters here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from ledgerbridge.internal_candidate_command import (
    DatabaseInternalReviewService,
    reset_classification_group_cache,
)
from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.internal_read_cursor import ReadCursorSigner
from ledgerbridge.internal_read_service import SyntheticInternalReadService

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
        principal_ref="workload:group-cache-test",
        san_uri="spiffe://ledgerbridge.test/group-cache-test",
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


def _service(session: _Session) -> DatabaseInternalReviewService:
    factory = cast("Any", lambda: cast(Session, session))
    return DatabaseInternalReviewService(factory, factory, ReadCursorSigner(CURSOR_KEY))


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    reset_classification_group_cache()
    yield
    reset_classification_group_cache()


def test_repeated_reads_at_one_horizon_walk_the_candidates_once() -> None:
    session = _Session(_rows())
    service = _service(session)
    principal = _principal()

    first = service.list_classification_groups(principal)
    walked = session.candidate_queries
    second = service.list_classification_groups(principal)

    assert walked > 0
    assert session.candidate_queries == walked, "a second read must not walk again"
    assert second.items == first.items


def test_an_advanced_horizon_invalidates_the_cached_grouping() -> None:
    session = _Session(_rows())
    service = _service(session)
    principal = _principal()

    service.list_classification_groups(principal)
    walked = session.candidate_queries

    # Any write anywhere advances the audit horizon.
    session.sequence += 1
    session.hash = b"i" * 32
    service.list_classification_groups(principal)

    assert session.candidate_queries > walked, "an advanced horizon must be rebuilt"


def test_a_different_principal_scope_does_not_reuse_the_entry() -> None:
    session = _Session(_rows())
    service = _service(session)

    service.list_classification_groups(_principal())
    walked = session.candidate_queries
    service.list_classification_groups(_principal(entity=OTHER_ENTITY))

    assert session.candidate_queries > walked, "another scope must not read a foreign grouping"


def test_a_horizon_that_moves_during_the_walk_is_not_cached() -> None:
    rows = _rows()

    class _MovingSession(_Session):
        def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
            result = super().execute(statement, params)
            if "list_candidates_as_of" in str(statement):
                # A write lands while the grouping is being built.
                self.sequence += 1
            return result

    session = _MovingSession(rows)
    service = _service(session)
    principal = _principal()

    service.list_classification_groups(principal)
    walked = session.candidate_queries
    service.list_classification_groups(principal)

    assert session.candidate_queries > walked, "a straddled grouping must never be cached"
