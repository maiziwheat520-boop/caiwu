from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ledgerbridge.candidate_contract import (
    CandidateProjection,
    CandidateRevisionConflict,
    CandidateStatus,
    IngestChannel,
)
from ledgerbridge.internal_candidate_command import (
    CandidateClassificationBatchReceipt,
    CandidateClassificationBatchRequest,
    CandidateCommandIdempotencyConflict,
    CandidateDecision,
    CandidateDecisionReceipt,
    CandidateDecisionRequest,
    ClassificationBatchMember,
    ClassificationBatchMemberResult,
    ClassificationTarget,
    DatabaseInternalReviewService,
    SyntheticInternalReviewService,
)
from ledgerbridge.internal_read_contract import (
    Capability,
    EntityGrant,
    ResourceNotVisible,
    WorkloadPrincipal,
)
from ledgerbridge.internal_read_service import InternalReadBackendUnavailable

BUSINESS_UNIT_ID = UUID("71000000-0000-4000-8000-000000000001")
SECOND_BUSINESS_UNIT_ID = UUID("71000000-0000-4000-8000-000000000002")


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class _Session:
    def __init__(
        self,
        candidate_row: dict[str, Any],
        *,
        receipt: dict[str, Any] | None = None,
        events: list[dict[str, Any]] | None = None,
        failure: SQLAlchemyError | None = None,
    ) -> None:
        self.candidate_row = candidate_row
        self.receipt = receipt
        self.events = events or []
        self.failure = failure
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.committed = False

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(statement)
        values = params or {}
        self.calls.append((sql, values))
        if self.failure is not None:
            raise self.failure
        if "current_audit_horizon" in sql:
            return _Result([{"sequence": 11, "hash": b"h" * 32}])
        if "list_candidates_as_of" in sql:
            return _Result([self.candidate_row])
        if "list_candidate_evidence_satisfactions" in sql:
            return _Result([])
        if "list_candidate_counterparty_facts" in sql:
            return _Result([])
        if "list_candidate_events_as_of" in sql:
            return _Result([{"event": event} for event in self.events])
        if "apply_candidate_decision" in sql:
            return _Result([] if self.receipt is None else [{"receipt": self.receipt}])
        if "replay_candidate_classification_batch" in sql:
            return _Result([] if self.receipt is None else [{"receipt": self.receipt}])
        raise AssertionError(f"unexpected SQL: {sql}")

    def commit(self) -> None:
        self.committed = True


class _SqlStateError(SQLAlchemyError):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.orig = type("Orig", (), {"sqlstate": sqlstate})()


def _fixtures() -> tuple[
    CandidateProjection,
    WorkloadPrincipal,
    CandidateDecisionRequest,
    CandidateDecisionReceipt,
]:
    synthetic = SyntheticInternalReviewService()
    candidate = next(
        item for item in synthetic._fixture.candidates if item.status == CandidateStatus.PENDING
    )
    principal = WorkloadPrincipal(
        principal_ref="workload:web-review",
        san_uri="spiffe://ledgerbridge.local/web-review",
        policy_generation=1,
        capabilities=frozenset({Capability.CANDIDATE_READ, Capability.CANDIDATE_DECIDE}),
        grants=(
            EntityGrant(
                entity_ref=candidate.entity_ref,
                business_unit_refs=frozenset({cast(str, candidate.business_unit_ref)}),
                business_unit_ids=frozenset({BUSINESS_UNIT_ID}),
                business_unit_bindings=(
                    (cast(str, candidate.business_unit_ref), BUSINESS_UNIT_ID),
                ),
            ),
        ),
    )
    request = CandidateDecisionRequest(
        decision=CandidateDecision.CONFIRM,
        expected_revision=candidate.revision,
        reason="reviewed against encrypted evidence",
    )
    receipt = synthetic.append_decision(
        principal,
        candidate_ref=candidate.candidate_ref,
        operation_id=uuid4(),
        assertion_jti=uuid4(),
        actor_ref="human:web-reviewer",
        request=request,
        decided_at=datetime.now(UTC),
    )
    return candidate, principal, request, receipt


