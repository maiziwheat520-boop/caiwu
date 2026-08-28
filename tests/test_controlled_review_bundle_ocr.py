import json
from pathlib import Path
from uuid import uuid4

from scripts.build_controlled_review_bundle import _ocr_photo_candidates


def test_ocr_candidates_use_real_confidence_and_only_the_matching_image(tmp_path: Path) -> None:
    evidence_ref = uuid4()
    other_ref = uuid4()
    payload = {
        "schema_version": "ledgerbridge.bill-ocr.v1",
        "engine": "test",
        "results": [
            {
                "source_name": "bill-a.png",
                "bills": [
                    {
                        "source_kind": "MEITUAN_MOBILE",
                        "bill_id": "bill-1",
                        "period_start": "2026-05-18",
                        "period_end": "2026-05-24",
                        "amount_minor": 12345,
                        "confidence_basis_points": 9321,
                        "blockers": [],
                    },
                    {
                        "source_kind": "MEITUAN_DESKTOP",
                        "bill_id": "bill-blocked",
                        "period_start": None,
                        "period_end": None,
                        "amount_minor": 999,
                        "confidence_basis_points": 9900,
                        "blockers": ["MISSING_PERIOD"],
                    },
                ],
            }
        ],
    }
    path = tmp_path / "ocr.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    candidates = _ocr_photo_candidates(
        path,
        evidence_ref_by_source_name={"bill-a.png": evidence_ref, "bill-b.png": other_ref},
    )

    assert len(candidates) == 1
    assert candidates[0]["confidence_basis_points"] == 9321
    assert candidates[0]["evidence_refs"] == (evidence_ref,)
    assert other_ref not in candidates[0]["evidence_refs"]
