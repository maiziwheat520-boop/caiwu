from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from ledgerbridge.bank_statement_contract import BankStatement, BankStatementParserProfile
from ledgerbridge.bank_statement_cutover_plan import (
    BankStatementExistingAccountPlan,
    ExistingStatementEvidenceMode,
)
from ledgerbridge.bank_statement_cutover_plan_builder import (
    BANK_STATEMENT_EXISTING_ACCOUNT_DRAFT_SCHEMA,
    BANK_STATEMENT_EXISTING_ACCOUNT_DRAFT_SCHEMA_V2,
    finalize_private_bank_statement_plan,
    load_private_bank_statement_plan,
)
from ledgerbridge.bank_statement_parsers import parse_bank_statement
from ledgerbridge.bank_statement_persistence import BankStatementImportContext, _build_request
from ledgerbridge.models import EntityType
from ledgerbridge.mybank_statement import (
    MyBankEmptyStatementError,
    MyBankStatementError,
    parse_mybank_company_daily_xlsx,
    parse_mybank_company_range_xlsx,
)
from ledgerbridge.mybank_statement_cutover import (
    ProductionCounts,
    _expected_after_existing_account,
)

_HEADERS = (
    "账务流水号",
    "提交时间",
    "交易时间",
    "交易名称",
    "借方金额(收)",
    "贷方金额(支)",
    "余额",
    "对方户名",
    "对方账号",
    "对方机构",
    "备注",
)
_SUFFIX = "7968"
_ENTITY_REF = UUID("72000000-0000-4000-8000-000000000001")
_BUSINESS_UNIT_REF = UUID("72000000-0000-4000-8000-000000000002")
_ACCOUNT_REF = UUID("72000000-0000-4000-8000-000000000003")
_EVIDENCE_REF = UUID("72000000-0000-4000-8000-000000000004")
_FW_LEFT = "\N{FULLWIDTH LEFT PARENTHESIS}"
_FW_RIGHT = "\N{FULLWIDTH RIGHT PARENTHESIS}"


def _column_name(index: int) -> str:
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _inline_cell(reference: str, value: str) -> str:
    return f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>'


def _row(number: int, values: tuple[str, ...]) -> str:
    cells = "".join(
        _inline_cell(f"{_column_name(index)}{number}", value)
        for index, value in enumerate(values, start=1)
    )
    return f'<row r="{number}">{cells}</row>'


def _rows(*, empty: bool = False) -> list[tuple[str, ...]]:
    rows = [
        ("浙江网商银行企业账户交易明细",),
        ("企业名称", "合成测试公司", "", "", "企业账号", f"000000000000{_SUFFIX}(人民币)"),
        (
            "借方交易笔数",
            "0笔" if empty else "1笔",
            "",
            "",
            "借方交易金额",
            "￥0" if empty else "￥125.34",
        ),
        (
            "贷方交易笔数",
            "0笔" if empty else "1笔",
            "",
            "",
            "贷方交易金额",
            "￥0" if empty else "￥20.00",
        ),
        _HEADERS,
    ]
    if not empty:
        rows.extend(
            [
                (
                    "synthetic-0001",
                    "2026-01-02 06:00:00",
                    "2026-01-02 06:07:08",
                    "转入",
                    "125.34",
                    "",
                    "5125.34",
                    "合成商户甲",
                    "0000000000001111",
                    "合成银行",
                    "货款",
                ),
                (
                    "synthetic-0002",
                    "2026-01-02 03:00:00",
                    "2026-01-02 03:04:05",
                    "消费",
                    "",
                    "20.00",
                    "5000.00",
                    "合成商户乙",
                    "0000000000002222",
                    "合成银行",
                    "",
                ),
            ]
        )
    return rows


def _write_xlsx(
    path: Path,
    rows: list[tuple[str, ...]],
    *,
    sheet_name: str = "sheet1",
) -> bytes:
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:A1"/><sheetData>'
        f"{''.join(_row(number, values) for number, values in enumerate(rows, start=1))}"
        "</sheetData></worksheet>"
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>'
        ),
        "xl/worksheets/sheet1.xml": worksheet,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value.encode("utf-8"))
    return path.read_bytes()


def _parse(
    path: Path,
    rows: list[tuple[str, ...]] | None = None,
) -> BankStatement:
    raw = _write_xlsx(path, rows or _rows())
    return parse_mybank_company_daily_xlsx(
        path,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        managed_account_suffix=_SUFFIX,
    )


