"""Deterministic review-risk projection for candidate facts.

Extraction confidence answers whether fields were read reliably.  It does not
answer whether a transaction is safe to bulk-confirm.  This module keeps that
second decision in Core so Web never has to infer risk from display text.
"""

from __future__ import annotations

from ledgerbridge.candidate_contract import ReviewRisk, ReviewRiskCode
from ledgerbridge.counterparty import CounterpartyClass

_HOTEL_PAYOUT_SOURCES = frozenset(
    {
        "hotel_photo_reconciliation",
        "hotel_bill_ocr",
    }
)
_PLATFORM_CATEGORIES = frozenset(
    {
        "WECHAT_TRANSACTION_REVIEW",
        "ALIPAY_TRANSACTION_REVIEW",
    }
)
_TRANSFER_TERMS = ("转账", "提现", "投资理财", "信用卡还款", "信用借还", "余额互转")
_REVERSAL_TERMS = ("退款", "冲正", "撤销")
_UNSETTLED_TERMS = ("付款中", "生成中", "未出账", "交易关闭", "付款异常")
_EXTERNAL_FUNDING_TERMS = ("银行", "储蓄卡", "信用卡")
_PLATFORM_INTERNAL_MOVEMENT_TERMS = ("余额互转", "充值", "赎回", "花呗还款")
_PLATFORM_SUBACCOUNT_TERMS = ("账户余额", "余额宝", "花呗", "零钱", "零钱通")


def derive_review_risks(
    *,
    source_system: str,
    category_code: str | None,
    summary: str,
    satisfied_codes: frozenset[ReviewRiskCode] = frozenset(),
    counterparty_class: CounterpartyClass | None = None,
    bilateral_statement_evidence: bool = False,
) -> tuple[ReviewRisk, ...]:
    """Return ordered, de-duplicated risks from immutable candidate facts."""

    risks: list[ReviewRisk] = []
    if (
        source_system in _HOTEL_PAYOUT_SOURCES
        and ReviewRiskCode.HOTEL_PAYOUT_STATEMENT_REQUIRED not in satisfied_codes
    ):
        risks.append(
            ReviewRisk(
                code=ReviewRiskCode.HOTEL_PAYOUT_STATEMENT_REQUIRED,
                message="酒店平台结算或提现需关联收款银行流水; 未匹配前保留人工审核",
            )
        )

    if category_code in _PLATFORM_CATEGORIES:
        parts = tuple(part.strip() for part in summary.split(" | "))
        transaction_category = parts[3] if len(parts) > 3 else summary
        counterparty = parts[4] if len(parts) > 4 else ""
        payment_method = parts[5] if len(parts) > 5 else ""
        transaction_status = parts[6] if len(parts) > 6 else ""
        transfer_text = " ".join((transaction_category, counterparty, payment_method))

        if (
            any(term in payment_method for term in _EXTERNAL_FUNDING_TERMS)
            and ReviewRiskCode.FUNDING_STATEMENT_REQUIRED not in satisfied_codes
        ):
            risks.append(
                ReviewRisk(
                    code=ReviewRiskCode.FUNDING_STATEMENT_REQUIRED,
                    message="平台交易使用银行或信用账户支付; 需关联资金账户明细后再确认",
                )
            )

        if (
            any(term in transaction_status for term in _UNSETTLED_TERMS)
            and ReviewRiskCode.UNSETTLED_TRANSACTION not in satisfied_codes
        ):
            risks.append(
                ReviewRisk(
                    code=ReviewRiskCode.UNSETTLED_TRANSACTION,
                    message="交易尚未最终结算; 需人工确认最终状态",
                )
            )
        if (
            any(
                term in transaction_category or term in transaction_status
                for term in _REVERSAL_TERMS
            )
            and ReviewRiskCode.REVERSAL_MATCH_REQUIRED not in satisfied_codes
        ):
            risks.append(
                ReviewRisk(
                    code=ReviewRiskCode.REVERSAL_MATCH_REQUIRED,
                    message="退款或冲正需先关联原交易再确认",
                )
            )
        platform_internal = (
            any(term in transaction_category for term in _PLATFORM_INTERNAL_MOVEMENT_TERMS)
            and any(term in transfer_text for term in _PLATFORM_SUBACCOUNT_TERMS)
            and not any(term in payment_method for term in _EXTERNAL_FUNDING_TERMS)
        )
        if any(term in transfer_text for term in _TRANSFER_TERMS) and not platform_internal:
            if (
                counterparty_class
                in {
                    CounterpartyClass.SELF_MANAGED,
                    CounterpartyClass.RELATED_PARTY,
                }
                and not bilateral_statement_evidence
                and ReviewRiskCode.RELATED_ACCOUNT_STATEMENT_REQUIRED not in satisfied_codes
            ):
                risks.append(
                    ReviewRisk(
                        code=ReviewRiskCode.RELATED_ACCOUNT_STATEMENT_REQUIRED,
                        message="内部或关联账户资金流需提交并关联另一侧账户同期流水",
                    )
                )
            elif ReviewRiskCode.TRANSFER_REVIEW_REQUIRED not in satisfied_codes:
                risks.append(
                    ReviewRisk(
                        code=ReviewRiskCode.TRANSFER_REVIEW_REQUIRED,
                        message="转账类交易需人工确认收付款方及资金性质",
                    )
                )

    return tuple(risks)
