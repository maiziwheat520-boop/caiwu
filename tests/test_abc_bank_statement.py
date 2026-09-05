from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from ledgerbridge.abc_statement import AbcStatementError, parse_abc_personal_pdf
from ledgerbridge.bank_statement_contract import BankStatement, BankStatementParserProfile
from ledgerbridge.bank_statement_cutover_plan import (
    BankStatementExistingAccountPlan,
    BankStatementPlanError,
    ExistingStatementEvidenceMode,
)
from ledgerbridge.bank_statement_cutover_plan_builder import (
    BANK_STATEMENT_EXISTING_ACCOUNT_DRAFT_SCHEMA,
    finalize_private_bank_statement_plan,
    load_private_bank_statement_plan,
)
from ledgerbridge.bank_statement_parsers import parse_bank_statement
from ledgerbridge.bank_statement_persistence import BankStatementImportContext, _build_request
from ledgerbridge.models import EntityType

_SUFFIX = "8642"
_ACCOUNT = f"622200000000{_SUFFIX}"
_EVIDENCE_REF = UUID("73000000-0000-4000-8000-000000000001")
_ENTITY_REF = UUID("73000000-0000-4000-8000-000000000002")
_BUSINESS_UNIT_REF = UUID("73000000-0000-4000-8000-000000000003")
_ACCOUNT_REF = UUID("73000000-0000-4000-8000-000000000004")
_WIDTHS = (12, 12, 12, 13, 13, 20, 14, 14, 30)
_HEADERS = (
    "交易日期",
    "交易时间",
    "交易摘要",
    "交易金额",
    "本次余额",
    "对手信息",
    "日 志 号",
    "交易渠道",
    "交易附言",
)


class _Page:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self, *, extraction_mode: str) -> str:
        assert extraction_mode == "layout"
        return self._text


class _Reader:
    def __init__(self, text: str, *, encrypted: bool = False) -> None:
        self.is_encrypted = encrypted
        self.pages = [_Page(text)]


def _line(values: tuple[str, ...]) -> str:
    return "".join(value.ljust(_WIDTHS[index]) for index, value in enumerate(values))


def _page_text(
    *,
    account: str = _ACCOUNT,
    second_balance: str = "120.00",
    second_date: str = "20260603",
    marker: str = "第1页\uff0c共1页",
) -> str:
    return "\n".join(
        (
            "中国农业银行账户活期交易明细清单",
            f"户名: 合成测试用户                                  账户: {account}",
            "币种: 人民币                                        汇钞标识: 汇",
            "起止日期: 20260501 - 20260603                        电子流水号: 10000000000000000001",
            "",
            _line(_HEADERS),
            _line(
                (
                    "20260501",
                    "090000",
                    "消费",
                    "-12.34",
                    "100.00",
                    "合成商户",
                    "A000000001",
                    "网银",
                    "合成附言",
                )
            ),
            _line(("", "", "", "", "", "6222000000001234", "", "", "")),
            _line(
                (
                    second_date,
                    "",
                    "转入",
                    "20.00",
                    second_balance,
                    "-",
                    "A000000002",
                    "掌银",
                    "",
                )
            ),
            "核对说明",
            marker,
        )
    )


def _source_and_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    page_text: str | None = None,
    encrypted: bool = False,
) -> tuple[Path, str]:
    source = (tmp_path / "synthetic-abc.pdf").resolve()
    source.write_bytes(b"%PDF-1.7\nsynthetic-abc")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "ledgerbridge.abc_statement.PdfReader",
        lambda _: _Reader(page_text or _page_text(), encrypted=encrypted),
    )
    return source, digest


def _parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    page_text: str | None = None,
) -> tuple[Path, BankStatement]:
    source, digest = _source_and_digest(tmp_path, monkeypatch, page_text=page_text)
    return source, parse_abc_personal_pdf(
        source,
        expected_sha256=digest,
        managed_account_suffix=_SUFFIX,
    )


def _plan(source: Path, statement: Any) -> BankStatementExistingAccountPlan:
    return BankStatementExistingAccountPlan.bind(
        statement,
        source_path=source,
        evidence_ref=_EVIDENCE_REF,
        entity_ref=_ENTITY_REF,
        business_unit_ref=_BUSINESS_UNIT_REF,
        managed_account_ref=_ACCOUNT_REF,
        expected_owner_kind=EntityType.PERSON,
        actor="worker:abc-personal-import",
        reason="operator-confirmed synthetic ABC statement import",
    )


