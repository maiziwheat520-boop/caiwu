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

BUSINESS_UNIT_ID = UUID("71000000-0000-4000-8000-000000000001")


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
        if "get_candidate_as_of" in sql:
            assert params is not None
            matches = [
                row
                for row in [self.candidate_row]
                if row.get("candidate_ref") == params["candidate_ref"]
            ]
            return _Result(matches[:1])
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


def test_database_review_service_merges_scopes_and_maps_historical_channel_ids() -> None:
    candidate, principal, _, receipt = _fixtures()
    second_unit = UUID("71000000-0000-4000-8000-000000000002")
    principal = principal.model_copy(
        update={
            "grants": (
                principal.grants[0].model_copy(
                    update={
                        "business_unit_refs": frozenset(
                            {candidate.business_unit_ref, "unit-second"}
                        ),
                        "business_unit_ids": frozenset({BUSINESS_UNIT_ID, second_unit}),
                        "business_unit_bindings": (
                            (cast(str, candidate.business_unit_ref), BUSINESS_UNIT_ID),
                            ("unit-second", second_unit),
                        ),
                    }
                ),
            )
        }
    )
    raw_event = receipt.events[0].model_dump(mode="json")
    raw_event["prior_projection"]["source"]["ingest_channel"] = "controlled_upload"
    raw_event["result_projection"]["source"]["ingest_channel"] = "controlled_upload"
    read = _Session(candidate.model_dump(), events=[raw_event])
    service = DatabaseInternalReviewService(_factory(read), _factory(_Session({})))

    page = service.list_candidate_events(principal)

    assert len(page.items) == 1
    assert page.items[0].prior_projection.source.ingest_channel == IngestChannel.CONTROLLED_UPLOAD
    assert sum("list_candidate_events_as_of" in sql for sql, _ in read.calls) == 2


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
