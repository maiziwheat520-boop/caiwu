"""Every refusal the hotel payout cutover makes before it opens a transaction.

The cutover ignores weekly candidates that more precise OCR rows replace, so a
manifest that names the wrong candidate, the wrong amount, or the wrong source
system would silently retire real financial facts.  Each of those refusals is
proven here against an engine that raises if it is touched at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import Engine

from ledgerbridge.controlled_import import prepare_source_manifest
from ledgerbridge.file_key_provider import bootstrap_file_key
from ledgerbridge.hotel_payout_cutover import (
    CandidateEvidenceLink,
    HotelMatchBasis,
    HotelPayoutCutoverError,
    HotelPayoutCutoverManifest,
    HotelPayoutCutoverResult,
    HotelReplacement,
    _read_bounded_regular_file,
    _result_from_receipt,
    import_hotel_payout_cutover,
    load_hotel_payout_cutover_manifest,
    write_private_cutover_manifest,
)

ENTITY = UUID("71000000-0000-4000-8000-000000000002")
BUSINESS_UNIT = UUID("71000000-0000-4000-8000-000000000003")
OCR_ONE = UUID("71000000-0000-4000-8000-000000000010")
OCR_TWO = UUID("71000000-0000-4000-8000-000000000011")
BANK_ONE = UUID("71000000-0000-4000-8000-000000000020")
BANK_TWO = UUID("71000000-0000-4000-8000-000000000021")
LEGACY_ONE = UUID("71000000-0000-4000-8000-000000000030")
AMOUNT_ONE = 1_608_380
AMOUNT_TWO = 762_520


class _RefusingEngine:
    """Fails the test if the cutover reaches the database at all."""

    def begin(self) -> object:
        raise AssertionError("the cutover opened a transaction before validating its manifests")


def _engine() -> Engine:
    return cast(Engine, _RefusingEngine())


def _basis(**overrides: object) -> HotelMatchBasis:
    fields: dict[str, object] = {
        "method": "EXACT_AMOUNT_DATE_PLATFORM_ONE_TO_ONE",
        "platform": "CTRIP_EBOOKING",
        "subject_period_start": date(2026, 5, 18),
        "subject_period_end": date(2026, 5, 24),
        "evidence_date": date(2026, 5, 25),
        "evidence_transaction_ref": "TX-0122",
    }
    fields.update(overrides)
    return HotelMatchBasis(**fields)  # type: ignore[arg-type]


def _link(
    subject: UUID = OCR_ONE,
    evidence: UUID = BANK_ONE,
    amount: int = AMOUNT_ONE,
) -> CandidateEvidenceLink:
    return CandidateEvidenceLink(
        link_ref=uuid4(),
        subject_candidate_ref=subject,
        evidence_candidate_ref=evidence,
        risk_code="HOTEL_PAYOUT_STATEMENT_REQUIRED",
        relation="SAME_ECONOMIC_TRANSACTION",
        amount_minor=amount,
        currency="CNY",
        match_basis=_basis(),
    )


def _candidate(
    candidate_ref: UUID,
    *,
    source_system: str,
    amount_minor: int,
    evidence_ref: str,
    label: str,
) -> dict[str, object]:
    return {
        "candidate_ref": str(candidate_ref),
        "operation_id": str(uuid4()),
        "ingest_channel": "CONTROLLED_UPLOAD",
        "source_system": source_system,
        "source_event_ref": str(uuid4()),
        "display_label": label,
        "category_code": "HOTEL_PAYOUT",
        "amount_minor": amount_minor,
        "accounting_month": "2026-05",
        "summary": f"{label} cutover fixture",
        "confidence_basis_points": 9000,
        "evidence_refs": [evidence_ref],
    }


def _write_source_manifest(root: Path, *, digest: str, size: int) -> Path:
    evidence_ref = "71000000-0000-4000-8000-000000000005"
    payload = {
        "schema_version": "ledgerbridge.controlled-review-source.v1",
        "batch_ref": "71000000-0000-4000-8000-000000000001",
        "generated_at": datetime(2026, 5, 28, tzinfo=UTC).isoformat(),
        "source_description": "hotel payout cutover fixture",
        "entity": {"entity_ref": str(ENTITY), "name": "Hotel fixture entity"},
        "business_unit": {
            "business_unit_ref": str(BUSINESS_UNIT),
            "ref": "hotel-fixture",
            "label": "Hotel fixture",
        },
        "categories": [
            {
                "category_ref": "71000000-0000-4000-8000-000000000004",
                "code": "HOTEL_PAYOUT",
                "label": "Hotel payout",
            }
        ],
        "evidence": [
            {
                "evidence_ref": evidence_ref,
                "source_file": "fixture.bin",
                "display_name": "fixture.bin",
                "declared_media_type": "application/octet-stream",
                "plaintext_sha256": digest,
                "plaintext_size": size,
            }
        ],
        "candidates": [
            _candidate(
                OCR_ONE,
                source_system="hotel_bill_ocr",
                amount_minor=AMOUNT_ONE,
                evidence_ref=evidence_ref,
                label="Ctrip payout",
            ),
            _candidate(
                OCR_TWO,
                source_system="hotel_bill_ocr",
                amount_minor=AMOUNT_TWO,
                evidence_ref=evidence_ref,
                label="Meituan payout",
            ),
            _candidate(
                BANK_ONE,
                source_system="boc_mail_derived_review",
                amount_minor=AMOUNT_ONE,
                evidence_ref=evidence_ref,
                label="BOC credit one",
            ),
            _candidate(
                BANK_TWO,
                source_system="boc_mail_derived_review",
                amount_minor=AMOUNT_TWO,
                evidence_ref=evidence_ref,
                label="BOC credit two",
            ),
        ],
    }
    path = (root / "source-manifest.json").resolve()
    path.write_text(json.dumps(payload), encoding="ascii")
    return path


@pytest.fixture
def prepared_pair(tmp_path: Path) -> tuple[Path, Path, str]:
    """A matching source/prepared manifest pair and the source digest."""

    evidence = b"HOTEL-PAYOUT-CUTOVER-FIXTURE"
    (tmp_path / "fixture.bin").write_bytes(evidence)
    source_path = _write_source_manifest(
        tmp_path,
        digest=hashlib.sha256(evidence).hexdigest(),
        size=len(evidence),
    )
    key_dir = tmp_path / "keys"
    key_dir.mkdir(mode=0o700)
    key_file = (key_dir / "evidence-key.json").resolve()
    bootstrap_file_key(key_file, generation="hotel-fixture-1")
    prepared_path = (tmp_path / "prepared-manifest.json").resolve()
    prepare_source_manifest(
        source_path,
        key_file=key_file,
        artifact_root=(tmp_path / "artifacts").resolve(),
        prepared_manifest_path=prepared_path,
    )
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    return source_path, prepared_path, digest


def _manifest(source_digest: str, **overrides: object) -> HotelPayoutCutoverManifest:
    fields: dict[str, object] = {
        "schema_version": "ledgerbridge.hotel-payout-cutover.v1",
        "cutover_ref": uuid4(),
        "generated_at": datetime(2026, 5, 29, tzinfo=UTC),
        "source_manifest_sha256": source_digest,
        "entity_ref": ENTITY,
        "business_unit_ref": BUSINESS_UNIT,
        "ocr_candidate_refs": (OCR_ONE, OCR_TWO),
        "replacements": (
            HotelReplacement(
                legacy_candidate_ref=LEGACY_ONE,
                ocr_candidate_ref=OCR_ONE,
                amount_minor=AMOUNT_ONE,
            ),
        ),
        "evidence_links": (_link(),),
    }
    fields.update(overrides)
    return HotelPayoutCutoverManifest(**fields)  # type: ignore[arg-type]


def _run(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
    manifest: HotelPayoutCutoverManifest,
) -> None:
    source_path, prepared_path, _ = prepared_pair
    cutover_path = (tmp_path / f"cutover-{uuid4().hex}.json").resolve()
    write_private_cutover_manifest(cutover_path, manifest)
    import_hotel_payout_cutover(
        _engine(),
        source_manifest_path=source_path,
        prepared_manifest_path=prepared_path,
        cutover_manifest_path=cutover_path,
    )


# --- the manifest's own closure rules -------------------------------------


def test_a_replacement_may_not_point_a_candidate_at_itself() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        HotelReplacement(
            legacy_candidate_ref=OCR_ONE,
            ocr_candidate_ref=OCR_ONE,
            amount_minor=AMOUNT_ONE,
        )


def test_a_link_may_not_use_one_candidate_as_its_own_evidence() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        _link(subject=OCR_ONE, evidence=OCR_ONE)


def test_an_inverted_payout_period_is_rejected() -> None:
    with pytest.raises(ValidationError, match="period is inverted"):
        _basis(subject_period_start=date(2026, 5, 25), subject_period_end=date(2026, 5, 18))


def test_a_bank_credit_before_the_period_ends_is_rejected() -> None:
    with pytest.raises(ValidationError, match="within seven days"):
        _basis(evidence_date=date(2026, 5, 23))


def test_the_seven_day_settlement_window_is_inclusive_at_both_ends() -> None:
    assert _basis(evidence_date=date(2026, 5, 24)).evidence_date == date(2026, 5, 24)
    assert _basis(evidence_date=date(2026, 5, 31)).evidence_date == date(2026, 5, 31)


def test_a_naive_generation_time_is_rejected(prepared_pair: tuple[Path, Path, str]) -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _manifest(prepared_pair[2], generated_at=datetime(2026, 5, 29))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"ocr_candidate_refs": (OCR_ONE, OCR_ONE)}, "OCR candidate references must be unique"),
        (
            {
                "replacements": (
                    HotelReplacement(
                        legacy_candidate_ref=LEGACY_ONE,
                        ocr_candidate_ref=OCR_ONE,
                        amount_minor=AMOUNT_ONE,
                    ),
                    HotelReplacement(
                        legacy_candidate_ref=LEGACY_ONE,
                        ocr_candidate_ref=OCR_TWO,
                        amount_minor=AMOUNT_TWO,
                    ),
                )
            },
            "legacy replacement references must be unique",
        ),
        (
            {
                "replacements": (
                    HotelReplacement(
                        legacy_candidate_ref=LEGACY_ONE,
                        ocr_candidate_ref=OCR_ONE,
                        amount_minor=AMOUNT_ONE,
                    ),
                    HotelReplacement(
                        legacy_candidate_ref=uuid4(),
                        ocr_candidate_ref=OCR_ONE,
                        amount_minor=AMOUNT_ONE,
                    ),
                )
            },
            "replacement OCR references must be unique",
        ),
        (
            {"evidence_links": (_link(), _link(subject=OCR_ONE, evidence=BANK_TWO))},
            "evidence-link subjects must be unique",
        ),
        (
            {
                "evidence_links": (
                    _link(),
                    _link(subject=OCR_TWO, evidence=BANK_ONE, amount=AMOUNT_TWO),
                )
            },
            "bank evidence candidates cannot be reused",
        ),
        (
            {"ocr_candidate_refs": (OCR_TWO,), "evidence_links": ()},
            "undeclared OCR candidate",
        ),
    ],
)
def test_the_manifest_must_be_closed_over_its_declared_candidates(
    prepared_pair: tuple[Path, Path, str],
    overrides: dict[str, object],
    expected: str,
) -> None:
    with pytest.raises(ValidationError, match=expected):
        _manifest(prepared_pair[2], **overrides)


# --- refusals that must happen before any database work -------------------


def test_a_cutover_naming_the_wrong_source_manifest_is_refused(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
) -> None:
    with pytest.raises(HotelPayoutCutoverError, match="source manifest digest does not match"):
        _run(tmp_path, prepared_pair, _manifest("b" * 64))


@pytest.mark.parametrize("field", ["entity_ref", "business_unit_ref"])
def test_a_cutover_scoped_to_another_entity_or_unit_is_refused(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
    field: str,
) -> None:
    with pytest.raises(HotelPayoutCutoverError, match="scope does not match"):
        _run(tmp_path, prepared_pair, _manifest(prepared_pair[2], **{field: uuid4()}))


def test_an_ocr_candidate_absent_from_the_source_is_refused(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
) -> None:
    stranger = uuid4()
    manifest = _manifest(
        prepared_pair[2],
        ocr_candidate_refs=(OCR_ONE, stranger),
        replacements=(
            HotelReplacement(
                legacy_candidate_ref=LEGACY_ONE,
                ocr_candidate_ref=stranger,
                amount_minor=AMOUNT_ONE,
            ),
        ),
        evidence_links=(),
    )
    with pytest.raises(HotelPayoutCutoverError, match="absent from source"):
        _run(tmp_path, prepared_pair, manifest)


def test_a_bank_candidate_declared_as_an_ocr_row_is_refused(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
) -> None:
    manifest = _manifest(
        prepared_pair[2],
        ocr_candidate_refs=(OCR_ONE, BANK_ONE),
        replacements=(
            HotelReplacement(
                legacy_candidate_ref=LEGACY_ONE,
                ocr_candidate_ref=BANK_ONE,
                amount_minor=AMOUNT_ONE,
            ),
        ),
        evidence_links=(),
    )
    with pytest.raises(HotelPayoutCutoverError, match="OCR candidate source is invalid"):
        _run(tmp_path, prepared_pair, manifest)


def test_a_replacement_amount_that_disagrees_with_the_ocr_row_is_refused(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
) -> None:
    manifest = _manifest(
        prepared_pair[2],
        replacements=(
            HotelReplacement(
                legacy_candidate_ref=LEGACY_ONE,
                ocr_candidate_ref=OCR_ONE,
                amount_minor=AMOUNT_ONE + 1,
            ),
        ),
        evidence_links=(),
    )
    with pytest.raises(HotelPayoutCutoverError, match="replacement amount does not match"):
        _run(tmp_path, prepared_pair, manifest)


@pytest.mark.parametrize("evidence", [OCR_TWO, UUID("71000000-0000-4000-8000-0000000000ff")])
def test_a_link_whose_evidence_is_not_a_bank_credit_is_refused(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
    evidence: UUID,
) -> None:
    # OCR_TWO exists but is a hotel OCR row; the second UUID is not in the
    # source at all.  Both must fail closed rather than link an arbitrary row.
    manifest = _manifest(prepared_pair[2], evidence_links=(_link(evidence=evidence),))
    with pytest.raises(HotelPayoutCutoverError, match="link bank evidence candidate is invalid"):
        _run(tmp_path, prepared_pair, manifest)


def test_a_link_amount_that_disagrees_with_either_candidate_is_refused(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
) -> None:
    manifest = _manifest(
        prepared_pair[2],
        evidence_links=(_link(subject=OCR_ONE, evidence=BANK_TWO, amount=AMOUNT_ONE),),
    )
    with pytest.raises(HotelPayoutCutoverError, match="link amount does not match both"):
        _run(tmp_path, prepared_pair, manifest)


def test_a_prepared_manifest_from_another_source_is_refused(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
) -> None:
    source_path, prepared_path, digest = prepared_pair
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    prepared["source_manifest_sha256"] = "c" * 64
    other = (tmp_path / "other-prepared.json").resolve()
    other.write_text(json.dumps(prepared), encoding="ascii")
    cutover_path = (tmp_path / "cutover.json").resolve()
    write_private_cutover_manifest(cutover_path, _manifest(digest))
    with pytest.raises(HotelPayoutCutoverError, match="belongs to another source"):
        import_hotel_payout_cutover(
            _engine(),
            source_manifest_path=source_path,
            prepared_manifest_path=other,
            cutover_manifest_path=cutover_path,
        )


# --- the bounded file reader ----------------------------------------------


def test_the_loader_round_trips_a_manifest_it_just_wrote(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
) -> None:
    manifest = _manifest(prepared_pair[2])
    path = (tmp_path / "cutover.json").resolve()
    write_private_cutover_manifest(path, manifest)
    loaded, raw = load_hotel_payout_cutover_manifest(path)
    assert loaded == manifest
    assert json.loads(raw)["schema_version"] == "ledgerbridge.hotel-payout-cutover.v1"


def test_writing_a_manifest_never_overwrites_an_existing_one(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
) -> None:
    manifest = _manifest(prepared_pair[2])
    path = (tmp_path / "cutover.json").resolve()
    write_private_cutover_manifest(path, manifest)
    with pytest.raises(FileExistsError):
        write_private_cutover_manifest(path, manifest)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes are unavailable")
def test_a_written_manifest_is_readable_only_by_its_owner(
    tmp_path: Path,
    prepared_pair: tuple[Path, Path, str],
) -> None:
    path = (tmp_path / "cutover.json").resolve()
    write_private_cutover_manifest(path, _manifest(prepared_pair[2]))
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600


def test_a_relative_path_is_refused_before_the_filesystem_is_touched() -> None:
    with pytest.raises(HotelPayoutCutoverError, match="must be absolute"):
        _read_bounded_regular_file(Path("cutover.json"), max_bytes=1024)


def test_a_missing_file_is_reported_as_unavailable(tmp_path: Path) -> None:
    with pytest.raises(HotelPayoutCutoverError, match="is unavailable"):
        _read_bounded_regular_file((tmp_path / "absent.json").resolve(), max_bytes=1024)


def test_a_directory_is_not_a_regular_file(tmp_path: Path) -> None:
    with pytest.raises(HotelPayoutCutoverError, match="must be regular"):
        _read_bounded_regular_file(tmp_path.resolve(), max_bytes=1024)


@pytest.mark.parametrize(("content", "limit"), [(b"", 1024), (b"x" * 40, 16)])
def test_an_empty_or_oversized_file_is_refused(
    tmp_path: Path,
    content: bytes,
    limit: int,
) -> None:
    path = (tmp_path / "bounded.json").resolve()
    path.write_bytes(content)
    with pytest.raises(HotelPayoutCutoverError, match="size is invalid"):
        _read_bounded_regular_file(path, max_bytes=limit)


def test_a_symlink_is_never_followed(tmp_path: Path) -> None:
    target = (tmp_path / "real.json").resolve()
    target.write_bytes(b"{}")
    link = (tmp_path / "link.json").resolve()
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(HotelPayoutCutoverError, match="must be regular"):
        _read_bounded_regular_file(link, max_bytes=1024)


def test_a_manifest_that_is_not_this_schema_is_refused(tmp_path: Path) -> None:
    path = (tmp_path / "cutover.json").resolve()
    path.write_text(json.dumps({"schema_version": "something.else.v1"}), encoding="ascii")
    with pytest.raises(HotelPayoutCutoverError, match="manifest is invalid"):
        load_hotel_payout_cutover_manifest(path)


# --- the replay receipt ----------------------------------------------------


def test_a_stored_receipt_is_reported_as_a_replay() -> None:
    row: dict[str, Any] = {
        "ignored_candidate_count": 1,
        "imported_candidate_count": 2,
        "link_count": 1,
        "audit_horizon_sequence": 4096,
        "audit_horizon_hash": bytes.fromhex("d" * 64),
    }
    cutover_ref = uuid4()
    result = _result_from_receipt(cutover_ref, row, replayed=True)
    assert result == HotelPayoutCutoverResult(
        cutover_ref=cutover_ref,
        replayed=True,
        ignored_candidate_count=1,
        imported_candidate_count=2,
        link_count=1,
        audit_horizon_sequence=4096,
        audit_horizon_hash="d" * 64,
    )
