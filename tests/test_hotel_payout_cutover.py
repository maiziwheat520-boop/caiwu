from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ledgerbridge.hotel_payout_cutover import (
    CandidateEvidenceLink,
    HotelMatchBasis,
    HotelPayoutCutoverManifest,
    HotelReplacement,
)
from scripts.build_hotel_payout_cutover import (
    HotelPayoutBuildError,
    _evidence_links,
)


def test_match_basis_rejects_bank_credit_outside_seven_days() -> None:
    with pytest.raises(ValidationError, match="within seven days"):
        HotelMatchBasis(
            method="EXACT_AMOUNT_DATE_PLATFORM_ONE_TO_ONE",
            platform="CTRIP_EBOOKING",
            subject_period_start=date(2026, 5, 18),
            subject_period_end=date(2026, 5, 24),
            evidence_date=date(2026, 6, 1),
            evidence_transaction_ref="TX-0122",
        )


def test_builder_matches_only_unique_amount_date_and_platform_credit() -> None:
    subject = uuid4()
    evidence = uuid4()
    links = _evidence_links(
        (
            {
                "candidate_ref": subject,
                "amount_minor": 1608380,
                "platform": "CTRIP_EBOOKING",
                "period_start": "2026-05-18",
                "period_end": "2026-05-24",
            },
        ),
        (
            {
                "candidate_ref": evidence,
                "amount_minor": 1608380,
                "date": date(2026, 5, 25),
                "item_id": "TX-0122",
                "text": "上海赫程国际旅行社",
            },
            {
                "candidate_ref": uuid4(),
                "amount_minor": 1608380,
                "date": date(2026, 5, 25),
                "item_id": "TX-0999",
                "text": "无关付款人",
            },
        ),
    )
    assert len(links) == 1
    assert links[0].subject_candidate_ref == subject
    assert links[0].evidence_candidate_ref == evidence


def test_builder_fails_closed_on_ambiguous_bank_credit() -> None:
    detail = {
        "candidate_ref": uuid4(),
        "amount_minor": 762520,
        "platform": "MEITUAN_MOBILE",
        "period_start": "2026-05-18",
        "period_end": "2026-05-24",
    }
    rows = tuple(
        {
            "candidate_ref": uuid4(),
            "amount_minor": 762520,
            "date": date(2026, 5, 27),
            "item_id": f"TX-01{index}",
            "text": "北京钱袋宝支付",
        }
        for index in (18, 19)
    )
    with pytest.raises(HotelPayoutBuildError, match="ambiguous"):
        _evidence_links((detail,), rows)


def test_cutover_manifest_rejects_reused_bank_credit() -> None:
    ocr_one = uuid4()
    ocr_two = uuid4()
    bank = uuid4()
    basis = HotelMatchBasis(
        method="EXACT_AMOUNT_DATE_PLATFORM_ONE_TO_ONE",
        platform="MEITUAN_MOBILE",
        subject_period_start=date(2026, 5, 18),
        subject_period_end=date(2026, 5, 24),
        evidence_date=date(2026, 5, 27),
        evidence_transaction_ref="TX-0118",
    )
    links = tuple(
        CandidateEvidenceLink(
            link_ref=uuid4(),
            subject_candidate_ref=subject,
            evidence_candidate_ref=bank,
            risk_code="HOTEL_PAYOUT_STATEMENT_REQUIRED",
            relation="SAME_ECONOMIC_TRANSACTION",
            amount_minor=762520,
            currency="CNY",
            match_basis=basis,
        )
        for subject in (ocr_one, ocr_two)
    )
    with pytest.raises(ValidationError, match="cannot be reused"):
        HotelPayoutCutoverManifest(
            schema_version="ledgerbridge.hotel-payout-cutover.v1",
            cutover_ref=uuid4(),
            generated_at=datetime(2026, 8, 29, tzinfo=UTC),
            source_manifest_sha256="a" * 64,
            entity_ref=uuid4(),
            business_unit_ref=uuid4(),
            ocr_candidate_refs=(ocr_one, ocr_two),
            replacements=(
                HotelReplacement(
                    legacy_candidate_ref=uuid4(),
                    ocr_candidate_ref=ocr_one,
                    amount_minor=762520,
                ),
            ),
            evidence_links=links,
        )
