"""Verify forwarded financial ZIP passwords one archive at a time, in memory."""

from __future__ import annotations

import argparse
import getpass
import importlib
import io
import json
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
MAX_ENTRY_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024


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


def run(message_index: int, *, visible_password_input: bool = False) -> dict[str, Any]:
    message = _load_message(message_index)
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
        password = input(prompt) if visible_password_input else getpass.getpass(prompt)
        results.append(
            {
                "position": position,
                "filename": attachment.filename,
                "size_bytes": len(attachment.content),
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
    args = parser.parse_args()
    if args.list_messages:
        _print_json(list_messages())
        raise SystemExit(0)
    if args.message_index is None:
        parser.error("--message-index is required unless --list-messages is used")
    _print_json(run(args.message_index, visible_password_input=args.visible_password_input))