def test_company_daily_parser_is_stable_and_maps_eleven_columns(tmp_path: Path) -> None:
    path = (tmp_path / "synthetic-company.xlsx").resolve()
    statement = _parse(path)
    again = parse_bank_statement(
        BankStatementParserProfile.MYBANK_COMPANY_DAILY_XLSX_V2,
        path,
        expected_sha256=statement.source_sha256,
        managed_account_suffix=_SUFFIX,
    )

    assert statement == again
    assert statement.parser_profile is BankStatementParserProfile.MYBANK_COMPANY_DAILY_XLSX_V2
    assert statement.source_system == "mybank_daily_statement"
    assert statement.institution_code == "mybank"
    assert statement.header_row_number == 5
    assert statement.period_start == statement.period_end
    assert len(statement.transactions) == 2
    assert statement.transactions[0].amount_minor == 12_534
    assert statement.transactions[0].transaction_name == "转入 | 货款"
    assert statement.transactions[1].amount_minor == -2_000
    assert len(statement.parser_facts_sha256) == 64


@pytest.mark.parametrize(
    "headers",
    [
        (
            "账务流水号",
            "提交时间",
            "交易时间",
            "交易名称",
            f"借方金额{_FW_LEFT}收{_FW_RIGHT}",
            f"贷方金额{_FW_LEFT}支{_FW_RIGHT}",
            "余额",
            "对方户名",
            "对方账号",
            "对方机构",
            "备注",
        ),
        (
            "账务流水号",
            "交易时间",
            "交易名称",
            f"借方金额{_FW_LEFT}收{_FW_RIGHT}",
            f"贷方金额{_FW_LEFT}支{_FW_RIGHT}",
            "余额",
            "对方户名",
            "对方机构",
            "备注",
        ),
        (
            "账务流水号",
            "交易时间",
            f"借方金额{_FW_LEFT}收{_FW_RIGHT}",
            f"贷方金额{_FW_LEFT}支{_FW_RIGHT}",
            "余额",
            "对方户名",
            "对方账号",
            "对方机构",
            "备注",
        ),
    ],
)
def test_company_range_parser_accepts_official_header_variants(
    tmp_path: Path,
    headers: tuple[str, ...],
) -> None:
    metadata = _rows()[:4]
    transactions: list[tuple[str, ...]]
    if len(headers) == 11:
        transactions = [
            (
                "synthetic-0001",
                "",
                "2026-01-03 06:07:08",
                "转入",
                "125.34",
                "",
                "5125.34",
                "合成商户甲",
                "0000000000001111",
                "合成银行",
                "货款",
            ),
            (
                "synthetic-0002",
                "2026-01-02 03:04:05",
                "2026-01-02 03:04:05",
                "消费",
                "",
                "20.00",
                "5000.00",
                "合成商户乙",
                "0000000000002222",
                "合成银行",
                "消费",
            ),
        ]
    elif headers[2] == "交易名称":
        transactions = [
            (
                "synthetic-0001",
                "2026-01-03 06:07:08",
                "转入",
                "125.34",
                "",
                "5125.34",
                "合成商户甲",
                "合成银行",
                "货款",
            ),
            (
                "synthetic-0002",
                "2026-01-02 03:04:05",
                "消费",
                "",
                "20.00",
                "5000.00",
                "合成商户乙",
                "合成银行",
                "消费",
            ),
        ]
    else:
        transactions = [
            (
                "synthetic-0001",
                "2026-01-03 06:07:08",
                "125.34",
                "",
                "5125.34",
                "合成商户甲",
                "0000000000001111",
                "合成银行",
                "货款",
            ),
            (
                "synthetic-0002",
                "2026-01-02 03:04:05",
                "",
                "20.00",
                "5000.00",
                "合成商户乙",
                "0000000000002222",
                "合成银行",
                "消费",
            ),
        ]
    path = (tmp_path / "synthetic-company-range.xlsx").resolve()
    raw = _write_xlsx(path, [*metadata, headers, *transactions])

    statement = parse_mybank_company_range_xlsx(
        path,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        managed_account_suffix=_SUFFIX,
    )

    assert statement.parser_profile is BankStatementParserProfile.MYBANK_COMPANY_RANGE_XLSX_V3
    assert statement.period_start.isoformat() == "2026-01-02"
    assert statement.period_end.isoformat() == "2026-01-03"
    assert len(statement.transactions) == 2
    assert statement.transactions[0].amount_minor == 12_534
    assert statement.transactions[1].amount_minor == -2_000


