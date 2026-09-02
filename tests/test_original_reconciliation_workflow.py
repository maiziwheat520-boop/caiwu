from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from ledgerbridge.original_reconciliation import OriginalReconciliationScope
from ledgerbridge.original_reconciliation_workflow import (
    OriginalReconciliationAuthoritativeSource,
    OriginalReconciliationEvidenceRelinkCommand,
    OriginalReconciliationFlowKind,
    OriginalReconciliationMonthCloseCommand,
    OriginalReconciliationSourceChannel,
    OriginalReconciliationWorkflowIdempotencyConflict,
    OriginalReconciliationWorkflowRejected,
    OriginalReconciliationWorkflowReviewStatus,
    OriginalReconciliationWorkflowRevisionConflict,
    SyntheticOriginalReconciliationWorkflowService,
    close_original_reconciliation_month,
    create_original_reconciliation_workflow_item,
    create_original_reconciliation_workflow_month,
    original_reconciliation_item_set_sha256,
    stable_original_reconciliation_item_ref,
)

ENTITY_REF = UUID("10000000-0000-4000-8000-000000000001")
ACCOUNT_REF = UUID("20000000-0000-4000-8000-000000000001")
EVIDENCE_ONE = UUID("30000000-0000-4000-8000-000000000001")
EVIDENCE_TWO = UUID("30000000-0000-4000-8000-000000000002")
EVIDENCE_THREE = UUID("30000000-0000-4000-8000-000000000003")
OPERATION_ONE = UUID("40000000-0000-4000-8000-000000000001")
OPERATION_TWO = UUID("40000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
SCOPE = OriginalReconciliationScope(
    entity_ref=ENTITY_REF,
    business_unit_ref="synthetic-hotel",
)


def _source(
    *,
    channel: OriginalReconciliationSourceChannel = (
        OriginalReconciliationSourceChannel.BANK_STATEMENT
    ),
) -> OriginalReconciliationAuthoritativeSource:
    return OriginalReconciliationAuthoritativeSource(
        source_channel=channel,
        authority_ref="source:synthetic:001",
        managed_account_ref=(
            None
            if channel == OriginalReconciliationSourceChannel.PAYROLL_PUBLICATION
            else ACCOUNT_REF
        ),
        source_fact_refs=("fact:synthetic:001",),
    )


def _item(
    key: str,
    *,
    flow: OriginalReconciliationFlowKind = OriginalReconciliationFlowKind.INCOME,
    amount_minor: int = 12_345,
    status: OriginalReconciliationWorkflowReviewStatus = (
        OriginalReconciliationWorkflowReviewStatus.CONFIRMED
    ),
    evidence_refs: tuple[UUID, ...] = (EVIDENCE_ONE,),
):
    return create_original_reconciliation_workflow_item(
        stable_item_key=key,
        entity_ref=ENTITY_REF,
        business_unit_ref=SCOPE.business_unit_ref,
        month="2026-08",
        flow_kind=flow,
        signed_amount_minor=amount_minor,
        review_status=status,
        authoritative_sources=(_source(),),
        evidence_refs=evidence_refs,
    )


def _month(*items):
    return create_original_reconciliation_workflow_month(
        month="2026-08",
        scope=SCOPE,
        items=tuple(items) if items else (_item("legacy:income:001"),),
    )


def test_stable_item_ref_ignores_amount_evidence_and_review_changes() -> None:
    expected = stable_original_reconciliation_item_ref(
        entity_ref=ENTITY_REF,
        business_unit_ref=SCOPE.business_unit_ref,
        month="2026-08",
        stable_item_key="legacy:income:001",
    )
    first = _item("legacy:income:001")
    changed = _item(
        "legacy:income:001",
        amount_minor=99_900,
        status=OriginalReconciliationWorkflowReviewStatus.RETURNED,
        evidence_refs=(EVIDENCE_TWO,),
    )

    assert first.item_ref == expected == changed.item_ref


@pytest.mark.parametrize(
    "channel",
    [
        OriginalReconciliationSourceChannel.BANK_STATEMENT,
        OriginalReconciliationSourceChannel.WECHAT_TRANSFER,
    ],
)
def test_statement_and_wechat_sources_require_registered_account_ref(
    channel: OriginalReconciliationSourceChannel,
) -> None:
    with pytest.raises(ValidationError, match="managed account ref"):
        OriginalReconciliationAuthoritativeSource(
            source_channel=channel,
            authority_ref="source:missing-account",
            source_fact_refs=("fact:001",),
        )


def test_workflow_month_rejects_duplicate_or_cross_scope_items() -> None:
    item = _item("legacy:income:001")
    with pytest.raises(ValidationError, match="must be unique"):
        create_original_reconciliation_workflow_month(
            month="2026-08",
            scope=SCOPE,
            items=(item, item),
        )
    with pytest.raises(ValidationError, match="escaped"):
        create_original_reconciliation_workflow_month(
            month="2026-07",
            scope=SCOPE,
            items=(item,),
        )


def test_evidence_relink_increments_item_and_month_and_returns_append_only_event() -> None:
    initial = _month()
    service = SyntheticOriginalReconciliationWorkflowService((initial,))
    item = initial.items[0]
    command = OriginalReconciliationEvidenceRelinkCommand(
        expected_month_revision=1,
        expected_item_revision=1,
        add_evidence_refs=(EVIDENCE_THREE, EVIDENCE_TWO),
        remove_evidence_refs=(EVIDENCE_ONE,),
        reason="Replace the stale source attachment",
    )

    receipt = service.relink_evidence(
        entity_ref=ENTITY_REF,
        business_unit_ref=SCOPE.business_unit_ref,
        month="2026-08",
        item_ref=item.item_ref,
        operation_id=OPERATION_ONE,
        actor_ref="user:synthetic-reviewer",
        command=command,
        changed_at=NOW,
    )
    saved = service.get_month(
        entity_ref=ENTITY_REF,
        business_unit_ref=SCOPE.business_unit_ref,
        month="2026-08",
    )

    assert receipt.replayed is False
    assert receipt.event.from_month_revision == 1
    assert receipt.event.to_month_revision == 2
    assert receipt.event.from_item_revision == 1
    assert receipt.event.to_item_revision == 2
    assert receipt.item.evidence_refs == (EVIDENCE_TWO, EVIDENCE_THREE)
    assert saved.revision == 2
    assert saved.items[0] == receipt.item
    assert initial.revision == 1
    assert initial.items[0].evidence_refs == (EVIDENCE_ONE,)


def test_evidence_relink_rejects_stale_and_inexact_change_sets() -> None:
    initial = _month()
    service = SyntheticOriginalReconciliationWorkflowService((initial,))
    item_ref = initial.items[0].item_ref
    stale = OriginalReconciliationEvidenceRelinkCommand(
        expected_month_revision=2,
        expected_item_revision=1,
        add_evidence_refs=(EVIDENCE_TWO,),
        reason="Stale browser state",
    )
    with pytest.raises(OriginalReconciliationWorkflowRevisionConflict):
        service.relink_evidence(
            entity_ref=ENTITY_REF,
            business_unit_ref=SCOPE.business_unit_ref,
            month="2026-08",
            item_ref=item_ref,
            operation_id=OPERATION_ONE,
            actor_ref="user:synthetic-reviewer",
            command=stale,
            changed_at=NOW,
        )

    duplicate_add = stale.model_copy(
        update={
            "expected_month_revision": 1,
            "add_evidence_refs": (EVIDENCE_ONE,),
        }
    )
    with pytest.raises(OriginalReconciliationWorkflowRejected, match="already linked"):
        service.relink_evidence(
            entity_ref=ENTITY_REF,
            business_unit_ref=SCOPE.business_unit_ref,
            month="2026-08",
            item_ref=item_ref,
            operation_id=OPERATION_ONE,
            actor_ref="user:synthetic-reviewer",
            command=duplicate_add,
            changed_at=NOW,
        )


def test_evidence_relink_is_exactly_idempotent_and_actor_bound() -> None:
    initial = _month()
    service = SyntheticOriginalReconciliationWorkflowService((initial,))
    item_ref = initial.items[0].item_ref
    command = OriginalReconciliationEvidenceRelinkCommand(
        expected_month_revision=1,
        expected_item_revision=1,
        add_evidence_refs=(EVIDENCE_TWO,),
        reason="Add the reviewed bank export",
    )
    call = {
        "entity_ref": ENTITY_REF,
        "business_unit_ref": SCOPE.business_unit_ref,
        "month": "2026-08",
        "item_ref": item_ref,
        "operation_id": OPERATION_ONE,
        "actor_ref": "user:synthetic-reviewer",
        "command": command,
        "changed_at": NOW,
    }

    first = service.relink_evidence(**call)
    replay = service.relink_evidence(**call)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.event == first.event
    assert replay.item == first.item
    with pytest.raises(OriginalReconciliationWorkflowIdempotencyConflict):
        service.relink_evidence(**{**call, "actor_ref": "user:other-reviewer"})


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (
            _item(
                "legacy:pending:001",
                status=OriginalReconciliationWorkflowReviewStatus.PENDING,
            ),
            "confirmed",
        ),
        (
            _item("legacy:no-evidence:001", evidence_refs=()),
            "evidence",
        ),
    ],
)
def test_close_fails_closed_until_review_and_evidence_are_complete(item, message: str) -> None:
    state = _month(item)
    with pytest.raises(OriginalReconciliationWorkflowRejected, match=message):
        close_original_reconciliation_month(
            state,
            operation_id=OPERATION_TWO,
            actor_ref="user:synthetic-reviewer",
            command=OriginalReconciliationMonthCloseCommand(
                expected_month_revision=1,
                reason="Close reviewed month",
            ),
            closed_at=NOW,
        )


