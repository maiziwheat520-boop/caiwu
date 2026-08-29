from ledgerbridge.candidate_contract import ReviewRiskCode
from ledgerbridge.counterparty import CounterpartyClass
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


def test_investment_counterparty_is_not_guessed_to_be_a_managed_account() -> None:
    codes = _codes(
        source="alipay_export",
        category="ALIPAY_TRANSACTION_REVIEW",
        summary="支付宝 | 2026-05-08 | 支出 | 投资理财 | 网商银行 | 账户余额 | 交易成功",
    )
    assert codes == {ReviewRiskCode.TRANSFER_REVIEW_REQUIRED}


def test_merchant_name_containing_bank_does_not_imply_related_account() -> None:
    assert not _codes(
        source="alipay_export",
        category="ALIPAY_TRANSACTION_REVIEW",
        summary="支付宝 | 2026-05-08 | 支出 | 日常消费 | 银行主题餐厅 | 账户余额 | 交易成功",
    )


def test_related_party_registry_match_requires_the_other_statement() -> None:
    risks = derive_review_risks(
        source_system="alipay_export",
        category_code="ALIPAY_TRANSACTION_REVIEW",
        summary="支付宝 | 2026-05-08 | 支出 | 转账 | 已登记账户 | 账户余额 | 交易成功",
        counterparty_class=CounterpartyClass.RELATED_PARTY,
    )

    assert {risk.code for risk in risks} == {ReviewRiskCode.RELATED_ACCOUNT_STATEMENT_REQUIRED}


def test_transfer_counterparty_classes_do_not_change_the_safe_default() -> None:
    summary = "微信 | 2026-05-13 | 支出 | 转账 | 某收款人 | / | 对方已收钱"
    for counterparty_class in (
        CounterpartyClass.KNOWN_BUSINESS,
        CounterpartyClass.UNKNOWN,
    ):
        risks = derive_review_risks(
            source_system="wechat_pay_export",
            category_code="WECHAT_TRANSACTION_REVIEW",
            summary=summary,
            counterparty_class=counterparty_class,
        )
        assert {risk.code for risk in risks} == {ReviewRiskCode.TRANSFER_REVIEW_REQUIRED}


def test_self_managed_transfer_with_bilateral_evidence_still_needs_nature_review() -> None:
    risks = derive_review_risks(
        source_system="alipay_export",
        category_code="ALIPAY_TRANSACTION_REVIEW",
        summary="支付宝 | 2026-05-08 | 支出 | 转账 | 自有账户 | 账户余额 | 交易成功",
        counterparty_class=CounterpartyClass.SELF_MANAGED,
        bilateral_statement_evidence=True,
    )

    assert {risk.code for risk in risks} == {ReviewRiskCode.TRANSFER_REVIEW_REQUIRED}


def test_person_transfer_stays_in_manual_review() -> None:
    codes = _codes(
        source="wechat_pay_export",
        category="WECHAT_TRANSACTION_REVIEW",
        summary="微信 | 2026-05-13 | 支出 | 转账 | 某收款人 | / | 对方已收钱",
    )
    assert codes == {ReviewRiskCode.TRANSFER_REVIEW_REQUIRED}


def test_huabei_purchases_are_covered_by_the_alipay_statement() -> None:
    assert not _codes(
        source="alipay_export",
        category="ALIPAY_TRANSACTION_REVIEW",
        summary="支付宝 | 2026-05-02 | 支出 | 生活缴费 | 供电 | 花呗 | 交易成功",
    )
    assert not _codes(
        source="alipay_export",
        category="ALIPAY_TRANSACTION_REVIEW",
        summary="支付宝 | 2026-05-03 | 支出 | 医疗健康 | 药店 | 花呗 | 交易成功",
    )


def test_yuebao_fund_income_is_not_an_internal_transfer() -> None:
    assert not _codes(
        source="alipay_export",
        category="ALIPAY_TRANSACTION_REVIEW",
        summary="支付宝 | 2026-05-04 | 收入 | 基金收益 | 余额宝 | 余额宝 | 交易成功",
    )


def test_platform_internal_balance_movements_do_not_need_external_statements() -> None:
    assert not _codes(
        source="alipay_export",
        category="ALIPAY_TRANSACTION_REVIEW",
        summary="支付宝 | 2026-05-05 | 不计收支 | 余额互转 | 余额宝 | 账户余额 | 交易成功",
    )
    assert not _codes(
        source="wechat_pay_export",
        category="WECHAT_TRANSACTION_REVIEW",
        summary="微信 | 2026-05-05 | 不计收支 | 余额互转 | 零钱通 | 零钱 | 交易成功",
    )


def test_hotel_payout_requires_matching_bank_statement() -> None:
    codes = _codes(
        source="hotel_bill_ocr",
        category="PHOTO_RECONCILIATION",
        summary="OCR账单待复核: CTRIP_EBOOKING 66784978:2026-05-18:2026-05-24",
    )
    assert codes == {ReviewRiskCode.HOTEL_PAYOUT_STATEMENT_REQUIRED}


def test_audited_hotel_bank_match_satisfies_only_the_hotel_risk() -> None:
    risks = derive_review_risks(
        source_system="hotel_bill_ocr",
        category_code="PHOTO_RECONCILIATION",
        summary="OCR账单待复核: CTRIP_EBOOKING 66784978:2026-05-18:2026-05-24",
        satisfied_codes=frozenset({ReviewRiskCode.HOTEL_PAYOUT_STATEMENT_REQUIRED}),
    )
    assert risks == ()


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


def test_audited_partial_refund_match_satisfies_only_the_reversal_risk() -> None:
    risks = derive_review_risks(
        source_system="wechat_pay_export",
        category_code="WECHAT_TRANSACTION_REVIEW",
        summary="微信 | 2026-05-03 | 退款收入 | 商户消费-退款 | 商户 | / | 已退款(￥49.95)",
        satisfied_codes=frozenset({ReviewRiskCode.REVERSAL_MATCH_REQUIRED}),
    )

    assert risks == ()