def test_company_daily_parser_recognizes_valid_empty_statement(tmp_path: Path) -> None:
    path = (tmp_path / "synthetic-empty.xlsx").resolve()
    raw = _write_xlsx(path, _rows(empty=True))

    with pytest.raises(MyBankEmptyStatementError, match="no transactions"):
        parse_mybank_company_daily_xlsx(
            path,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            managed_account_suffix=_SUFFIX,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rows: rows.__setitem__(0, ("错误标题",)), "title"),
        (
            lambda rows: rows.__setitem__(
                1, ("企业名称", "合成测试公司", "", "", "企业账号", "0000000000009999(人民币)")
            ),
            "managed account",
        ),
        (lambda rows: rows[5].__setitem__(4, ""), "direction"),
        (lambda rows: rows[5].__setitem__(6, "9999.99"), "balance"),
        (lambda rows: rows[3].__setitem__(5, "￥21.00"), "credit summary"),
        (lambda rows: rows[6].__setitem__(2, "2026-01-03 03:04:05"), "dates conflict"),
    ],
)
def test_company_daily_parser_rejects_identity_and_arithmetic_drift(
    tmp_path: Path,
    mutate: Callable[[list[list[str]]], None],
    message: str,
) -> None:
    mutable = [list(row) for row in _rows()]
    mutate(mutable)
    path = (tmp_path / "synthetic-invalid.xlsx").resolve()
    raw = _write_xlsx(path, [tuple(row) for row in mutable])

    with pytest.raises(MyBankStatementError, match=message):
        parse_mybank_company_daily_xlsx(
            path,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            managed_account_suffix=_SUFFIX,
        )


def test_company_profile_requires_company_owner_and_persists_exact_identity(tmp_path: Path) -> None:
    path = (tmp_path / "synthetic-company.xlsx").resolve()
    statement = _parse(path)
    plan = BankStatementExistingAccountPlan.bind(
        statement,
        source_path=path,
        evidence_ref=_EVIDENCE_REF,
        entity_ref=_ENTITY_REF,
        business_unit_ref=_BUSINESS_UNIT_REF,
        managed_account_ref=_ACCOUNT_REF,
        expected_owner_kind=EntityType.COMPANY,
        actor="worker:company-statement-import",
        reason="operator-confirmed synthetic company statement import",
    )
    plan.require_statement(statement)

    with pytest.raises(ValueError, match="COMPANY"):
        replace(plan, expected_owner_kind=EntityType.PERSON)

    request = _build_request(
        statement,
        BankStatementImportContext(
            owner_entity_ref=_ENTITY_REF,
            managed_account_ref=_ACCOUNT_REF,
            evidence_ref=_EVIDENCE_REF,
            actor="worker:company-statement-import",
            reason="synthetic persistence request",
        ),
    )
    assert request["parser_profile"] == "mybank_company_daily_xlsx_v2"
    assert request["source_system"] == "mybank_daily_statement"
    assert request["institution_code"] == "mybank"


def test_company_daily_parser_requires_exact_sheet_name(tmp_path: Path) -> None:
    path = (tmp_path / "synthetic-company.xlsx").resolve()
    raw = _write_xlsx(path, _rows(), sheet_name="Sheet1")

    with pytest.raises(MyBankStatementError, match="worksheet name"):
        parse_mybank_company_daily_xlsx(
            path,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            managed_account_suffix=_SUFFIX,
        )