def test_close_binds_exact_item_set_totals_and_immutable_receipt() -> None:
    income = _item("legacy:income:001", amount_minor=10_000)
    expense = _item(
        "legacy:expense:001",
        flow=OriginalReconciliationFlowKind.EXPENSE,
        amount_minor=-2_500,
        evidence_refs=(EVIDENCE_TWO,),
    )
    current = _item(
        "legacy:current:001",
        flow=OriginalReconciliationFlowKind.CURRENT,
        amount_minor=-1_000,
        evidence_refs=(EVIDENCE_THREE,),
    )
    initial = _month(expense, current, income)
    service = SyntheticOriginalReconciliationWorkflowService((initial,))
    command = OriginalReconciliationMonthCloseCommand(
        expected_month_revision=1,
        reason="All original-scope items reviewed",
    )

    receipt = service.close_month(
        entity_ref=ENTITY_REF,
        business_unit_ref=SCOPE.business_unit_ref,
        month="2026-08",
        operation_id=OPERATION_TWO,
        actor_ref="user:synthetic-reviewer",
        command=command,
        closed_at=NOW,
    )
    saved = service.get_month(
        entity_ref=ENTITY_REF,
        business_unit_ref=SCOPE.business_unit_ref,
        month="2026-08",
    )

    close_receipt = receipt.close_receipt
    assert close_receipt.item_set_sha256 == original_reconciliation_item_set_sha256(
        initial.items
    )
    assert close_receipt.item_count == 3
    assert close_receipt.income_minor == 10_000
    assert close_receipt.expense_minor == 2_500
    assert close_receipt.current_net_minor == -1_000
    assert close_receipt.net_minor == 6_500
    assert close_receipt.closed_revision == 2
    assert saved.close_receipt == close_receipt
    assert saved.revision == 2

    tampered_receipt = close_receipt.model_copy(
        update={"income_minor": 10_001, "net_minor": 6_501}
    )
    with pytest.raises(ValidationError, match="exact workflow snapshot"):
        type(saved).model_validate(
            {
                **saved.model_dump(mode="python"),
                "close_receipt": tampered_receipt,
            }
        )

    replay = service.close_month(
        entity_ref=ENTITY_REF,
        business_unit_ref=SCOPE.business_unit_ref,
        month="2026-08",
        operation_id=OPERATION_TWO,
        actor_ref="user:synthetic-reviewer",
        command=command,
        closed_at=NOW,
    )
    assert replay.replayed is True
    assert replay.close_receipt == close_receipt


