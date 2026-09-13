from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from ledgerbridge.local_candidates import (
    _PLATFORMS,
    ALIPAY_TRANSACTION_REVIEW,
    CANDIDATE_BATCH_SCHEMA,
    WECHAT_TRANSACTION_REVIEW,
    LocalCandidateError,
    _candidate,
    _identity_scope,
    _one_account,
    _reconcile,
    load_candidate_batch,
)
from ledgerbridge.local_payments import DIRECTIONLESS, PaymentExport, PaymentRow
from ledgerbridge.review_risk import derive_review_risks

_ZONE = ZoneInfo("Asia/Shanghai")
_WECHAT = _PLATFORMS["wechat"]


def _export(account_hint: str) -> PaymentExport:
    """One synthetic export, carrying only what `_one_account` looks at."""

    moment = datetime(2026, 3, 1, tzinfo=_ZONE)
    return PaymentExport(
        period_start=moment,
        period_end=moment,
        exported_at=moment,
        export_kind="全部",
        account_hint=account_hint,
        rows=(),
    )


def _row(**changed: object) -> PaymentRow:
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
    return PaymentRow(**fields)  # type: ignore[arg-type]


def _manifest(tmp_path: Path, **changed: object) -> Path:
    payload: dict[str, object] = {
        "schema_version": CANDIDATE_BATCH_SCHEMA,
        "platform": "wechat",
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


def test_an_expenditure_is_stated_in_the_platform_import_shape() -> None:
    """The shape Core's released platform import writes, field for field."""

    evidence_ref = uuid4()
    candidate = _candidate(_WECHAT, (), _row(), evidence_ref)

    assert candidate.amount_minor == -2600
    assert candidate.category_code == WECHAT_TRANSACTION_REVIEW
    assert candidate.source_system == "wechat_pay_export"
    assert candidate.accounting_month == "2026-03"
    assert candidate.evidence_refs == (evidence_ref,)
    assert candidate.summary == "微信 | 2026-03-01 | 支出 | 商户消费 | 合成商户 | 零钱通 | 支付成功"
    assert candidate.confidence_basis_points == 9900


def test_alipay_uses_the_name_and_category_the_released_import_uses() -> None:
    candidate = _candidate(_PLATFORMS["alipay"], (), _row(), uuid4())

    assert candidate.source_system == "alipay_export"
    assert candidate.category_code == ALIPAY_TRANSACTION_REVIEW
    assert candidate.summary.startswith("支付宝 | 2026-03-01 | 支出 | ")


def test_a_directionless_row_keeps_its_magnitude_and_says_so() -> None:
    candidate = _candidate(
        _WECHAT,
        (),
        _row(direction=DIRECTIONLESS, kind="零钱提现", counterparty="", status="提现已到账"),
        uuid4(),
    )

    assert candidate.amount_minor == 2600
    assert candidate.category_code == WECHAT_TRANSACTION_REVIEW
    assert candidate.summary == "微信 | 2026-03-01 | 不计收支 | 零钱提现 | / | 零钱通 | 提现已到账"


def test_a_field_cannot_shift_the_fields_after_it() -> None:
    """The contract is positional, so a separator inside a name must not split it."""

    candidate = _candidate(_WECHAT, (), _row(counterparty="甲 | 乙", funding=""), uuid4())

    fields = [part.strip() for part in candidate.summary.split("|")]
    assert len(fields) == 7
    assert fields[4] == "甲 / 乙"
    assert fields[5] == "/"


def test_review_risks_are_raised_on_what_the_importer_writes() -> None:
    """The reason the shape matters: a local candidate is no longer exempt.

    A payment funded from a bank card has to be matched to that card's
    statement before it is confirmed. Written the old way, Core raised nothing.
    """

    candidate = _candidate(_WECHAT, (), _row(funding="中国银行储蓄卡(0000)"), uuid4())

    risks = derive_review_risks(
        source_system=candidate.source_system,
        category_code=candidate.category_code,
        summary=candidate.summary,
    )

    assert "FUNDING_STATEMENT_REQUIRED" in {risk.code for risk in risks}


def test_candidate_references_are_derived_from_the_transaction() -> None:
    """Re-running the builder must produce the same manifest, or replay is a fiction."""

    first = _candidate(_WECHAT, (), _row(), UUID(int=1))
    again = _candidate(_WECHAT, (), _row(counterparty="改了昵称"), UUID(int=1))

    assert first.candidate_ref == again.candidate_ref
    assert first.source_event_ref == again.source_event_ref
    assert first.operation_id == again.operation_id


def test_refuses_a_manifest_naming_a_platform_that_is_not_read_here(tmp_path: Path) -> None:
    with pytest.raises(LocalCandidateError, match="platform must be one of"):
        load_candidate_batch(_manifest(tmp_path, platform="paypal"))


def test_two_platforms_do_not_mint_the_same_reference_from_one_order_number() -> None:
    """Two order numbers can collide across platforms; two candidates may not."""

    wechat = _candidate(_WECHAT, (), _row(), UUID(int=1))
    alipay = _PLATFORMS["alipay"]
    other = _candidate(alipay, alipay.ref_scope, _row(), UUID(int=1))

    assert wechat.candidate_ref != other.candidate_ref
    assert wechat.source_event_ref != other.source_event_ref


def test_refuses_a_batch_whose_exports_name_different_accounts(tmp_path: Path) -> None:
    """Alipay names its login; two logins in one batch is two books' worth of facts."""

    exports = [
        (tmp_path / "a.csv", b"", _export("owner-one")),
        (tmp_path / "b.csv", b"", _export("owner-two")),
    ]

    with pytest.raises(LocalCandidateError, match="name different accounts"):
        _one_account(exports)


def test_a_batch_of_exports_naming_no_account_is_allowed(tmp_path: Path) -> None:
    """WeChat's bill names none, so there is nothing to compare and nothing claimed."""

    _one_account([(tmp_path / "a.xlsx", b"", _export("")) for _ in range(2)])


def test_one_order_number_in_two_accounts_is_two_facts() -> None:
    """Alipay writes the same order number into both sides of a transfer."""

    alipay = _PLATFORMS["alipay"]
    paid = _candidate(alipay, _identity_scope(alipay, "one@example.com"), _row(), UUID(int=1))
    received = _candidate(alipay, _identity_scope(alipay, "two@example.com"), _row(), UUID(int=1))

    assert paid.candidate_ref != received.candidate_ref


def test_an_account_is_not_written_into_the_reference_it_scopes() -> None:
    """A login is an email or a phone number; a stored reference must not carry it."""

    alipay = _PLATFORMS["alipay"]
    scope = _identity_scope(alipay, "owner@example.com")

    assert "owner@example.com" not in "".join(scope)
