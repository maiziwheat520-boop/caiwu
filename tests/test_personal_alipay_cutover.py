from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from scripts import run_personal_alipay_cutover as cutover


def _reviewed_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for month, (count, total) in cutover._EXPECTED_MONTHS.items():
        quotient, remainder = divmod(total, count)
        rows.extend(
            {
                "candidate_ref": uuid4(),
                "source_event_ref": uuid4(),
                "amount_minor": quotient + (1 if index < remainder else 0),
                "accounting_month": month,
                "summary": "支付宝 | verified | 收入 | 转账红包 | account2",
                "confidence_basis_points": 10_000,
            }
            for index in range(count)
        )
    return rows


def test_reviewed_cohort_contract_accepts_only_exact_snapshot() -> None:
    rows = _reviewed_rows()
    cutover._validate_cohort(rows)

    rows[0]["amount_minor"] = int(str(rows[0]["amount_minor"])) + 1
    with pytest.raises(cutover.PersonalAlipayCutoverError, match="total changed"):
        cutover._validate_cohort(rows)


def test_manifest_creates_deterministic_personal_replacements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rows = _reviewed_rows()
    monkeypatch.setattr(cutover, "_require_private_source", lambda _path: None)
    monkeypatch.setattr(cutover, "_validate_scope", lambda _connection: None)
    monkeypatch.setattr(cutover, "_load_legacy_rows", lambda _connection: rows)
    account_ref = uuid4()

    manifest, pairs = cutover.build_source_manifest(
        object(), source_path=tmp_path / "annual.csv", account_ref=account_ref  # type: ignore[arg-type]
    )
    replay, replay_pairs = cutover.build_source_manifest(
        object(), source_path=tmp_path / "annual.csv", account_ref=account_ref  # type: ignore[arg-type]
    )

    assert manifest == replay
    assert pairs == replay_pairs
    assert manifest.entity.entity_ref == cutover._PERSON_ENTITY
    assert manifest.business_unit.business_unit_ref == cutover._PERSON_UNIT
    assert len(manifest.candidates) == cutover._EXPECTED_COUNT
    assert sum(candidate.amount_minor for candidate in manifest.candidates) == 21_362_070
    assert len({candidate.candidate_ref for candidate in manifest.candidates}) == 1_574
    assert len({candidate.source_event_ref for candidate in manifest.candidates}) == 1_574
    assert all(
        candidate.source_system == "alipay_export_account2"
        for candidate in manifest.candidates
    )
    assert all(
        candidate.evidence_refs == (manifest.evidence[0].evidence_ref,)
        for candidate in manifest.candidates
    )
    assert all(pair.legacy_ref != pair.replacement_ref for pair in pairs)


def test_source_header_must_match_reviewed_owner_and_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "annual.csv"
    source.write_bytes("姓名:另一人\n支付宝账户:********5002".encode("gb18030"))
    monkeypatch.setattr(cutover, "_SOURCE_SIZE", source.stat().st_size)
    monkeypatch.setattr(cutover, "_SOURCE_SHA256", cutover._digest(source))

    with pytest.raises(cutover.PersonalAlipayCutoverError, match="owner or account suffix"):
        cutover._require_private_source(source)