def test_closed_month_rejects_later_mutation_and_second_close() -> None:
    initial = _month()
    service = SyntheticOriginalReconciliationWorkflowService((initial,))
    service.close_month(
        entity_ref=ENTITY_REF,
        business_unit_ref=SCOPE.business_unit_ref,
        month="2026-08",
        operation_id=OPERATION_TWO,
        actor_ref="user:synthetic-reviewer",
        command=OriginalReconciliationMonthCloseCommand(
            expected_month_revision=1,
            reason="Close reviewed month",
        ),
        closed_at=NOW,
    )
    with pytest.raises(OriginalReconciliationWorkflowRejected, match="immutable"):
        service.relink_evidence(
            entity_ref=ENTITY_REF,
            business_unit_ref=SCOPE.business_unit_ref,
            month="2026-08",
            item_ref=initial.items[0].item_ref,
            operation_id=OPERATION_ONE,
            actor_ref="user:synthetic-reviewer",
            command=OriginalReconciliationEvidenceRelinkCommand(
                expected_month_revision=2,
                expected_item_revision=1,
                add_evidence_refs=(EVIDENCE_TWO,),
                reason="Attempt after close",
            ),
            changed_at=NOW,
        )
    with pytest.raises(OriginalReconciliationWorkflowRejected, match="already closed"):
        service.close_month(
            entity_ref=ENTITY_REF,
            business_unit_ref=SCOPE.business_unit_ref,
            month="2026-08",
            operation_id=OPERATION_ONE,
            actor_ref="user:synthetic-reviewer",
            command=OriginalReconciliationMonthCloseCommand(
                expected_month_revision=2,
                reason="Second close",
            ),
            closed_at=NOW,
        )


def test_close_digest_and_receipt_identity_do_not_depend_on_item_order() -> None:
    first = _item("legacy:income:001")
    second = _item(
        "legacy:expense:001",
        flow=OriginalReconciliationFlowKind.EXPENSE,
        amount_minor=-500,
        evidence_refs=(EVIDENCE_TWO,),
    )
    command = OriginalReconciliationMonthCloseCommand(
        expected_month_revision=1,
        reason="Close reviewed month",
    )

    _, left = close_original_reconciliation_month(
        _month(first, second),
        operation_id=OPERATION_ONE,
        actor_ref="user:synthetic-reviewer",
        command=command,
        closed_at=NOW,
    )
    _, right = close_original_reconciliation_month(
        _month(second, first),
        operation_id=OPERATION_TWO,
        actor_ref="user:synthetic-reviewer",
        command=command,
        closed_at=NOW,
    )

    assert left.close_receipt.item_set_sha256 == right.close_receipt.item_set_sha256
    assert left.close_receipt.receipt_ref == right.close_receipt.receipt_ref
