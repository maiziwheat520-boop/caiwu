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
        _load_messages,
        _mybank_identity,
    )
except ModuleNotFoundError:
    from r1_staging_financial_zip_check import (  # type: ignore[import-not-found,no-redef]
        DEFAULT_COMPANY_REGISTRY,
        _load_messages,
        _mybank_identity,
    )

DEFAULT_ONE_TIME_PASSWORDS = Path("G:/我的云端硬盘/凭据/ledgerbridge-one-time-mail-passwords.json")
DEFAULT_OUTPUT = Path("data/parsed_mail/verified-financial-mail-2026-08-26.json")
MAX_PASSWORD_FILE_BYTES = 64 * 1024
MAX_INNER_WORKBOOK_BYTES = 50 * 1024 * 1024


def _verified_passwords(path: Path) -> dict[tuple[str, str], str]:
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
    result: dict[tuple[str, str], str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "verified":
            continue
        subject = entry.get("matched_subject")
        filename = entry.get("attachment_filename")
        password = entry.get("password")
        if not all(isinstance(value, str) and value for value in (subject, filename, password)):
            raise RuntimeError("verified one-time password entry is incomplete")
        assert isinstance(subject, str)
        assert isinstance(filename, str)
        assert isinstance(password, str)
        if len(subject) > 500 or len(filename) > 255 or len(password) > 200:
            raise RuntimeError("verified one-time password entry exceeds a limit")
        key = (subject, filename)
        if key in result:
            raise RuntimeError("verified one-time password entry is ambiguous")
        result[key] = password
    return result


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


def _parse_mybank(message: MailMessage) -> dict[str, object]:
    company_name, company_code = _mybank_identity(message, DEFAULT_COMPANY_REGISTRY)
    password = company_code[-6:].upper().encode("utf-8")
    zip_attachments = [
        attachment
        for attachment in message.attachments
        if attachment.filename.casefold().endswith(".zip")
    ]
    if len(zip_attachments) != 1:
        raise RuntimeError("verified MyBank mail must contain exactly one ZIP")
    attachment = zip_attachments[0]
    try:
        with zipfile.ZipFile(io.BytesIO(attachment.content)) as archive:
            entries = archive.infolist()
            if (
                len(entries) != 1
                or not entries[0].filename.casefold().endswith(".xlsx")
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
    parsed["source"] = _source(message, attachment.filename, attachment.content)
    return parsed


def _parse_boc(
    message: MailMessage, passwords: dict[tuple[str, str], str]
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for attachment in message.attachments:
        if not attachment.filename.casefold().endswith(".pdf"):
            continue
        password = passwords.get((message.subject, attachment.filename))
        if password is None:
            continue
        parsed = parse_boc_pdf(
            attachment.content,
            password=password,
            source_filename=attachment.filename,
        ).to_dict()
        parsed["source"] = _source(message, attachment.filename, attachment.content)
        results.append(parsed)
    return results


def parse_verified_mail() -> dict[str, object]:
    messages = _load_messages()
    passwords = _verified_passwords(DEFAULT_ONE_TIME_PASSWORDS)
    statements: list[dict[str, object]] = []
    for message in messages:
        if message.sender_address == "service@mail.mybank.cn" and message.subject.startswith(
            "浙江网商银行电子凭证-"
        ):
            statements.append(_parse_mybank(message))
        if "中国银行交易流水" in message.subject:
            statements.extend(_parse_boc(message, passwords))
    if not statements:
        raise RuntimeError("no verified financial statements were found")
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "verified-mail-local-parse",
        "writes_ledger": False,
        "statements": statements,
    }


def _atomic_write_json(path: Path, payload: dict[str, object], *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise RuntimeError("output already exists; pass --replace to overwrite it")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
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
