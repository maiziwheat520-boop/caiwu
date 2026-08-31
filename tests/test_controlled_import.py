from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from ledgerbridge.controlled_import import (
    ControlledImportError,
    PartialRefundMatchBasis,
    load_prepared_manifest,
    load_source_manifest,
    prepare_source_manifest,
)
from ledgerbridge.file_key_provider import bootstrap_file_key


def test_partial_refund_match_basis_accepts_non_2026_dates() -> None:
    basis = PartialRefundMatchBasis(
        method="UNIQUE_PLATFORM_PARTIAL_REFUND",
        original_record_id="WX-0123456789ab",
        refund_record_id="WX-abcdef012345",
        original_date="2025-12-30",
        refund_date="2026-01-02",
    )

    assert basis.original_date == "2025-12-30"


def _source_manifest(root: Path, *, digest: str, size: int) -> Path:
    payload = {
        "schema_version": "ledgerbridge.controlled-review-source.v1",
        "batch_ref": "70000000-0000-4000-8000-000000000001",
        "generated_at": datetime(2026, 8, 28, tzinfo=UTC).isoformat(),
        "source_description": "isolated controlled import fixture",
        "entity": {
            "entity_ref": "70000000-0000-4000-8000-000000000002",
            "name": "Controlled fixture entity",
        },
        "business_unit": {
            "business_unit_ref": "70000000-0000-4000-8000-000000000003",
            "ref": "controlled-fixture",
            "label": "Controlled fixture",
        },
        "categories": [
            {
                "category_ref": "70000000-0000-4000-8000-000000000004",
                "code": "CONTROLLED_FIXTURE",
                "label": "Controlled fixture category",
            }
        ],
        "evidence": [
            {
                "evidence_ref": "70000000-0000-4000-8000-000000000005",
                "source_file": "fixture.bin",
                "display_name": "fixture.bin",
                "declared_media_type": "application/octet-stream",
                "plaintext_sha256": digest,
                "plaintext_size": size,
            }
        ],
        "candidates": [
            {
                "candidate_ref": "70000000-0000-4000-8000-000000000006",
                "operation_id": "70000000-0000-4000-8000-000000000007",
                "ingest_channel": "CONTROLLED_UPLOAD",
                "source_system": "controlled_fixture",
                "source_event_ref": "70000000-0000-4000-8000-000000000008",
                "display_label": "Controlled fixture",
                "category_code": "CONTROLLED_FIXTURE",
                "amount_minor": 123,
                "accounting_month": "2026-08",
                "summary": "Controlled import fixture candidate",
                "confidence_basis_points": 8000,
                "evidence_refs": ["70000000-0000-4000-8000-000000000005"],
                "counterparty_ref": "cp_" + "1" * 64,
                "counterparty_class": "unknown",
            }
        ],
    }
    path = (root / "source-manifest.json").resolve()
    path.write_text(json.dumps(payload), encoding="ascii")
    return path


def test_prepare_encrypts_evidence_and_replays_descriptor(tmp_path: Path) -> None:
    evidence = b"CONTROLLED-IMPORT-PLAINTEXT-CANARY"
    (tmp_path / "fixture.bin").write_bytes(evidence)
    source = _source_manifest(
        tmp_path,
        digest=hashlib.sha256(evidence).hexdigest(),
        size=len(evidence),
    )
    key_dir = tmp_path / "keys"
    key_dir.mkdir(mode=0o700)
    key_file = (key_dir / "evidence-key.json").resolve()
    bootstrap_file_key(key_file, generation="controlled-fixture-1")
    artifact_root = (tmp_path / "artifacts").resolve()
    prepared_path = (tmp_path / "prepared-manifest.json").resolve()

    first = prepare_source_manifest(
        source,
        key_file=key_file,
        artifact_root=artifact_root,
        prepared_manifest_path=prepared_path,
    )
    second = prepare_source_manifest(
        source,
        key_file=key_file,
        artifact_root=artifact_root,
        prepared_manifest_path=prepared_path,
    )
    loaded, _ = load_prepared_manifest(prepared_path)

    assert first == second == loaded
    assert first.batch_ref == UUID("70000000-0000-4000-8000-000000000001")
    assert first.evidence[0].plaintext_sha256 == hashlib.sha256(evidence).hexdigest()
    assert first.evidence[0].ciphertext_sha256 != first.evidence[0].plaintext_sha256
    assert first.candidates[0].counterparty_ref == "cp_" + "1" * 64
    assert first.candidates[0].counterparty_class is not None
    assert first.candidates[0].counterparty_class.value == "unknown"
    durable_files = [
        path for path in artifact_root.rglob("*") if path.is_file() and path.name != ".quota.lock"
    ]
    assert durable_files
    assert all(evidence not in path.read_bytes() for path in durable_files)