def _factory(session: _Session):  # type: ignore[no-untyped-def]
    return lambda: cast(Session, session)


def _batch_fixtures() -> tuple[
    WorkloadPrincipal,
    CandidateClassificationBatchRequest,
    CandidateClassificationBatchReceipt,
]:
    candidate, principal, _, decision_receipt = _fixtures()
    second_ref = uuid4()
    request = CandidateClassificationBatchRequest(
        source_candidate_ref=candidate.candidate_ref,
        accounting_month=cast(str, candidate.accounting_month),
        target=ClassificationTarget(
            business_unit_ref=cast(str, candidate.business_unit_ref),
            category_code=cast(str, candidate.category_code),
        ),
        members=(
            ClassificationBatchMember(
                candidate_ref=candidate.candidate_ref,
                expected_revision=candidate.revision,
            ),
            ClassificationBatchMember(candidate_ref=second_ref, expected_revision=1),
        ),
        reason="reviewed exact classification group",
        acknowledged_risk_codes=(),
    )
    operation_id = uuid4()
    results = tuple(
        ClassificationBatchMemberResult(
            candidate_ref=candidate_ref,
            operation_id=uuid4(),
            status="REPLAYED",
            candidate=decision_receipt.candidate,
            events=decision_receipt.events,
        )
        for candidate_ref in (candidate.candidate_ref, second_ref)
    )
    receipt = CandidateClassificationBatchReceipt(
        operation_id=operation_id,
        replayed=True,
        group_ref="cg_0123456789abcdef0123456789abcdef",
        accounting_month=cast(str, candidate.accounting_month),
        source_candidate_ref=candidate.candidate_ref,
        target=request.target,
        acknowledged_risk_codes=request.acknowledged_risk_codes,
        results=results,
    )
    return principal, request, receipt


def test_database_batch_replay_is_resolved_before_current_candidate_reads() -> None:
    principal, request, receipt = _batch_fixtures()
    read = _Session({}, failure=SQLAlchemyError("current Candidate reads are stale"))
    command = _Session({}, receipt=receipt.model_dump(mode="json"))
    service = DatabaseInternalReviewService(_factory(read), _factory(command))

    result = service.apply_classification_batch(
        principal,
        group_ref=receipt.group_ref,
        operation_id=receipt.operation_id,
        assertion_jti=uuid4(),
        actor_ref="human:web-reviewer",
        request=request,
        decided_at=datetime.now(UTC),
    )

    assert result == receipt
    assert read.calls == []
    assert command.committed is True
    assert "internal_command.replay_candidate_classification_batch" in command.calls[0][0]
    replay_payload = json.loads(command.calls[0][1]["request"])
    assert replay_payload["authorized_entity_ids"] == [str(principal.grants[0].entity_ref)]
    assert replay_payload["authorized_business_unit_ids"] == [str(BUSINESS_UNIT_ID)]
    assert replay_payload["authorized_unassigned_entity_ids"] == []


def test_database_batch_replay_conflict_fails_closed_before_candidate_reads() -> None:
    principal, request, receipt = _batch_fixtures()
    read = _Session({}, failure=SQLAlchemyError("must not disclose Candidate scope"))
    command = _Session({}, failure=_SqlStateError("LB001"))
    service = DatabaseInternalReviewService(_factory(read), _factory(command))

    with pytest.raises(CandidateCommandIdempotencyConflict, match="idempotency conflict"):
        service.apply_classification_batch(
            principal,
            group_ref=receipt.group_ref,
            operation_id=receipt.operation_id,
            assertion_jti=uuid4(),
            actor_ref="human:wrong-actor",
            request=request,
            decided_at=datetime.now(UTC),
        )

    assert read.calls == []
    assert command.committed is False


