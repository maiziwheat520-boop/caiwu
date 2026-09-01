"""Fail-closed parser for MYbank XLSX statement exports."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Final, Never
from uuid import UUID, uuid5
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

_XLSX_MEDIA_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_MAIN_NS: Final = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_REL_NS: Final = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NAMESPACE: Final = UUID("e2c4d403-fbc3-4dbf-9fd4-cc886eef5c15")
_EXPECTED_HEADERS: Final = (
    "交易时间",
    "交易金额",
    "余额",
    "对方户名",
    "对方账号",
    "对方机构名称",
    "交易流水号",
    "交易名称",
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SUFFIX = re.compile(r"^[0-9]{4,8}$")
_CELL_REFERENCE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
_MONEY = re.compile(r"^[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]{1,2})?$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_XML_BYTES = 20 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 200
_MAX_TOTAL_UNCOMPRESSED_BYTES = 80 * 1024 * 1024
_MAX_ROWS = 100_000
_MAX_COLUMNS = 100


class MyBankStatementError(RuntimeError):
    """The source file could not prove a valid MYbank statement."""


class MyBankEmptyStatementError(MyBankStatementError):
    """The source is a valid MYbank export with no transactions to import."""


@dataclass(frozen=True, slots=True)
class MyBankTransaction:
    source_event_ref: UUID
    source_row_number: int
    source_row_sha256: str
    occurred_at: datetime
    amount_minor: int
    balance_minor: int
    counterparty_name: str
    counterparty_account: str
    counterparty_institution: str
    transaction_serial: str
    transaction_name: str


@dataclass(frozen=True, slots=True)
class MyBankStatement:
    statement_ref: UUID
    source_sha256: str
    source_size: int
    declared_media_type: str
    currency: str
    institution_code: str
    account_suffix: str
    worksheet_index: int
    header_row_number: int
    transactions: tuple[MyBankTransaction, ...]


def parse_mybank_xlsx(
    source_path: Path,
    *,
    expected_sha256: str,
    managed_account_suffix: str,
) -> MyBankStatement:
    """Parse one digest-bound MYbank export without trusting formulas or macros."""

    if not _DIGEST.fullmatch(expected_sha256):
        raise MyBankStatementError("expected source digest is invalid")
    if not _ACCOUNT_SUFFIX.fullmatch(managed_account_suffix):
        raise MyBankStatementError("managed account suffix is invalid")
    raw = _read_source(source_path)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    if source_sha256 != expected_sha256:
        raise MyBankStatementError("source digest changed")
    rows = _read_workbook_rows(raw)
    header_position = _find_header(rows)
    header_row_number, _ = rows[header_position]
    metadata_rows = rows[:header_position]
    metadata = " ".join(value for _, values in metadata_rows for value in values if value)
    if "网商银行" not in metadata:
        raise MyBankStatementError("statement institution is not proven")
    card_values = tuple(
        values[index + 1]
        for _, values in metadata_rows
        for index, value in enumerate(values[:-1])
        if value.strip().rstrip("\N{FULLWIDTH COLON}:").strip() == "卡号"
        and values[index + 1].strip()
    )
    if len(card_values) != 1 or not card_values[0].strip().endswith(managed_account_suffix):
        raise MyBankStatementError("statement does not belong to the managed account")
    if "人民币" not in metadata and "CNY" not in metadata.upper():
        raise MyBankStatementError("statement currency is not proven")

    transactions: list[MyBankTransaction] = []
    known_serials: set[str] = set()
    for source_row_number, values in rows[header_position + 1 :]:
        normalized = _normalized_width(values)
        if not any(normalized):
            continue
        if any(normalized[len(_EXPECTED_HEADERS) :]):
            raise MyBankStatementError("statement contains unexpected populated columns")
        transaction_values = normalized[: len(_EXPECTED_HEADERS)]
        if not all(transaction_values[index] for index in (0, 1, 2, 6, 7)):
            raise MyBankStatementError("statement transaction is incomplete")
        transaction_serial = transaction_values[6]
        if transaction_serial in known_serials:
            raise MyBankStatementError("statement transaction serial is duplicated")
        known_serials.add(transaction_serial)
        occurred_at = _parse_occurred_at(transaction_values[0])
        amount_minor = _parse_minor(transaction_values[1], field="amount")
        balance_minor = _parse_minor(transaction_values[2], field="balance")
        row_sha256 = hashlib.sha256(
            json.dumps(
                transaction_values,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        source_event_ref = uuid5(
            _NAMESPACE,
            f"mybank-event:{source_sha256}:{source_row_number}:{row_sha256}",
        )
        transactions.append(
            MyBankTransaction(
                source_event_ref=source_event_ref,
                source_row_number=source_row_number,
                source_row_sha256=row_sha256,
                occurred_at=occurred_at,
                amount_minor=amount_minor,
                balance_minor=balance_minor,
                counterparty_name=transaction_values[3],
                counterparty_account=transaction_values[4],
                counterparty_institution=transaction_values[5],
                transaction_serial=transaction_serial,
                transaction_name=transaction_values[7],
            )
        )
    if not transactions:
        raise MyBankEmptyStatementError("statement contains no transactions")
    return MyBankStatement(
        statement_ref=uuid5(_NAMESPACE, f"mybank-statement:{source_sha256}"),
        source_sha256=source_sha256,
        source_size=len(raw),
        declared_media_type=_XLSX_MEDIA_TYPE,
        currency="CNY",
        institution_code="mybank",
        account_suffix=managed_account_suffix,
        worksheet_index=1,
        header_row_number=header_row_number,
        transactions=tuple(transactions),
    )


def build_mybank_source_manifest(
    statement: MyBankStatement,
    *,
    source_file: str,
    context: object,
) -> Never:
    """Reject the retired per-transaction review-manifest path."""

    _ = (statement, source_file, context)
    raise MyBankStatementError(
        "per-transaction MYbank review manifests are retired; use BankStatementPersistenceService"
    )


def _read_source(path: Path) -> bytes:
    if not path.is_absolute():
        raise MyBankStatementError("statement path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MyBankStatementError("statement file is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MyBankStatementError("statement file must be regular")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_FILE_BYTES:
        raise MyBankStatementError("statement file size is invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MyBankStatementError("statement file cannot be opened") from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(_MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise MyBankStatementError("statement file cannot be read") from exc
    if len(raw) != metadata.st_size or len(raw) > _MAX_FILE_BYTES:
        raise MyBankStatementError("statement file changed while reading")
    return raw


def _read_workbook_rows(raw: bytes) -> list[tuple[int, tuple[str, ...]]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (OSError, zipfile.BadZipFile) as exc:
        raise MyBankStatementError("statement is not a valid XLSX container") from exc
    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > _MAX_ARCHIVE_ENTRIES:
            raise MyBankStatementError("statement archive entry count is invalid")
        names = [info.filename for info in infos]
        if len(set(names)) != len(names):
            raise MyBankStatementError("statement archive contains duplicate entries")
        total_size = 0
        for info in infos:
            name = PurePosixPath(info.filename)
            if name.is_absolute() or ".." in name.parts or info.flag_bits & 0x1:
                raise MyBankStatementError("statement archive entry is unsafe")
            total_size += info.file_size
            if info.file_size > _MAX_XML_BYTES or total_size > _MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise MyBankStatementError("statement archive is too large")
        workbook_root = _parse_xml(_read_entry(archive, "xl/workbook.xml"))
        sheets = workbook_root.findall(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet")
        if len(sheets) != 1:
            raise MyBankStatementError("statement must contain exactly one worksheet")
        relationship_id = sheets[0].get(f"{{{_OFFICE_REL_NS}}}id")
        if not relationship_id:
            raise MyBankStatementError("statement worksheet relationship is missing")
        relationships = _parse_xml(_read_entry(archive, "xl/_rels/workbook.xml.rels"))
        targets = [
            item.get("Target")
            for item in relationships.findall(f"{{{_REL_NS}}}Relationship")
            if item.get("Id") == relationship_id
        ]
        if len(targets) != 1 or not targets[0]:
            raise MyBankStatementError("statement worksheet target is invalid")
        worksheet_path = _resolve_worksheet_path(targets[0])
        shared_strings = _read_shared_strings(archive)
        worksheet = _parse_xml(_read_entry(archive, worksheet_path))
        if worksheet.find(f".//{{{_MAIN_NS}}}f") is not None:
            raise MyBankStatementError("statement formulas are not accepted")
        return _worksheet_rows(worksheet, shared_strings)


def _read_entry(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise MyBankStatementError("statement archive is incomplete") from exc
    if info.file_size <= 0 or info.file_size > _MAX_XML_BYTES:
        raise MyBankStatementError("statement XML part size is invalid")
    try:
        return archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise MyBankStatementError("statement XML part cannot be read") from exc


def _parse_xml(raw: bytes) -> ElementTree.Element:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise MyBankStatementError("statement XML declarations are unsafe")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise MyBankStatementError("statement XML is invalid") from exc


def _resolve_worksheet_path(target: str) -> str:
    path = PurePosixPath(target)
    if path.is_absolute() or ".." in path.parts:
        raise MyBankStatementError("statement worksheet path is unsafe")
    resolved = path if path.parts and path.parts[0] == "xl" else PurePosixPath("xl") / path
    value = resolved.as_posix()
    if not value.startswith("xl/worksheets/") or not value.endswith(".xml"):
        raise MyBankStatementError("statement worksheet path is unexpected")
    return value


def _read_shared_strings(archive: zipfile.ZipFile) -> tuple[str, ...]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return ()
    if len(raw) > _MAX_XML_BYTES:
        raise MyBankStatementError("statement shared strings are too large")
    root = _parse_xml(raw)
    values = [
        "".join(text.text or "" for text in item.iter(f"{{{_MAIN_NS}}}t"))
        for item in root.findall(f"{{{_MAIN_NS}}}si")
    ]
    if len(values) > _MAX_ROWS * _MAX_COLUMNS:
        raise MyBankStatementError("statement shared string count is invalid")
    return tuple(values)


def _worksheet_rows(
    worksheet: ElementTree.Element,
    shared_strings: tuple[str, ...],
) -> list[tuple[int, tuple[str, ...]]]:
    rows: list[tuple[int, tuple[str, ...]]] = []
    for row in worksheet.findall(f".//{{{_MAIN_NS}}}sheetData/{{{_MAIN_NS}}}row"):
        row_number_raw = row.get("r")
        if not row_number_raw or not row_number_raw.isdigit():
            raise MyBankStatementError("statement row identity is invalid")
        row_number = int(row_number_raw)
        if row_number <= 0 or row_number > _MAX_ROWS:
            raise MyBankStatementError("statement row count is invalid")
        values: list[str] = []
        for cell in row.findall(f"{{{_MAIN_NS}}}c"):
            reference = cell.get("r", "")
            match = _CELL_REFERENCE.fullmatch(reference)
            if not match or int(match.group(2)) != row_number:
                raise MyBankStatementError("statement cell reference is invalid")
            column = _column_index(match.group(1))
            if column > _MAX_COLUMNS:
                raise MyBankStatementError("statement column count is invalid")
            while len(values) < column:
                values.append("")
            values[column - 1] = _cell_text(cell, shared_strings)
        rows.append((row_number, tuple(values)))
    if not rows or len(rows) > _MAX_ROWS:
        raise MyBankStatementError("statement contains no readable rows")
    return rows


def _column_index(name: str) -> int:
    value = 0
    for character in name:
        value = value * 26 + ord(character) - 64
    return value


def _cell_text(cell: ElementTree.Element, shared_strings: tuple[str, ...]) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.iter(f"{{{_MAIN_NS}}}t")).strip()
    value = cell.findtext(f"{{{_MAIN_NS}}}v", default="")
    if cell_type == "s":
        try:
            index = int(value)
            return shared_strings[index].strip()
        except (ValueError, IndexError) as exc:
            raise MyBankStatementError("statement shared string reference is invalid") from exc
    if cell_type in (None, "n", "str"):
        return value.strip()
    raise MyBankStatementError("statement cell type is unsupported")


def _find_header(rows: list[tuple[int, tuple[str, ...]]]) -> int:
    positions = [
        index
        for index, (_, values) in enumerate(rows[:50])
        if _normalized_width(values)[: len(_EXPECTED_HEADERS)] == _EXPECTED_HEADERS
        and not any(_normalized_width(values)[len(_EXPECTED_HEADERS) :])
    ]
    if len(positions) != 1:
        raise MyBankStatementError("statement header is missing or ambiguous")
    return positions[0]


def _normalized_width(values: tuple[str, ...]) -> tuple[str, ...]:
    width = max(len(values), len(_EXPECTED_HEADERS))
    return tuple(values[index].strip() if index < len(values) else "" for index in range(width))


def _parse_occurred_at(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise MyBankStatementError("statement transaction time is invalid") from exc
    return parsed.replace(tzinfo=_SHANGHAI)


def _parse_minor(value: str, *, field: str) -> int:
    if not _MONEY.fullmatch(value):
        raise MyBankStatementError(f"statement {field} is invalid")
    try:
        decimal_value = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise MyBankStatementError(f"statement {field} is invalid") from exc
    minor = decimal_value * 100
    if minor != minor.to_integral_value() or abs(minor) > 9_007_199_254_740_991:
        raise MyBankStatementError(f"statement {field} is out of range")
    return int(minor)
