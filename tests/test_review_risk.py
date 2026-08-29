from ledgerbridge.candidate_contract import ReviewRiskCode
from ledgerbridge.review_risk import derive_review_risks


def _codes(*, source: str, category: str, summary: str) -> set[ReviewRiskCode]:
    return {
        risk.code
        for risk in derive_review_risks(
            source_system=source,
            category_code=category,
            summary=summary,
        )
    }


def test_ordinary_high_confidence_purchase_has_no_review_risk() -> None:
    assert not _codes(
        source="wechat_pay_export",
        category="WECHAT_TRANSACTION_REVIEW",
        summary="微信 | 2026-05-01 | 支出 | 商户消费 | 文记虾一跳 | 零钱通 | 支付成功",
    )


def test_bank_funded_purchase_requires_funding_statement() -> None:
    codes = _codes(
        source="wechat_pay_export",
        category="WECHAT_TRANSACTION_REVIEW",
        summary="微信 | 2026-05-02 | 支出 | 商户消费 | 通信商 | 中国银行储蓄卡(2574) | 支付成功",
    )
    assert codes == {ReviewRiskCode.FUNDING_STATEMENT_REQUIRED}


def test_platform_balance_purchase_does_not_require_a_second_statement() -> None:
    assert not _codes(
        source="alipay_export",
        category="ALIPAY_TRANSACTION_REVIEW",
        summary="支付宝 | 2026-05-02 | 支出 | 日用百货 | 便利店 | 账户余额 | 交易成功",
    )


def test_related_account_transfer_requires_the_other_statement() -> None:
    codes = _codes(
        source="alipay_export",
        category="ALIPAY_TRANSACTION_REVIEW",
        summary="支付宝 | 2026-05-08 | 支出 | 投资理财 | 网商银行 | 账户余额 | 交易成功",
    )
    assert codes == {ReviewRiskCode.RELATED_ACCOUNT_STATEMENT_REQUIRED}


def test_person_transfer_stays_in_manual_review() -> None:
    codes = _codes(
        source="wechat_pay_export",
        category="WECHAT_TRANSACTION_REVIEW",
        summary="微信 | 2026-05-13 | 支出 | 转账 | 某收款人 | / | 对方已收钱",
    )
    assert codes == {ReviewRiskCode.TRANSFER_REVIEW_REQUIRED}


def test_hotel_payout_requires_matching_bank_statement() -> None:
    codes = _codes(
        source="hotel_bill_ocr",
        category="PHOTO_RECONCILIATION",
        summary="OCR账单待复核: CTRIP_EBOOKING 66784978:2026-05-18:2026-05-24",
    )
    assert codes == {ReviewRiskCode.HOTEL_PAYOUT_STATEMENT_REQUIRED}


def test_refund_and_unsettled_status_are_not_bulk_eligible() -> None:
    codes = _codes(
        source="wechat_pay_export",
        category="WECHAT_TRANSACTION_REVIEW",
        summary="微信 | 2026-05-13 | 已退款 | 商户消费-退款 | 商户 | 信用卡 | 付款中",
    )
    assert codes == {
        ReviewRiskCode.FUNDING_STATEMENT_REQUIRED,
        ReviewRiskCode.REVERSAL_MATCH_REQUIRED,
        ReviewRiskCode.UNSETTLED_TRANSACTION,
    }
