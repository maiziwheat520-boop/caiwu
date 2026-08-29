"""Deterministic review-risk projection for candidate facts.

Extraction confidence answers whether fields were read reliably.  It does not
answer whether a transaction is safe to bulk-confirm.  This module keeps that
second decision in Core so Web never has to infer risk from display text.
"""

from __future__ import annotations

from ledgerbridge.candidate_contract import ReviewRisk, ReviewRiskCode

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
_ACCOUNT_TERMS = ("银行", "储蓄卡", "信用卡", "账户余额", "零钱", "余额宝", "零钱通")


def derive_review_risks(
    *, source_system: str, category_code: str | None, summary: str
) -> tuple[ReviewRisk, ...]:
    """Return ordered, de-duplicated risks from immutable candidate facts."""

    risks: list[ReviewRisk] = []
    if source_system in _HOTEL_PAYOUT_SOURCES:
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

        if any(term in transaction_status for term in _UNSETTLED_TERMS):
            risks.append(
                ReviewRisk(
                    code=ReviewRiskCode.UNSETTLED_TRANSACTION,
                    message="交易尚未最终结算; 需人工确认最终状态",
                )
            )
        if any(
            term in transaction_category or term in transaction_status
            for term in _REVERSAL_TERMS
        ):
            risks.append(
                ReviewRisk(
                    code=ReviewRiskCode.REVERSAL_MATCH_REQUIRED,
                    message="退款或冲正需先关联原交易再确认",
                )
            )
        if any(term in transfer_text for term in _TRANSFER_TERMS):
            if any(term in transfer_text for term in _ACCOUNT_TERMS):
                risks.append(
                    ReviewRisk(
                        code=ReviewRiskCode.RELATED_ACCOUNT_STATEMENT_REQUIRED,
                        message="内部或关联账户资金流需提交并关联另一侧账户同期流水",
                    )
                )
            else:
                risks.append(
                    ReviewRisk(
                        code=ReviewRiskCode.TRANSFER_REVIEW_REQUIRED,
                        message="转账类交易需人工确认收付款方及资金性质",
                    )
                )

    return tuple(risks)
