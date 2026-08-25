"""Bounded, read-only IMAP mailbox adapter.

The adapter keeps protocol I/O behind an injected transport.  Staging may use
XOAUTH2 (preferred) or a separately generated Microsoft app password; neither
credential is persisted by this module.
"""

from __future__ import annotations

import email
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Protocol

from ledgerbridge.mail_collector import MailAttachment, MailCollectorError, MailMessage

MAX_IMAP_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_IMAP_MESSAGES = 20
MAX_IMAP_ATTACHMENTS = 32
MAX_IMAP_ATTACHMENT_BYTES = 50 * 1024 * 1024


class ImapCredentialProvider(Protocol):
    """Resolve one short-lived OAuth token or app password in memory."""

    def get_secret(self) -> str: ...


class ImapTransport(Protocol):
    """The only IMAP I/O seam used by the provider."""

    def authenticate(self, mailbox: str, secret: str, *, mode: str) -> None: ...

    def select_inbox(self) -> None: ...

    def search_all(self) -> tuple[str, ...]: ...

    def fetch_rfc822(self, message_id: str) -> bytes: ...

    def close(self) -> None: ...


class ImapMailProvider:
    """Read-only bounded IMAP provider producing the common MailMessage type."""

    def __init__(
        self,
        transport: ImapTransport,
        credential_provider: ImapCredentialProvider,
        *,
        mailbox: str,
        auth_mode: str = "xoauth2",
        max_messages: int = 5,
    ) -> None:
        if not mailbox or len(mailbox) > 500 or any(ch in mailbox for ch in "\r\n"):
            raise ValueError("mailbox is invalid")
        if auth_mode not in {"xoauth2", "password"}:
            raise ValueError("auth_mode must be xoauth2 or password")
        if max_messages <= 0 or max_messages > MAX_IMAP_MESSAGES:
            raise ValueError("max_messages is out of bounds")
        self._transport = transport
        self._credential_provider = credential_provider
        self._mailbox = mailbox
        self._auth_mode = auth_mode
        self._max_messages = max_messages

    def iter_messages(self) -> Iterator[MailMessage]:
        try:
            secret = self._credential_provider.get_secret()
            self._transport.authenticate(self._mailbox, secret, mode=self._auth_mode)
            self._transport.select_inbox()
            ids = self._transport.search_all()
            for message_id in tuple(reversed(ids[-self._max_messages :])):
                raw = self._transport.fetch_rfc822(message_id)
                if len(raw) > MAX_IMAP_MESSAGE_BYTES:
                    raise MailCollectorError(
                        "MAIL_MESSAGE_TOO_LARGE", "IMAP message exceeds staging limit"
                    )
                yield parse_imap_message(raw, fallback_id=message_id)
        except MailCollectorError:
            raise
        except Exception as exc:
            raise MailCollectorError("MAIL_PROVIDER_UNAVAILABLE", "IMAP request failed") from exc
        finally:
            with suppress(Exception):
                self._transport.close()


def parse_imap_message(raw: bytes, *, fallback_id: str) -> MailMessage:
    if not raw or len(raw) > MAX_IMAP_MESSAGE_BYTES:
        raise MailCollectorError("MAIL_MESSAGE_TOO_LARGE", "IMAP message exceeds staging limit")
    try:
        message = email.message_from_bytes(raw)
    except (TypeError, ValueError, email.errors.MessageParseError) as exc:
        raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "IMAP message is invalid") from exc
    message_id = _header(message, "Message-ID") or fallback_id
    subject = _header(message, "Subject") or "(no subject)"
    received_at = _normalize_received_at(_header(message, "Date"))
    body_parts: list[str] = []
    attachments: list[MailAttachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        if filename:
            if len(attachments) >= MAX_IMAP_ATTACHMENTS:
                raise MailCollectorError("MAIL_ATTACHMENT_LIMIT", "IMAP attachment limit exceeded")
            if len(payload) > MAX_IMAP_ATTACHMENT_BYTES:
                raise MailCollectorError(
                    "MAIL_ATTACHMENT_TOO_LARGE", "IMAP attachment is too large"
                )
            attachments.append(
                MailAttachment(
                    attachment_id=f"{message_id}:{len(attachments) + 1}",
                    filename=_safe_filename(filename),
                    media_type=part.get_content_type()[:200],
                    content=payload,
                )
            )
        elif part.get_content_type().startswith("text/") and len(body_parts) < 4:
            charset = part.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace")[:500])
    return MailMessage(
        message_id=message_id[:500],
        subject=subject[:500],
        received_at=received_at[:100],
        attachments=tuple(attachments),
        body_preview="\n".join(body_parts).strip()[:500],
    )


def _header(message: Message, name: str) -> str:
    value = message.get(name, "")
    if not isinstance(value, str):
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (UnicodeError, ValueError):
        return value.strip()


def _normalize_received_at(value: str) -> str:
    if not value:
        return "1970-01-01T00:00:00+00:00"
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return "1970-01-01T00:00:00+00:00"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _safe_filename(value: str) -> str:
    try:
        value = str(make_header(decode_header(value))).strip()
    except (UnicodeError, ValueError):
        value = value.strip()
    cleaned = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return cleaned[:255] or "attachment.bin"
