"""Multi-scope candidate pagination must resume every scope independently.

A merged page is ordered globally, but the reader functions fail closed when a
keyset cursor names a candidate outside the scope being queried.  Carrying a
single global position therefore broke every walk past the first page as soon
as one principal was granted a second business unit.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.internal_read_contract import Capability, EntityGrant, WorkloadPrincipal
from ledgerbridge.internal_read_cursor import ReadCursorSigner
from ledgerbridge.internal_read_service import (
    DatabaseInternalReadService,
    SyntheticInternalReadService,
)

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
UNIT_A = UUID("11000000-0000-4000-8000-00000000000a")
UNIT_B = UUID("11000000-0000-4000-8000-00000000000b")
CURSOR_KEY = "k" * 48
BASE_TIME = datetime(2026, 8, 1, tzinfo=UTC)


class _CursorOutsideScope(SQLAlchemyError):
    """Mirrors PostgreSQL 22023 'candidate cursor is outside requested scope'."""


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self._rows)


def _rows_for(
    unit_ref: str, unit_id: UUID, count: int, offset: int, id_base: int
) -> list[dict[str, Any]]:
    template = SyntheticInternalReadService()._fixture.candidates[1].model_dump()
    rows = []
    for index in range(count):
        row = dict(template)
        row["entity_ref"] = ENTITY
        row["business_unit_ref"] = unit_ref
        row["candidate_ref"] = UUID(int=id_base + index)
        # Interleave the two scopes so a global sort genuinely mixes them.
        moment = BASE_TIME + timedelta(minutes=2 * index + offset)
        row["created_at"] = moment
        row["updated_at"] = moment
        rows.append(row)
    return rows


class _ScopedSession:
    """Serves each scope's own ordered stream and enforces cursor scope."""

    def __init__(self, streams: dict[UUID, list[dict[str, Any]]]) -> None:
        self.streams = streams
        self.rejected_cross_scope = 0

    def __enter__(self) -> _ScopedSession:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        if "current_audit_horizon" in sql:
            return _Result([{"sequence": 7, "hash": b"h" * 32}])
        if "list_candidate_evidence_satisfactions" in sql:
            return _Result([])
        if "list_candidate_counterparty_facts" in sql:
            return _Result([])
        if "list_candidates_as_of" not in sql:
            raise AssertionError(f"unexpected SQL: {sql}")

        assert params is not None
        stream = self.streams[params["business_unit_id"]]
        last_id = params["last_candidate_id"]
        start = 0
        if last_id is not None:
            match = [index for index, row in enumerate(stream) if row["candidate_ref"] == last_id]
            if not match:
                # Exactly what PostgreSQL does today.
                self.rejected_cross_scope += 1
                raise _CursorOutsideScope("candidate cursor is outside requested scope")
            start = match[0] + 1
        limit = params["limit"]
        return _Result(stream[start : start + limit + 1])


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:multi-scope-test",
        san_uri="spiffe://ledgerbridge.test/multi-scope-test",
        policy_generation=1,
        capabilities=frozenset({Capability.CANDIDATE_READ}),
        grants=(
            EntityGrant(
                entity_ref=ENTITY,
                business_unit_refs=frozenset({"unit-a", "unit-b"}),
                business_unit_ids=frozenset({UNIT_A, UNIT_B}),
                business_unit_bindings=(("unit-a", UNIT_A), ("unit-b", UNIT_B)),
            ),
        ),
    )


def _service(session: _ScopedSession) -> DatabaseInternalReadService:
    def factory() -> Session:
        return cast(Session, session)

    return DatabaseInternalReadService(factory, ReadCursorSigner(CURSOR_KEY))


def test_multi_scope_walk_completes_without_cross_scope_cursor() -> None:
    session = _ScopedSession(
        {
            UNIT_A: _rows_for("unit-a", UNIT_A, 260, offset=0, id_base=1 << 100),
            UNIT_B: _rows_for("unit-b", UNIT_B, 130, offset=1, id_base=1 << 101),
        }
    )
    service = _service(session)
    principal = _principal()

    seen: list[UUID] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = service.list_candidates(principal, cursor=cursor)
        pages += 1
        seen.extend(item.candidate_ref for item in page.items)
        assert pages <= 20, "pagination did not terminate"
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert session.rejected_cross_scope == 0
    assert len(seen) == len(set(seen)) == 390


def test_merged_page_is_globally_ordered_across_scopes() -> None:
    session = _ScopedSession(
        {
            UNIT_A: _rows_for("unit-a", UNIT_A, 60, offset=0, id_base=1 << 100),
            UNIT_B: _rows_for("unit-b", UNIT_B, 60, offset=1, id_base=1 << 101),
        }
    )
    service = _service(session)

    page = service.list_candidates(_principal())

    created = [item.created_at for item in page.items]
    assert created == sorted(created)
    refs = {item.business_unit_ref for item in page.items}
    assert refs == {"unit-a", "unit-b"}, "a merged page must interleave both scopes"
