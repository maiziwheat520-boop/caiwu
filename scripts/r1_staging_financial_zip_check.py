"""Verify forwarded financial ZIP passwords one archive at a time, in memory."""

from __future__ import annotations

import argparse
import getpass
import importlib
import io
import json
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


def _load_message(message_index: int) -> MailMessage:
    provider = ImapMailProvider(
        _ImapSslTransport(host="imap.163.com", port=993),
        CredentialFileSecretProvider(DEFAULT_CREDENTIAL_FILE, key="authorization_code"),
        mailbox="redeatt@163.com",
        auth_mode="password",
        max_messages=5,
    )
    messages = list(provider.iter_messages())
    if message_index < 1 or message_index > len(messages):
        raise RuntimeError(f"message index must be between 1 and {len(messages)}")
    return messages[message_index - 1]


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


def run(message_index: int) -> dict[str, Any]:
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
        password = getpass.getpass(
            f"Password for ZIP {position}/{len(message.attachments)} "
            f"({attachment.filename}) — blank skips: "
        )
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
        "subject": message.subject,
        "attachments_checked": len(results),
        "results": results,
        "storage": "memory-only",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--message-index", type=int, default=2, help="1-based index in latest-five view"
    )
    args = parser.parse_args()
    print(json.dumps(run(args.message_index), ensure_ascii=False, sort_keys=True))
