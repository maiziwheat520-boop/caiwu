from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from uuid import UUID

import pytest

from scripts.scope_alipay_annual_manifest import (
    AnnualManifestScopeError,
    scope_alipay_annual_manifest,
)


def _source(tmp_path: Path) -> Path:
    evidence = tmp_path / "alipay.csv"
    evidence.write_bytes(b"synthetic annual statement")
    source = tmp_path / "source-manifest.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": "ledgerbridge.controlled-review-source.v1",
                "batch_ref": "10000000-0000-4000-8000-000000000001",
                "generated_at": "2026-09-03T00:00:00Z",
                "source_description": "Synthetic annual Alipay review.",
                "entity": {"entity_ref": "20000000-0000-4000-8000-000000000001", "name": "Test"},
                "business_unit": {
                    "business_unit_ref": "30000000-0000-4000-8000-000000000001",
                    "ref": "test",
                    "label": "Test",
                },
                "categories": [
                    {
                        "category_ref": "40000000-0000-4000-8000-000000000001",
                        "code": "ALIPAY_TRANSACTION_REVIEW",
                        "label": "Review",
                    }
                ],
                "evidence": [
                    {
                        "evidence_ref": "50000000-0000-4000-8000-000000000001",
                        "source_file": "alipay.csv",
                        "display_name": "alipay.csv",
                        "declared_media_type": "text/csv",
                        "plaintext_sha256": (
                            "1005e6263bca76e1111eb003c394d63970e99f77131350293628ffc7ef99e967"
                        ),
                        "plaintext_size": 26,
                    }
                ],
                "candidates": [
                    {
                        "candidate_ref": "60000000-0000-4000-8000-000000000001",
                        "operation_id": "70000000-0000-4000-8000-000000000001",
                        "ingest_channel": "CONTROLLED_UPLOAD",
                        "source_system": "alipay_export",
                        "source_event_ref": "80000000-0000-4000-8000-000000000001",
                        "display_label": "支付宝交易",
                        "category_code": "ALIPAY_TRANSACTION_REVIEW",
                        "amount_minor": 123,
                        "accounting_month": "2026-05",
                        "summary": "unchanged",
                        "confidence_basis_points": 9900,
                        "evidence_refs": ["50000000-0000-4000-8000-000000000001"],
                    }
                ],
                "candidate_links": [],
                "welfare_benefit_facts": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return source


def test_scoping_is_byte_identical_and_separates_accounts(tmp_path: Path) -> None:
    source = _source(tmp_path)
    account_one = UUID("90000000-0000-4000-8000-000000000001")
    account_two = UUID("90000000-0000-4000-8000-000000000002")
    first = scope_alipay_annual_manifest(
        source_manifest=source, source_account_ref=account_one, output_directory=tmp_path / "first"
    )
    replay = scope_alipay_annual_manifest(
        source_manifest=source, source_account_ref=account_one, output_directory=tmp_path / "replay"
    )
    other = scope_alipay_annual_manifest(
        source_manifest=source, source_account_ref=account_two, output_directory=tmp_path / "other"
    )
    assert first.read_bytes() == replay.read_bytes()
    one = json.loads(first.read_text(encoding="utf-8"))
    two = json.loads(other.read_text(encoding="utf-8"))
    assert one["generated_at"] == "2026-09-03T00:00:00Z"
    for field in ("candidate_ref", "operation_id", "source_event_ref"):
        assert one["candidates"][0][field] != two["candidates"][0][field]
    assert one["candidates"][0]["amount_minor"] == 123
    assert one["candidates"][0]["summary"] == "unchanged"
    if os.name != "nt":
        assert stat.S_IMODE(first.stat().st_mode) == 0o600
        assert stat.S_IMODE((first.parent / "alipay.csv").stat().st_mode) == 0o600
    with pytest.raises(AnnualManifestScopeError, match="already exists"):
        scope_alipay_annual_manifest(
            source_manifest=source, source_account_ref=account_one, output_directory=first.parent
        )
