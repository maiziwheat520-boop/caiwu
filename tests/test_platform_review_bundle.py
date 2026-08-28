from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    }


def _write_normalized(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            {
                "schemaVersion": "ledgerbridge.financial-foundation-normalized.v1",
                "period": "2026-05",
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
