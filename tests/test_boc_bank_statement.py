from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

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
from ledgerbridge.bank_statement_persistence import BankStatementImportContext, _build_request
from ledgerbridge.boc_statement import (
    BocStatementError,
    _clean_layout_cells,
    parse_boc_personal_pdf,
)
from ledgerbridge.models import EntityType

_SUFFIX = "4321"
_CARD = "6200000000008765"
_ACCOUNT = f"620000000000{_SUFFIX}"
_PASSWORD = "synthetic-password"
_EVIDENCE_REF = UUID("72000000-0000-4000-8000-000000000001")
_ENTITY_REF = UUID("72000000-0000-4000-8000-000000000002")
_BUSINESS_UNIT_REF = UUID("72000000-0000-4000-8000-000000000003")
_ACCOUNT_REF = UUID("72000000-0000-4000-8000-000000000004")
_WIDTHS = (12, 12, 8, 14, 14, 18, 14, 18, 18, 18, 22, 20)
_HEADERS = (
    "记账日期",
    "记账时间",
    "币别",
    "金额",
    "余额",
    "交易名称",
    "渠道",
    "网点名称",
    "附言",
    "对方账户名",
    "对方卡号/账号",
    "对方开户行",
)


class _Page:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self, *, extraction_mode: str) -> str:
        assert extraction_mode == "layout"
        return self._text


class _Reader:
    def __init__(self, text: str, *, encrypted: bool = True) -> None:
        self.is_encrypted = encrypted
        self.pages = [_Page(text)]

    def decrypt(self, password: str) -> int:
        return 1 if password == _PASSWORD else 0


def _line(values: tuple[str, ...]) -> str:
    return "".join(value.ljust(_WIDTHS[index]) for index, value in enumerate(values))


def _page_text(
    *,
    account: str = _ACCOUNT,
    debit: str = "12.34",
    first_amount: str = "20.00",
    first_channel: str = "手机银行",
    first_branch: str = "测试网点",
    first_note: str = "合成附言",
    first_counterparty: str = "乙",
    first_counterparty_account: str = "6222000000000002",
    first_counterparty_institution: str = "测试银行乙",
) -> str:
    return "\n".join(
        (
            "中国银行交易明细",
            "交易区间: 2026-05-01 至 2026-05-02 客户姓名: 合成测试用户 页数: 1 / 1",
            f"借记卡号: {_CARD} 借方发生数: {debit} 贷方发生数: 20.00 行数: 2",
            f"账号: {account} 打印时间: 2026/06/03 12:00:00",
            _line(_HEADERS),
            _line(
                (
                    "2026-05-02",
                    "10:00:00",
                    "人民币",
                    first_amount,
                    "107.66",
                    "转入",
                    first_channel,
                    first_branch,
                    first_note,
                    first_counterparty,
                    first_counterparty_account,
                    first_counterparty_institution,
                )
            ),
            _line(
                (
                    "2026-05-01",
                    "09:00:00",
                    "人民币",
                    "-12.34",
                    "87.66",
                    "消费",
                    "快捷支付",
                    "测试网点",
                    "合成附言",
                    "甲",
                    "6222000000000001",
                    "测试银行甲",
                )
            ),
            "END",
        )
    )


def _source_and_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    page_text: str | None = None,
    encrypted: bool = True,
) -> tuple[Path, str]:
    source = (tmp_path / "synthetic.pdf").resolve()
    source.write_bytes(b"%PDF-1.7\nsynthetic-boc")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    registry = (tmp_path / "passwords.json").resolve()
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "status": "verified",
                        "attachment_sha256": digest,
                        "password": _PASSWORD,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEDGERBRIDGE_BANK_STATEMENT_PASSWORD_REGISTRY", str(registry))
    monkeypatch.setattr(
        "ledgerbridge.boc_statement.PdfReader",
        lambda _: _Reader(page_text or _page_text(), encrypted=encrypted),
    )
    return source, digest


def _parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    page_text: str | None = None,
) -> tuple[Path, BankStatement]:
    source, digest = _source_and_registry(tmp_path, monkeypatch, page_text=page_text)
    return source, parse_boc_personal_pdf(
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
        actor="worker:boc-personal-import",
        reason="operator-confirmed synthetic BOC statement import",
    )


def test_parser_adapts_verified_boc_pdf_to_generic_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, statement = _parse(tmp_path, monkeypatch)

    assert statement.parser_profile is BankStatementParserProfile.BOC_PERSONAL_PDF_V1
    assert statement.institution_code == "boc"
    assert statement.source_system == "boc_transaction_statement"
    assert statement.declared_media_type == "application/pdf"
    assert statement.account_suffix == _SUFFIX
    assert statement.period_start == date(2026, 5, 1)
    assert statement.period_end == date(2026, 5, 2)
    assert statement.monthly_transaction_counts == (("2026-05", 2),)
    assert len(statement.transactions) == 2
    first = statement.transactions[0]
    assert first.amount_minor == 2000
    assert first.balance_minor == 10766
    assert first.counterparty_name == "乙"
    assert first.counterparty_account.endswith("0002")
    assert first.counterparty_institution == "测试银行乙"
    assert first.transaction_name == "转入 | 手机银行 | 测试网点 | 合成附言"
    assert first.occurred_at.tzinfo is not None
    assert len(statement.transaction_set_sha256) == 64
    assert len(statement.parser_facts_sha256) == 64


