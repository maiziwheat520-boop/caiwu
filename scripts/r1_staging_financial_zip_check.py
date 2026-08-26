"""Verify forwarded financial ZIP passwords one archive at a time, in memory."""

from __future__ import annotations

import argparse
import getpass
import importlib
import io
import json
import re
import sys
import zipfile
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


def _mybank_password(message: MailMessage, registry_path: Path) -> str:
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
    return matches[0][1][-6:].upper()


def _verify_zip(content: bytes, password: str | None) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if not entries:
                return {"status": "empty_archive", "entries": 0}
            total_size = sum(info.file_size for info in entries)
            if any(info.file_size > MAX_ENTRY_BYTES for info in entries):
                return {"status": "rejected_entry_size", "entries": len(entries)}
            if total_size > MAX_ARCHIVE_BYTES:
                return {"status": "rejected_archive_size", "entries": len(entries)}
            encrypted = any(info.flag_bits & 0x1 for info in entries)
            if encrypted and password is None:
                return {"status": "skipped", "entries": len(entries)}
            password_bytes = password.encode("utf-8") if password is not None else None
            verified_bytes = 0
            for info in entries:
                with archive.open(info, "r", pwd=password_bytes) as stream:
                    while chunk := stream.read(1024 * 1024):
                        verified_bytes += len(chunk)
            return {
                "status": "verified",
                "entries": len(entries),
                "uncompressed_bytes": verified_bytes,
            }
    except (RuntimeError, ValueError, OSError, zipfile.BadZipFile) as exc:
        if isinstance(exc, RuntimeError) and "password" not in str(exc).casefold():
            return {"status": "archive_error"}
        return {"status": "wrong_password"}


def run(
    message_index: int,
    *,
    visible_password_input: bool = False,
    use_company_registry: bool = False,
    company_registry: Path = DEFAULT_COMPANY_REGISTRY,
) -> dict[str, Any]:
    message = _load_message(message_index)
    stored_password = _mybank_password(message, company_registry) if use_company_registry else None
    results: list[dict[str, Any]] = []
    for position, attachment in enumerate(message.attachments, start=1):
        if not attachment.filename.casefold().endswith(".zip"):
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
        prompt = (
            f"Password for ZIP {position}/{len(message.attachments)} "
            f"({attachment.filename}) — blank skips: "
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
                "size_bytes": len(attachment.content),
                "password_source": password_source,
                **_verify_zip(attachment.content, password or None),
            }
        )
    return {
        "message_index": message_index,
        "sender": message.sender_address,
        "forwarder": message.resent_from_address,
        "subject": message.subject,
        "attachments_checked": len(results),
        "results": results,
        "storage": "memory-only",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message-index", type=int, help="1-based index from --list-messages")
    parser.add_argument(
        "--list-messages",
        action="store_true",
        help="list recent sender/subject/attachment metadata without asking for passwords",
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
    args = parser.parse_args()
    if args.list_messages:
        _print_json(list_messages())
        raise SystemExit(0)
    if args.message_index is None:
        parser.error("--message-index is required unless --list-messages is used")
    if args.visible_password_input and args.use_company_registry:
        parser.error("--visible-password-input and --use-company-registry are mutually exclusive")
    _print_json(
        run(
            args.message_index,
            visible_password_input=args.visible_password_input,
            use_company_registry=args.use_company_registry,
        )
    )
