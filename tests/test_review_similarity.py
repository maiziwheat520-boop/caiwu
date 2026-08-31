from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from ledgerbridge.candidate_contract import (
    CandidateAction,
    CandidateProjection,
    CandidateStatus,
    EvidenceKind,
    EvidenceReference,
    IngestChannel,
    ReviewRisk,
    ReviewRiskCode,
    ReviewSummary,
    SourceProjection,
)
from ledgerbridge.review_similarity import (
    GroupExclusionCode,
    RuleLearningBlockCode,
    SimilarityBasis,
    build_classification_groups,
    candidate_similarity,
    parse_exact_platform_summary,
)

ENTITY = UUID("6e6bb858-57ac-4ad9-84df-e3700c59604d")
OTHER_ENTITY = UUID("e3131945-32ae-4554-a9d5-12aa2e320220")
NOW = datetime(2026, 5, 31, 10, tzinfo=UTC)


def _candidate(
    *,
    short_id: str,
    summary: str,
    amount_minor: int,
    status: CandidateStatus = CandidateStatus.PENDING,
    entity_ref: UUID = ENTITY,
    source_system: str = "alipay_export",
    counterparty_ref: str | None = None,
    risks: tuple[ReviewRiskCode, ...] = (),
    confidence: int = 9_900,
    category_code: str = "ALIPAY_TRANSACTION_REVIEW",
) -> CandidateProjection:
    revision = 1 if status == CandidateStatus.PENDING else 2
    action = None
    if status == CandidateStatus.CONFIRMED:
        action = CandidateAction.CONFIRM
    elif status == CandidateStatus.IGNORED:
        action = CandidateAction.IGNORE
    return CandidateProjection(
        candidate_ref=uuid4(),
        short_id=short_id,
        revision=revision,
        status=status,
        entity_ref=entity_ref,
        business_unit_ref="hotel-a",
        business_unit_label="景怡酒店",
        category_code=category_code,
        category_label="支付宝交易复核",
        amount_minor=amount_minor,
        accounting_month="2026-05",
        summary=summary,
        counterparty_ref=counterparty_ref,
        counterparty_class="known_business" if counterparty_ref is not None else None,
        confidence_basis_points=confidence,
        source=SourceProjection(
            ingest_channel=IngestChannel.CONTROLLED_UPLOAD,
            source_system=source_system,
            source_event_ref=uuid4(),
            display_label="支付宝交易",
        ),
        evidence=(
            EvidenceReference(
                evidence_ref=uuid4(),
                kind=EvidenceKind.ATTACHMENT,
                media_type="text/csv",
                download_available=True,
            ),
        ),
        review_risks=tuple(ReviewRisk(code=code, message=f"risk {code.value}") for code in risks),
        review_summary=ReviewSummary(
            event_count=revision - 1,
            last_action=action,
            last_decided_at=NOW if action is not None else None,
            current_revision=revision,
        ),
        created_at=NOW,
        updated_at=NOW,
    )


def _yuebao_summary(date: str, *, direction: str = "收入", kind: str = "投资理财") -> str:
    return f"支付宝 | {date} | {direction} | {kind} | 中欧基金管理有限公司 | 余额宝 | 交易成功"


def test_exact_platform_parser_rejects_fuzzy_or_incomplete_summaries() -> None:
    parsed = parse_exact_platform_summary(_yuebao_summary("2026-05-04"))
    assert parsed is not None
    assert parsed.counterparty == "中欧基金管理有限公司"
    assert parsed.funding_instrument == "余额宝"
    assert parse_exact_platform_summary("支付宝 | 收入 | 中欧基金 | 余额宝") is None
    assert (
        parse_exact_platform_summary(
            "支付宝 | 2026-05-04 | 未知方向 | 投资理财 | 中欧基金 | 余额宝 | 交易成功"
        )
        is None
    )


def test_yuebao_income_groups_different_amounts_but_keeps_manual_risk() -> None:
    candidates = tuple(
        _candidate(
            short_id=f"C-YB{index:02d}",
            summary=_yuebao_summary(f"2026-05-{index + 1:02d}"),
            amount_minor=amount,
            risks=(ReviewRiskCode.TRANSFER_REVIEW_REQUIRED,),
        )
        for index, amount in enumerate((1, 2, 3))
    )
    groups = build_classification_groups(candidates)
    assert len(groups) == 1
    group = groups[0]
    assert group.conditions.counterparty_basis == SimilarityBasis.EXACT_PLATFORM_SUMMARY_V1
    assert group.batch_member_count == 3
    assert group.one_click_member_count == 0
    assert all(member.batch_eligible for member in group.members)
    assert RuleLearningBlockCode.PROVISIONAL_BASIS in group.rule_learning_blocks
    assert RuleLearningBlockCode.REVIEW_RISK_PRESENT in group.rule_learning_blocks


