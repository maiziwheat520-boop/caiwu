"""The personal finance rules moved out of the browser unchanged.

Each case here pins a branch that decides whether money appears on the page, so
a later simplification cannot quietly change what a total means.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from ledgerbridge.candidate_contract import CandidateProjection
from ledgerbridge.internal_read_service import SyntheticInternalReadService
from ledgerbridge.personal_finance_summary import build_personal_finance_summary

ENTITY = UUID("10000000-0000-4000-8000-000000000001")


def _candidate(
    summary: str,
    *,
    amount_minor: int | None = 10_000,
    status: str = "CONFIRMED",
    business_unit_ref: str | None = "review-2026-05",
    business_unit_label: str | None = "待校准账户",
    category_label: str | None = "餐饮",
    accounting_month: str | None = "2026-08",
    index: int = 1,
) -> CandidateProjection:
    template = SyntheticInternalReadService()._fixture.candidates[1].model_dump()
    moment = datetime(2026, 8, 1, tzinfo=UTC)
    # A terminal status must match the action that produced it, and a
    # first-revision PENDING candidate must carry no action at all.
    confirmed = status == "CONFIRMED"
    template.update(
        blockers=(),
        review_risks=(),
        revision=2 if confirmed else 1,
        review_summary={
            "event_count": 1 if confirmed else 0,
            "last_action": "CONFIRM" if confirmed else None,
            "last_decided_at": "2026-08-01T00:00:00+00:00" if confirmed else None,
            "current_revision": 2 if confirmed else 1,
        },
        candidate_ref=UUID(int=(1 << 100) + index),
        short_id=f"C-{index:04X}",
        entity_ref=ENTITY,
        status=status,
        summary=summary,
        amount_minor=amount_minor,
        business_unit_ref=business_unit_ref,
        business_unit_label=business_unit_label,
        category_label=category_label,
        category_code="MISC" if category_label else None,
        accounting_month=accounting_month,
        created_at=moment,
        updated_at=moment,
    )
    return CandidateProjection.model_validate(template)


def _personal(**kwargs: object) -> CandidateProjection:
    summary = kwargs.pop("summary", "支付宝|2026-08-03|收入|红包|张三|余额宝|备注")
    return _candidate(str(summary), **kwargs)  # type: ignore[arg-type]


def test_only_confirmed_candidates_count() -> None:
    summary = build_personal_finance_summary((_personal(status="PENDING"),))
    assert summary.entry_total == 0
    assert summary.pending_total == 1
    assert summary.excluded_count == 1


def test_a_business_scope_is_never_personal() -> None:
    for label in ("城南酒店", "某某公司", "机场门店"):
        summary = build_personal_finance_summary((_personal(business_unit_label=label),))
        assert summary.entry_total == 0, label


def test_the_stated_direction_overrides_the_sign() -> None:
    income = build_personal_finance_summary(
        (_personal(summary="支付宝|2026-08-03|收入|红包|张三|余额宝|备注", amount_minor=-500),)
    )
    expense = build_personal_finance_summary(
        (_personal(summary="支付宝|2026-08-03|支出|付款|张三|余额宝|备注", amount_minor=500),)
    )
    assert income.income_minor == 500 and income.expense_minor == 0
    assert expense.expense_minor == 500 and expense.income_minor == 0


def test_a_confirmed_candidate_cannot_have_an_unknown_amount() -> None:
    """Only confirmed candidates become entries, and Core will not confirm one
    that is missing an amount, a category, a month or a business unit.

    That is why the web boundary's habit of projecting a null amount as zero
    can never reach a personal finance total: the candidates it could affect
    are excluded one step earlier.
    """
    for field in ("amount_minor", "category_label", "accounting_month", "business_unit_ref"):
        with pytest.raises(ValidationError):
            _personal(**{field: None})


def test_a_transfer_seen_on_both_sides_is_counted_once_from_the_bank() -> None:
    platform = _personal(
        summary="支付宝|2026-08-03|支出|转账|张三|余额宝|备注", amount_minor=700, index=1
    )
    bank = _personal(
        summary="中国银行|2026-08-03|支出|转账|张三|储蓄卡|备注", amount_minor=700, index=2
    )
    summary = build_personal_finance_summary((platform, bank))

    assert summary.entry_total == 1
    assert summary.deduplicated_count == 1
    assert summary.expense_minor == 700
    assert summary.unassigned_entries[0].source_kind == "BANK"


def test_a_non_transfer_seen_on_both_sides_keeps_the_platform_row() -> None:
    platform = _personal(
        summary="微信|2026-08-03|收入|红包|李四|零钱|备注", amount_minor=900, index=3
    )
    bank = _personal(
        summary="中国银行|2026-08-03|收入|入账|李四|储蓄卡|备注", amount_minor=900, index=4
    )
    summary = build_personal_finance_summary((platform, bank))

    assert summary.entry_total == 1
    assert summary.unassigned_entries[0].source_kind == "PLATFORM"


def test_an_unmatched_surplus_survives_deduplication() -> None:
    rows = (
        _personal(summary="微信|2026-08-03|收入|红包|李四|零钱|备注", amount_minor=900, index=5),
        _personal(
            summary="中国银行|2026-08-03|收入|入账|李四|储蓄卡|备注", amount_minor=900, index=6
        ),
        _personal(
            summary="中国银行|2026-08-03|收入|入账|李四|储蓄卡|备注", amount_minor=900, index=7
        ),
    )
    summary = build_personal_finance_summary(rows)

    assert summary.entry_total == 2
    assert summary.deduplicated_count == 1


def test_shares_and_months_are_ordered_for_display() -> None:
    rows = (
        _personal(
            summary="微信|2026-07-03|收入|红包|李四|零钱|备注",
            amount_minor=100,
            category_label="礼金",
            accounting_month="2026-07",
            index=8,
        ),
        _personal(
            summary="微信|2026-08-03|收入|红包|王五|零钱|备注",
            amount_minor=300,
            category_label="餐饮",
            accounting_month="2026-08",
            index=9,
        ),
    )
    summary = build_personal_finance_summary(rows)

    assert [share.category for share in summary.category_shares] == ["餐饮", "礼金"]
    assert [share.basis_points for share in summary.category_shares] == [7500, 2500]
    assert [month.month for month in summary.monthly_totals] == ["2026-08", "2026-07"]


def test_the_pending_preview_is_bounded() -> None:
    rows = tuple(_personal(status="PENDING", index=index) for index in range(10, 20))
    summary = build_personal_finance_summary(rows)
    assert summary.pending_total == 10
    assert len(summary.pending_preview) == 4


def test_an_explicitly_personal_scope_is_counted_but_not_listed() -> None:
    # The page lists only the entries whose attribution still needs checking;
    # a clearly personal one still counts toward the totals.
    summary = build_personal_finance_summary(
        (_personal(business_unit_label="个人账户", business_unit_ref="personal-2026"),)
    )
    assert summary.entry_total == 1
    assert summary.unassigned_entries == ()