def test_parser_adapts_unlocked_abc_pdf_to_generic_statement_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, statement = _parse(tmp_path, monkeypatch)
    repeated = parse_bank_statement(
        BankStatementParserProfile.ABC_PERSONAL_PDF_V1,
        source,
        expected_sha256=statement.source_sha256,
        managed_account_suffix=_SUFFIX,
    )

    assert statement.parser_profile is BankStatementParserProfile.ABC_PERSONAL_PDF_V1
    assert statement.institution_code == "abc"
    assert statement.source_system == "abc_personal_pdf_export"
    assert statement.declared_media_type == "application/pdf"
    assert statement.period_start == date(2026, 5, 1)
    assert statement.period_end == date(2026, 6, 3)
    assert statement.monthly_transaction_counts == (("2026-05", 1), ("2026-06", 1))
    assert len(statement.transactions) == 2
    first, second = statement.transactions
    assert first.amount_minor == -1234
    assert first.balance_minor == 10000
    assert first.counterparty_name == "合成商户"
    assert first.counterparty_account == "6222000000001234"
    assert first.transaction_name == "消费 | 网银 | 合成附言"
    assert second.occurred_at.hour == 0
    assert second.occurred_at.minute == 0
    assert statement.statement_ref == repeated.statement_ref
    assert statement.transaction_set_sha256 == repeated.transaction_set_sha256
    assert statement.parser_facts_sha256 == repeated.parser_facts_sha256


@pytest.mark.parametrize(
    ("page_text", "message"),
    [
        (_page_text(account="6222000000009999"), "managed account"),
        (_page_text(second_balance="120.01"), "balance chain"),
        (_page_text(second_date="20260604"), "declared period"),
        (_page_text(marker="第1页\uff0c共2页"), "page number"),
    ],
)
def test_parser_rejects_identity_balance_period_and_page_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_text: str,
    message: str,
) -> None:
    with pytest.raises(AbcStatementError, match=message):
        _parse(tmp_path, monkeypatch, page_text=page_text)


def test_parser_rejects_encrypted_abc_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, digest = _source_and_digest(tmp_path, monkeypatch, encrypted=True)

    with pytest.raises(AbcStatementError, match="already be unlocked"):
        parse_abc_personal_pdf(
            source,
            expected_sha256=digest,
            managed_account_suffix=_SUFFIX,
        )


def test_generic_plan_and_persistence_preserve_abc_person_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, statement = _parse(tmp_path, monkeypatch)
    plan = _plan(source, statement)

    assert plan.evidence_mode is ExistingStatementEvidenceMode.REUSE_EXISTING
    plan.require_statement(statement)
    with pytest.raises(BankStatementPlanError, match="conflicts"):
        plan.require_statement(replace(statement, parser_facts_sha256="0" * 64))
    with pytest.raises(ValueError, match="PERSON"):
        replace(plan, expected_owner_kind=EntityType.COMPANY)

    request = _build_request(
        statement,
        BankStatementImportContext(
            owner_entity_ref=_ENTITY_REF,
            managed_account_ref=_ACCOUNT_REF,
            evidence_ref=_EVIDENCE_REF,
            actor="worker:abc-personal-import",
            reason="synthetic persistence request",
        ),
    )
    assert request["institution_code"] == "abc"
    assert request["parser_profile"] == "abc_personal_pdf_v1"
    assert request["source_system"] == "abc_personal_pdf_export"
    assert request["transaction_count"] == 2


def test_private_plan_builder_derives_abc_facts_without_credential_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, statement = _parse(tmp_path, monkeypatch)
    backup = (tmp_path / "backup").resolve()
    backup.mkdir()
    draft = (tmp_path / "draft.json").resolve()
    output = (tmp_path / "plan.json").resolve()
    payload = {
        "schema_version": BANK_STATEMENT_EXISTING_ACCOUNT_DRAFT_SCHEMA,
        "target_revision": "a" * 40,
        "parser": {"profile": "abc_personal_pdf_v1"},
        "source": {"path": str(source), "account_suffix": _SUFFIX},
        "scope": {
            "evidence_ref": str(_EVIDENCE_REF),
            "evidence_mode": "REUSE_EXISTING",
            "owner_entity_ref": str(_ENTITY_REF),
            "business_unit_ref": str(_BUSINESS_UNIT_REF),
            "owner_kind": "PERSON",
        },
        "account": {"managed_account_ref": str(_ACCOUNT_REF)},
        "audit": {
            "actor": "worker:abc-personal-import",
            "reason": "operator-confirmed synthetic ABC statement import",
        },
        "safety": {
            "backup_directory": str(backup),
            "restore_report": str(backup / "restore.json"),
            "key_file": str((tmp_path / "key.json").resolve()),
            "artifact_root": str((tmp_path / "artifacts").resolve()),
        },
    }
    draft.write_text(json.dumps(payload), encoding="utf-8")
    draft.chmod(0o600)

    finalize_private_bank_statement_plan(draft, output)
    loaded = load_private_bank_statement_plan(output)

    assert loaded.cutover.expected_transaction_count == 2
    assert loaded.cutover.expected_parser_facts_sha256 == statement.parser_facts_sha256
    assert loaded.cutover.expected_monthly_transaction_counts == (
        ("2026-05", 1),
        ("2026-06", 1),
    )
    plan_text = output.read_text(encoding="utf-8").casefold()
    assert "password" not in plan_text
    assert "credential" not in plan_text
    assert "secret" not in plan_text