@pytest.mark.parametrize(
    ("changed", "value"),
    (
        ("summary", _yuebao_summary("2026-05-02", direction="支出")),
        ("summary", _yuebao_summary("2026-05-02", kind="基金赎回")),
        (
            "summary",
            "支付宝 | 2026-05-02 | 收入 | 投资理财 | 中欧基金管理有限公司 | 账户余额 | 交易成功",
        ),
        ("entity_ref", OTHER_ENTITY),
        ("source_system", "wechat_pay_export"),
    ),
)
def test_direction_type_entity_account_and_source_do_not_cross_group(
    changed: str,
    value: object,
) -> None:
    base = {
        "short_id": "C-BASE",
        "summary": _yuebao_summary("2026-05-01"),
        "amount_minor": 1,
    }
    other = {
        "short_id": "C-DIFF",
        "summary": _yuebao_summary("2026-05-02"),
        "amount_minor": -1 if changed == "summary" and "支出" in str(value) else 1,
        changed: value,
    }
    candidates = (_candidate(**base), _candidate(**other))
    assert len({candidate_similarity(item).group_ref for item in candidates}) == 2  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "risk",
    (
        ReviewRiskCode.REVERSAL_MATCH_REQUIRED,
        ReviewRiskCode.RELATED_ACCOUNT_STATEMENT_REQUIRED,
        ReviewRiskCode.HOTEL_PAYOUT_STATEMENT_REQUIRED,
        ReviewRiskCode.UNSETTLED_TRANSACTION,
        ReviewRiskCode.FUNDING_STATEMENT_REQUIRED,
    ),
)
def test_structural_risks_are_grouped_for_visibility_but_never_batch_confirmed(
    risk: ReviewRiskCode,
) -> None:
    candidates = tuple(
        _candidate(
            short_id=f"C-R{index:03d}",
            summary=_yuebao_summary(f"2026-05-{index + 1:02d}"),
            amount_minor=1,
            risks=(risk,),
        )
        for index in range(2)
    )
    group = build_classification_groups(candidates)[0]
    assert group.batch_member_count == 0
    assert all(GroupExclusionCode.STRUCTURAL_RISK in item.exclusion_codes for item in group.members)


def test_amount_is_not_a_key_but_large_outlier_is_a_separate_risk() -> None:
    candidates = tuple(
        _candidate(
            short_id=f"C-A{index:03d}",
            summary=_yuebao_summary(f"2026-05-{index + 1:02d}"),
            amount_minor=amount,
        )
        for index, amount in enumerate((1_000, 1_100, 1_000_000))
    )
    group = build_classification_groups(candidates)[0]
    assert len(group.members) == 3
    outlier = next(member for member in group.members if member.amount_minor == 1_000_000)
    assert outlier.amount_outlier
    assert GroupExclusionCode.AMOUNT_OUTLIER in outlier.exclusion_codes


def test_conflicting_terminal_history_blocks_rule_learning_without_hiding_pending_group() -> None:
    candidates = (
        _candidate(
            short_id="C-PEND",
            summary=_yuebao_summary("2026-05-01"),
            amount_minor=1,
        ),
        _candidate(
            short_id="C-CONF",
            summary=_yuebao_summary("2026-05-02"),
            amount_minor=1,
            status=CandidateStatus.CONFIRMED,
        ),
        _candidate(
            short_id="C-IGNR",
            summary=_yuebao_summary("2026-05-03"),
            amount_minor=1,
            status=CandidateStatus.IGNORED,
        ),
    )
    group = build_classification_groups(candidates)[0]
    assert group.batch_member_count == 1
    assert set(group.terminal_statuses) == {CandidateStatus.CONFIRMED, CandidateStatus.IGNORED}
    assert RuleLearningBlockCode.TERMINAL_DECISION_CONFLICT in group.rule_learning_blocks


def test_registry_backed_consistent_group_can_learn_from_confirmed_source() -> None:
    candidates = (
        _candidate(
            short_id="C-REG1",
            summary=_yuebao_summary("2026-05-01"),
            amount_minor=1,
            counterparty_ref="cp_zhongou_fund",
            status=CandidateStatus.CONFIRMED,
        ),
        _candidate(
            short_id="C-REG2",
            summary=_yuebao_summary("2026-05-02"),
            amount_minor=2,
            counterparty_ref="cp_zhongou_fund",
        ),
    )
    group = build_classification_groups(candidates)[0]
    assert group.conditions.counterparty_basis == SimilarityBasis.REGISTRY_COUNTERPARTY
    assert group.rule_learning_eligible
    assert group.rule_learning_blocks == ()
