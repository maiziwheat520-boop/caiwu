from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ledgerbridge.candidate_contract import (
    JSON_SAFE_INTEGER,
    CandidateAction,
    CandidateAggregate,
    CandidateCommand,
    CandidateEvent,
    CandidateIdempotencyConflict,
    CandidatePatch,
    CandidateProjection,
    CandidateRevisionConflict,
    CandidateStatus,
    CandidateTransitionRejected,
    EvidenceKind,
    EvidenceReference,
    EvidenceUnlockStatus,
    apply_candidate_command,
    create_candidate_aggregate,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURE_DIR / "r0_contract_fixture.json"
NOW = datetime(2026, 8, 24, 2, 0, tzinfo=UTC)


def _fixture() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))


def _candidate(status: CandidateStatus) -> CandidateProjection:
    payload = _fixture()
    rows = payload["candidates"]
    assert isinstance(rows, list)
    return next(
        CandidateProjection.model_validate(row) for row in rows if row["status"] == status.value
    )


def _command(
    action: CandidateAction,
    revision: int,
    *,
    patch: CandidatePatch | None = None,
    resolutions: dict[UUID, str] | None = None,
    operation_id: UUID | None = None,
    at: datetime = NOW,
    derived_candidate_ref: UUID | None = None,
    derived_short_id: str | None = None,
) -> CandidateCommand:
    return CandidateCommand(
        operation_id=operation_id or uuid4(),
        action=action,
        expected_revision=revision,
        reason="synthetic operator decision",
        patch=patch,
        conflict_resolutions=resolutions or {},
        derived_candidate_ref=derived_candidate_ref,
        derived_short_id=derived_short_id,
        decided_at=at,
    )


def _confirmed_aggregate() -> CandidateAggregate:
    pending = _candidate(CandidateStatus.PENDING)
    return apply_candidate_command(
        CandidateAggregate(projection=pending),
        _command(CandidateAction.CONFIRM, pending.revision),
        actor_ref="human:test-reviewer",
    ).aggregate


def test_r0_fixture_is_synthetic_allowlisted_and_digest_consistent() -> None:
    payload = _fixture()
    assert payload["provenance"] == {
        "kind": "synthetic",
        "contract_version": "ledgerbridge.candidate.v1",
        "state_graph_version": "ledgerbridge.candidate-state.v1",
        "contains_real_data": False,
    }
    candidates = [CandidateProjection.model_validate(row) for row in payload["candidates"]]
    assert {candidate.status for candidate in candidates} == set(CandidateStatus)
    assert {candidate.source.source_system for candidate in candidates} >= {
        "telegram",
        "dingtalk",
        "weixin",
        "outlook_mail",
        "synthetic_bank",
    }

    wire = json.dumps(payload, ensure_ascii=False).lower()
    for forbidden in (
        "raw_fields",
        "storage_key",
        "connector_instance_id",
        "native_message_id",
        "oauth_token",
        "private_key",
        "refresh_token",
    ):
        assert forbidden not in wire

    evidence = payload["evidence_objects"]
    assert isinstance(evidence, list)
    for item in evidence:
        body = (FIXTURE_DIR / item["path"]).read_bytes()
        assert len(body) == item["byte_size"]
        assert hashlib.sha256(body).hexdigest() == item["sha256"]
    active = next(item for item in evidence if item["declared_media_type"] == "image/svg+xml")
    assert active["served_media_type"] == "application/octet-stream"


def test_evidence_reference_projects_the_closed_unlock_state_contract() -> None:
    ordinary = EvidenceReference(
        evidence_ref=UUID("20000000-0000-4000-8000-000000000001"),
        kind=EvidenceKind.ATTACHMENT,
        media_type="application/pdf",
        display_name="statement.pdf",
        download_available=True,
    )
    assert ordinary.unlock_status == EvidenceUnlockStatus.NOT_REQUIRED
    assert ordinary.source_ref is None

    source_ref = UUID("21000000-0000-4000-8000-000000000001")
    required = ordinary.model_copy(
        update={
            "unlock_status": EvidenceUnlockStatus.PASSWORD_REQUIRED,
            "source_ref": source_ref,
        }
    )
    assert EvidenceReference.model_validate(required).source_ref == source_ref

    with pytest.raises(ValidationError, match="PASSWORD_REQUIRED evidence requires source_ref"):
        EvidenceReference.model_validate(
            ordinary.model_dump() | {"unlock_status": EvidenceUnlockStatus.PASSWORD_REQUIRED}
        )

    with pytest.raises(ValidationError, match="NOT_REQUIRED evidence cannot expose source_ref"):
        EvidenceReference.model_validate(ordinary.model_dump() | {"source_ref": source_ref})