def test_database_review_service_uses_only_scoped_functions_and_commits() -> None:
    candidate, principal, request, receipt = _fixtures()
    read = _Session(candidate.model_dump())
    command = _Session(candidate.model_dump(), receipt=receipt.model_dump(mode="json"))
    service = DatabaseInternalReviewService(_factory(read), _factory(command))

    result = service.append_decision(
        principal,
        candidate_ref=candidate.candidate_ref,
        operation_id=receipt.operation_id,
        assertion_jti=uuid4(),
        actor_ref="human:web-reviewer",
        request=request,
        decided_at=datetime.now(UTC),
    )

    assert result == receipt
    assert command.committed is True
    sql, params = command.calls[-1]
    assert "internal_command.apply_candidate_decision" in sql
    assert "public.candidate" not in sql
    assert params["current_business_unit_id"] == BUSINESS_UNIT_ID
    assert params["target_business_unit_id"] == BUSINESS_UNIT_ID
    assert params["verified_san"] == principal.san_uri


def test_database_review_service_maps_database_ingest_channel_ids_in_receipt() -> None:
    candidate, principal, request, receipt = _fixtures()
    raw_receipt = receipt.model_dump(mode="json")
    raw_receipt["candidate"]["source"]["ingest_channel"] = "controlled_upload"
    event = raw_receipt["events"][0]
    event["prior_projection"]["source"]["ingest_channel"] = "controlled_upload"
    event["result_projection"]["source"]["ingest_channel"] = "controlled_upload"
    command = _Session(candidate.model_dump(), receipt=raw_receipt)
    service = DatabaseInternalReviewService(
        _factory(_Session(candidate.model_dump())),
        _factory(command),
    )

    result = service.append_decision(
        principal,
        candidate_ref=candidate.candidate_ref,
        operation_id=receipt.operation_id,
        assertion_jti=uuid4(),
        actor_ref="human:web-reviewer",
        request=request,
        decided_at=datetime.now(UTC),
    )

    assert result.candidate.source.ingest_channel == IngestChannel.CONTROLLED_UPLOAD
    assert all(
        event.prior_projection.source.ingest_channel == IngestChannel.CONTROLLED_UPLOAD
        and event.result_projection.source.ingest_channel == IngestChannel.CONTROLLED_UPLOAD
        for event in result.events
    )
    assert command.committed is True


def test_database_review_service_reads_events_through_horizon_scoped_function() -> None:
    candidate, principal, _, receipt = _fixtures()
    read = _Session(
        candidate.model_dump(),
        events=[event.model_dump(mode="json") for event in receipt.events],
    )
    service = DatabaseInternalReviewService(_factory(read), _factory(_Session({})))

    page = service.list_candidate_events(principal, candidate_ref=candidate.candidate_ref)

    assert page.items == receipt.events
    event_sql = next(sql for sql, _ in read.calls if "list_candidate_events_as_of" in sql)
    assert "internal_read.list_candidate_events_as_of" in event_sql
    assert "public.candidate_event" not in event_sql


