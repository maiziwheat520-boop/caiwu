from __future__ import annotations

from uuid import UUID

import pytest

from ledgerbridge.welfare_benefit_split import (
    WelfareBenefitComponentKind,
    WelfareBenefitSplitError,
    WelfareBenefitSplitStatus,
    split_welfare_benefit,
)

SOURCE_REF = UUID("10000000-0000-4000-8000-000000000001")
STATEMENT_REF = UUID("20000000-0000-4000-8000-000000000001")


def test_proven_full_welfare_offset_keeps_purchase_and_adds_equal_income() -> None:
    result = split_welfare_benefit(
        source_record_ref=SOURCE_REF,
        purchase_amount_minor=-2_999,
        summary="网商银行储蓄卡与网商银行福利金抵扣",
        funding_statement_ref=STATEMENT_REF,
        matched_bank_debit_minor=None,
        bank_debit_absence_proven=True,
    )

    assert result.status is WelfareBenefitSplitStatus.READY
    assert result.purchase.kind is WelfareBenefitComponentKind.PURCHASE_EXPENSE
    assert result.purchase.amount_minor == -2_999
    assert result.welfare_income is not None
    assert result.welfare_income.kind is WelfareBenefitComponentKind.WELFARE_INCOME
    assert result.welfare_income.amount_minor == 2_999
    assert result.purchase.source_record_ref == SOURCE_REF
    assert result.welfare_income.source_record_ref == SOURCE_REF
    assert result.source_cash_effect_minor == 0


def test_partial_welfare_offset_is_platform_total_minus_bank_debit() -> None:
    result = split_welfare_benefit(
        source_record_ref=SOURCE_REF,
        purchase_amount_minor=-10_000,
        summary="购物, 福利金抵扣",
        funding_statement_ref=STATEMENT_REF,
        matched_bank_debit_minor=-7_500,
        bank_debit_absence_proven=False,
    )

    assert result.status is WelfareBenefitSplitStatus.READY
    assert result.purchase.amount_minor == -10_000
    assert result.welfare_income is not None
    assert result.welfare_income.amount_minor == 2_500
    assert result.source_cash_effect_minor == -7_500


def test_welfare_summary_without_exact_bank_fact_waits_for_extraction() -> None:
    result = split_welfare_benefit(
        source_record_ref=SOURCE_REF,
        purchase_amount_minor=-10_000,
        summary="购物, 福利金抵扣 20.00 元",
        funding_statement_ref=None,
        matched_bank_debit_minor=None,
        bank_debit_absence_proven=False,
    )

    assert result.status is WelfareBenefitSplitStatus.AMOUNT_EXTRACTION_REQUIRED
    assert result.purchase.amount_minor == -10_000
    assert result.welfare_income is None
    assert result.components == (result.purchase,)


def test_bank_debit_requires_a_linked_statement() -> None:
    with pytest.raises(WelfareBenefitSplitError, match="statement reference"):
        split_welfare_benefit(
            source_record_ref=SOURCE_REF,
            purchase_amount_minor=-10_000,
            summary="福利金抵扣",
            funding_statement_ref=None,
            matched_bank_debit_minor=-7_500,
            bank_debit_absence_proven=False,
        )


def test_proven_absence_requires_a_linked_statement() -> None:
    with pytest.raises(WelfareBenefitSplitError, match="statement reference"):
        split_welfare_benefit(
            source_record_ref=SOURCE_REF,
            purchase_amount_minor=-10_000,
            summary="福利金抵扣",
            funding_statement_ref=None,
            matched_bank_debit_minor=None,
            bank_debit_absence_proven=True,
        )


def test_debit_and_proven_absence_are_mutually_exclusive() -> None:
    with pytest.raises(WelfareBenefitSplitError, match="cannot both"):
        split_welfare_benefit(
            source_record_ref=SOURCE_REF,
            purchase_amount_minor=-10_000,
            summary="福利金抵扣",
            funding_statement_ref=STATEMENT_REF,
            matched_bank_debit_minor=-7_500,
            bank_debit_absence_proven=True,
        )


@pytest.mark.parametrize("invalid", [True, False, 0, 1, 1.5, "-7500"])
def test_invalid_bank_debit_is_rejected(invalid: object) -> None:
    with pytest.raises(WelfareBenefitSplitError, match="bank debit"):
        split_welfare_benefit(
            source_record_ref=SOURCE_REF,
            purchase_amount_minor=-10_000,
            summary="福利金抵扣",
            funding_statement_ref=STATEMENT_REF,
            matched_bank_debit_minor=invalid,  # type: ignore[arg-type]
            bank_debit_absence_proven=False,
        )


def test_bank_debit_cannot_exceed_preserved_purchase() -> None:
    with pytest.raises(WelfareBenefitSplitError, match="exceeds purchase"):
        split_welfare_benefit(
            source_record_ref=SOURCE_REF,
            purchase_amount_minor=-10_000,
            summary="福利金抵扣",
            funding_statement_ref=STATEMENT_REF,
            matched_bank_debit_minor=-10_001,
            bank_debit_absence_proven=False,
        )


def test_equal_bank_debit_does_not_prove_positive_welfare_income() -> None:
    with pytest.raises(WelfareBenefitSplitError, match="positive welfare income"):
        split_welfare_benefit(
            source_record_ref=SOURCE_REF,
            purchase_amount_minor=-10_000,
            summary="福利金抵扣",
            funding_statement_ref=STATEMENT_REF,
            matched_bank_debit_minor=-10_000,
            bank_debit_absence_proven=False,
        )


def test_non_welfare_summary_cannot_create_welfare_income() -> None:
    with pytest.raises(WelfareBenefitSplitError, match="summary does not prove"):
        split_welfare_benefit(
            source_record_ref=SOURCE_REF,
            purchase_amount_minor=-10_000,
            summary="普通购物",
            funding_statement_ref=STATEMENT_REF,
            matched_bank_debit_minor=-7_500,
            bank_debit_absence_proven=False,
        )


@pytest.mark.parametrize("invalid", [True, False, 0, 1, 1.5, "-10000"])
def test_purchase_must_be_a_negative_integer_minor_amount(invalid: object) -> None:
    with pytest.raises(WelfareBenefitSplitError, match="purchase amount"):
        split_welfare_benefit(
            source_record_ref=SOURCE_REF,
            purchase_amount_minor=invalid,  # type: ignore[arg-type]
            summary="福利金抵扣",
            funding_statement_ref=None,
            matched_bank_debit_minor=None,
            bank_debit_absence_proven=False,
        )