def test_complete_fields_goes_to_pending_and_is_append_only_and_idempotent() -> None:
    initial = _candidate(CandidateStatus.INCOMPLETE)
    aggregate = CandidateAggregate(projection=initial)
    operation_id = uuid4()
    command = _command(
        CandidateAction.COMPLETE_FIELDS,
        initial.revision,
        operation_id=operation_id,
        patch=CandidatePatch(
            business_unit_ref="unit-demo-a",
            business_unit_label="Demo unit A",
        ),
    )

    outcome = apply_candidate_command(aggregate, command, actor_ref="human:test-reviewer")
    assert outcome.aggregate.projection.status == CandidateStatus.PENDING
    assert outcome.aggregate.projection.revision == initial.revision + 1
    assert initial.status == CandidateStatus.INCOMPLETE
    assert len(outcome.aggregate.events) == 1
    assert outcome.aggregate.events[0].from_status == CandidateStatus.INCOMPLETE
    changes = {change.field: change for change in outcome.aggregate.events[0].changes}
    assert changes["business_unit_ref"].previous_value is None
    assert changes["business_unit_ref"].new_value == "unit-demo-a"
    assert changes["status"].new_value == CandidateStatus.PENDING

    replay = apply_candidate_command(
        outcome.aggregate,
        command,
        actor_ref="human:test-reviewer",
    )
    assert replay.replayed is True
    assert replay.aggregate == outcome.aggregate

    conflicting = command.model_copy(update={"reason": "different replay content"})
    with pytest.raises(CandidateIdempotencyConflict):
        apply_candidate_command(outcome.aggregate, conflicting, actor_ref="human:test-reviewer")
    with pytest.raises(CandidateIdempotencyConflict, match="actor"):
        apply_candidate_command(outcome.aggregate, command, actor_ref="human:other-reviewer")


def test_pending_candidate_can_correct_posting_fields_and_confirm_append_only() -> None:
    initial = _candidate(CandidateStatus.PENDING)
    outcome = apply_candidate_command(
        CandidateAggregate(projection=initial),
        _command(
            CandidateAction.CORRECT_AND_CONFIRM,
            initial.revision,
            patch=CandidatePatch(
                business_unit_ref="unit-reviewed",
                business_unit_label="Reviewed unit",
                category_code="TRAVEL",
                category_label="Reviewed travel",
                amount_minor=-1_999,
                accounting_month="2026-09",
            ),
        ),
        actor_ref="human:test-reviewer",
    )

    corrected = outcome.aggregate.projection
    assert corrected.status == CandidateStatus.CONFIRMED
    assert corrected.revision == initial.revision + 1
    assert (
        corrected.business_unit_ref,
        corrected.category_code,
        corrected.amount_minor,
        corrected.accounting_month,
    ) == ("unit-reviewed", "TRAVEL", -1_999, "2026-09")
    assert len(outcome.aggregate.events) == 1
    event = outcome.aggregate.events[0]
    assert event.action == CandidateAction.CORRECT_AND_CONFIRM
    assert event.prior_projection == initial
    assert event.result_projection == corrected
    assert event.prior_projection.source == corrected.source
    assert event.prior_projection.evidence == corrected.evidence
    changes = {change.field for change in event.changes}
    assert changes == {
        "accounting_month",
        "amount_minor",
        "business_unit_label",
        "business_unit_ref",
        "category_code",
        "category_label",
        "status",
    }