def test_prepare_rejects_source_digest_drift(tmp_path: Path) -> None:
    original = b"original"
    evidence_path = tmp_path / "fixture.bin"
    evidence_path.write_bytes(original)
    source = _source_manifest(
        tmp_path,
        digest=hashlib.sha256(original).hexdigest(),
        size=len(original),
    )
    evidence_path.write_bytes(b"tampered")
    key_dir = tmp_path / "keys"
    key_dir.mkdir(mode=0o700)
    key_file = (key_dir / "evidence-key.json").resolve()
    bootstrap_file_key(key_file, generation="controlled-fixture-1")

    with pytest.raises(ControlledImportError, match="digest changed"):
        prepare_source_manifest(
            source,
            key_file=key_file,
            artifact_root=(tmp_path / "artifacts").resolve(),
            prepared_manifest_path=(tmp_path / "prepared-manifest.json").resolve(),
        )


def test_controlled_source_expands_proven_welfare_offset_into_income_candidate(
    tmp_path: Path,
) -> None:
    evidence = b"synthetic platform and linked bank evidence"
    source = _source_manifest(
        tmp_path,
        digest=hashlib.sha256(evidence).hexdigest(),
        size=len(evidence),
    )
    payload = json.loads(source.read_text(encoding="ascii"))
    payload["categories"].append(
        {
            "category_ref": "70000000-0000-4000-8000-000000000009",
            "code": "WELFARE_INCOME",
            "label": "Welfare income",
        }
    )
    payload["candidates"][0]["amount_minor"] = -10000
    payload["candidates"][0]["summary"] = "平台消费 福利金抵扣 4995 分"
    payload["candidates"].append(
        {
            "candidate_ref": "70000000-0000-4000-8000-000000000010",
            "operation_id": "70000000-0000-4000-8000-000000000011",
            "ingest_channel": "CONTROLLED_UPLOAD",
            "source_system": "synthetic_bank_statement",
            "source_event_ref": "70000000-0000-4000-8000-000000000012",
            "display_label": "Linked synthetic bank debit",
            "category_code": "CONTROLLED_FIXTURE",
            "amount_minor": -5005,
            "accounting_month": "2026-08",
            "summary": "Linked bank debit",
            "confidence_basis_points": 10000,
            "evidence_refs": ["70000000-0000-4000-8000-000000000005"],
        }
    )
    payload["welfare_benefit_facts"] = [
        {
            "purchase_candidate_ref": "70000000-0000-4000-8000-000000000006",
            "funding_statement_evidence_ref": "70000000-0000-4000-8000-000000000005",
            "matched_bank_candidate_ref": "70000000-0000-4000-8000-000000000010",
            "bank_debit_absence_proven": False,
        }
    ]
    source.write_text(json.dumps(payload), encoding="ascii")

    manifest, _ = load_source_manifest(source)

    assert len(manifest.candidates) == 3
    purchase, bank_debit, welfare_income = manifest.candidates
    assert purchase.amount_minor == -10000
    assert bank_debit.amount_minor == -5005
    assert welfare_income.amount_minor == 4995
    assert welfare_income.category_code == "WELFARE_INCOME"
    assert welfare_income.evidence_refs == (UUID("70000000-0000-4000-8000-000000000005"),)
