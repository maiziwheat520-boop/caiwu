from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.internal_read_contract import (
    CandidatePage,
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_cursor import ReadCursorSigner
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
    def __init__(
        self,
        candidate_row: dict[str, Any],
        *,
        candidate_rows: list[dict[str, Any]] | None = None,
        reconciliation_row: dict[str, Any] | None = None,
        fail: bool = False,
    ) -> None:
        self.candidate_row = candidate_row
        self.candidate_rows = candidate_rows or [candidate_row]
        self.reconciliation_row = reconciliation_row
        self.fail = fail
        self.statements: list[str] = []

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if self.fail:
            raise SQLAlchemyError("synthetic database failure")
        if "current_audit_horizon" in sql:
            return _Result([{"sequence": 7, "hash": b"h" * 32}])
        if "list_candidates_as_of" in sql:
            return _Result(self.candidate_rows)
        if "get_reconciliation_as_of" in sql:
            return _Result([] if self.reconciliation_row is None else [self.reconciliation_row])
        raise AssertionError(f"unexpected SQL: {sql} / {params}")


def _principal() -> WorkloadPrincipal:
    return WorkloadPrincipal(
        principal_ref="workload:database-test",
        san_uri="spiffe://ledgerbridge.test/database-test",
        policy_generation=1,
        capabilities=frozenset(
            {
                Capability.CANDIDATE_READ,
                Capability.SYSTEM_READ,
                Capability.EVIDENCE_READ,
                Capability.RECONCILIATION_READ,
            }
        ),
        grants=(
            EntityGrant(
                entity_ref=ENTITY,
                business_unit_refs=frozenset({"unit-demo-a"}),
                business_unit_ids=frozenset({BUSINESS_UNIT}),
                business_unit_bindings=(("unit-demo-a", BUSINESS_UNIT),),
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

    with pytest.raises(InternalReadBackendUnavailable, match="explicit business-unit"):
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


def test_database_candidate_reader_issues_and_verifies_a_keyset_cursor() -> None:
    template = SyntheticInternalReadService()._fixture.candidates[1].model_dump()
    rows: list[dict[str, Any]] = []
    for index in range(101):
        row = dict(template)
        row["candidate_ref"] = UUID(f"30000000-0000-4000-8000-{index + 100:012d}")
        row["short_id"] = f"C-{index:05d}"
        row["created_at"] = datetime(2026, 8, 24, tzinfo=UTC) + timedelta(seconds=index)
        row["updated_at"] = row["created_at"]
        rows.append(row)
    session = _Session(rows[0], candidate_rows=rows)
    signer_key = "k" * 32
    service = DatabaseInternalReadService(
        lambda: cast(Session, session),
        cursor_signer=ReadCursorSigner(signer_key),
    )

    page = service.list_candidates(_principal())

    assert len(page.items) == 100
    assert page.next_cursor is not None
    claims = ReadCursorSigner(signer_key).verify(
        page.next_cursor, _principal(), month=None, status=None, business_unit=None
    )
    assert claims["horizon_sequence"] == 7
    assert claims["last_candidate_id"] == page.items[-1].candidate_ref


def test_database_reconciliation_reader_projects_rows_and_hides_missing() -> None:
    row = {
        "entity_ref": ENTITY,
        "business_unit_ref": "unit-demo-a",
        "month": "2026-08",
        "snapshot_revision": 1,
        "blockers": (),
        "proposals": (),
        "suspense": (),
        "posted_amount_minor": 123,
        "currency": "CNY",
    }
    session = _Session({}, reconciliation_row=row)
    service = _service(session)

    projection = service.get_reconciliation(
        _principal(), month="2026-08", entity_ref=ENTITY, business_unit_ref="unit-demo-a"
    )
    assert projection.posted_amount_minor == 123

    missing = _service(_Session({}))
    with pytest.raises(ResourceNotVisible, match="resource was not found"):
        missing.get_reconciliation(
            _principal(), month="2026-08", entity_ref=ENTITY, business_unit_ref="unit-demo-a"
        )


def test_database_reader_translates_driver_and_projection_failures() -> None:
    failing = _service(_Session({}, fail=True))
    with pytest.raises(InternalReadBackendUnavailable, match="candidate read failed"):
        failing.list_candidates(_principal())

    malformed = _Session({}, reconciliation_row={"entity_ref": ENTITY})
    with pytest.raises(InternalReadBackendUnavailable, match="projection is invalid"):
        _service(malformed).get_reconciliation(
            _principal(), month="2026-08", entity_ref=ENTITY, business_unit_ref="unit-demo-a"
        )


def test_database_reader_rejects_malformed_horizon_and_unbound_business_unit() -> None:
    class BadHorizon(_Session):
        def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
            if "current_audit_horizon" in str(statement):
                return _Result([{"sequence": 0, "hash": b"h" * 31}])
            return super().execute(statement, params)

    with pytest.raises(InternalReadBackendUnavailable, match="audit horizon"):
        _service(BadHorizon({})).list_candidates(_principal())

    unbound = _principal().model_copy(
        update={
            "grants": (
                EntityGrant(
                    entity_ref=ENTITY,
                    business_unit_refs=frozenset({"unit-demo-a", "unit-demo-b"}),
                    business_unit_ids=frozenset(
                        {BUSINESS_UNIT, UUID("11000000-0000-4000-8000-000000000002")}
                    ),
                    business_unit_bindings=(
                        ("unit-demo-a", BUSINESS_UNIT),
                        ("unit-demo-b", UUID("11000000-0000-4000-8000-000000000002")),
                    ),
                ),
            )
        }
    )
    with pytest.raises(ResourceNotVisible, match="resource was not found"):
        _service(_Session({})).get_reconciliation(
            unbound, month="2026-08", entity_ref=ENTITY, business_unit_ref="unit-demo-a"
        )


def test_database_reader_rejects_multiple_scopes_and_missing_cursor_signer() -> None:
    multi = _principal().model_copy(
        update={
            "grants": (
                EntityGrant(
                    entity_ref=ENTITY,
                    business_unit_refs=frozenset({"unit-demo-a", "unit-demo-b"}),
                    business_unit_ids=frozenset(
                        {BUSINESS_UNIT, UUID("11000000-0000-4000-8000-000000000002")}
                    ),
                    business_unit_bindings=(
                        ("unit-demo-a", BUSINESS_UNIT),
                        ("unit-demo-b", UUID("11000000-0000-4000-8000-000000000002")),
                    ),
                ),
            )
        }
    )
    with pytest.raises(InternalReadBackendUnavailable, match="one bound"):
        _service(_Session({})).list_candidates(multi)

    unassigned = _principal().model_copy(
        update={
            "grants": (
                _principal().grants[0].model_copy(update={"allow_unassigned_candidates": True}),
            )
        }
    )
    with pytest.raises(InternalReadBackendUnavailable, match="multiple scopes"):
        _service(_Session({})).list_candidates(unassigned)

    with pytest.raises(InternalReadBackendUnavailable, match="signed cursor"):
        _service(_Session({})).list_candidates(_principal(), cursor="invalid")


def test_database_reader_verifies_cursor_and_row_scope_before_returning() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]
    row = candidate.model_dump()
    row["entity_ref"] = ENTITY
    row["business_unit_ref"] = "unit-demo-a"
    session = _Session(row)
    signer = ReadCursorSigner("k" * 32)
    principal = _principal()
    token = signer.issue(
        principal,
        month=None,
        status=None,
        business_unit=None,
        horizon_sequence=7,
        horizon_hash=b"h" * 32,
        last_created_at=datetime(2026, 8, 23, tzinfo=UTC),
        last_candidate_id=UUID("30000000-0000-4000-8000-000000000001"),
    )
    page = DatabaseInternalReadService(lambda: cast(Session, session), signer).list_candidates(
        principal, cursor=token
    )
    assert len(page.items) == 1

    row["business_unit_ref"] = "unit-demo-b"
    with pytest.raises(InternalReadBackendUnavailable, match="scope binding"):
        DatabaseInternalReadService(lambda: cast(Session, _Session(row)), signer).list_candidates(
            principal
        )


def test_database_reader_scans_past_nonmatching_month_rows() -> None:
    template = SyntheticInternalReadService()._fixture.candidates[1].model_dump()
    rows = [dict(template) for _ in range(101)]
    for index, row in enumerate(rows):
        row["candidate_ref"] = UUID(f"30000000-0000-4000-8000-{index + 200:012d}")

    class PagedSession(_Session):
        calls = 0

        def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
            if "list_candidates_as_of" in str(statement):
                self.calls += 1
                return _Result(rows if self.calls == 1 else [])
            return super().execute(statement, params)

    session = PagedSession(rows[0])
    page = _service(session).list_candidates(_principal(), month="2026-09")
    assert page.items == ()
    assert session.calls == 2


def test_database_candidate_detail_follows_issued_cursors() -> None:
    candidate = SyntheticInternalReadService()._fixture.candidates[1]

    class PagedService(DatabaseInternalReadService):
        calls = 0

        def list_candidates(self, principal: WorkloadPrincipal, **kwargs: Any) -> CandidatePage:
            self.calls += 1
            if self.calls == 1:
                return CandidatePage(items=(), next_cursor="next")
            return CandidatePage(items=(candidate,))

    service = PagedService(lambda: cast(Session, _Session({})), ReadCursorSigner("k" * 32))
    assert service.get_candidate(_principal(), candidate.candidate_ref) == candidate
