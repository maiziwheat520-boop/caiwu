"""Bounded RFC 5322/EML parser for the local intake staging boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Final

from ledgerbridge.mail_collector import MailAttachment, MailCollectorError

MAX_EML_BYTES: Final = 10 * 1024 * 1024
MAX_BODY_CHARS: Final = 1_000_000
MAX_HEADER_CHARS: Final = 500
MAX_ATTACHMENTS: Final = 32


@dataclass(frozen=True, slots=True)
class ParsedEml:
    message_id: str
    subject: str
    received_at: datetime
    text: str
    attachments: tuple[MailAttachment, ...]


def parse_eml(raw: bytes) -> ParsedEml:
    if not isinstance(raw, bytes) or not raw:
        raise MailCollectorError("MAIL_EML_INVALID", "EML input is empty")
    if len(raw) > MAX_EML_BYTES:
        raise MailCollectorError("MAIL_EML_TOO_LARGE", "EML input exceeds the file limit")
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise MailCollectorError("MAIL_EML_INVALID", "EML input cannot be parsed") from exc

    message_id = _header(message.get("Message-ID"), "synthetic-eml-message")
    subject = _header(message.get("Subject"), "(no subject)")
    received_at = _received_at(message.get("Date"))
    body_parts: list[str] = []
    attachments: list[MailAttachment] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        if disposition == "attachment" or part.get_filename():
            if len(attachments) >= MAX_ATTACHMENTS:
                raise MailCollectorError("MAIL_ATTACHMENT_LIMIT", "EML has too many attachments")
            filename = part.get_filename() or "attachment.bin"
            content = part.get_payload(decode=True)
            if not isinstance(content, bytes):
                raise MailCollectorError(
                    "MAIL_ATTACHMENT_UNAVAILABLE", "EML attachment content is unavailable"
                )
            attachments.append(
                MailAttachment(
                    attachment_id=f"{message_id}:{len(attachments)}",
                    filename=filename,
                    media_type=part.get_content_type() or "application/octet-stream",
                    content=content,
                )
            )
            continue
        if part.get_content_type() == "text/plain":
            try:
                value = part.get_content()
            except Exception as exc:
                raise MailCollectorError("MAIL_EML_INVALID", "EML text body is invalid") from exc
            if isinstance(value, str):
                body_parts.append(value)
    text = "\n".join(body_parts)
    if len(text) > MAX_BODY_CHARS:
        raise MailCollectorError("MAIL_EML_TOO_LARGE", "EML text body exceeds the limit")
    return ParsedEml(message_id, subject, received_at, text, tuple(attachments))


def _header(value: str | None, default: str) -> str:
    if value is None:
        return default
    value = value.strip()
    if not value or len(value) > MAX_HEADER_CHARS or "\x00" in value:
        raise MailCollectorError("MAIL_EML_INVALID", "EML header is invalid")
    return value


def _received_at(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise MailCollectorError("MAIL_EML_INVALID", "EML date is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
