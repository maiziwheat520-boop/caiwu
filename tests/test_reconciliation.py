from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from ledgerbridge.reconciliation import (
    DedupDecision,
    DedupIndex,
    DedupRecord,
    ExternalTransactionIdentity,
    Phase5Error,
    ReconciliationLeg,
    ReconciliationProposal,
    ReconciliationRelation,
    ReconciliationStatus,
    SuspenseItem,
    SuspenseReason,
    SuspenseStatus,
    TransactionFingerprint,
)


def _fingerprint(amount: int = 500) -> TransactionFingerprint:
    return TransactionFingerprint(
        occurred_on=date(2026, 8, 24),
        amount_minor=amount,
        counterparty="Merchant",
        description="Lunch",
        balance_minor=10_000,
    )


def _record(
    locator: str = "row-1", amount: int = 500, external_id: str | None = "tx-1"
) -> DedupRecord:
    identity = (
        ExternalTransactionIdentity("bank_cn", "assets:bank", external_id)
        if external_id is not None
        else None
    )
    return DedupRecord(locator, _fingerprint(amount), identity)


def test_external_id_is_authoritative_but_conflicts_require_review() -> None:
    index = DedupIndex([_record()])

    duplicate = index.classify(_record(locator="row-2"))
    conflict = index.classify(_record(locator="row-3", amount=501))

    assert duplicate.decision is DedupDecision.DUPLICATE
    assert duplicate.reason == "EXTERNAL_ID_MATCH"
    assert conflict.decision is DedupDecision.NEEDS_REVIEW
    assert conflict.reason == "EXTERNAL_ID_CONFLICT"
    assert index.record_count == 1


def test_fingerprint_match_never_auto_deletes_or_registers() -> None:
    index = DedupIndex([_record()])
    candidate = _record(locator="row-2", external_id=None)

    result = index.classify(candidate)

    assert result.decision is DedupDecision.NEEDS_REVIEW
    assert result.reason == "FINGERPRINT_MATCH"
    assert index.record_count == 1
    with pytest.raises(Phase5Error, match="requires review"):
        index.register(candidate)


def test_fingerprint_is_stable_for_normalized_text() -> None:
    first = _fingerprint()
    second = TransactionFingerprint(
        occurred_on=first.occurred_on,
        amount_minor=first.amount_minor,
        counterparty=" merchant ",
        description="LUNCH",
        balance_minor=first.balance_minor,
    )
    assert first.digest_hex == second.digest_hex


@pytest.mark.parametrize(
    ("relation", "legs"),
    [
        (
            ReconciliationRelation.ONE_TO_ONE,
            [
                ReconciliationLeg("bank-1", "bank_cn", -500),
                ReconciliationLeg("wallet-1", "alipay", 500),
            ],
        ),
        (
            ReconciliationRelation.ONE_TO_MANY,
            [
                ReconciliationLeg("bank-1", "bank_cn", -1_000),
                ReconciliationLeg("wallet-1", "alipay", 600),
                ReconciliationLeg("wallet-2", "wechat", 400),
            ],
        ),
        (
            ReconciliationRelation.MANY_TO_ONE,
            [
                ReconciliationLeg("bank-1", "bank_cn", -600),
                ReconciliationLeg("bank-2", "bank_cn", -400),
                ReconciliationLeg("wallet-1", "alipay", 1_000),
            ],
        ),
    ],
)
def test_reconciliation_proposals_require_zero_sum_and_explicit_relation(
    relation: ReconciliationRelation, legs: list[ReconciliationLeg]
) -> None:
    proposal = ReconciliationProposal.propose(uuid4(), relation, legs)

    assert proposal.status is ReconciliationStatus.PROPOSED
    confirmed = proposal.confirm(actor="operator", reason="matched transfer evidence")
    assert confirmed.status is ReconciliationStatus.CONFIRMED
    assert confirmed.legs == proposal.legs
    assert sum(leg.amount_minor for leg in confirmed.legs) == 0


def test_reconciliation_rejects_bad_sum_duplicate_locator_and_second_decision() -> None:
    with pytest.raises(Phase5Error, match="net to zero"):
        ReconciliationProposal.propose(
            uuid4(),
            ReconciliationRelation.ONE_TO_ONE,
            [ReconciliationLeg("same", "bank_cn", -500), ReconciliationLeg("other", "alipay", 400)],
        )
    with pytest.raises(Phase5Error, match="locators"):
        ReconciliationProposal.propose(
            uuid4(),
            ReconciliationRelation.ONE_TO_ONE,
            [ReconciliationLeg("same", "bank_cn", -500), ReconciliationLeg("same", "alipay", 500)],
        )

    proposal = ReconciliationProposal.propose(
        uuid4(),
        ReconciliationRelation.ONE_TO_ONE,
        [ReconciliationLeg("bank", "bank_cn", -500), ReconciliationLeg("wallet", "alipay", 500)],
    ).reject(actor="operator", reason="insufficient evidence")
    with pytest.raises(Phase5Error, match="only a proposed"):
        proposal.confirm(actor="operator", reason="retry")


def test_suspense_requires_explicit_resolution_and_preserves_amount() -> None:
    item = SuspenseItem(uuid4(), "row-1", 123, SuspenseReason.UNKNOWN_COUNTERPARTY)
    resolved = item.resolve(
        account="expense:meals",
        actor="operator",
        reason="reviewed source evidence",
    )

    assert item.status is SuspenseStatus.OPEN
    assert resolved.status is SuspenseStatus.RESOLVED
    assert resolved.amount_minor == item.amount_minor
    assert resolved.resolution_account == "expense:meals"
    with pytest.raises(Phase5Error, match="only an open"):
        resolved.resolve(account="expense:other", actor="operator", reason="again")


def test_phase5_rejects_non_cny_and_unstorable_identifiers() -> None:
    with pytest.raises(Phase5Error, match="currency"):
        TransactionFingerprint(date(2026, 8, 24), 1, currency="USD")
    with pytest.raises(Phase5Error, match="account_key"):
        ExternalTransactionIdentity("bank_cn", "\x00", "tx")
