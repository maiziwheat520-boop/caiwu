from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest

from ledgerbridge.reconciliation import (
    ConcurrentDedupIndex,
    DedupDecision,
    DedupIndex,
    DedupRecord,
    ExternalTransactionIdentity,
    ManagedAccount,
    ManagedAccountRegistry,
    ManagedTransferKind,
    Phase5Error,
    ReconciliationLeg,
    ReconciliationProposal,
    ReconciliationRelation,
    ReconciliationStatus,
    SuspenseItem,
    SuspenseReason,
    SuspenseStatus,
    TransactionFingerprint,
    TransferEvidenceStatus,
    TransferObservation,
    _normalize_optional,
    assess_managed_transfer,
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


def test_concurrent_admission_has_one_winner_for_equivalent_candidates() -> None:
    from concurrent.futures import ThreadPoolExecutor

    index = ConcurrentDedupIndex()
    records = [_record(locator=f"parallel-{number}") for number in range(16)]
    with ThreadPoolExecutor(max_workers=len(records)) as pool:
        results = tuple(pool.map(index.admit, records))

    assert sum(result.decision is DedupDecision.NEW for result in results) == 1
    assert sum(result.decision is DedupDecision.DUPLICATE for result in results) == 15
    assert index.record_count == 1


def test_concurrent_admission_keeps_conflicts_reviewable() -> None:
    from concurrent.futures import ThreadPoolExecutor

    index = ConcurrentDedupIndex()
    records = [_record(locator=f"conflict-{number}", amount=500 + number) for number in range(8)]
    with ThreadPoolExecutor(max_workers=len(records)) as pool:
        results = tuple(pool.map(index.admit, records))

    assert sum(result.decision is DedupDecision.NEW for result in results) == 1
    assert sum(result.decision is DedupDecision.NEEDS_REVIEW for result in results) == 7
    assert all(
        result.reason == "EXTERNAL_ID_CONFLICT"
        for result in results
        if result.decision is DedupDecision.NEEDS_REVIEW
    )
    assert index.record_count == 1


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


def test_phase5_rejects_conflicts_invalid_cardinality_and_bad_amounts() -> None:
    with pytest.raises(Phase5Error, match="canonical"):
        ExternalTransactionIdentity("Bank CN", "assets:bank", "tx")

    index = DedupIndex([_record()])
    same_locator = index.classify(_record())
    assert same_locator.decision is DedupDecision.DUPLICATE
    assert same_locator.reason == "RECORD_LOCATOR_MATCH"
    conflict = index.classify(_record(locator="row-1", amount=501))
    assert conflict.decision is DedupDecision.NEEDS_REVIEW
    assert conflict.reason == "RECORD_LOCATOR_CONFLICT"

    with pytest.raises(Phase5Error, match="canonical"):
        ReconciliationLeg("row", "Bank CN", -1)
    with pytest.raises(Phase5Error, match="currency"):
        ReconciliationLeg("row", "bank_cn", -1, currency="USD")
    with pytest.raises(Phase5Error, match="integer"):
        ReconciliationLeg("row", "bank_cn", True)
    with pytest.raises(Phase5Error, match="must not be zero"):
        ReconciliationLeg("row", "bank_cn", 0)
    with pytest.raises(Phase5Error, match="signed 64-bit"):
        ReconciliationLeg("row", "bank_cn", 2**63)

    with pytest.raises(Phase5Error, match="out of bounds"):
        ReconciliationProposal.propose(uuid4(), ReconciliationRelation.ONE_TO_ONE, [])
    with pytest.raises(Phase5Error, match="cardinality"):
        ReconciliationProposal.propose(
            uuid4(),
            ReconciliationRelation.ONE_TO_MANY,
            [ReconciliationLeg("one", "bank_cn", -1), ReconciliationLeg("two", "alipay", 1)],
        )
    with pytest.raises(Phase5Error, match="proposed"):
        ReconciliationProposal(
            uuid4(),
            ReconciliationRelation.ONE_TO_ONE,
            (
                ReconciliationLeg("one", "bank_cn", -1),
                ReconciliationLeg("two", "alipay", 1),
            ),
            decision_actor="operator",
        )

    rejected = ReconciliationProposal.propose(
        uuid4(),
        ReconciliationRelation.ONE_TO_ONE,
        [ReconciliationLeg("bank", "bank_cn", -1), ReconciliationLeg("wallet", "alipay", 1)],
    ).reject(actor="operator", reason="not enough")
    with pytest.raises(Phase5Error, match="only a proposed"):
        rejected.reject(actor="operator", reason="again")

    with pytest.raises(Phase5Error, match="open Suspense"):
        SuspenseItem(
            uuid4(),
            "row",
            1,
            SuspenseReason.BALANCE_GAP,
            resolution_account="expense:x",
        )
    with pytest.raises(Phase5Error, match="different account"):
        SuspenseItem(
            uuid4(),
            "row",
            1,
            SuspenseReason.BALANCE_GAP,
            suspense_account="suspense:default",
            status=SuspenseStatus.RESOLVED,
            resolution_account="suspense:default",
            resolution_actor="operator",
            resolution_reason="mistake",
        )
    assert _normalize_optional(None) is None


def test_statement_backed_accounts_require_the_other_side_before_internal_match() -> None:
    owner = uuid4()
    boc = ManagedAccount(
        "bank:boc:2574",
        owner,
        ("boc-2574", "中国银行2574"),
        (uuid4(),),
    )
    abc = ManagedAccount(
        "bank:abc:7788",
        owner,
        ("abc-7788", "农业银行7788"),
        (uuid4(),),
    )
    registry = ManagedAccountRegistry((boc, abc))
    outgoing = TransferObservation(
        "boc-row-1", boc.account_key, "农业银行7788", date(2026, 5, 18), -100_000, (uuid4(),)
    )

    missing = assess_managed_transfer(outgoing, registry=registry)

    assert missing.kind is ManagedTransferKind.INTERNAL
    assert missing.status is TransferEvidenceStatus.COUNTERPARTY_STATEMENT_REQUIRED
    incoming = TransferObservation(
        "abc-row-9", abc.account_key, "中国银行2574", date(2026, 5, 19), 100_000, (uuid4(),)
    )
    matched = assess_managed_transfer(
        outgoing, registry=registry, possible_counterparts=(incoming,)
    )
    assert matched.status is TransferEvidenceStatus.BILATERAL_EVIDENCE_MATCHED
    assert matched.matched_record_locator == "abc-row-9"


def test_managed_transfer_registry_uses_entity_uuid_as_the_only_owner_identity() -> None:
    account = ManagedAccount(
        account_key="bank:synthetic:1234",
        owner_entity_ref=uuid4(),
        aliases=("synthetic-1234",),
        statement_evidence_refs=(uuid4(),),
    )

    assert isinstance(account.owner_entity_ref, UUID)
    assert not hasattr(account, "owner_kind")


def test_company_accounts_are_managed_but_cross_company_transfer_is_related_party() -> None:
    company_a = ManagedAccount(
        "bank:a:1111",
        uuid4(),
        ("a-1111",),
        (uuid4(),),
    )
    company_b = ManagedAccount(
        "bank:b:2222",
        uuid4(),
        ("b-2222",),
        (uuid4(),),
    )
    registry = ManagedAccountRegistry((company_a, company_b))
    transfer = TransferObservation(
        "a-row", company_a.account_key, "b-2222", date(2026, 5, 20), -5_000, (uuid4(),)
    )

    result = assess_managed_transfer(transfer, registry=registry)

    assert result.kind is ManagedTransferKind.RELATED_PARTY
    assert result.status is TransferEvidenceStatus.COUNTERPARTY_STATEMENT_REQUIRED


def test_account_without_statement_evidence_cannot_enter_managed_registry() -> None:
    with pytest.raises(Phase5Error, match="statement evidence"):
        ManagedAccount(
            "bank:test:0000",
            uuid4(),
            ("test-0000",),
            (),
        )
