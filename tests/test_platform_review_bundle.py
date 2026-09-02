from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from ledgerbridge.controlled_import import SourceManifest
from scripts.build_platform_review_bundle import (
    PlatformBundleError,
    build_platform_bundle,
)


def _record(record_id: str, *, source: str, effect: bool = True) -> dict[str, object]:
    return {
        "recordId": record_id,
        "date": "2026-05-18",
        "source": source,
        "amountMinor": -12345 if effect else 0,
        "effect": effect,
        "direction": "支出" if effect else "不计收支",
        "category": "消费",
        "counterparty": "合成商户",
        "description": "合成测试交易",
        "paymentMethod": "平台余额",
        "status": "交易成功",
        "evidenceAlias": "wechat" if source == "微信" else "alipay",
        "counterpartyRef": "cp_" + "1" * 64,
        "counterpartyClass": "unknown",
    }


def _write_normalized(
    path: Path, records: list[dict[str, object]], *, period: str = "2026-05"
) -> None:
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "ledgerbridge.financial-foundation-normalized.v1",
                "period": period,
                "records": records,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_platform_bundle_keeps_native_evidence_and_only_effective_records(
    tmp_path: Path,
) -> None:
    normalized = tmp_path / "normalized.json"
    wechat = tmp_path / "wechat.xlsx"
    alipay = tmp_path / "alipay.csv"
    wechat.write_bytes(b"synthetic-wechat")
    alipay.write_bytes(b"synthetic-alipay")
    _write_normalized(
        normalized,
        [
            _record("WX-0123456789ab", source="微信"),
            _record("ZFB-abcdef012345", source="支付宝"),
            _record("WX-fedcba987654", source="微信", effect=False),
        ],
    )

    manifest_path, manifest = build_platform_bundle(
        normalized_records=normalized,
        wechat_statement=wechat,
        alipay_statement=alipay,
        output_directory=tmp_path / "bundle-one",
    )
    _, replay = build_platform_bundle(
        normalized_records=normalized,
        wechat_statement=wechat,
        alipay_statement=alipay,
        output_directory=tmp_path / "bundle-two",
    )

    assert manifest_path.stat().st_mode
    assert len(manifest.evidence) == 2
    assert len(manifest.candidates) == 2
    assert {item.source_system for item in manifest.candidates} == {
        "wechat_pay_export",
        "alipay_export",
    }
    assert {item.source_file for item in manifest.evidence} == {
        "wechat-pay-statement.xlsx",
        "alipay-statement.csv",
    }
    assert all("合成商户" in item.summary for item in manifest.candidates)
    assert replay.batch_ref == manifest.batch_ref
    assert tuple(item.candidate_ref for item in replay.candidates) == tuple(
        item.candidate_ref for item in manifest.candidates
    )


def test_alipay_identity_is_idempotent_per_account_and_distinct_across_accounts(
    tmp_path: Path,
) -> None:
    normalized = tmp_path / "normalized.json"
    wechat = tmp_path / "wechat.xlsx"
    alipay = tmp_path / "alipay.csv"
    wechat.write_bytes(b"synthetic-wechat")
    alipay.write_bytes(b"synthetic-alipay")
    _write_normalized(normalized, [_record("ZFB-abcdef012345", source="支付宝")])
    account_one = UUID("10000000-0000-4000-8000-000000000001")
    account_two = UUID("10000000-0000-4000-8000-000000000002")

    def build(name: str, account_ref: UUID) -> SourceManifest:
        return build_platform_bundle(
            normalized_records=normalized,
            wechat_statement=wechat,
            alipay_statement=alipay,
            alipay_source_account_ref=account_ref,
            output_directory=tmp_path / name,
        )[1]

    first = build("first", account_one)
    replay = build("replay", account_one)
    second_account = build("second-account", account_two)

    assert first.batch_ref == replay.batch_ref
    assert first.candidates[0].candidate_ref == replay.candidates[0].candidate_ref
    assert first.candidates[0].source_event_ref == replay.candidates[0].source_event_ref
    assert first.candidates[0].candidate_ref != second_account.candidates[0].candidate_ref
    assert first.candidates[0].source_event_ref != second_account.candidates[0].source_event_ref
    assert first.evidence[1].evidence_ref != second_account.evidence[1].evidence_ref