def test_worker_creation_accepts_only_clean_open_initial_projections() -> None:
    for status in (
        CandidateStatus.INCOMPLETE,
        CandidateStatus.CONFLICTED,
        CandidateStatus.PENDING,
    ):
        candidate = _candidate(status)
        assert create_candidate_aggregate(candidate).projection == candidate

    for status in (CandidateStatus.CONFIRMED, CandidateStatus.IGNORED, CandidateStatus.SUPERSEDED):
        with pytest.raises(CandidateTransitionRejected, match="open state"):
            create_candidate_aggregate(_candidate(status))

    pending = _candidate(CandidateStatus.PENDING)
    invalid_revision = pending.model_copy(
        update={
            "revision": 2,
            "review_summary": pending.review_summary.model_copy(update={"current_revision": 2}),
        }
    )
    with pytest.raises(CandidateTransitionRejected, match="initial projection"):
        create_candidate_aggregate(invalid_revision)


def test_complete_fields_cannot_overwrite_or_leave_required_fields_missing() -> None:
    initial = _candidate(CandidateStatus.INCOMPLETE)
    with pytest.raises(CandidateTransitionRejected, match="overwrite"):
        apply_candidate_command(
            CandidateAggregate(projection=initial),
            _command(
                CandidateAction.COMPLETE_FIELDS,
                initial.revision,
                patch=CandidatePatch(amount_minor=1),
            ),
            actor_ref="human:test-reviewer",
        )

    values = initial.model_dump()
    values.update(
        business_unit_ref=None,
        business_unit_label=None,
        category_code=None,
        category_label=None,
        blockers=[
            {
                "code": "MISSING_BUSINESS_UNIT",
                "message": "missing unit",
                "field": "business_unit",
            },
            {"code": "MISSING_CATEGORY", "message": "missing category", "field": "category"},
        ],
    )
    two_missing = CandidateProjection.model_validate(values)
    with pytest.raises(CandidateTransitionRejected, match="all missing"):
        apply_candidate_command(
            CandidateAggregate(projection=two_missing),
            _command(
                CandidateAction.COMPLETE_FIELDS,
                two_missing.revision,
                patch=CandidatePatch(
                    business_unit_ref="unit-demo-a",
                    business_unit_label="Demo unit A",
                ),
            ),
            actor_ref="human:test-reviewer",
        )


def test_resolve_conflict_goes_to_pending_only_when_every_conflict_is_resolved() -> None:
    initial = _candidate(CandidateStatus.CONFLICTED)
    conflict_ref = initial.blockers[0].conflict_ref
    assert conflict_ref is not None
    command = _command(
        CandidateAction.RESOLVE_CONFLICT,
        initial.revision,
        patch=CandidatePatch(amount_minor=-12346),
        resolutions={conflict_ref: "keep the first synthetic business key"},
    )
    outcome = apply_candidate_command(
        CandidateAggregate(projection=initial), command, actor_ref="human:test-reviewer"
    )
    assert outcome.aggregate.projection.status == CandidateStatus.PENDING
    assert outcome.aggregate.projection.blockers == ()
    assert outcome.aggregate.events[0].resolved_conflicts[0].conflict_ref == conflict_ref
    amount_change = next(
        change for change in outcome.aggregate.events[0].changes if change.field == "amount_minor"
    )
    assert amount_change.previous_value == -12345
    assert amount_change.new_value == -12346

    wrong_ref = uuid4()
    with pytest.raises(CandidateTransitionRejected, match="every conflict"):
        apply_candidate_command(
            CandidateAggregate(projection=initial),
            _command(
                CandidateAction.RESOLVE_CONFLICT,
                initial.revision,
                resolutions={wrong_ref: "wrong conflict"},
            ),
            actor_ref="human:test-reviewer",
        )

    invalid = initial.model_dump()
    invalid["blockers"] = [
        *invalid["blockers"],
        {"code": "PARSE_FAILED", "message": "synthetic parser failure"},
    ]
    with pytest.raises(ValidationError, match="only opaque conflict blockers"):
        CandidateProjection.model_validate(invalid)


