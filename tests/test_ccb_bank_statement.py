from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from ledgerbridge.bank_statement_contract import (
    BankStatementParserProfile,
)
from ledgerbridge.bank_statement_cutover_plan import (
    BankStatementExistingAccountPlan,
    BankStatementPlanError,
    ExistingStatementEvidenceMode,
)
from ledgerbridge.bank_statement_cutover_plan_builder import (
    BANK_STATEMENT_EXISTING_ACCOUNT_DRAFT_SCHEMA,
    BankStatementPlanBuildError,
    finalize_private_bank_statement_plan,
    load_private_bank_statement_plan,
)
from ledgerbridge.bank_statement_persistence import (
    BankStatementImportContext,
    _build_request,
)
from ledgerbridge.ccb_statement import CcbStatementError, parse_ccb_personal_xls
from ledgerbridge.models import EntityType

_OLE = bytes.fromhex("D0CF11E0A1B11AE1") + b"synthetic-ccb-xls"
_SUFFIX = "7564"
_EVIDENCE_REF = UUID("71000000-0000-4000-8000-000000000001")
_ENTITY_REF = UUID("71000000-0000-4000-8000-000000000002")
_BUSINESS_UNIT_REF = UUID("71000000-0000-4000-8000-000000000003")
_ACCOUNT_REF = UUID("71000000-0000-4000-8000-000000000004")


class _Sheet:
    def __init__(self, rows: list[list[str]]) -> None:
        self._rows = rows
        self.nrows = len(rows)
        self.ncols = 9

    def row_values(self, index: int) -> list[str]:
        return self._rows[index]


class _Book:
    nsheets = 1

    def __init__(self, rows: list[list[str]]) -> None:
        self._sheet = _Sheet(rows)
        self.released = False

    def sheet_by_index(self, index: int) -> _Sheet:
        assert index == 0
        return self._sheet

    def release_resources(self) -> None:
        self.released = True


def _rows() -> list[list[str]]:
    return [
        ["", "", "", "", "中国建设银行个人活期账户全部交易明细", "", "", "", ""],
        [
            "",
            f"卡号/账号:000000000000000{_SUFFIX}",
            "",
            "客户名称:合成测试用户",
            "",
            "起始日期:20260501",
            "",
            "结束日期:20260603",
            "",
        ],
        ["", "合成说明", "", "", "", "", "", "", ""],
        [
            "序号",
            "摘要",
            "币别",
            "钞汇",
            "交易日期",
            "交易金额",
            "账户余额",
            "交易地点/附言",
            "对方账号与户名",
        ],
        ["1", "消费", "人民币元", "钞", "20260501", "-12.34", "100.00", "网银", "1111/甲"],
        ["2", "转入", "人民币元", "钞", "20260603", "20.00", "120.00", "", "2222/乙"],
    ]


def _parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[list[str]] | None = None,
):
    source = (tmp_path / "synthetic.xls").resolve()
    source.write_bytes(_OLE)
    book = _Book(rows or _rows())
    monkeypatch.setattr("ledgerbridge.ccb_statement.xlrd.open_workbook", lambda **_: book)
    statement = parse_ccb_personal_xls(
        source,
        expected_sha256=hashlib.sha256(_OLE).hexdigest(),
        managed_account_suffix=_SUFFIX,
    )
    assert book.released
    return source, statement


def _plan(source: Path, statement: Any) -> BankStatementExistingAccountPlan:
    return BankStatementExistingAccountPlan.bind(
        statement,
        source_path=source,
        evidence_ref=_EVIDENCE_REF,
        entity_ref=_ENTITY_REF,
        business_unit_ref=_BUSINESS_UNIT_REF,
        managed_account_ref=_ACCOUNT_REF,
        expected_owner_kind=EntityType.PERSON,
        actor="worker:ccb-personal-import",
        reason="operator-confirmed synthetic CCB statement import",
    )