def test_parser_corrects_only_sign_loss_proven_by_balance_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _page_text(first_amount="-20.00")
    _, statement = _parse(tmp_path, monkeypatch, page_text=text)

    assert statement.transactions[0].amount_minor == 2000


def test_parser_discards_pdf_layout_rules_and_repairs_account_spill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _page_text(
        first_channel="手机银行 ----",
        first_branch="--------------- --",
        first_note="--------",
        first_counterparty="乙          1",
        first_counterparty_account="6222000000000002  测",
        first_counterparty_institution="试银行乙",
    )

    _, statement = _parse(tmp_path, monkeypatch, page_text=text)

    first = statement.transactions[0]
    assert first.counterparty_name == "乙"
    assert first.counterparty_account == "16222000000000002"
    assert first.counterparty_institution == "测试银行乙"
    assert first.transaction_name == "转入 | 手机银行"


def test_parser_repairs_real_single_space_account_spill_shape() -> None:
    cells = [
        "transfer",
        "mobile",
        "",
        "",
        "Synthetic Person 6",
        "222034000051377442",
        "Synthetic Bank",
    ]

    repaired = _clean_layout_cells(cells)

    assert repaired[4] == "Synthetic Person"
    assert repaired[5] == "6222034000051377442"
    assert repaired[6] == "Synthetic Bank"


@pytest.mark.parametrize(
    ("page_text", "message"),
    [
        (_page_text(account="6200000000009999"), "managed account"),
        (_page_text(debit="12.35"), "debit total"),
        (
            _page_text().replace("2026-05-01  09:00:00", "2026-04-30  09:00:00"),
            "declared period",
        ),
    ],
)
def test_parser_rejects_identity_period_and_totals_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_text: str,
    message: str,
) -> None:
    with pytest.raises(BocStatementError, match=message):
        _parse(tmp_path, monkeypatch, page_text=page_text)


def test_parser_requires_unique_digest_bound_verified_password_without_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, digest = _source_and_registry(tmp_path, monkeypatch)
    registry = Path(os.environ["LEDGERBRIDGE_BANK_STATEMENT_PASSWORD_REGISTRY"])
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "status": "verified",
                        "attachment_sha256": digest,
                        "password": _PASSWORD,
                    },
                    {
                        "status": "verified",
                        "attachment_sha256": digest,
                        "password": "second-synthetic-password",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BocStatementError) as captured:
        parse_boc_personal_pdf(
            source,
            expected_sha256=digest,
            managed_account_suffix=_SUFFIX,
        )
    assert _PASSWORD not in str(captured.value)


def test_parser_rejects_unencrypted_pdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, digest = _source_and_registry(tmp_path, monkeypatch, encrypted=False)

    with pytest.raises(BocStatementError, match="must be encrypted"):
        parse_boc_personal_pdf(
            source,
            expected_sha256=digest,
            managed_account_suffix=_SUFFIX,
        )


def test_generic_plan_and_persistence_preserve_boc_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
            actor="worker:boc-personal-import",
            reason="synthetic persistence request",
        ),
    )
    assert request["institution_code"] == "boc"
    assert request["parser_profile"] == "boc_personal_pdf_v1"
    assert request["source_system"] == "boc_transaction_statement"
    assert request["transaction_count"] == 2


def test_private_plan_builder_derives_boc_facts_without_secret_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, statement = _parse(tmp_path, monkeypatch)
    backup = (tmp_path / "backup").resolve()
    backup.mkdir()
    draft = (tmp_path / "draft.json").resolve()
    output = (tmp_path / "plan.json").resolve()
    payload = {
        "schema_version": BANK_STATEMENT_EXISTING_ACCOUNT_DRAFT_SCHEMA,
        "target_revision": "a" * 40,
        "parser": {"profile": "boc_personal_pdf_v1"},
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
            "actor": "worker:boc-personal-import",
            "reason": "operator-confirmed synthetic BOC statement import",
        },
        "safety": {
            "backup_directory": str(backup),
            "restore_report": str(backup / "restore.json"),
            "key_file": str((tmp_path / "key.json").resolve()),
            "artifact_root": str((tmp_path / "artifacts").resolve()),
        },
    }
    draft.write_text(json.dumps(payload), encoding="utf-8")

    finalize_private_bank_statement_plan(draft, output)
    loaded = load_private_bank_statement_plan(output)

    assert loaded.cutover.expected_transaction_count == 2
    assert loaded.cutover.expected_parser_facts_sha256 == statement.parser_facts_sha256
    assert loaded.cutover.expected_monthly_transaction_counts == (("2026-05", 2),)
    plan_text = output.read_text(encoding="utf-8")
    assert _PASSWORD not in plan_text
    assert "password" not in plan_text.casefold()