def test_processing_and_ambiguous_blockers_do_not_require_fake_missing_fields() -> None:
    pending = _candidate(CandidateStatus.PENDING)
    processing_values = pending.model_dump()
    processing_values.update(
        status=CandidateStatus.INCOMPLETE,
        amount_minor=None,
        blockers=[
            {
                "code": "MISSING_AMOUNT",
                "message": "synthetic amount is missing",
                "field": "amount_minor",
            },
            {"code": "PARSE_FAILED", "message": "synthetic parser failure"},
        ],
    )
    processing = CandidateProjection.model_validate(processing_values)
    assert processing.amount_minor is None
    completed = apply_candidate_command(
        CandidateAggregate(projection=processing),
        _command(
            CandidateAction.COMPLETE_FIELDS,
            processing.revision,
            patch=CandidatePatch(amount_minor=1),
        ),
        actor_ref="human:test-reviewer",
    )
    assert completed.aggregate.projection.status == CandidateStatus.PENDING

    ambiguous_values = pending.model_dump()
    ambiguous_values.update(
        status=CandidateStatus.CONFLICTED,
        blockers=[
            {
                "code": "AMBIGUOUS_EXTRACTION",
                "message": "synthetic extraction ambiguity",
                "conflict_ref": str(uuid4()),
            }
        ],
    )
    ambiguous = CandidateProjection.model_validate(ambiguous_values)
    assert ambiguous.status == CandidateStatus.CONFLICTED


def test_pending_confirms_open_states_ignore_and_terminal_states_reject() -> None:
    pending = _candidate(CandidateStatus.PENDING)
    confirmed_aggregate = apply_candidate_command(
        CandidateAggregate(projection=pending),
        _command(CandidateAction.CONFIRM, pending.revision),
        actor_ref="human:test-reviewer",
    ).aggregate
    assert confirmed_aggregate.projection.status == CandidateStatus.CONFIRMED

    ignored_aggregates: list[CandidateAggregate] = []
    for status in (
        CandidateStatus.INCOMPLETE,
        CandidateStatus.CONFLICTED,
        CandidateStatus.PENDING,
    ):
        candidate = _candidate(status)
        ignored = apply_candidate_command(
            CandidateAggregate(projection=candidate),
            _command(CandidateAction.IGNORE, candidate.revision),
            actor_ref="human:test-reviewer",
        ).aggregate
        assert ignored.projection.status == CandidateStatus.IGNORED
        ignored_aggregates.append(ignored)

    superseded_aggregate = apply_candidate_command(
        confirmed_aggregate,
        _command(
            CandidateAction.SUPERSEDE,
            confirmed_aggregate.projection.revision,
            patch=CandidatePatch(amount_minor=501),
            derived_candidate_ref=uuid4(),
            derived_short_id="C-TERMINAL",
            at=NOW + timedelta(seconds=1),
        ),
        actor_ref="human:test-supervisor",
    ).aggregate
    terminal_aggregates = [confirmed_aggregate, superseded_aggregate, *ignored_aggregates]
    for aggregate in terminal_aggregates:
        for command in (
            _command(CandidateAction.CONFIRM, aggregate.projection.revision),
            _command(
                CandidateAction.CORRECT_AND_CONFIRM,
                aggregate.projection.revision,
                patch=CandidatePatch(amount_minor=777),
            ),
        ):
            with pytest.raises(CandidateTransitionRejected):
                apply_candidate_command(
                    aggregate,
                    command,
                    actor_ref="human:test-reviewer",
                )


