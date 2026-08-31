"""Evidence-bound source decomposition for platform welfare-fund offsets.

These values are signed source-transaction components for Candidate creation,
not balanced ledger Postings.  The platform purchase remains the expense fact.
A separate welfare-income fact is emitted only when a linked funding statement
proves either the exact bank debit or the absence of any debit for a fully
offset purchase.  Free text is a classification signal only; it is never parsed
to guess money.  Double-entry balancing remains the downstream ledger's job.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class WelfareBenefitSplitError(ValueError):
    """The supplied facts cannot safely produce a welfare-income split."""


class WelfareBenefitSplitStatus(StrEnum):
    READY = "READY"
    AMOUNT_EXTRACTION_REQUIRED = "AMOUNT_EXTRACTION_REQUIRED"


class WelfareBenefitComponentKind(StrEnum):
    PURCHASE_EXPENSE = "PURCHASE_EXPENSE"
    WELFARE_INCOME = "WELFARE_INCOME"


@dataclass(frozen=True, slots=True)
class WelfareBenefitComponent:
    source_record_ref: UUID
    kind: WelfareBenefitComponentKind
    amount_minor: int


@dataclass(frozen=True, slots=True)
class WelfareBenefitSplit:
    status: WelfareBenefitSplitStatus
    funding_statement_ref: UUID | None
    purchase: WelfareBenefitComponent
    welfare_income: WelfareBenefitComponent | None

    @property
    def components(self) -> tuple[WelfareBenefitComponent, ...]:
        if self.welfare_income is None:
            return (self.purchase,)
        return (self.purchase, self.welfare_income)

    @property
    def source_cash_effect_minor(self) -> int:
        """Return the signed external cash effect represented by the source facts."""

        return sum(component.amount_minor for component in self.components)


def split_welfare_benefit(
    *,
    source_record_ref: UUID,
    purchase_amount_minor: int,
    summary: str,
    funding_statement_ref: UUID | None,
    matched_bank_debit_minor: int | None,
    bank_debit_absence_proven: bool,
) -> WelfareBenefitSplit:
    """Keep the purchase and derive welfare income from exact linked bank facts."""

    if not isinstance(source_record_ref, UUID):
        raise WelfareBenefitSplitError("source record reference is invalid")
    if isinstance(purchase_amount_minor, bool) or not isinstance(purchase_amount_minor, int):
        raise WelfareBenefitSplitError("purchase amount must be a negative integer minor amount")
    if purchase_amount_minor >= 0:
        raise WelfareBenefitSplitError("purchase amount must be a negative integer minor amount")
    if not isinstance(summary, str) or not summary.strip():
        raise WelfareBenefitSplitError("summary does not prove a welfare offset")
    normalized_summary = " ".join(unicodedata.normalize("NFKC", summary).split())
    if "福利金" not in normalized_summary or "抵扣" not in normalized_summary:
        raise WelfareBenefitSplitError("summary does not prove a welfare offset")
    if not isinstance(bank_debit_absence_proven, bool):
        raise WelfareBenefitSplitError("bank debit absence proof flag is invalid")
    if funding_statement_ref is not None and not isinstance(funding_statement_ref, UUID):
        raise WelfareBenefitSplitError("funding statement reference is invalid")

    purchase = WelfareBenefitComponent(
        source_record_ref=source_record_ref,
        kind=WelfareBenefitComponentKind.PURCHASE_EXPENSE,
        amount_minor=purchase_amount_minor,
    )
    if matched_bank_debit_minor is None and not bank_debit_absence_proven:
        return WelfareBenefitSplit(
            status=WelfareBenefitSplitStatus.AMOUNT_EXTRACTION_REQUIRED,
            funding_statement_ref=funding_statement_ref,
            purchase=purchase,
            welfare_income=None,
        )
    if funding_statement_ref is None:
        raise WelfareBenefitSplitError(
            "an exact welfare split requires a linked funding statement reference"
        )
    if matched_bank_debit_minor is not None and bank_debit_absence_proven:
        raise WelfareBenefitSplitError(
            "bank debit and proven debit absence cannot both be supplied"
        )

    purchase_magnitude = -purchase_amount_minor
    if bank_debit_absence_proven:
        welfare_income_minor = purchase_magnitude
    else:
        if (
            isinstance(matched_bank_debit_minor, bool)
            or not isinstance(matched_bank_debit_minor, int)
            or matched_bank_debit_minor >= 0
        ):
            raise WelfareBenefitSplitError("matched bank debit must be a negative integer")
        bank_debit_magnitude = -matched_bank_debit_minor
        if bank_debit_magnitude > purchase_magnitude:
            raise WelfareBenefitSplitError("matched bank debit exceeds purchase amount")
        welfare_income_minor = purchase_magnitude - bank_debit_magnitude
        if welfare_income_minor == 0:
            raise WelfareBenefitSplitError("bank facts do not prove positive welfare income")

    welfare_income = WelfareBenefitComponent(
        source_record_ref=source_record_ref,
        kind=WelfareBenefitComponentKind.WELFARE_INCOME,
        amount_minor=welfare_income_minor,
    )
    return WelfareBenefitSplit(
        status=WelfareBenefitSplitStatus.READY,
        funding_statement_ref=funding_statement_ref,
        purchase=purchase,
        welfare_income=welfare_income,
    )
