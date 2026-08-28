from __future__ import annotations

import base64
import math
import posixpath
import re
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REF = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")
_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_ARCHIVE_ENTRIES = 2_000
_MAX_XML_BYTES = 8 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_MAX_SHARED_STRINGS = 100_000
_MAX_CELL_TEXT = 1_000
_MAX_PREVIEW_FIELDS = 30
_MAX_PREVIEW_ROWS = 12
_MAX_PREVIEW_COLUMNS = 16
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_TEXT_BYTES = 256 * 1024


class EvidencePreviewError(ValueError):
    pass


def build_evidence_preview(
    evidence: dict[str, object],
    *,
    reference: str | None = None,
) -> dict[str, object]:
    content = evidence.get("content")
    if not isinstance(content, bytes):
        raise EvidencePreviewError("证据内容不可用")
    filename = str(evidence.get("filename") or "evidence.bin")
    suffix = PurePosixPath(filename).suffix.lower()

    if reference is not None and _SAFE_REFERENCE.fullmatch(reference) is None:
        raise EvidencePreviewError("证据定位标识无效")

    image_type = _image_media_type(content)
    if image_type is not None:
        if len(content) > _MAX_IMAGE_BYTES:
            return _unsupported(filename, "图片超过网页预览上限，请下载原文件查看")
        encoded = base64.b64encode(content).decode("ascii")
        return {
            "kind": "image",
            "filename": filename,
            "media_type": image_type,
            "data_url": f"data:{image_type};base64,{encoded}",
        }

    if suffix == ".xlsx" and content.startswith(b"PK"):
        try:
            return _xlsx_preview(content, filename=filename, reference=reference)
        except (BadZipFile, ElementTree.ParseError, EvidencePreviewError, KeyError, ValueError):
            return _unsupported(filename, "工作簿无法安全解析，请下载原文件查看")

    if suffix in {".txt", ".csv", ".log"} or str(evidence.get("content_type", "")).startswith("text/"):
        if len(content) > _MAX_TEXT_BYTES:
            return _unsupported(filename, "文本超过网页预览上限，请下载原文件查看")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return _unsupported(filename, "文本编码无法安全识别，请下载原文件查看")
        return {"kind": "text", "filename": filename, "text": text}

    return _unsupported(filename, "该文件类型暂不支持网页预览")


def _unsupported(filename: str, reason: str) -> dict[str, object]:
    return {"kind": "unsupported", "filename": filename, "reason": reason}


