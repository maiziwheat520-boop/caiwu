from pathlib import Path

import pytest

from ledgerbridge.bill_preprocessing import (
    BillSourceKind,
    OcrToken,
    PreprocessingBlocker,
    PreprocessingError,
    classify_source,
    extract_bills,
    preprocess_image,
)

_BOX = ((0, 0), (10, 0), (10, 10), (0, 10))


def _tokens(*values: str, confidence: int = 9900) -> tuple[OcrToken, ...]:
    return tuple(OcrToken(value, confidence, _BOX) for value in values)


def test_mobile_meituan_ocr_extracts_bill_id_period_amount_and_status() -> None:
    tokens = _tokens(
        "预付账单",
        "测试商务公寓(车站店)",
        "2026年",
        "账单周期:05/18-05/24",
        "7625.20",
        "预订",
        "账单ID:2056095067379466251",
        "美团已付款",
    )

    kind = classify_source(tokens)
    result = extract_bills("bill.png", kind, tokens)

    assert kind is BillSourceKind.MEITUAN_MOBILE
    assert result.bills[0].bill_id == "2056095067379466251"
    assert result.bills[0].period_start == "2026-05-18"
    assert result.bills[0].period_end == "2026-05-24"
    assert result.bills[0].amount_minor == 762_520
    assert result.bills[0].review_ready is True


def test_ctrip_ocr_masks_account_and_uses_period_bound_source_id() -> None:
    tokens = _tokens(
        "eBooking.",
        "测试酒店(车站店)",
        "结算账期",
        "银行账号:62170000000012574",
        "66784978",
        "2026-05-18至2026-05-24",
        "152",
        "RMB 16,083.80",
        "RMB 0.00",
        "已付款",
    )

    result = extract_bills("ctrip.png", classify_source(tokens), tokens)

    assert result.source_kind is BillSourceKind.CTRIP_EBOOKING
    assert result.bills[0].bill_id == "66784978:2026-05-18:2026-05-24"
    assert result.bills[0].account_last4 == "2574"
    assert result.bills[0].amount_minor == 1_608_380
    assert result.bills[0].review_ready is True


def test_incomplete_desktop_period_stays_blocked_instead_of_being_guessed() -> None:
    tokens = _tokens(
        "美团酒店商家",
        "付款单ID",
        "测试酒店",
        "37606491",
        "3",
        "2026/05/25 至 2026/05/3",
        "预订",
        "87654.40",
        "付款成功",
    )

    result = extract_bills("desktop.png", classify_source(tokens), tokens)

    assert result.bills[0].bill_id == "376064913"
    assert result.bills[0].period_start is None
    assert result.bills[0].amount_minor == 8_765_440
    assert PreprocessingBlocker.MISSING_PERIOD in result.bills[0].blockers


def test_bank_summary_is_context_only_and_low_confidence_fields_are_blocked() -> None:
    summary = _tokens("年度", "月收入均值")
    result = extract_bills("summary.jpg", classify_source(summary), summary)
    assert result.bills == ()
    assert result.blockers == (PreprocessingBlocker.CONTEXT_ONLY_IMAGE,)

    weak = _tokens(
        "预付账单",
        "2026年",
        "账单周期:05/18-05/24",
        "7625.20",
        "账单ID:2056095067379466251",
        confidence=8000,
    )
    weak_result = extract_bills("weak.png", classify_source(weak), weak)
    assert PreprocessingBlocker.LOW_FIELD_CONFIDENCE in weak_result.bills[0].blockers


def test_preprocess_rejects_symlinks_and_unsupported_inputs(tmp_path: Path) -> None:
    text = tmp_path / "bill.txt"
    text.write_text("not an image", encoding="utf-8")
    with pytest.raises(PreprocessingError, match="type"):
        preprocess_image(text, object())  # type: ignore[arg-type]
