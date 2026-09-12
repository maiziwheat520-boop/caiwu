from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from ledgerbridge.local_candidates import (
    CANDIDATE_BATCH_SCHEMA,
    INTERNAL_TRANSFER,
    UNCLASSIFIED,
    LocalCandidateError,
    _candidate,
    _reconcile,
    load_candidate_batch,
)
from ledgerbridge.local_wechat import DIRECTIONLESS, WeChatRow

_ZONE = ZoneInfo("Asia/Shanghai")


def _row(**changed: object) -> WeChatRow:
    """One synthetic row. Nothing here is a real transaction."""

    fields: dict[str, object] = {
        "occurred_at": datetime(2026, 3, 1, 9, 0, tzinfo=_ZONE),
        "kind": "商户消费",
        "counterparty": "合成商户",
        "product": "合成商品",
        "direction": "支出",
        "amount_minor": 2600,
        "funding": "零钱通",
        "status": "支付成功",
        "serial": "1" * 28,
        "merchant_serial": "2" * 30,
        "note": "/",
    }
    fields.update(changed)
    return WeChatRow(**fields)  # type: ignore[arg-type]


def _manifest(tmp_path: Path, **changed: object) -> Path:
    payload: dict[str, object] = {
        "schema_version": CANDIDATE_BATCH_SCHEMA,
        "book": {"entity_ref": str(uuid4()), "business_unit_ref": str(uuid4())},
        "sources": [str(tmp_path / "export.xlsx")],
        "description": "synthetic",
    }
    payload.update(changed)
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_loads_a_batch_manifest(tmp_path: Path) -> None:
    batch = load_candidate_batch(_manifest(tmp_path))

    assert len(batch.sources) == 1
    assert batch.description == "synthetic"


def test_refuses_a_manifest_of_another_schema(tmp_path: Path) -> None:
    with pytest.raises(LocalCandidateError, match="must declare"):
        load_candidate_batch(_manifest(tmp_path, schema_version="something.else.v1"))


def test_refuses_a_manifest_carrying_an_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(LocalCandidateError, match="keys must be exactly"):
        load_candidate_batch(_manifest(tmp_path, accounts=[]))


def test_refuses_a_relative_source_path(tmp_path: Path) -> None:
    with pytest.raises(LocalCandidateError, match="must be absolute"):
        load_candidate_batch(_manifest(tmp_path, sources=["export.xlsx"]))


def test_refuses_one_file_named_twice(tmp_path: Path) -> None:
    path = str(tmp_path / "export.xlsx")
    with pytest.raises(LocalCandidateError, match="names one file twice"):
        load_candidate_batch(_manifest(tmp_path, sources=[path, path]))


def test_refuses_a_manifest_naming_no_source(tmp_path: Path) -> None:
    with pytest.raises(LocalCandidateError, match="names no source file"):
        load_candidate_batch(_manifest(tmp_path, sources=[]))


def test_refuses_a_manifest_that_is_not_json(tmp_path: Path) -> None:
    path = tmp_path / "batch.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(LocalCandidateError, match="not valid JSON"):
        load_candidate_batch(path)


def test_a_later_export_wins_on_description(tmp_path: Path) -> None:
    """A counterparty's display name is their profile, not a fact of the payment."""

    held = _row(counterparty="旧昵称")
    found = _row(counterparty="新昵称", status="已全额退款")

    assert _reconcile(held, found) is found


def test_two_exports_may_not_disagree_about_the_money(tmp_path: Path) -> None:
    with pytest.raises(LocalCandidateError, match="rather than a description"):
        _reconcile(_row(), _row(amount_minor=2700))


def test_two_exports_may_not_disagree_about_when_it_happened(tmp_path: Path) -> None:
    with pytest.raises(LocalCandidateError, match="rather than a description"):
        _reconcile(_row(), _row(occurred_at=datetime(2026, 3, 2, 9, 0, tzinfo=_ZONE)))


def test_an_expenditure_becomes_a_negative_unclassified_candidate() -> None:
    evidence_ref = uuid4()
    candidate = _candidate(_row(), evidence_ref)

    assert candidate.amount_minor == -2600
    assert candidate.category_code == UNCLASSIFIED
    assert candidate.accounting_month == "2026-03"
    assert candidate.evidence_refs == (evidence_ref,)
    # Nothing looked at what this was for, and the queue should say so.
    assert candidate.confidence_basis_points == 0


def test_a_directionless_row_keeps_its_magnitude_and_its_own_category() -> None:
    candidate = _candidate(
        _row(direction=DIRECTIONLESS, kind="零钱提现", counterparty="/", status="提现已到账"),
        uuid4(),
    )

    assert candidate.amount_minor == 2600
    assert candidate.category_code == INTERNAL_TRANSFER
    assert "零钱提现" in candidate.summary


def test_candidate_references_are_derived_from_the_transaction() -> None:
    """Re-running the builder must produce the same manifest, or replay is a fiction."""

    first = _candidate(_row(), UUID(int=1))
    again = _candidate(_row(counterparty="改了昵称"), UUID(int=1))

    assert first.candidate_ref == again.candidate_ref
    assert first.source_event_ref == again.source_event_ref
    assert first.operation_id == again.operation_id