def test_platform_bundle_rejects_duplicate_normalized_ids(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized.json"
    wechat = tmp_path / "wechat.xlsx"
    alipay = tmp_path / "alipay.csv"
    wechat.write_bytes(b"synthetic-wechat")
    alipay.write_bytes(b"synthetic-alipay")
    repeated = _record("WX-0123456789ab", source="微信")
    _write_normalized(normalized, [repeated, repeated])

    with pytest.raises(PlatformBundleError, match="normalized platform record file is invalid"):
        build_platform_bundle(
            normalized_records=normalized,
            wechat_statement=wechat,
            alipay_statement=alipay,
            output_directory=tmp_path / "bundle",
        )


def test_platform_bundle_rejects_partial_counterparty_projection(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized.json"
    record = _record("WX-0123456789ab", source="微信")
    record.pop("counterpartyClass")
    _write_normalized(normalized, [record])
    wechat = tmp_path / "wechat.xlsx"
    alipay = tmp_path / "alipay.csv"
    wechat.write_bytes(b"synthetic-wechat")
    alipay.write_bytes(b"synthetic-alipay")

    with pytest.raises(PlatformBundleError, match="normalized platform record file is invalid"):
        build_platform_bundle(
            normalized_records=normalized,
            wechat_statement=wechat,
            alipay_statement=alipay,
            output_directory=tmp_path / "bundle",
        )


def test_platform_bundle_persists_counterparty_and_unique_partial_refund_relation(
    tmp_path: Path,
) -> None:
    normalized = tmp_path / "normalized.json"
    payment = _record("WX-0123456789ab", source="微信")
    payment["amountMinor"] = -10000
    payment["status"] = "已退款(￥49.95)"
    payment["refundMatch"] = {
        "matchedRecordId": "WX-abcdef012345",
        "role": "ORIGINAL",
        "amountMinor": 4995,
    }
    refund = _record("WX-abcdef012345", source="微信")
    refund["amountMinor"] = 4995
    refund["direction"] = "退款收入"
    refund["category"] = "消费-退款"
    refund["status"] = "已退款(￥49.95)"
    refund["refundMatch"] = {
        "matchedRecordId": "WX-0123456789ab",
        "role": "REFUND",
        "amountMinor": 4995,
    }
    _write_normalized(normalized, [payment, refund])
    wechat = tmp_path / "wechat.xlsx"
    alipay = tmp_path / "alipay.csv"
    wechat.write_bytes(b"synthetic-wechat")
    alipay.write_bytes(b"synthetic-alipay")

    _, manifest = build_platform_bundle(
        normalized_records=normalized,
        wechat_statement=wechat,
        alipay_statement=alipay,
        output_directory=tmp_path / "bundle",
    )

    assert all(candidate.counterparty_ref == "cp_" + "1" * 64 for candidate in manifest.candidates)
    assert all(candidate.counterparty_class is not None for candidate in manifest.candidates)
    assert all(
        candidate.counterparty_class is not None and candidate.counterparty_class.value == "unknown"
        for candidate in manifest.candidates
    )
    assert len(manifest.candidate_links) == 1
    link = manifest.candidate_links[0]
    assert link.risk_code == "REVERSAL_MATCH_REQUIRED"
    assert link.relation == "PARTIAL_REFUND"
    assert link.amount_minor == 4995


def test_platform_bundle_rejects_one_sided_refund_match(tmp_path: Path) -> None:
    normalized = tmp_path / "normalized.json"
    payment = _record("WX-0123456789ab", source="微信")
    payment["refundMatch"] = {
        "matchedRecordId": "WX-abcdef012345",
        "role": "ORIGINAL",
        "amountMinor": 4995,
    }
    refund = _record("WX-abcdef012345", source="微信")
    _write_normalized(normalized, [payment, refund])
    wechat = tmp_path / "wechat.xlsx"
    alipay = tmp_path / "alipay.csv"
    wechat.write_bytes(b"synthetic-wechat")
    alipay.write_bytes(b"synthetic-alipay")

    with pytest.raises(PlatformBundleError, match="normalized platform record file is invalid"):
        build_platform_bundle(
            normalized_records=normalized,
            wechat_statement=wechat,
            alipay_statement=alipay,
            output_directory=tmp_path / "bundle",
        )


@pytest.mark.parametrize(
    ("period", "date"),
    [("2025-12", "2026-05-18"), ("2026-06", "2026-05-18")],
)
def test_platform_bundle_only_accepts_2026_records_in_declared_period(
    tmp_path: Path, period: str, date: str
) -> None:
    normalized = tmp_path / "normalized.json"
    record = _record("WX-0123456789ab", source="微信")
    record["date"] = date
    _write_normalized(normalized, [record], period=period)
    wechat = tmp_path / "wechat.xlsx"
    alipay = tmp_path / "alipay.csv"
    wechat.write_bytes(b"synthetic-wechat")
    alipay.write_bytes(b"synthetic-alipay")

    with pytest.raises(PlatformBundleError, match="normalized platform record file is invalid"):
        build_platform_bundle(
            normalized_records=normalized,
            wechat_statement=wechat,
            alipay_statement=alipay,
            output_directory=tmp_path / "bundle",
        )
