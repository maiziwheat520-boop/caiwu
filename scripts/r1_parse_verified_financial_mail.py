"""Parse already-verified financial mail into local, non-ledger JSON."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ledgerbridge.bank_statement_parser import parse_boc_pdf, parse_mybank_xlsx
from ledgerbridge.mail_collector import MailMessage

try:
    from scripts.r1_staging_financial_zip_check import (
        DEFAULT_COMPANY_REGISTRY,
        DEFAULT_VERIFICATION_MANIFEST,
        _exclusive_file_lock,
        _load_messages,
        _mybank_identity,
    )
except ModuleNotFoundError:
    from r1_staging_financial_zip_check import (  # type: ignore[import-not-found,no-redef]
        DEFAULT_COMPANY_REGISTRY,
        DEFAULT_VERIFICATION_MANIFEST,
        _exclusive_file_lock,
        _load_messages,
        _mybank_identity,
    )

DEFAULT_ONE_TIME_PASSWORDS = Path("G:/我的云端硬盘/凭据/ledgerbridge-one-time-mail-passwords.json")
DEFAULT_OUTPUT = Path("data/parsed_mail/verified-financial-mail-2026-08-26.json")
MAX_PASSWORD_FILE_BYTES = 64 * 1024
MAX_INNER_WORKBOOK_BYTES = 50 * 1024 * 1024


def _verified_passwords(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("one-time password file is unavailable") from exc
    if len(content) > MAX_PASSWORD_FILE_BYTES:
        raise RuntimeError("one-time password file exceeds the local size limit")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("one-time password file is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("one-time password contract is invalid")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("one-time password entries are invalid")
    result: dict[tuple[str, str], dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "verified":
            continue
        subject = entry.get("matched_subject")
        filename = entry.get("attachment_filename")
        message_id = entry.get("message_id")
        attachment_sha256 = entry.get("attachment_sha256")
        password = entry.get("password")
        if not all(
            isinstance(value, str) and value
            for value in (
                subject,
                filename,
                message_id,
                attachment_sha256,
                password,
            )
        ):
            raise RuntimeError("verified one-time password entry is incomplete")
        assert isinstance(subject, str)
        assert isinstance(filename, str)
        assert isinstance(message_id, str)
        assert isinstance(attachment_sha256, str)
        assert isinstance(password, str)
        if (
            len(subject) > 500
            or len(filename) > 255
            or len(message_id) > 1000
            or len(password) > 200
            or len(attachment_sha256) != 64
        ):
            raise RuntimeError("verified one-time password entry exceeds a limit")
        key = (message_id, attachment_sha256)
        if key in result:
            raise RuntimeError("verified one-time password entry is ambiguous")
        result[key] = {
            "subject": subject,
            "filename": filename,
            "password": password,
        }
    return result


def _verification_records(path: Path) -> tuple[dict[str, object], ...]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("verification manifest is unavailable") from exc
    if len(content) > 1024 * 1024:
        raise RuntimeError("verification manifest exceeds the local size limit")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("verification manifest is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("verification manifest contract is invalid")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise RuntimeError("verification manifest contains no records")
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    required_strings = (
        "message_id",
        "subject",
        "sender",
        "received_at",
        "filename",
        "attachment_sha256",
        "media_type",
        "document_type",
        "verified_at",
    )
    for item in records:
        if not isinstance(item, dict):
            raise RuntimeError("verification manifest record is invalid")
        if not all(isinstance(item.get(name), str) and item[name] for name in required_strings):
            raise RuntimeError("verification manifest record is incomplete")
        if item.get("encrypted") is not True:
            raise RuntimeError("verification manifest includes an unencrypted attachment")
        if item.get("document_type") not in {"MYBANK_XLSX_ZIP", "BOC_PDF"}:
            raise RuntimeError("verification manifest document type is unsupported")
        digest = str(item["attachment_sha256"])
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise RuntimeError("verification manifest attachment digest is invalid")
        size = item.get("size_bytes")
        if not isinstance(size, int) or size <= 0:
            raise RuntimeError("verification manifest attachment size is invalid")
        key = (str(item["message_id"]), digest)
        if key in seen:
            raise RuntimeError("verification manifest contains a duplicate")
        seen.add(key)
        result.append(dict(item))
    return tuple(result)


def _source(message: MailMessage, filename: str, content: bytes) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "subject": message.subject,
        "received_at": message.received_at,
        "sender": message.sender_address,
        "forwarder": message.resent_from_address,
        "attachment_filename": filename,
        "attachment_sha256": hashlib.sha256(content).hexdigest(),
    }


def _parse_mybank(message: MailMessage, attachment_index: int) -> dict[str, object]:
    company_name, company_code = _mybank_identity(message, DEFAULT_COMPANY_REGISTRY)
    password = company_code[-6:].upper().encode("utf-8")
    attachment = message.attachments[attachment_index]
    if not attachment.filename.casefold().endswith(".zip"):
        raise RuntimeError("verified MyBank attachment is not a ZIP")
    try:
        with zipfile.ZipFile(io.BytesIO(attachment.content)) as archive:
            entries = archive.infolist()
            if (
                len(entries) != 1
                or not entries[0].filename.casefold().endswith(".xlsx")
                or not entries[0].flag_bits & 0x1
                or entries[0].file_size > MAX_INNER_WORKBOOK_BYTES
            ):
                raise RuntimeError("verified MyBank ZIP contract is invalid")
            workbook_content = archive.read(entries[0], pwd=password)
    except (RuntimeError, ValueError, OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError("verified MyBank workbook cannot be opened") from exc
    parsed = parse_mybank_xlsx(
        workbook_content,
        source_filename=entries[0].filename,
        company_name=company_name,
    ).to_dict()
    parsed["source_mail"] = _source(message, attachment.filename, attachment.content)
    parsed["admission"] = {
        "verification": "password_decryption",
        "mail_authentication": "not_verified",
        "identity_binding": "message_id_and_attachment_sha256",
        "metadata_binding": "mail_subject_and_encrypted_archive_filename",
        "requires_review": True,
    }
    return parsed


def _parse_boc(
    message: MailMessage,
    attachment_index: int,
    digest: str,
    passwords: dict[tuple[str, str], dict[str, str]],
) -> dict[str, object]:
    attachment = message.attachments[attachment_index]
    if not attachment.filename.casefold().endswith(".pdf"):
        raise RuntimeError("verified BOC attachment is not a PDF")
    credential = passwords.get((message.message_id, digest))
    if credential is None:
        raise RuntimeError("verified BOC attachment has no exact-bound password")
    if credential["subject"] != message.subject or credential["filename"] != attachment.filename:
        raise RuntimeError("verified BOC password metadata does not match")
    parsed = parse_boc_pdf(
        attachment.content,
        password=credential["password"],
        source_filename=attachment.filename,
    ).to_dict()
    parsed["source_mail"] = _source(message, attachment.filename, attachment.content)
    parsed["admission"] = {
        "verification": "password_decryption",
        "mail_authentication": "not_verified",
        "identity_binding": "message_id_and_attachment_sha256",
        "metadata_binding": "document_content",
        "text_fields": "layout_derived_requires_review",
        "requires_review": True,
    }
    return parsed


def parse_verified_mail() -> dict[str, object]:
    messages = _load_messages()
    passwords = _verified_passwords(DEFAULT_ONE_TIME_PASSWORDS)
    records = _verification_records(DEFAULT_VERIFICATION_MANIFEST)
    messages_by_id: dict[str, MailMessage] = {}
    for message in messages:
        if message.message_id in messages_by_id:
            raise RuntimeError("mailbox contains a duplicate message ID")
        messages_by_id[message.message_id] = message
    statements: list[dict[str, object]] = []
    consumed = 0
    for record in records:
        message_id = str(record["message_id"])
        digest = str(record["attachment_sha256"])
        selected_message = messages_by_id.get(message_id)
        if selected_message is None:
            raise RuntimeError("verified mail is no longer available in the mailbox window")
        expected_metadata = {
            "subject": selected_message.subject,
            "sender": selected_message.sender_address,
            "forwarder": selected_message.resent_from_address,
            "received_at": selected_message.received_at,
        }
        if any(record.get(name) != value for name, value in expected_metadata.items()):
            raise RuntimeError("verified mail metadata changed")
        matches = [
            index
            for index, attachment in enumerate(selected_message.attachments)
            if attachment.filename == record["filename"]
            and len(attachment.content) == record["size_bytes"]
            and hashlib.sha256(attachment.content).hexdigest() == digest
        ]
        if len(matches) != 1:
            raise RuntimeError("verified attachment is missing or ambiguous")
        attachment_index = matches[0]
        document_type = record["document_type"]
        if document_type == "MYBANK_XLSX_ZIP":
            statements.append(_parse_mybank(selected_message, attachment_index))
        elif document_type == "BOC_PDF":
            statements.append(_parse_boc(selected_message, attachment_index, digest, passwords))
        else:  # pragma: no cover - validated by _verification_records
            raise RuntimeError("verified attachment type is unsupported")
        consumed += 1
    if consumed != len(records) or len(statements) != len(records):
        raise RuntimeError("verified attachment set was not consumed completely")
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "verified-mail-local-parse",
        "writes_ledger": False,
        "statements": statements,
    }


def _atomic_write_json(path: Path, payload: dict[str, object], *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        lock_path = path.with_name(f".{path.name}.lock")
        with _exclusive_file_lock(lock_path):
            if not replace and path.exists():
                raise RuntimeError("output already exists; pass --replace to overwrite it")
            os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _summary(payload: dict[str, object], output: Path) -> dict[str, Any]:
    statements = payload["statements"]
    assert isinstance(statements, list)
    transaction_count = 0
    by_bank: dict[str, int] = {}
    for statement in statements:
        assert isinstance(statement, dict)
        bank_code = str(statement.get("bank_code"))
        by_bank[bank_code] = by_bank.get(bank_code, 0) + 1
        transactions = statement.get("transactions", [])
        assert isinstance(transactions, list)
        transaction_count += len(transactions)
    return {
        "output": str(output.resolve()),
        "statements": len(statements),
        "transactions": transaction_count,
        "by_bank": by_bank,
        "writes_ledger": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true")
    arguments = parser.parse_args()
    result = parse_verified_mail()
    _atomic_write_json(arguments.output, result, replace=arguments.replace)
    print(json.dumps(_summary(result, arguments.output), ensure_ascii=True, sort_keys=True))