def test_database_review_service_merges_events_from_each_authorized_scope() -> None:
    candidate, _, _, receipt = _fixtures()
    second_ref = uuid4()

    def move_projection(projection: CandidateProjection) -> CandidateProjection:
        return projection.model_copy(
            update={
                "candidate_ref": second_ref,
                "business_unit_ref": "unit-demo-b",
                "evidence": tuple(
                    evidence.model_copy(update={"business_unit_ref": "unit-demo-b"})
                    for evidence in projection.evidence
                ),
            }
        )

    second_event = receipt.events[0].model_copy(
        update={
            "operation_id": uuid4(),
            "candidate_ref": second_ref,
            "prior_projection": move_projection(receipt.events[0].prior_projection),
            "result_projection": move_projection(receipt.events[0].result_projection),
        }
    )
    principal = WorkloadPrincipal(
        principal_ref="workload:web-review",
        san_uri="spiffe://ledgerbridge.local/web-review",
        policy_generation=1,
        capabilities=frozenset({Capability.CANDIDATE_READ}),
        grants=(
            EntityGrant(
                entity_ref=candidate.entity_ref,
                business_unit_refs=frozenset({"unit-demo-a", "unit-demo-b"}),
                business_unit_ids=frozenset({BUSINESS_UNIT_ID, SECOND_BUSINESS_UNIT_ID}),
                business_unit_bindings=(
                    ("unit-demo-a", BUSINESS_UNIT_ID),
                    ("unit-demo-b", SECOND_BUSINESS_UNIT_ID),
                ),
            ),
        ),
    )

    event_calls: list[dict[str, Any]] = []

    class ScopedEventSession(_Session):
        def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
            if "list_candidate_events_as_of" in str(statement):
                assert params is not None
                event_calls.append(params)
                event = {
                    BUSINESS_UNIT_ID: receipt.events[0],
                    SECOND_BUSINESS_UNIT_ID: second_event,
                }[params["business_unit_id"]]
                return _Result([{"event": event.model_dump(mode="json")}])
            return super().execute(statement, params)

    read = ScopedEventSession(candidate.model_dump())
    service = DatabaseInternalReviewService(_factory(read), _factory(_Session({})))

    page = service.list_candidate_events(principal)

    assert {event.operation_id for event in page.items} == {
        receipt.events[0].operation_id,
        second_event.operation_id,
    }
    assert {params["business_unit_id"] for params in event_calls} == {
        BUSINESS_UNIT_ID,
        SECOND_BUSINESS_UNIT_ID,
    }


def test_database_review_service_builds_groups_from_all_authorized_scopes() -> None:
    candidate, _, _, _ = _fixtures()
    candidate = candidate.model_copy(
        update={
            "summary": "支付宝 | 2026-08-01 | 支出 | 消费 | 商户 | 招商银行(1234) | 交易成功",
            "amount_minor": -1_000,
        }
    )
    second_ref = uuid4()
    second = candidate.model_copy(
        update={
            "candidate_ref": second_ref,
            "short_id": "C-MULTI02",
            "business_unit_ref": "unit-demo-b",
            "evidence": tuple(
                evidence.model_copy(update={"business_unit_ref": "unit-demo-b"})
                for evidence in candidate.evidence
            ),
        }
    )
    principal = WorkloadPrincipal(
        principal_ref="workload:web-review",
        san_uri="spiffe://ledgerbridge.local/web-review",
        policy_generation=1,
        capabilities=frozenset({Capability.CANDIDATE_READ}),
        grants=(
            EntityGrant(
                entity_ref=candidate.entity_ref,
                business_unit_refs=frozenset({"unit-demo-a", "unit-demo-b"}),
                business_unit_ids=frozenset({BUSINESS_UNIT_ID, SECOND_BUSINESS_UNIT_ID}),
                business_unit_bindings=(
                    ("unit-demo-a", BUSINESS_UNIT_ID),
                    ("unit-demo-b", SECOND_BUSINESS_UNIT_ID),
                ),
            ),
        ),
    )

    class ScopedCandidateSession(_Session):
        def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
            if "list_candidates_as_of" in str(statement):
                assert params is not None
                projection = {
                    BUSINESS_UNIT_ID: candidate,
                    SECOND_BUSINESS_UNIT_ID: second,
                }[params["business_unit_id"]]
                return _Result([projection.model_dump()])
            return super().execute(statement, params)

    read = ScopedCandidateSession(candidate.model_dump())
    service = DatabaseInternalReviewService(_factory(read), _factory(_Session({})))

    page = service.list_classification_groups(principal)

    assert len(page.items) == 1
    assert {member.candidate_ref for member in page.items[0].members} == {
        candidate.candidate_ref,
        second_ref,
    }