def test_parser_adapts_ccb_date_precision_and_source_fields_without_semantic_overload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, statement = _parse(tmp_path, monkeypatch)

    assert statement.parser_profile is BankStatementParserProfile.CCB_PERSONAL_XLS_V1
    assert statement.institution_code == "ccb"
    assert statement.account_suffix == _SUFFIX
    assert statement.period_start == date(2026, 5, 1)
    assert statement.period_end == date(2026, 6, 3)
    assert statement.monthly_transaction_counts == (("2026-05", 1), ("2026-06", 1))
    assert len(statement.transactions) == 2
    first = statement.transactions[0]
    assert first.amount_minor == -1234
    assert first.transaction_name == "消费 | 网银"
    assert first.counterparty_account == "1111"
    assert first.counterparty_name == "甲"
    assert first.counterparty_institution == ""
    assert first.occurred_at.hour == 0
    assert len(statement.transaction_set_sha256) == 64
    assert len(statement.parser_facts_sha256) == 64


def test_parser_accepts_empty_days_at_the_declared_period_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _rows()
    rows[1][5] = "起始日期:20260428"
    rows[1][7] = "结束日期:20260606"

    _, statement = _parse(tmp_path, monkeypatch, rows)

    assert statement.period_start == date(2026, 5, 1)
    assert statement.period_end == date(2026, 6, 3)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rows: rows[1].__setitem__(1, "卡号/账号:0000000000000009999"), "managed account"),
        (lambda rows: rows[3].__setitem__(1, "交易说明"), "header"),
        (lambda rows: rows[5].__setitem__(0, "3"), "sequence"),
        (lambda rows: rows[4].__setitem__(4, "20260430"), "metadata period"),
        (lambda rows: rows[5].__setitem__(6, "not-money"), "balance"),
    ],
)
def test_parser_rejects_identity_and_structure_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
    message: str,
) -> None:
    rows = _rows()
    mutate(rows)

    with pytest.raises(CcbStatementError, match=message):
        _parse(tmp_path, monkeypatch, rows)


def test_generic_plan_requires_person_ccb_and_strict_existing_evidence(
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


@pytest.mark.parametrize(
    "changes",
    [
        {"institution_code": "mybank"},
        {"source_system": "mybank_xlsx_export"},
        {
            "declared_media_type": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        },
        {"parser_profile": BankStatementParserProfile.MYBANK_XLSX_V1},
    ],
)
def test_contract_rejects_profile_identity_mixing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    _, statement = _parse(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="parser identity"):
        replace(statement, **changes)


def test_generic_persistence_request_carries_ccb_source_profile_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, statement = _parse(tmp_path, monkeypatch)

    request = _build_request(
        statement,
        BankStatementImportContext(
            owner_entity_ref=_ENTITY_REF,
            managed_account_ref=_ACCOUNT_REF,
            evidence_ref=_EVIDENCE_REF,
            actor="worker:ccb-personal-import",
            reason="synthetic persistence request",
        ),
    )

    assert request["institution_code"] == "ccb"
    assert request["parser_profile"] == "ccb_personal_xls_v1"
    assert request["source_system"] == "ccb_personal_xls_export"
    assert request["transaction_count"] == 2
    first = request["transactions"][0]
    assert first["transaction_name"] == "消费 | 网银"


def test_plan_builder_derives_all_statement_facts_and_rejects_legacy_evidence_policy(
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
        "parser": {"profile": "ccb_personal_xls_v1"},
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
            "actor": "worker:ccb-personal-import",
            "reason": "operator-confirmed synthetic CCB statement import",
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

    assert loaded.cutover.expected_sha256 == statement.source_sha256
    assert loaded.cutover.expected_transaction_count == 2
    assert loaded.cutover.expected_transaction_set_sha256 == statement.transaction_set_sha256
    assert loaded.cutover.expected_parser_facts_sha256 == statement.parser_facts_sha256
    assert loaded.cutover.expected_monthly_transaction_counts == (("2026-05", 1), ("2026-06", 1))

    rejected = (tmp_path / "rejected.json").resolve()
    payload["scope"]["evidence_mode"] = "REUSE_EXACT_OR_CREATE"
    rejected.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BankStatementPlanBuildError):
        finalize_private_bank_statement_plan(rejected, (tmp_path / "rejected-plan.json").resolve())
