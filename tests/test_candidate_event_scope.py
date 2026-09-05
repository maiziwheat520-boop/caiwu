"""The event feed must refuse the same grants every other candidate read refuses.

A grant may carry business-unit refs or IDs without the explicit bindings the
database reader needs. `list_candidates` and `get_candidate` reject such a
principal outright. The event feed used to build its scopes by reading
`business_unit_bindings` directly, so the same principal was served a narrower
scope set instead -- hiding the events of every assigned candidate rather than
saying it could not answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from ledgerbridge.internal_candidate_command import DatabaseInternalReviewService
from ledgerbridge.internal_read_contract import (
    Capability,
    EntityGrant,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_cursor import ReadCursorSigner
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable

ENTITY = UUID("10000000-0000-4000-8000-000000000001")
UNIT = UUID("11000000-0000-4000-8000-00000000000a")
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
    def __init__(self) -> None:
        self.event_queries = 0

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        if "current_audit_horizon" in sql:
            return _Result([{"sequence": 7, "hash": b"h" * 32}])
        if "list_candidate_events_as_of" in sql:
            self.event_queries += 1
            return _Result([])
        raise AssertionError(f"unexpected SQL: {sql}")


def _unbound_grant_principal() -> WorkloadPrincipal:
    """Refs and IDs are present; the explicit bindings the reader needs are not."""
    return WorkloadPrincipal(
        principal_ref="workload:event-scope-test",
        san_uri="spiffe://ledgerbridge.test/event-scope-test",
        policy_generation=1,
        capabilities=frozenset({Capability.CANDIDATE_READ}),
        grants=(
            EntityGrant(
                entity_ref=ENTITY,
                business_unit_refs=frozenset({"unit-a"}),
                business_unit_ids=frozenset({UNIT}),
                allow_unassigned_candidates=True,
            ),
        ),
    )


def _service(session: _Session) -> DatabaseInternalReviewService:
    factory = cast("Any", lambda: cast(Session, session))
    return DatabaseInternalReviewService(factory, factory, ReadCursorSigner(CURSOR_KEY))


def test_unbound_grant_is_refused_rather_than_narrowed() -> None:
    session = _Session()
    service = _service(session)

    with pytest.raises(InternalReadBackendUnavailable):
        service.list_candidate_events(_unbound_grant_principal())

    assert session.event_queries == 0, "an unusable grant must be refused before any event is read"


def test_the_candidate_reader_refuses_the_same_grant() -> None:
    # The point of the fix: both paths agree about which grants are usable.
    session = _Session()
    service = _service(session)

    with pytest.raises(InternalReadBackendUnavailable):
        service.list_candidates(_unbound_grant_principal())
