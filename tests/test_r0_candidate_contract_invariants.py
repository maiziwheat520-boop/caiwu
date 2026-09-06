"""Every invariant the candidate contract refuses to let a caller break.

The contract is the whole state machine: a projection is only well-formed when
its status, revision, blockers, timestamps and review summary agree with one
another, an event is only well-formed when it is a legal edge whose receipts
match the projections on either side, and an aggregate is only well-formed when
its events form one contiguous, append-only chain ending exactly at the
projection.  Nothing else in the system re-checks these, so each refusal below
is written down as its own fact.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ledgerbridge.candidate_contract import (
    BlockerCode,
    CandidateAction,
    CandidateAggregate,
    CandidateCommand,
    CandidateEvent,
    CandidatePatch,
    CandidateProjection,
    CandidateStatus,
    CandidateTransitionRejected,
    EvidenceKind,
    EvidenceReference,
    EvidenceUnlockStatus,
    ReviewSummary,
    _audit_value,
    apply_candidate_command,
)
from tests.test_r0_candidate_contract import NOW, _candidate, _command, _confirmed_aggregate

DERIVED_REF = UUID("60000000-0000-4000-8000-000000000001")
DERIVED_SHORT_ID = "C-R0D001"


def _dump(model: Any) -> dict[str, Any]:
    return copy.deepcopy(model.model_dump())


def _projection(status: CandidateStatus, **overrides: Any) -> dict[str, Any]:
    values = _dump(_candidate(status))
    values.update(overrides)
    return values


def _refuse_projection(values: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        CandidateProjection.model_validate(values)


# --- evidence references ---------------------------------------------------


@pytest.mark.parametrize(
    "display_name", ["a/b.txt", "a\\b.txt", "a\rb.txt", "a\nb.txt", "a\x00.txt"]
)
def test_evidence_display_names_may_not_carry_a_path(display_name: str) -> None:
    with pytest.raises(ValidationError, match="sanitized basename"):
        EvidenceReference(
            evidence_ref=uuid4(),
            kind=EvidenceKind.ATTACHMENT,
            media_type="text/plain",
            display_name=display_name,
            download_available=True,
        )


def test_locked_evidence_must_name_the_source_it_can_be_unlocked_from() -> None:
    with pytest.raises(ValidationError, match="PASSWORD_REQUIRED evidence requires source_ref"):
        EvidenceReference(
            evidence_ref=uuid4(),
            kind=EvidenceKind.ATTACHMENT,
            media_type="application/pdf",
            download_available=False,
            unlock_status=EvidenceUnlockStatus.PASSWORD_REQUIRED,
        )


def test_unlocked_evidence_may_not_leak_the_source_it_came_from() -> None:
    # The source ref is only meaningful while a password is outstanding; after
    # that it is one more internal identity the wire does not need.
    with pytest.raises(ValidationError, match="NOT_REQUIRED evidence cannot expose source_ref"):
        EvidenceReference(
            evidence_ref=uuid4(),
            kind=EvidenceKind.ATTACHMENT,
            media_type="application/pdf",
            download_available=True,
            source_ref=uuid4(),
        )


# --- review summaries ------------------------------------------------------


def test_a_review_decision_timestamp_must_carry_a_zone() -> None:
    with pytest.raises(ValidationError, match="review decision timestamp must be timezone-aware"):
        ReviewSummary(
            event_count=1,
            last_action=CandidateAction.CONFIRM,
            last_decided_at=NOW.replace(tzinfo=None),
            current_revision=2,
        )


def test_an_empty_review_summary_cannot_claim_a_decision() -> None:
    with pytest.raises(ValidationError, match="empty review summary cannot have a last decision"):
        ReviewSummary(event_count=0, last_action=CandidateAction.CONFIRM, current_revision=1)


def test_a_review_summary_with_history_must_name_its_last_decision() -> None:
    with pytest.raises(ValidationError, match="non-empty review summary requires a last decision"):
        ReviewSummary(event_count=1, current_revision=2)


# --- candidate projection shape -------------------------------------------


def test_a_counterparty_reference_and_class_travel_together() -> None:
    _refuse_projection(
        _projection(CandidateStatus.PENDING, counterparty_ref="cp_synthetic_vendor"),
        "counterparty reference and class must be supplied together",
    )


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "26-01", "2026/01"])
def test_an_accounting_month_that_is_not_a_calendar_month_is_refused(month: str) -> None:
    _refuse_projection(
        _projection(CandidateStatus.PENDING, accounting_month=month),
        "accounting_month must use YYYY-MM",
    )


def test_the_review_summary_revision_must_be_the_candidate_revision() -> None:
    values = _projection(CandidateStatus.PENDING)
    values["review_summary"]["current_revision"] = 2
    _refuse_projection(values, "review summary revision must match candidate revision")


def test_the_candidate_revision_is_one_more_than_its_event_count() -> None:
    values = _projection(CandidateStatus.CONFIRMED)
    values["review_summary"]["event_count"] = 5
    _refuse_projection(values, "revision must equal review event count plus one")


@pytest.mark.parametrize("status", [CandidateStatus.INCOMPLETE, CandidateStatus.CONFLICTED])
def test_incomplete_and_conflicted_can_only_be_the_first_revision(
    status: CandidateStatus,
) -> None:
    values = _projection(status, revision=2)
    values["review_summary"] = {
        "event_count": 1,
        "last_action": CandidateAction.COMPLETE_FIELDS.value,
        "last_decided_at": values["updated_at"],
        "current_revision": 2,
    }
    _refuse_projection(values, "INCOMPLETE and CONFLICTED are initial candidate states")


def test_a_first_revision_candidate_cannot_name_a_last_action() -> None:
    # A revision-1 candidate has an empty review summary by construction, so
    # the refusal lands on the summary before the status rule ever runs.
    values = _projection(CandidateStatus.PENDING)
    values["review_summary"] = {
        "event_count": 0,
        "last_action": CandidateAction.CONFIRM.value,
        "last_decided_at": None,
        "current_revision": 1,
    }
    _refuse_projection(values, "empty review summary cannot have a last decision")


def test_a_derived_pending_candidate_must_have_been_completed_or_resolved() -> None:
    values = _projection(CandidateStatus.PENDING, revision=2)
    values["review_summary"] = {
        "event_count": 1,
        "last_action": CandidateAction.CONFIRM.value,
        "last_decided_at": values["updated_at"],
        "current_revision": 2,
    }
    _refuse_projection(values, "derived PENDING state requires completion or conflict resolution")


@pytest.mark.parametrize(
    ("status", "action"),
    [
        (CandidateStatus.CONFIRMED, CandidateAction.IGNORE),
        (CandidateStatus.IGNORED, CandidateAction.CONFIRM),
        (CandidateStatus.SUPERSEDED, CandidateAction.CONFIRM),
    ],
)
def test_a_terminal_status_must_match_the_action_that_produced_it(
    status: CandidateStatus, action: CandidateAction
) -> None:
    values = _projection(status)
    values["review_summary"]["last_action"] = action.value
    _refuse_projection(values, "terminal candidate status must match its last action")


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_candidate_timestamps_must_carry_a_zone(field: str) -> None:
    values = _projection(CandidateStatus.PENDING)
    values[field] = values[field].replace(tzinfo=None)
    _refuse_projection(values, "candidate timestamps must be timezone-aware")


def test_a_candidate_cannot_have_been_updated_before_it_was_created() -> None:
    values = _projection(CandidateStatus.PENDING)
    values["created_at"] = values["updated_at"] + timedelta(seconds=1)
    _refuse_projection(values, "candidate timestamps cannot move backward")


def test_an_untouched_candidate_has_one_timestamp_not_two() -> None:
    values = _projection(CandidateStatus.PENDING)
    values["updated_at"] = values["created_at"] + timedelta(seconds=1)
    _refuse_projection(values, "initial candidate timestamps must match")


def test_updated_at_is_the_time_of_the_last_decision() -> None:
    values = _projection(CandidateStatus.CONFIRMED)
    values["updated_at"] = values["updated_at"] + timedelta(seconds=1)
    _refuse_projection(values, "updated_at must equal its last decision time")


@pytest.mark.parametrize(
    ("cleared", "message"),
    [
        ("business_unit_ref", "business unit label requires a reference"),
        ("business_unit_label", "business unit reference requires a label"),
        ("category_code", "category label requires a code"),
        ("category_label", "category code requires a label"),
    ],
)
def test_a_normalized_reference_and_its_label_travel_together(cleared: str, message: str) -> None:
    # Half a normalized value would render as a label with nothing behind it.
    values = _projection(CandidateStatus.PENDING)
    values[cleared] = None
    _refuse_projection(values, message)


# --- status against blockers ----------------------------------------------


def test_an_incomplete_candidate_must_describe_exactly_what_is_missing() -> None:
    values = _projection(CandidateStatus.INCOMPLETE)
    values["blockers"] = [
        {"code": BlockerCode.MISSING_CATEGORY.value, "message": "wrong code", "field": "category"}
    ]
    _refuse_projection(values, "INCOMPLETE must describe missing fields")


def test_a_conflicted_candidate_must_be_complete_and_carry_only_conflicts() -> None:
    values = _projection(
        CandidateStatus.CONFLICTED, business_unit_ref=None, business_unit_label=None
    )
    _refuse_projection(values, "CONFLICTED must be complete")


def test_a_conflict_blocker_without_an_opaque_reference_is_refused() -> None:
    values = _projection(CandidateStatus.CONFLICTED)
    values["blockers"] = [
        {
            "code": BlockerCode.BUSINESS_KEY_CONFLICT.value,
            "message": "conflict",
            "conflict_ref": None,
        }
    ]
    _refuse_projection(values, "only opaque conflict blockers")


@pytest.mark.parametrize("status", [CandidateStatus.PENDING, CandidateStatus.CONFIRMED])
def test_reviewable_and_confirmed_candidates_carry_no_blockers(status: CandidateStatus) -> None:
    values = _projection(status)
    values["blockers"] = [
        {"code": BlockerCode.MISSING_AMOUNT.value, "message": "amount is missing"}
    ]
    _refuse_projection(values, "must be complete and unblocked")


def test_a_superseded_candidate_must_name_its_replacement() -> None:
    _refuse_projection(
        _projection(CandidateStatus.SUPERSEDED, superseded_by_candidate_ref=None),
        "SUPERSEDED must link to its derived replacement",
    )


def test_only_a_superseded_candidate_may_name_a_replacement() -> None:
    _refuse_projection(
        _projection(CandidateStatus.PENDING, superseded_by_candidate_ref=uuid4()),
        "only SUPERSEDED candidates may link to a replacement",
    )


# --- patches ---------------------------------------------------------------


def test_a_patch_that_changes_nothing_is_refused() -> None:
    with pytest.raises(ValidationError, match="must change at least one field"):
        CandidatePatch()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"business_unit_ref": "unit-demo-a"}, "business unit reference and label"),
        ({"category_code": "SUPPLIES"}, "category code and label"),
    ],
)
def test_a_patch_may_not_change_half_a_normalized_value(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        CandidatePatch.model_validate(payload)


def test_a_patch_month_must_be_a_calendar_month() -> None:
    with pytest.raises(ValidationError, match="accounting_month must use YYYY-MM"):
        CandidatePatch(accounting_month="2026-00")


# --- commands --------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [CandidateAction.COMPLETE_FIELDS, CandidateAction.CORRECT_AND_CONFIRM],
)
def test_an_action_that_edits_fields_requires_a_patch(action: CandidateAction) -> None:
    with pytest.raises(ValidationError, match="requires a patch"):
        _command(action, 1)


@pytest.mark.parametrize("action", [CandidateAction.CONFIRM, CandidateAction.IGNORE])
def test_a_decision_only_action_refuses_a_patch(action: CandidateAction) -> None:
    with pytest.raises(ValidationError, match="does not accept a patch"):
        _command(action, 1, patch=CandidatePatch(amount_minor=-1))


def test_resolving_a_conflict_requires_the_resolutions() -> None:
    with pytest.raises(ValidationError, match="RESOLVE_CONFLICT requires resolutions"):
        _command(CandidateAction.RESOLVE_CONFLICT, 1)


def test_any_other_action_refuses_conflict_resolutions() -> None:
    with pytest.raises(ValidationError, match="does not accept conflict resolutions"):
        _command(CandidateAction.CONFIRM, 1, resolutions={uuid4(): "resolved"})


def test_supersede_requires_the_identity_of_the_candidate_it_creates() -> None:
    with pytest.raises(ValidationError, match="SUPERSEDE requires a derived candidate identity"):
        _command(
            CandidateAction.SUPERSEDE,
            1,
            patch=CandidatePatch(amount_minor=-1),
            derived_candidate_ref=DERIVED_REF,
        )


def test_only_supersede_may_name_a_derived_candidate() -> None:
    with pytest.raises(ValidationError, match="only valid for SUPERSEDE"):
        _command(CandidateAction.CONFIRM, 1, derived_candidate_ref=DERIVED_REF)


def test_a_decision_time_without_a_zone_is_refused() -> None:
    with pytest.raises(ValidationError, match="decided_at must be timezone-aware"):
        _command(CandidateAction.CONFIRM, 1, at=NOW.replace(tzinfo=None))


# --- audit field changes ---------------------------------------------------


def test_an_audit_change_that_records_no_change_is_refused() -> None:
    aggregate = _confirmed_aggregate()
    event = _dump(aggregate.events[0])
    event["changes"] = [
        {"field": "amount_minor", "previous_value": 1, "new_value": 1},
        *event["changes"],
    ]
    with pytest.raises(ValidationError, match="must contain different values"):
        CandidateEvent.model_validate(event)


def test_an_audit_value_outside_the_allowlist_is_refused() -> None:
    # Only strings, ints and enum members are safe to put on the audit wire.
    with pytest.raises(CandidateTransitionRejected, match="audit value is not allowlisted"):
        _audit_value(1.5)
    with pytest.raises(CandidateTransitionRejected, match="audit value is not allowlisted"):
        _audit_value(True)


# --- events ----------------------------------------------------------------


def _confirm_event() -> dict[str, Any]:
    return _dump(_confirmed_aggregate().events[0])


def _refuse_event(values: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        CandidateEvent.model_validate(values)


def test_an_event_timestamp_must_carry_a_zone() -> None:
    values = _confirm_event()
    values["created_at"] = values["created_at"].replace(tzinfo=None)
    _refuse_event(values, "candidate event timestamp must be timezone-aware")


def test_an_event_advances_exactly_one_revision() -> None:
    values = _confirm_event()
    values["to_revision"] = values["from_revision"] + 2
    _refuse_event(values, "must advance exactly one revision")


def test_an_event_edge_the_action_does_not_allow_is_refused() -> None:
    values = _confirm_event()
    values["from_status"] = CandidateStatus.INCOMPLETE.value
    _refuse_event(values, "action does not match its state edge")


def test_only_supersede_may_link_a_derived_candidate() -> None:
    values = _confirm_event()
    values["derived_candidate_ref"] = str(DERIVED_REF)
    _refuse_event(values, "only SUPERSEDE events may link a derived candidate")


def test_only_a_supersede_receipt_may_carry_a_derived_snapshot() -> None:
    values = _confirm_event()
    values["result_derived_candidate"] = _projection(CandidateStatus.PENDING)
    _refuse_event(values, "only SUPERSEDE receipts may contain a derived candidate snapshot")


def test_only_a_conflict_resolution_event_records_resolutions() -> None:
    values = _confirm_event()
    values["resolved_conflicts"] = [{"conflict_ref": str(uuid4()), "resolution": "done"}]
    _refuse_event(values, "only RESOLVE_CONFLICT events may record conflict resolutions")


def test_an_event_must_record_the_status_change_exactly_once() -> None:
    values = _confirm_event()
    values["changes"] = [
        {"field": "amount_minor", "previous_value": 1, "new_value": 2},
    ]
    _refuse_event(values, "changes must uniquely include status")


def test_the_status_audit_change_must_match_the_edge() -> None:
    values = _confirm_event()
    values["changes"] = [
        {
            "field": "status",
            "previous_value": CandidateStatus.INCOMPLETE.value,
            "new_value": CandidateStatus.CONFIRMED.value,
        }
    ]
    _refuse_event(values, "status audit change must match the event state edge")


def test_a_decision_only_event_cannot_also_change_a_normalized_field() -> None:
    values = _confirm_event()
    values["changes"] = [
        *values["changes"],
        {"field": "amount_minor", "previous_value": 1, "new_value": 2},
    ]
    _refuse_event(values, "decision-only events cannot change normalized fields")


def test_an_event_whose_prior_projection_is_not_the_source_state_is_refused() -> None:
    values = _confirm_event()
    values["prior_projection"]["candidate_ref"] = str(uuid4())
    _refuse_event(values, "prior projection must match its state edge")


def test_an_event_whose_receipt_records_a_different_action_is_refused() -> None:
    # CONFIRMED accepts either confirming action, so the receipt is internally
    # valid; only the event knows which one actually happened.
    values = _confirm_event()
    values["result_projection"]["review_summary"]["last_action"] = (
        CandidateAction.CORRECT_AND_CONFIRM.value
    )
    _refuse_event(values, "receipt projection must match its state edge")


def test_an_event_that_changes_an_immutable_projection_field_is_refused() -> None:
    values = _confirm_event()
    values["result_projection"]["summary"] = "a different summary entirely"
    _refuse_event(values, "changed an immutable projection field")


def test_a_confirmation_may_not_silently_clear_the_blockers_it_inherited() -> None:
    values = _confirm_event()
    values["prior_projection"]["blockers"] = [
        {"code": BlockerCode.MISSING_AMOUNT.value, "message": "amount is missing"}
    ]
    _refuse_event(
        values, "prior projection must match its state edge|must be complete and unblocked"
    )


def test_a_non_supersede_event_may_not_change_the_replacement_link() -> None:
    values = _confirm_event()
    values["result_projection"]["superseded_by_candidate_ref"] = str(DERIVED_REF)
    _refuse_event(values, "only SUPERSEDED candidates may link to a replacement")


# --- aggregates ------------------------------------------------------------


def _refuse_aggregate(values: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        CandidateAggregate.model_validate(values)


def test_an_aggregate_must_carry_its_whole_history() -> None:
    aggregate = _confirmed_aggregate()
    values = _dump(aggregate)
    values["events"] = []
    _refuse_aggregate(values, "complete event history from revision 1")


def test_the_first_event_must_retain_the_candidate_creation_time() -> None:
    values = _dump(_confirmed_aggregate())
    shifted = values["events"][0]["prior_projection"]["created_at"] - timedelta(seconds=1)
    values["events"][0]["prior_projection"]["created_at"] = shifted
    values["events"][0]["prior_projection"]["updated_at"] = shifted
    values["events"][0]["result_projection"]["created_at"] = shifted
    _refuse_aggregate(values, "first event must retain the candidate creation time")


def test_event_timestamps_may_not_move_backwards_through_the_chain() -> None:
    aggregate = _confirmed_aggregate()
    values = _dump(aggregate)
    values["projection"]["created_at"] = values["projection"]["created_at"] + timedelta(days=1)
    _refuse_aggregate(values, "timestamps|initial candidate timestamps")


def test_the_projection_must_be_the_tip_of_the_event_chain() -> None:
    aggregate = _confirmed_aggregate()
    values = _dump(aggregate)
    values["projection"]["confidence_basis_points"] = 1
    _refuse_aggregate(values, "must equal the event chain tip|immutable projection field")


# --- transitions -----------------------------------------------------------


def test_a_transition_without_a_bounded_actor_is_refused() -> None:
    aggregate = CandidateAggregate(projection=_candidate(CandidateStatus.PENDING))
    for actor in ("", "a" * 201):
        with pytest.raises(CandidateTransitionRejected, match="trusted actor reference is invalid"):
            apply_candidate_command(
                aggregate, _command(CandidateAction.CONFIRM, 1), actor_ref=actor
            )


def test_completion_may_not_overwrite_a_value_that_is_already_there() -> None:
    aggregate = CandidateAggregate(projection=_candidate(CandidateStatus.INCOMPLETE))
    with pytest.raises(CandidateTransitionRejected, match="cannot overwrite existing values"):
        apply_candidate_command(
            aggregate,
            _command(
                CandidateAction.COMPLETE_FIELDS,
                1,
                patch=CandidatePatch(category_code="OTHER", category_label="Other"),
            ),
            actor_ref="human:test-reviewer",
        )


def test_completion_must_fill_every_missing_field_at_once() -> None:
    aggregate = CandidateAggregate(
        projection=CandidateProjection.model_validate(
            _projection(
                CandidateStatus.INCOMPLETE,
                category_code=None,
                category_label=None,
                blockers=[
                    {
                        "code": BlockerCode.MISSING_BUSINESS_UNIT.value,
                        "message": "business unit is missing",
                        "field": "business_unit",
                    },
                    {
                        "code": BlockerCode.MISSING_CATEGORY.value,
                        "message": "category is missing",
                        "field": "category",
                    },
                ],
            )
        )
    )
    with pytest.raises(CandidateTransitionRejected, match="all missing required fields"):
        apply_candidate_command(
            aggregate,
            _command(
                CandidateAction.COMPLETE_FIELDS,
                1,
                patch=CandidatePatch(business_unit_ref="unit-demo-a", business_unit_label="A"),
            ),
            actor_ref="human:test-reviewer",
        )


def test_a_correction_may_not_remove_required_data() -> None:
    aggregate = CandidateAggregate(projection=_candidate(CandidateStatus.PENDING))
    with pytest.raises(CandidateTransitionRejected, match="cannot remove required data"):
        apply_candidate_command(
            aggregate,
            _command(
                CandidateAction.CORRECT_AND_CONFIRM,
                1,
                patch=CandidatePatch(business_unit_ref=None, business_unit_label=None),
            ),
            actor_ref="human:test-reviewer",
        )


def test_a_correction_that_corrects_nothing_is_refused() -> None:
    pending = _candidate(CandidateStatus.PENDING)
    aggregate = CandidateAggregate(projection=pending)
    with pytest.raises(CandidateTransitionRejected, match="must change a normalized field"):
        apply_candidate_command(
            aggregate,
            _command(
                CandidateAction.CORRECT_AND_CONFIRM,
                1,
                patch=CandidatePatch(amount_minor=pending.amount_minor),
            ),
            actor_ref="human:test-reviewer",
        )


def test_a_conflict_resolution_must_answer_every_conflict_exactly() -> None:
    conflicted = _candidate(CandidateStatus.CONFLICTED)
    aggregate = CandidateAggregate(projection=conflicted)
    with pytest.raises(CandidateTransitionRejected, match="one bounded resolution"):
        apply_candidate_command(
            aggregate,
            _command(CandidateAction.RESOLVE_CONFLICT, 1, resolutions={uuid4(): "unrelated"}),
            actor_ref="human:test-reviewer",
        )


def test_a_blank_conflict_resolution_is_not_a_resolution() -> None:
    conflicted = _candidate(CandidateStatus.CONFLICTED)
    required = next(b.conflict_ref for b in conflicted.blockers if b.conflict_ref is not None)
    with pytest.raises(CandidateTransitionRejected, match="one bounded resolution"):
        apply_candidate_command(
            CandidateAggregate(projection=conflicted),
            _command(CandidateAction.RESOLVE_CONFLICT, 1, resolutions={required: "   "}),
            actor_ref="human:test-reviewer",
        )


def test_only_an_open_candidate_may_be_ignored() -> None:
    aggregate = _confirmed_aggregate()
    with pytest.raises(CandidateTransitionRejected, match="only open candidates may be ignored"):
        apply_candidate_command(
            aggregate,
            _command(CandidateAction.IGNORE, aggregate.projection.revision),
            actor_ref="human:test-reviewer",
        )


def test_a_supersede_may_not_reuse_the_source_identity() -> None:
    aggregate = _confirmed_aggregate()
    current = aggregate.projection
    with pytest.raises(CandidateTransitionRejected, match="reference must be new"):
        apply_candidate_command(
            aggregate,
            _command(
                CandidateAction.SUPERSEDE,
                current.revision,
                patch=CandidatePatch(amount_minor=(current.amount_minor or 0) + 1),
                derived_candidate_ref=current.candidate_ref,
                derived_short_id=DERIVED_SHORT_ID,
            ),
            actor_ref="human:test-reviewer",
        )


def test_a_supersede_may_not_reuse_the_source_short_id() -> None:
    aggregate = _confirmed_aggregate()
    current = aggregate.projection
    with pytest.raises(CandidateTransitionRejected, match="short ID must be new"):
        apply_candidate_command(
            aggregate,
            _command(
                CandidateAction.SUPERSEDE,
                current.revision,
                patch=CandidatePatch(amount_minor=(current.amount_minor or 0) + 1),
                derived_candidate_ref=DERIVED_REF,
                derived_short_id=current.short_id,
            ),
            actor_ref="human:test-reviewer",
        )


def test_a_supersede_that_corrects_nothing_is_refused() -> None:
    aggregate = _confirmed_aggregate()
    current = aggregate.projection
    with pytest.raises(CandidateTransitionRejected, match="SUPERSEDE must change a normalized"):
        apply_candidate_command(
            aggregate,
            _command(
                CandidateAction.SUPERSEDE,
                current.revision,
                patch=CandidatePatch(amount_minor=current.amount_minor),
                derived_candidate_ref=DERIVED_REF,
                derived_short_id=DERIVED_SHORT_ID,
            ),
            actor_ref="human:test-reviewer",
        )


def test_a_supersede_correction_may_not_remove_required_data() -> None:
    aggregate = _confirmed_aggregate()
    current = aggregate.projection
    with pytest.raises(CandidateTransitionRejected, match="cannot remove required data"):
        apply_candidate_command(
            aggregate,
            _command(
                CandidateAction.SUPERSEDE,
                current.revision,
                patch=CandidatePatch(business_unit_ref=None, business_unit_label=None),
                derived_candidate_ref=DERIVED_REF,
                derived_short_id=DERIVED_SHORT_ID,
            ),
            actor_ref="human:test-reviewer",
        )


def test_a_replayed_operation_with_different_content_is_a_conflict() -> None:
    aggregate = _confirmed_aggregate()
    replayed = aggregate.events[0].operation_id
    command = CandidateCommand(
        operation_id=replayed,
        action=CandidateAction.CONFIRM,
        expected_revision=1,
        reason="a different reason entirely",
        decided_at=NOW,
    )
    with pytest.raises(Exception, match="reused with different content or actor"):
        apply_candidate_command(aggregate, command, actor_ref="human:test-reviewer")


# --- receipts on the richer actions ----------------------------------------


def _supersede_aggregate() -> CandidateAggregate:
    aggregate = _confirmed_aggregate()
    current = aggregate.projection
    return apply_candidate_command(
        aggregate,
        _command(
            CandidateAction.SUPERSEDE,
            current.revision,
            patch=CandidatePatch(amount_minor=(current.amount_minor or 0) + 1),
            derived_candidate_ref=DERIVED_REF,
            derived_short_id=DERIVED_SHORT_ID,
            at=NOW + timedelta(minutes=1),
        ),
        actor_ref="human:test-reviewer",
    ).aggregate


def _supersede_event() -> dict[str, Any]:
    return _dump(_supersede_aggregate().events[-1])


def _completion_event() -> dict[str, Any]:
    incomplete = _candidate(CandidateStatus.INCOMPLETE)
    outcome = apply_candidate_command(
        CandidateAggregate(projection=incomplete),
        _command(
            CandidateAction.COMPLETE_FIELDS,
            1,
            patch=CandidatePatch(business_unit_ref="unit-demo-a", business_unit_label="Demo A"),
        ),
        actor_ref="human:test-reviewer",
    )
    return _dump(outcome.aggregate.events[0])


def _resolution_aggregate() -> CandidateAggregate:
    conflicted = _candidate(CandidateStatus.CONFLICTED)
    required = [b.conflict_ref for b in conflicted.blockers if b.conflict_ref is not None]
    return apply_candidate_command(
        CandidateAggregate(projection=conflicted),
        _command(
            CandidateAction.RESOLVE_CONFLICT,
            1,
            resolutions=dict.fromkeys(required, "operator resolved the conflict"),
        ),
        actor_ref="human:test-reviewer",
    ).aggregate


def _ignore_event() -> dict[str, Any]:
    incomplete = _candidate(CandidateStatus.INCOMPLETE)
    outcome = apply_candidate_command(
        CandidateAggregate(projection=incomplete),
        _command(CandidateAction.IGNORE, 1),
        actor_ref="human:test-reviewer",
    )
    return _dump(outcome.aggregate.events[0])


def test_one_conflict_may_not_be_resolved_twice_in_one_event() -> None:
    values = _dump(_resolution_aggregate().events[0])
    values["resolved_conflicts"] = [*values["resolved_conflicts"], values["resolved_conflicts"][0]]
    _refuse_event(values, "resolved conflict refs must be unique")


def test_a_field_editing_action_must_actually_audit_a_field() -> None:
    values = _completion_event()
    values["changes"] = [change for change in values["changes"] if change["field"] == "status"]
    _refuse_event(values, "requires a normalized field change")


def test_completion_may_only_audit_values_that_were_missing() -> None:
    values = _completion_event()
    for change in values["changes"]:
        if change["field"] == "business_unit_ref":
            change["previous_value"] = "unit-demo-z"
    _refuse_event(values, "may only fill previously missing values")


def test_a_supersede_receipt_must_carry_the_candidate_it_named() -> None:
    values = _supersede_event()
    values["result_derived_candidate"]["candidate_ref"] = str(uuid4())
    _refuse_event(values, "must match its derived candidate ref")


def test_an_audited_old_value_must_be_what_the_prior_projection_held() -> None:
    values = _supersede_event()
    for change in values["changes"]:
        if change["field"] == "amount_minor":
            change["previous_value"] = int(change["previous_value"]) - 1000
    _refuse_event(values, "must match every audited old value")


def test_a_supersede_may_not_edit_the_candidate_it_supersedes() -> None:
    # The correction belongs to the new candidate; the old one is frozen.
    values = _supersede_event()
    values["result_projection"]["amount_minor"] = (
        int(values["result_projection"]["amount_minor"]) - 5
    )
    _refuse_event(values, "supersede cannot overwrite its source projection")


def test_a_receipt_may_not_change_a_field_the_event_did_not_audit() -> None:
    values = _confirm_event()
    values["result_projection"]["amount_minor"] = (
        int(values["result_projection"]["amount_minor"]) + 5
    )
    _refuse_event(values, "unaudited normalized fields cannot change")


def test_ignoring_a_candidate_does_not_quietly_clear_its_blockers() -> None:
    values = _ignore_event()
    values["result_projection"]["blockers"] = []
    _refuse_event(values, "changed blockers without an allowed action")


def test_a_supersede_receipt_must_link_the_replacement_it_created() -> None:
    values = _supersede_event()
    values["result_projection"]["superseded_by_candidate_ref"] = str(uuid4())
    _refuse_event(values, "supersede receipt must link its replacement")


def test_a_derived_candidate_may_not_rewrite_its_source_evidence() -> None:
    values = _supersede_event()
    values["result_derived_candidate"]["summary"] = "a summary the source never had"
    _refuse_event(values, "derived candidate changed an immutable source field")


def test_resolving_a_conflict_may_not_leave_the_candidate_incomplete() -> None:
    conflicted = _candidate(CandidateStatus.CONFLICTED)
    required = [b.conflict_ref for b in conflicted.blockers if b.conflict_ref is not None]
    with pytest.raises(CandidateTransitionRejected, match="must remain complete"):
        apply_candidate_command(
            CandidateAggregate(projection=conflicted),
            _command(
                CandidateAction.RESOLVE_CONFLICT,
                1,
                resolutions=dict.fromkeys(required, "operator resolved the conflict"),
                patch=CandidatePatch(business_unit_ref=None, business_unit_label=None),
            ),
            actor_ref="human:test-reviewer",
        )