def test_confirmed_can_only_be_superseded_into_a_linked_pending_candidate() -> None:
    initial_aggregate = _confirmed_aggregate()
    initial = initial_aggregate.projection
    derived_ref = uuid4()
    command = _command(
        CandidateAction.SUPERSEDE,
        initial.revision,
        patch=CandidatePatch(amount_minor=50001),
        derived_candidate_ref=derived_ref,
        derived_short_id="C-NEW001",
    )
    outcome = apply_candidate_command(initial_aggregate, command, actor_ref="human:test-supervisor")
    assert outcome.aggregate.projection.status == CandidateStatus.SUPERSEDED
    assert outcome.aggregate.projection.superseded_by_candidate_ref == derived_ref
    assert outcome.derived_candidate is not None
    assert outcome.derived_candidate.status == CandidateStatus.PENDING
    assert outcome.derived_candidate.revision == 1
    assert outcome.derived_candidate.supersedes_candidate_ref == initial.candidate_ref
    assert initial.amount_minor == 0
    assert outcome.derived_candidate.amount_minor == 50001

    with pytest.raises(CandidateTransitionRejected):
        apply_candidate_command(
            initial_aggregate,
            _command(CandidateAction.IGNORE, initial.revision),
            actor_ref="human:test-reviewer",
        )

    for invalid_command in (
        _command(
            CandidateAction.SUPERSEDE,
            initial.revision,
            patch=CandidatePatch(amount_minor=initial.amount_minor),
            derived_candidate_ref=uuid4(),
            derived_short_id="C-NOCHANGE",
        ),
        _command(
            CandidateAction.SUPERSEDE,
            initial.revision,
            patch=CandidatePatch(amount_minor=50001),
            derived_candidate_ref=initial.candidate_ref,
            derived_short_id="C-SELFREF",
        ),
        _command(
            CandidateAction.SUPERSEDE,
            initial.revision,
            patch=CandidatePatch(amount_minor=50001),
            derived_candidate_ref=uuid4(),
            derived_short_id=initial.short_id,
        ),
    ):
        with pytest.raises(CandidateTransitionRejected):
            apply_candidate_command(
                initial_aggregate,
                invalid_command,
                actor_ref="human:test-supervisor",
            )

    with pytest.raises(CandidateTransitionRejected):
        apply_candidate_command(
            outcome.aggregate,
            _command(CandidateAction.CONFIRM, outcome.aggregate.projection.revision),
            actor_ref="human:test-reviewer",
        )


def test_stale_revision_and_money_or_shape_ambiguity_fail_closed() -> None:
    pending = _candidate(CandidateStatus.PENDING)
    with pytest.raises(CandidateRevisionConflict):
        apply_candidate_command(
            CandidateAggregate(projection=pending),
            _command(CandidateAction.CONFIRM, pending.revision + 1),
            actor_ref="human:test-reviewer",
        )

    values = pending.model_dump()
    for invalid in (True, 1.25, JSON_SAFE_INTEGER + 1):
        values["amount_minor"] = invalid
        with pytest.raises(ValidationError):
            CandidateProjection.model_validate(values)

    values = pending.model_dump()
    values["status"] = CandidateStatus.INCOMPLETE
    with pytest.raises(ValidationError, match="INCOMPLETE"):
        CandidateProjection.model_validate(values)

    with pytest.raises(ValidationError, match="timezone-aware"):
        _command(CandidateAction.CONFIRM, pending.revision, at=NOW.replace(tzinfo=None))

    inconsistent_revision = pending.model_dump()
    inconsistent_revision["revision"] = 99
    inconsistent_revision["review_summary"]["current_revision"] = 99
    with pytest.raises(ValidationError, match="event count plus one"):
        CandidateProjection.model_validate(inconsistent_revision)

    initial_terminal = pending.model_dump()
    initial_terminal["status"] = CandidateStatus.CONFIRMED
    with pytest.raises(ValidationError, match="terminal candidate status"):
        CandidateProjection.model_validate(initial_terminal)

    wrong_terminal_action = _candidate(CandidateStatus.CONFIRMED).model_dump()
    wrong_terminal_action["review_summary"]["last_action"] = CandidateAction.IGNORE
    with pytest.raises(ValidationError, match="terminal candidate status"):
        CandidateProjection.model_validate(wrong_terminal_action)

    with pytest.raises(CandidateTransitionRejected, match="time cannot move backward"):
        apply_candidate_command(
            CandidateAggregate(projection=pending),
            _command(
                CandidateAction.CONFIRM,
                pending.revision,
                at=pending.updated_at - timedelta(seconds=1),
            ),
            actor_ref="human:test-reviewer",
        )