def _image_media_type(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _xlsx_preview(content: bytes, *, filename: str, reference: str | None) -> dict[str, object]:
    with ZipFile(BytesIO(content)) as archive:
        _validate_archive(archive)
        workbook = _read_xml(archive, "xl/workbook.xml")
        relationships = _read_xml(archive, "xl/_rels/workbook.xml.rels")
        rel_targets = {
            node.attrib["Id"]: _normalise_xlsx_target(node.attrib["Target"])
            for node in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
            if "Id" in node.attrib and "Target" in node.attrib
        }
        shared_strings = _read_shared_strings(archive)
        date_1904 = workbook.find(f"{{{_MAIN_NS}}}workbookPr")
        uses_1904_dates = bool(date_1904 is not None and date_1904.attrib.get("date1904") in {"1", "true"})

        matched_records: list[dict[str, object]] = []
        fallback: dict[str, object] | None = None
        sheets = workbook.find(f"{{{_MAIN_NS}}}sheets")
        if sheets is None:
            raise EvidencePreviewError("工作簿没有工作表")

        for sheet in sheets:
            relationship_id = sheet.attrib.get(f"{{{_REL_NS}}}id")
            target = rel_targets.get(relationship_id or "")
            if target is None or not target.startswith("xl/worksheets/"):
                continue
            rows = _read_sheet_rows(archive, target, shared_strings)
            if not rows:
                continue
            if fallback is None:
                fallback = _fallback_sheet(str(sheet.attrib.get("name", "工作表")), rows)
            if reference is None:
                continue
            for row_number, cells in rows:
                if not any(value == reference for value in cells.values()):
                    continue
                header_number, headers = _choose_header(rows, row_number)
                fields = []
                for column, value in sorted(cells.items()):
                    if not value:
                        continue
                    label = headers.get(column) or f"第 {column + 1} 列"
                    fields.append(
                        {
                            "label": label[:_MAX_CELL_TEXT],
                            "value": _format_cell_value(label, value, uses_1904_dates),
                        }
                    )
                    if len(fields) >= _MAX_PREVIEW_FIELDS:
                        break
                matched_records.append(
                    {
                        "sheet": str(sheet.attrib.get("name", "工作表"))[:200],
                        "row_number": row_number,
                        "header_row_number": header_number,
                        "fields": fields,
                    }
                )
                if len(matched_records) >= 4:
                    break
            if len(matched_records) >= 4:
                break

        return {
            "kind": "spreadsheet",
            "filename": filename,
            "reference": reference,
            "matched": bool(matched_records),
            "records": matched_records,
            "fallback": fallback,
        }


def _validate_archive(archive: ZipFile) -> None:
    entries = archive.infolist()
    if not entries or len(entries) > _MAX_ARCHIVE_ENTRIES:
        raise EvidencePreviewError("工作簿压缩包结构异常")
    total_size = 0
    for entry in entries:
        path = PurePosixPath(entry.filename)
        if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
            raise EvidencePreviewError("工作簿包含不安全路径")
        total_size += entry.file_size
        if entry.file_size > _MAX_XML_BYTES and entry.filename.endswith(".xml"):
            raise EvidencePreviewError("工作簿 XML 过大")
    if total_size > _MAX_ARCHIVE_BYTES:
        raise EvidencePreviewError("工作簿解压后过大")


def _read_xml(archive: ZipFile, name: str) -> ElementTree.Element:
    info = archive.getinfo(name)
    if info.file_size > _MAX_XML_BYTES:
        raise EvidencePreviewError("工作簿 XML 过大")
    value = archive.read(info)
    lowered = value.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise EvidencePreviewError("工作簿 XML 声明不安全")
    return ElementTree.fromstring(value)


def _normalise_xlsx_target(target: str) -> str:
    cleaned = target.lstrip("/")
    normalised = posixpath.normpath(cleaned if cleaned.startswith("xl/") else posixpath.join("xl", cleaned))
    if not normalised.startswith("xl/") or ".." in PurePosixPath(normalised).parts:
        raise EvidencePreviewError("工作簿关系路径不安全")
    return normalised


def _read_shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/sharedStrings.xml")
    values = []
    for node in root.findall(f"{{{_MAIN_NS}}}si"):
        values.append("".join(text.text or "" for text in node.iter(f"{{{_MAIN_NS}}}t"))[:_MAX_CELL_TEXT])
        if len(values) > _MAX_SHARED_STRINGS:
            raise EvidencePreviewError("共享字符串过多")
    return values


def _column_number(cell_reference: str) -> int:
    match = _CELL_REF.fullmatch(cell_reference)
    if match is None:
        raise EvidencePreviewError("单元格坐标无效")
    result = 0
    for character in match.group(1):
        result = result * 26 + ord(character) - ord("A") + 1
    return result - 1


def _read_sheet_rows(
    archive: ZipFile,
    name: str,
    shared_strings: list[str],
) -> list[tuple[int, dict[int, str]]]:
    root = _read_xml(archive, name)
    rows: list[tuple[int, dict[int, str]]] = []
    sheet_data = root.find(f"{{{_MAIN_NS}}}sheetData")
    if sheet_data is None:
        return rows
    for row in sheet_data.findall(f"{{{_MAIN_NS}}}row"):
        row_number = int(row.attrib.get("r", "0"))
        if row_number <= 0:
            continue
        cells: dict[int, str] = {}
        for cell in row.findall(f"{{{_MAIN_NS}}}c"):
            reference = cell.attrib.get("r")
            if not reference:
                continue
            column = _column_number(reference)
            cell_type = cell.attrib.get("t")
            value_node = cell.find(f"{{{_MAIN_NS}}}v")
            value = value_node.text or "" if value_node is not None else ""
            if cell_type == "s" and value:
                index = int(value)
                if index >= len(shared_strings):
                    raise EvidencePreviewError("共享字符串索引无效")
                value = shared_strings[index]
            elif cell_type == "inlineStr":
                value = "".join(text.text or "" for text in cell.iter(f"{{{_MAIN_NS}}}t"))
            elif cell_type == "b":
                value = "是" if value == "1" else "否"
            cells[column] = value[:_MAX_CELL_TEXT]
        if any(cells.values()):
            rows.append((row_number, cells))
    return rows


def _choose_header(
    rows: list[tuple[int, dict[int, str]]],
    target_row_number: int,
) -> tuple[int | None, dict[int, str]]:
    candidates = [
        (row_number, cells)
        for row_number, cells in rows
        if row_number < target_row_number and target_row_number - row_number <= 40
    ]
    if not candidates:
        return None, {}
    row_number, cells = max(
        candidates,
        key=lambda item: (sum(bool(value) for value in item[1].values()), item[0]),
    )
    return row_number, cells


def _format_cell_value(label: str, value: str, uses_1904_dates: bool) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    try:
        number = float(stripped)
    except ValueError:
        return stripped
    if not math.isfinite(number):
        return stripped
    if "时间" in label or "日期" in label:
        origin = datetime(1904, 1, 1) if uses_1904_dates else datetime(1899, 12, 30)
        try:
            converted = origin + timedelta(days=number)
        except OverflowError:
            return stripped
        return converted.strftime("%Y-%m-%d %H:%M:%S" if converted.time() != datetime.min.time() else "%Y-%m-%d")
    if "金额" in label or "余额" in label:
        return f"¥{number:,.2f}"
    if number.is_integer():
        return str(int(number))
    return stripped


def _fallback_sheet(
    name: str,
    rows: list[tuple[int, dict[int, str]]],
) -> dict[str, object]:
    preview_rows = []
    for row_number, cells in rows[:_MAX_PREVIEW_ROWS]:
        values = [cells.get(column, "") for column in range(_MAX_PREVIEW_COLUMNS)]
        while values and not values[-1]:
            values.pop()
        preview_rows.append({"row_number": row_number, "cells": values})
    return {"sheet": name[:200], "rows": preview_rows}