def test_database_classification_write_remains_fail_closed_for_multiple_scopes() -> None:
    candidate, principal, _, _ = _fixtures()
    _, request, _ = _batch_fixtures()
    multi_scope = principal.model_copy(
        update={
            "grants": (
                EntityGrant(
                    entity_ref=candidate.entity_ref,
                    business_unit_refs=frozenset({"unit-demo-a", "unit-demo-b"}),
                    business_unit_ids=frozenset({BUSINESS_UNIT_ID, SECOND_BUSINESS_UNIT_ID}),
                    business_unit_bindings=(
                        ("unit-demo-a", BUSINESS_UNIT_ID),
                        ("unit-demo-b", SECOND_BUSINESS_UNIT_ID),
                    ),
                ),
            )
        }
    )
    service = DatabaseInternalReviewService(
        _factory(_Session(candidate.model_dump())),
        _factory(_Session({})),
    )

    with pytest.raises(InternalReadBackendUnavailable, match="exactly one scope"):
        service.apply_classification_batch(
            multi_scope,
            group_ref="cg_0123456789abcdef0123456789abcdef",
            operation_id=uuid4(),
            assertion_jti=uuid4(),
            actor_ref="human:web-reviewer",
            request=request,
            decided_at=datetime.now(UTC),
        )


def test_database_candidate_decision_remains_fail_closed_for_multiple_scopes() -> None:
    candidate, principal, request, receipt = _fixtures()
    multi_scope = principal.model_copy(
        update={
            "grants": (
                EntityGrant(
                    entity_ref=candidate.entity_ref,
                    business_unit_refs=frozenset({"unit-demo-a", "unit-demo-b"}),
                    business_unit_ids=frozenset({BUSINESS_UNIT_ID, SECOND_BUSINESS_UNIT_ID}),
                    business_unit_bindings=(
                        ("unit-demo-a", BUSINESS_UNIT_ID),
                        ("unit-demo-b", SECOND_BUSINESS_UNIT_ID),
                    ),
                ),
            )
        }
    )
    read = _Session(candidate.model_dump())
    command = _Session(candidate.model_dump(), receipt=receipt.model_dump(mode="json"))
    service = DatabaseInternalReviewService(_factory(read), _factory(command))

    with pytest.raises(InternalReadBackendUnavailable, match="exactly one scope"):
        service.append_decision(
            multi_scope,
            candidate_ref=candidate.candidate_ref,
            operation_id=receipt.operation_id,
            assertion_jti=uuid4(),
            actor_ref="human:web-reviewer",
            request=request,
            decided_at=datetime.now(UTC),
        )

    assert read.calls == []
    assert command.calls == []


def test_database_review_service_maps_stale_revision_without_leaking_database_error() -> None:
    candidate, principal, request, receipt = _fixtures()
    read = _Session(candidate.model_dump())
    command = _Session(candidate.model_dump(), failure=_SqlStateError("LB002"))
    service = DatabaseInternalReviewService(_factory(read), _factory(command))

    with pytest.raises(CandidateRevisionConflict, match="database revision conflict"):
        service.append_decision(
            principal,
            candidate_ref=candidate.candidate_ref,
            operation_id=receipt.operation_id,
            assertion_jti=uuid4(),
            actor_ref="human:web-reviewer",
            request=request,
            decided_at=datetime.now(UTC),
        )

    assert command.committed is False


def test_database_review_service_maps_unknown_or_cross_scope_without_committing() -> None:
    candidate, principal, request, receipt = _fixtures()
    read = _Session(candidate.model_dump())
    command = _Session(candidate.model_dump(), failure=_SqlStateError("LB004"))
    service = DatabaseInternalReviewService(_factory(read), _factory(command))

    with pytest.raises(ResourceNotVisible, match="authorized scope"):
        service.append_decision(
            principal,
            candidate_ref=candidate.candidate_ref,
            operation_id=receipt.operation_id,
            assertion_jti=uuid4(),
            actor_ref="human:web-reviewer",
            request=request,
            decided_at=datetime.now(UTC),
        )

    assert command.committed is False