def test_each_successful_action_advances_exactly_one_revision() -> None:
    pending = _candidate(CandidateStatus.PENDING)
    confirm_command = _command(CandidateAction.CONFIRM, pending.revision)
    first = apply_candidate_command(
        CandidateAggregate(projection=pending),
        confirm_command,
        actor_ref="human:test-reviewer",
    )
    derived_ref = uuid4()
    second = apply_candidate_command(
        first.aggregate,
        _command(
            CandidateAction.SUPERSEDE,
            first.aggregate.projection.revision,
            patch=CandidatePatch(category_code="OTHER", category_label="Synthetic other"),
            derived_candidate_ref=derived_ref,
            derived_short_id="C-R0NEXT",
            at=NOW + timedelta(seconds=1),
        ),
        actor_ref="human:test-supervisor",
    )
    assert [event.to_revision - event.from_revision for event in second.aggregate.events] == [1, 1]

    historical_replay = apply_candidate_command(
        second.aggregate,
        confirm_command,
        actor_ref="human:test-reviewer",
    )
    assert historical_replay.replayed is True
    assert historical_replay.aggregate == first.aggregate
    assert historical_replay.aggregate.projection.status == CandidateStatus.CONFIRMED
    assert len(historical_replay.aggregate.events) == 1


def test_aggregate_rejects_partial_duplicate_or_out_of_order_history() -> None:
    with pytest.raises(ValidationError, match="complete event history"):
        CandidateAggregate(projection=_candidate(CandidateStatus.CONFIRMED))

    aggregate = _confirmed_aggregate()
    event = aggregate.events[0]
    with pytest.raises(ValidationError, match="revision 1"):
        CandidateAggregate(projection=aggregate.projection, events=())
    with pytest.raises(ValidationError, match="unique contiguous"):
        CandidateAggregate(
            projection=aggregate.projection.model_copy(
                update={
                    "revision": 3,
                    "review_summary": aggregate.projection.review_summary.model_copy(
                        update={"event_count": 2, "current_revision": 3}
                    ),
                }
            ),
            events=(event, event),
        )

    illegal = event.model_dump()
    illegal.update(
        action=CandidateAction.CONFIRM,
        from_status=CandidateStatus.INCOMPLETE,
        to_status=CandidateStatus.CONFIRMED,
    )
    with pytest.raises(ValidationError, match="state edge"):
        CandidateEvent.model_validate(illegal)

    wrong_status_audit = event.model_dump()
    status_change = next(
        change for change in wrong_status_audit["changes"] if change["field"] == "status"
    )
    status_change["new_value"] = CandidateStatus.IGNORED
    with pytest.raises(ValidationError, match="status audit change"):
        CandidateEvent.model_validate(wrong_status_audit)

    naive_time = event.model_dump()
    naive_time["created_at"] = NOW.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone-aware"):
        CandidateEvent.model_validate(naive_time)

    superseded = apply_candidate_command(
        aggregate,
        _command(
            CandidateAction.SUPERSEDE,
            aggregate.projection.revision,
            patch=CandidatePatch(amount_minor=1),
            derived_candidate_ref=uuid4(),
            derived_short_id="C-HISTORY",
            at=NOW + timedelta(seconds=1),
        ),
        actor_ref="human:test-supervisor",
    ).aggregate
    forged_derived = superseded.derived_candidates[0].model_copy(
        update={"status": CandidateStatus.CONFIRMED}
    )
    with pytest.raises(ValidationError, match=r"terminal candidate|derived candidates"):
        CandidateAggregate(
            projection=superseded.projection,
            events=superseded.events,
            derived_candidates=(forged_derived,),
        )

    completion_source = _candidate(CandidateStatus.INCOMPLETE)
    completed = apply_candidate_command(
        CandidateAggregate(projection=completion_source),
        _command(
            CandidateAction.COMPLETE_FIELDS,
            completion_source.revision,
            patch=CandidatePatch(
                business_unit_ref="unit-demo-a",
                business_unit_label="Demo unit A",
            ),
        ),
        actor_ref="human:test-reviewer",
    ).aggregate
    tampered_event = completed.events[0].model_dump()
    unit_change = next(
        change for change in tampered_event["changes"] if change["field"] == "business_unit_ref"
    )
    unit_change["new_value"] = "unit-tampered"
    with pytest.raises(
        ValidationError,
        match=r"projection must match|event receipt must match",
    ):
        CandidateAggregate(
            projection=completed.projection,
            events=(CandidateEvent.model_validate(tampered_event),),
        )