@pytest.mark.parametrize("evidence_mode", ["CREATE_NEW", "REUSE_EXISTING"])
def test_generic_plan_builder_materializes_company_profile(
    tmp_path: Path,
    evidence_mode: str,
) -> None:
    source = (tmp_path / "synthetic-company.xlsx").resolve()
    statement = _parse(source)
    backup = (tmp_path / "backup").resolve()
    backup.mkdir()
    draft = (tmp_path / "draft.json").resolve()
    output = (tmp_path / "plan.json").resolve()
    draft.write_text(
        json.dumps(
            {
                "schema_version": BANK_STATEMENT_EXISTING_ACCOUNT_DRAFT_SCHEMA,
                "target_revision": "a" * 40,
                "parser": {"profile": "mybank_company_daily_xlsx_v2"},
                "source": {"path": str(source), "account_suffix": _SUFFIX},
                "scope": {
                    "evidence_ref": str(_EVIDENCE_REF),
                    "evidence_mode": evidence_mode,
                    "owner_entity_ref": str(_ENTITY_REF),
                    "business_unit_ref": str(_BUSINESS_UNIT_REF),
                    "owner_kind": "COMPANY",
                },
                "account": {"managed_account_ref": str(_ACCOUNT_REF)},
                "audit": {
                    "actor": "worker:company-statement-import",
                    "reason": "operator-confirmed synthetic company statement import",
                },
                "safety": {
                    "backup_directory": str(backup),
                    "restore_report": str(backup / "restore.json"),
                    "key_file": str((tmp_path / "key.json").resolve()),
                    "artifact_root": str((tmp_path / "artifacts").resolve()),
                },
            }
        ),
        encoding="utf-8",
    )

    finalize_private_bank_statement_plan(draft, output)
    loaded = load_private_bank_statement_plan(output)

    assert loaded.cutover.parser_profile is statement.parser_profile
    assert loaded.cutover.expected_owner_kind is EntityType.COMPANY
    assert loaded.cutover.expected_transaction_count == 2
    assert loaded.cutover.expected_parser_facts_sha256 == statement.parser_facts_sha256
    assert loaded.cutover.evidence_mode is ExistingStatementEvidenceMode(evidence_mode)
    if loaded.cutover.evidence_mode is ExistingStatementEvidenceMode.CREATE_NEW:
        before = ProductionCounts(
            evidence_objects=4,
            encrypted_object_identities=4,
            encrypted_blob_versions=4,
            managed_accounts=5,
            managed_account_lifecycles=5,
            account_registry_operations=5,
            managed_account_aliases=5,
            account_business_unit_assignments=5,
            fact_business_unit_allocation_sets=0,
            fact_business_unit_allocation_items=0,
            bank_statements=0,
            bank_statement_transactions=0,
            bank_statement_observations=0,
            bank_statement_reviews=0,
            candidates=7,
            latest_pending_candidates=2,
            audit_events=20,
        )
        after = _expected_after_existing_account(before, loaded.cutover)
        assert after.evidence_objects == before.evidence_objects + 1
        assert after.encrypted_object_identities == before.encrypted_object_identities + 1
        assert after.encrypted_blob_versions == before.encrypted_blob_versions + 1
        assert after.managed_accounts == before.managed_accounts


def test_range_plan_binds_expected_new_transaction_count(tmp_path: Path) -> None:
    source = (tmp_path / "synthetic-company-range.xlsx").resolve()
    rows = _rows()
    rows[4] = tuple(value.replace("(", _FW_LEFT).replace(")", _FW_RIGHT) for value in rows[4])
    rows[5] = (
        rows[5][0],
        "2026-01-03 06:00:00",
        "2026-01-03 06:07:08",
        *rows[5][3:],
    )
    raw = _write_xlsx(source, rows)
    statement = parse_mybank_company_range_xlsx(
        source,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        managed_account_suffix=_SUFFIX,
    )
    backup = (tmp_path / "backup").resolve()
    backup.mkdir()
    draft = (tmp_path / "draft.json").resolve()
    output = (tmp_path / "plan.json").resolve()
    draft.write_text(
        json.dumps(
            {
                "schema_version": BANK_STATEMENT_EXISTING_ACCOUNT_DRAFT_SCHEMA_V2,
                "target_revision": "a" * 40,
                "parser": {"profile": "mybank_company_range_xlsx_v3"},
                "source": {"path": str(source), "account_suffix": _SUFFIX},
                "scope": {
                    "evidence_ref": str(_EVIDENCE_REF),
                    "evidence_mode": "CREATE_NEW",
                    "owner_entity_ref": str(_ENTITY_REF),
                    "business_unit_ref": str(_BUSINESS_UNIT_REF),
                    "owner_kind": "COMPANY",
                    "expected_new_transaction_count": 1,
                },
                "account": {"managed_account_ref": str(_ACCOUNT_REF)},
                "audit": {"actor": "worker:test", "reason": "synthetic range import"},
                "safety": {
                    "backup_directory": str(backup),
                    "restore_report": str(backup / "restore.json"),
                    "key_file": str((tmp_path / "key.json").resolve()),
                    "artifact_root": str((tmp_path / "artifacts").resolve()),
                },
            }
        ),
        encoding="utf-8",
    )

    finalize_private_bank_statement_plan(draft, output)
    loaded = load_private_bank_statement_plan(output)

    assert loaded.cutover.expected_new_transaction_count == 1
    assert loaded.cutover.expected_transaction_count == len(statement.transactions)
