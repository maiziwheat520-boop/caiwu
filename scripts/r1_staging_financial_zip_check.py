"""Verify forwarded financial ZIP passwords one archive at a time, in memory."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ledgerbridge.mail_collector import MailMessage
from ledgerbridge.mail_credentials import CredentialFileSecretProvider
from ledgerbridge.mail_imap import ImapMailProvider

try:
    _transport_module = importlib.import_module("scripts.r1_staging_imap_replay")
except ModuleNotFoundError:
    _transport_module = importlib.import_module("r1_staging_imap_replay")
_ImapSslTransport = _transport_module.ImapSslTransport

DEFAULT_CREDENTIAL_FILE = Path("G:/我的云端硬盘/凭据/hermes-163-mail.env")
DEFAULT_COMPANY_REGISTRY = Path("data/company_registry.json")
DEFAULT_VERIFICATION_MANIFEST = Path("data/verified_financial_attachments.json")
MAX_ENTRY_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_COMPANY_REGISTRY_BYTES = 64 * 1024


def _print_json(value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")


def _load_messages() -> tuple[MailMessage, ...]:
    provider = ImapMailProvider(
        _ImapSslTransport(host="imap.163.com", port=993),
        CredentialFileSecretProvider(DEFAULT_CREDENTIAL_FILE, key="authorization_code"),
        mailbox="redeatt@163.com",
        auth_mode="password",
        max_messages=20,
    )
    return tuple(provider.iter_messages())


def _load_message(message_index: int) -> MailMessage:
    messages = _load_messages()
    if message_index < 1 or message_index > len(messages):
        raise RuntimeError(f"message index must be between 1 and {len(messages)}")
    return messages[message_index - 1]


def list_messages() -> dict[str, Any]:
    messages = _load_messages()
    return {
        "messages": [
            {
                "index": index,
                "sender": message.sender_address,
                "forwarder": message.resent_from_address,
                "subject": message.subject,
                "received_at": message.received_at,
                "attachments": len(message.attachments),
            }
            for index, message in enumerate(messages, start=1)
        ],
        "selection": "use --message-index with the selected index",
    }


def _redacted_body_preview(value: str) -> str:
    return re.sub(
        r"(?<![0-9A-Za-z])[0-9A-Za-z]{4,}(?![0-9A-Za-z])",
        "[REDACTED]",
        value,
    )


def list_attachments(message_index: int) -> dict[str, Any]:
    message = _load_message(message_index)
    return {
        "message_index": message_index,
        "sender": message.sender_address,
        "forwarder": message.resent_from_address,
        "subject": message.subject,
        "body_preview": _redacted_body_preview(message.body_preview),
        "attachments": [
            {
                "position": position,
                "filename": attachment.filename,
                "media_type": attachment.media_type,
                "size_bytes": len(attachment.content),
            }
            for position, attachment in enumerate(message.attachments, start=1)
        ],
    }


def _load_company_registry(path: Path) -> tuple[tuple[str, str], ...]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("company registry is unavailable") from exc
    if len(content) > MAX_COMPANY_REGISTRY_BYTES:
        raise RuntimeError("company registry exceeds the local size limit")
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("company registry is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("company registry contract is invalid")
    companies = payload.get("companies")
    if not isinstance(companies, list):
        raise RuntimeError("company registry companies are invalid")
    result: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    seen_codes: set[str] = set()
    for company in companies:
        if not isinstance(company, dict):
            raise RuntimeError("company registry entry is invalid")
        name = company.get("company_name")
        code = company.get("unified_social_credit_code")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise RuntimeError("company registry name is invalid")
        if not isinstance(code, str) or re.fullmatch(r"[0-9A-Z]{18}", code) is None:
            raise RuntimeError("company registry code is invalid")
        if name in seen_names or code in seen_codes:
            raise RuntimeError("company registry contains a duplicate")
        seen_names.add(name)
        seen_codes.add(code)
        result.append((name, code))
    return tuple(result)


def _mybank_identity(message: MailMessage, registry_path: Path) -> tuple[str, str]:
    prefix = "浙江网商银行电子凭证-"
    if not message.subject.startswith(prefix):
        raise RuntimeError("selected message is not a MyBank electronic voucher")
    masked_name = message.subject.removeprefix(prefix)
    if "*" in masked_name:
        pattern = (
            r"\A" + r".+".join(re.escape(part) for part in re.split(r"\*+", masked_name)) + r"\Z"
        )
        matches = [
            entry for entry in _load_company_registry(registry_path) if re.match(pattern, entry[0])
        ]
    else:
        matches = [
            entry for entry in _load_company_registry(registry_path) if entry[0] == masked_name
        ]
    if len(matches) != 1:
        raise RuntimeError("message subject does not uniquely match the company registry")
    return matches[0]


def _mybank_password(message: MailMessage, registry_path: Path) -> str:
    return _mybank_identity(message, registry_path)[1][-6:].upper()


def _verify_zip(content: bytes, password: str | None) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if not entries:
                return {"status": "empty_archive", "entries": 0, "encrypted": False}
            encrypted = any(info.flag_bits & 0x1 for info in entries)
            total_size = sum(info.file_size for info in entries)
            if any(info.file_size > MAX_ENTRY_BYTES for info in entries):
                return {
                    "status": "rejected_entry_size",
                    "entries": len(entries),
                    "encrypted": encrypted,
                }
            if total_size > MAX_ARCHIVE_BYTES:
                return {
                    "status": "rejected_archive_size",
                    "entries": len(entries),
                    "encrypted": encrypted,
                }
            if encrypted and password is None:
                return {"status": "skipped", "entries": len(entries), "encrypted": True}
            password_bytes = password.encode("utf-8") if password is not None else None
            verified_bytes = 0
            for info in entries:
                with archive.open(info, "r", pwd=password_bytes) as stream:
                    while chunk := stream.read(1024 * 1024):
                        verified_bytes += len(chunk)
            return {
                "status": "verified",
                "entries": len(entries),
                "encrypted": encrypted,
                "uncompressed_bytes": verified_bytes,
            }
    except (RuntimeError, ValueError, OSError, zipfile.BadZipFile) as exc:
        if isinstance(exc, RuntimeError) and "password" not in str(exc).casefold():
            return {"status": "archive_error"}
        return {"status": "wrong_password"}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            locking = importlib.import_module("msvcrt")
            locking.locking(stream.fileno(), locking.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                locking.locking(stream.fileno(), locking.LK_UNLCK, 1)
        else:
            locking = importlib.import_module("fcntl")
            locking.flock(stream.fileno(), locking.LOCK_EX)
            try:
                yield
            finally:
                locking.flock(stream.fileno(), locking.LOCK_UN)


def _record_verifications(
    path: Path,
    message: MailMessage,
    results: list[dict[str, Any]],
) -> int:
    lock_path = path.with_name(f".{path.name}.lock")
    with _exclusive_file_lock(lock_path):
        return _record_verifications_unlocked(path, message, results)


def _record_verifications_unlocked(
    path: Path,
    message: MailMessage,
    results: list[dict[str, Any]],
) -> int:
    if path.exists():
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("verification manifest is invalid") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise RuntimeError("verification manifest contract is invalid")
    else:
        payload = {"schema_version": 1, "records": []}
    records = payload.get("records")
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise RuntimeError("verification manifest records are invalid")
    existing_keys = [(item.get("message_id"), item.get("attachment_sha256")) for item in records]
    if len(existing_keys) != len(set(existing_keys)):
        raise RuntimeError("verification manifest contains a duplicate")
    by_key = dict(zip(existing_keys, records, strict=True))
    added = 0
    verified_at = datetime.now(UTC).isoformat()
    for result in results:
        if result.get("status") != "verified" or result.get("encrypted") is not True:
            continue
        position = result.get("position")
        if not isinstance(position, int) or not 1 <= position <= len(message.attachments):
            raise RuntimeError("verified attachment position is invalid")
        attachment = message.attachments[position - 1]
        filename = attachment.filename.casefold()
        if filename.endswith(".zip"):
            if message.sender_address != "service@mail.mybank.cn" or not message.subject.startswith(
                "浙江网商银行电子凭证-"
            ):
                raise RuntimeError("verified ZIP is not an identified MyBank statement")
            document_type = "MYBANK_XLSX_ZIP"
        elif filename.endswith(".pdf"):
            if "中国银行交易流水" not in message.subject:
                raise RuntimeError("verified PDF is not an identified BOC statement")
            document_type = "BOC_PDF"
        else:
            continue
        digest = hashlib.sha256(attachment.content).hexdigest()
        record = {
            "message_id": message.message_id,
            "subject": message.subject,
            "sender": message.sender_address,
            "forwarder": message.resent_from_address,
            "received_at": message.received_at,
            "filename": attachment.filename,
            "attachment_sha256": digest,
            "size_bytes": len(attachment.content),
            "media_type": attachment.media_type,
            "document_type": document_type,
            "encrypted": True,
            "verified_at": verified_at,
        }
        key = (message.message_id, digest)
        existing = by_key.get(key)
        if existing is not None:
            comparable_existing = {k: existing.get(k) for k in record if k != "verified_at"}
            comparable_record = {k: value for k, value in record.items() if k != "verified_at"}
            if comparable_existing != comparable_record:
                raise RuntimeError("verification manifest identity collision")
            continue
        records.append(record)
        by_key[key] = record
        added += 1
    if added:
        _atomic_write_json(path, payload)
    return added


def _verify_pdf(content: bytes, password: str | None) -> dict[str, Any]:
    try:
        pypdf = importlib.import_module("pypdf")
    except ModuleNotFoundError:
        return {"status": "pdf_dependency_unavailable"}
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        encrypted = bool(reader.is_encrypted)
        if encrypted:
            if password is None:
                return {"status": "skipped", "encrypted": True}
            if not reader.decrypt(password):
                return {"status": "wrong_password", "encrypted": True}
        return {
            "status": "verified",
            "encrypted": encrypted,
            "pages": len(reader.pages),
        }
    except Exception:
        return {"status": "pdf_error"}


def run(
    message_index: int,
    *,
    visible_password_input: bool = False,
    use_company_registry: bool = False,
    company_registry: Path = DEFAULT_COMPANY_REGISTRY,
    record_verification: bool = False,
    verification_manifest: Path = DEFAULT_VERIFICATION_MANIFEST,
) -> dict[str, Any]:
    message = _load_message(message_index)
    stored_password = _mybank_password(message, company_registry) if use_company_registry else None
    interactive_password_needed = not use_company_registry and any(
        attachment.filename.casefold().endswith((".zip", ".pdf"))
        for attachment in message.attachments
    )
    if interactive_password_needed and message.body_preview:
        preview = (
            "Mail body preview (sensitive sequences redacted):\n"
            f"{_redacted_body_preview(message.body_preview)}\n\n"
        )
        sys.stderr.buffer.write(preview.encode("utf-8"))
        sys.stderr.buffer.flush()
    results: list[dict[str, Any]] = []
    for position, attachment in enumerate(message.attachments, start=1):
        filename = attachment.filename.casefold()
        is_zip = filename.endswith(".zip")
        is_pdf = filename.endswith(".pdf")
        if not is_zip and not is_pdf:
            results.append(
                {
                    "position": position,
                    "filename": attachment.filename,
                    "status": "manual_parser_pending",
                    "media_type": attachment.media_type,
                    "size_bytes": len(attachment.content),
                }
            )
            continue
        document_type = "ZIP" if is_zip else "PDF"
        prompt = (
            f"Password for {document_type} attachment "
            f"{position}/{len(message.attachments)} - blank skips: "
        )
        if use_company_registry:
            password = stored_password
            password_source = "company_registry"
        else:
            password = input(prompt) if visible_password_input else getpass.getpass(prompt)
            password_source = "terminal"
        results.append(
            {
                "position": position,
                "filename": attachment.filename,
                "media_type": attachment.media_type,
                "size_bytes": len(attachment.content),
                "password_source": password_source,
                **(
                    _verify_zip(attachment.content, password or None)
                    if is_zip
                    else _verify_pdf(attachment.content, password or None)
                ),
            }
        )
    result = {
        "message_index": message_index,
        "sender": message.sender_address,
        "forwarder": message.resent_from_address,
        "subject": message.subject,
        "attachments_checked": len(results),
        "results": results,
        "storage": "memory-only",
    }
    if record_verification:
        if not any(
            item.get("status") == "verified" and item.get("encrypted") is True for item in results
        ):
            raise RuntimeError("no encrypted verified attachment is eligible to record")
        result["verification_records_added"] = _record_verifications(
            verification_manifest, message, results
        )
        result["verification_manifest"] = str(verification_manifest.resolve())
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message-index", type=int, help="1-based index from --list-messages")
    parser.add_argument(
        "--list-messages",
        action="store_true",
        help="list recent sender/subject/attachment metadata without asking for passwords",
    )
    parser.add_argument(
        "--list-attachments",
        action="store_true",
        help="list attachment metadata for --message-index without asking for passwords",
    )
    parser.add_argument(
        "--visible-password-input",
        action="store_true",
        help="echo each password while it is typed; still never store or log it",
    )
    parser.add_argument(
        "--use-company-registry",
        action="store_true",
        help="derive the MyBank ZIP password in memory from local company master data",
    )
    parser.add_argument(
        "--record-verification",
        action="store_true",
        help="record encrypted verified attachments by message ID and SHA-256",
    )
    args = parser.parse_args()
    if args.list_messages:
        _print_json(list_messages())
        raise SystemExit(0)
    if args.message_index is None:
        parser.error("--message-index is required unless --list-messages is used")
    if args.list_attachments:
        _print_json(list_attachments(args.message_index))
        raise SystemExit(0)
    if args.visible_password_input and args.use_company_registry:
        parser.error("--visible-password-input and --use-company-registry are mutually exclusive")
    _print_json(
        run(
            args.message_index,
            visible_password_input=args.visible_password_input,
            use_company_registry=args.use_company_registry,
            record_verification=args.record_verification,
        )
    )
