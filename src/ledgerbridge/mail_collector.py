"""Fail-closed Microsoft Graph mailbox adapter for the Phase 4 framework.

This module deliberately stops at a bounded provider boundary. It does not
read environment secrets, persist tokens, publish artifacts, or register a
real Connector. A deployment must inject both a token provider and a narrow
Graph transport after the authentication and manifest gates are approved.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlencode, urlsplit

from ledgerbridge.text import contains_unstorable_text

MAX_MAILBOX_TEXT = 500
MAX_ATTACHMENT_NAME = 255
MAX_MESSAGES_PER_RUN = 100
MAX_ATTACHMENTS_PER_MESSAGE = 32
MAX_GRAPH_PAGE_SIZE = 50
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
GRAPH_HOST = "graph.microsoft.com"
GRAPH_VERSION = "v1.0"
logger = logging.getLogger(__name__)


class MailCollectorError(RuntimeError):
    """A bounded, machine-readable mailbox collection failure."""

    def __init__(self, error_code: str, summary: str) -> None:
        super().__init__(summary)
        self.error_code = error_code
        self.summary = summary


class AccessTokenProvider(Protocol):
    """Resolve a short-lived Graph token without exposing its value to callers."""

    def get_access_token(self) -> str: ...


class GraphTransport(Protocol):
    """The only I/O seam used by the provider; production supplies a reviewed client."""

    def get_json(self, path: str, *, authorization: str) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class MailAttachment:
    attachment_id: str
    filename: str
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        _require_text("attachment_id", self.attachment_id, MAX_MAILBOX_TEXT)
        _require_filename(self.filename)
        _require_text("media_type", self.media_type, 200)
        if len(self.content) > MAX_ATTACHMENT_BYTES:
            raise MailCollectorError(
                "MAIL_ATTACHMENT_TOO_LARGE", "mail attachment exceeds the file limit"
            )

    @property
    def byte_size(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class MailMessage:
    message_id: str
    subject: str
    received_at: str
    attachments: tuple[MailAttachment, ...]
    body_preview: str = ""
    sender_address: str = ""
    resent_from_address: str = ""

    def __post_init__(self) -> None:
        _require_text("message_id", self.message_id, MAX_MAILBOX_TEXT)
        _require_text("subject", self.subject, MAX_MAILBOX_TEXT)
        _require_text("received_at", self.received_at, 100)
        _require_text("body_preview", self.body_preview, MAX_MAILBOX_TEXT, allow_empty=True)
        _require_text("sender_address", self.sender_address, MAX_MAILBOX_TEXT, allow_empty=True)
        _require_text(
            "resent_from_address", self.resent_from_address, MAX_MAILBOX_TEXT, allow_empty=True
        )
        if len(self.attachments) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise MailCollectorError(
                "MAIL_ATTACHMENT_LIMIT", "mail message has too many attachments"
            )


class MailProvider(Protocol):
    """Provider-neutral mailbox boundary shared by Graph and IMAP adapters."""

    def iter_messages(self) -> Iterator[MailMessage]: ...


@dataclass(frozen=True, slots=True)
class CollectedAttachment:
    """A bounded handoff value for the later ArtifactStore integration."""

    message_id: str
    received_at: str
    attachment: MailAttachment


class MicrosoftGraphMailProvider:
    """Read-only Graph adapter with injected transport and token authority."""

    def __init__(
        self,
        transport: GraphTransport,
        token_provider: AccessTokenProvider,
        *,
        mailbox: str,
        folder: str = "inbox",
        page_size: int = 20,
        max_pages: int = 10,
        graph_host: str = GRAPH_HOST,
    ) -> None:
        _require_text("mailbox", mailbox, MAX_MAILBOX_TEXT)
        _require_text("folder", folder, MAX_MAILBOX_TEXT)
        if page_size <= 0 or page_size > MAX_GRAPH_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_GRAPH_PAGE_SIZE}")
        if max_pages <= 0 or max_pages > MAX_MESSAGES_PER_RUN:
            raise ValueError(f"max_pages must be between 1 and {MAX_MESSAGES_PER_RUN}")
        _require_host(graph_host)
        self._transport = transport
        self._token_provider = token_provider
        self._mailbox = mailbox
        self._folder = folder
        self._page_size = page_size
        self._max_pages = max_pages
        self._graph_host = graph_host

    def iter_messages(self) -> Iterator[MailMessage]:
        path: str | None = self._messages_path()
        pages = 0
        yielded = 0
        while path is not None:
            pages += 1
            if pages > self._max_pages:
                raise MailCollectorError("MAIL_PAGE_LIMIT", "mail provider page limit exceeded")
            payload = self._get_json(path)
            values = payload.get("value")
            if not isinstance(values, list):
                raise MailCollectorError(
                    "MAIL_PROVIDER_RESPONSE", "mail provider response is invalid"
                )
            for raw in values:
                if not isinstance(raw, Mapping):
                    raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "mail message is invalid")
                yielded += 1
                if yielded > MAX_MESSAGES_PER_RUN:
                    raise MailCollectorError("MAIL_MESSAGE_LIMIT", "mail message limit exceeded")
                yield self._parse_message(raw)
            path = self._next_path(payload.get("@odata.nextLink"))

    def _parse_message(self, raw: Mapping[str, object]) -> MailMessage:
        message_id = _required_string(raw, "id", MAX_MAILBOX_TEXT)
        subject = _required_string(raw, "subject", MAX_MAILBOX_TEXT, default="(no subject)")
        received_at = _required_string(raw, "receivedDateTime", 100)
        has_attachments = raw.get("hasAttachments", False)
        if not isinstance(has_attachments, bool):
            raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "attachment flag is invalid")
        attachments = tuple(self._iter_attachments(message_id)) if has_attachments else ()
        body_preview = raw.get("bodyPreview", "")
        if not isinstance(body_preview, str):
            raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "mail body preview is invalid")
        return MailMessage(message_id, subject, received_at, attachments, body_preview)

    def _iter_attachments(self, message_id: str) -> Iterator[MailAttachment]:
        path = self._attachments_path(message_id)
        payload = self._get_json(path)
        values = payload.get("value")
        if not isinstance(values, list):
            raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "attachment response is invalid")
        if payload.get("@odata.nextLink") is not None:
            raise MailCollectorError(
                "MAIL_ATTACHMENT_LIMIT", "mail attachment page exceeds the bounded limit"
            )
        count = 0
        for raw in values:
            if not isinstance(raw, Mapping):
                raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "mail attachment is invalid")
            if raw.get("@odata.type") != "#microsoft.graph.fileAttachment":
                continue
            if raw.get("isInline", False) is True:
                continue
            count += 1
            if count > MAX_ATTACHMENTS_PER_MESSAGE:
                raise MailCollectorError(
                    "MAIL_ATTACHMENT_LIMIT", "mail message has too many attachments"
                )
            yield self._parse_attachment(raw)

    def _parse_attachment(self, raw: Mapping[str, object]) -> MailAttachment:
        attachment_id = _required_string(raw, "id", MAX_MAILBOX_TEXT)
        filename = _required_string(raw, "name", MAX_ATTACHMENT_NAME)
        media_type = _required_string(raw, "contentType", 200, default="application/octet-stream")
        encoded = raw.get("contentBytes")
        if not isinstance(encoded, str) or not encoded:
            raise MailCollectorError(
                "MAIL_ATTACHMENT_UNAVAILABLE", "mail attachment content is unavailable"
            )
        if len(encoded) > ((MAX_ATTACHMENT_BYTES + 2) * 4 // 3) + 4:
            raise MailCollectorError(
                "MAIL_ATTACHMENT_TOO_LARGE", "mail attachment exceeds the file limit"
            )
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise MailCollectorError(
                "MAIL_PROVIDER_RESPONSE", "mail attachment encoding is invalid"
            ) from exc
        declared_size = raw.get("size")
        if declared_size is not None and (
            isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size != len(content)
        ):
            raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "mail attachment size is invalid")
        return MailAttachment(attachment_id, filename, media_type, content)

    def _get_json(self, path: str) -> Mapping[str, object]:
        try:
            token = self._token_provider.get_access_token()
        except Exception as exc:
            logger.warning(
                "mail provider authorization unavailable",
                extra={"error_code": "MAIL_AUTH_UNAVAILABLE"},
            )
            raise MailCollectorError(
                "MAIL_AUTH_UNAVAILABLE", "mail provider authorization is unavailable"
            ) from exc
        if not isinstance(token, str) or not token.strip():
            raise MailCollectorError(
                "MAIL_AUTH_UNAVAILABLE", "mail provider authorization is unavailable"
            )
        try:
            value = self._transport.get_json(path, authorization=f"Bearer {token}")
        except MailCollectorError:
            raise
        except Exception as exc:
            logger.warning(
                "mail provider request failed",
                extra={"error_code": "MAIL_PROVIDER_UNAVAILABLE"},
            )
            raise MailCollectorError(
                "MAIL_PROVIDER_UNAVAILABLE", "mail provider request failed"
            ) from exc
        if not isinstance(value, Mapping):
            raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "mail provider response is invalid")
        return value

    def _messages_path(self) -> str:
        return (
            f"/{GRAPH_VERSION}/users/{quote(self._mailbox, safe='')}"
            f"/mailFolders/{quote(self._folder, safe='')}/messages?"
            + urlencode(
                {
                    "$top": self._page_size,
                    "$select": "id,subject,receivedDateTime,hasAttachments,bodyPreview",
                }
            )
        )

    def _attachments_path(self, message_id: str) -> str:
        return (
            f"/{GRAPH_VERSION}/users/{quote(self._mailbox, safe='')}"
            f"/messages/{quote(message_id, safe='')}/attachments?"
            + urlencode({"$top": MAX_ATTACHMENTS_PER_MESSAGE})
        )

    def _next_path(self, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "mail pagination link is invalid")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname != self._graph_host:
            raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "mail pagination host is invalid")
        path = parsed.path
        if not path.startswith(f"/{GRAPH_VERSION}/"):
            raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "mail pagination path is invalid")
        return path + (f"?{parsed.query}" if parsed.query else "")


class MailCollector:
    """Bounded collection facade; ArtifactStore publication is a later step."""

    def __init__(
        self,
        provider: MailProvider | None = None,
        *,
        max_messages: int = MAX_MESSAGES_PER_RUN,
    ) -> None:
        if max_messages <= 0 or max_messages > MAX_MESSAGES_PER_RUN:
            raise ValueError(f"max_messages must be between 1 and {MAX_MESSAGES_PER_RUN}")
        self._provider = provider
        self._max_messages = max_messages

    def collect(self) -> Iterator[CollectedAttachment]:
        """Stream bounded handoff values without retaining attachment bytes."""

        if self._provider is None:
            raise MailCollectorError("MAIL_PROVIDER_DISABLED", "mail collection is disabled")
        for index, message in enumerate(self._provider.iter_messages(), start=1):
            if index > self._max_messages:
                raise MailCollectorError("MAIL_MESSAGE_LIMIT", "mail message limit exceeded")
            yield from (
                CollectedAttachment(message.message_id, message.received_at, attachment)
                for attachment in message.attachments
            )


def _required_string(
    raw: Mapping[str, object],
    field: str,
    maximum: int,
    *,
    default: str | None = None,
) -> str:
    value = raw.get(field, default)
    if not isinstance(value, str):
        raise MailCollectorError("MAIL_PROVIDER_RESPONSE", f"mail field {field} is invalid")
    _require_text(field, value, maximum)
    return value


def _require_text(field: str, value: str, maximum: int, *, allow_empty: bool = False) -> None:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value.strip())
        or len(value) > maximum
        or contains_unstorable_text(value)
    ):
        raise MailCollectorError("MAIL_PROVIDER_RESPONSE", f"mail field {field} is invalid")


def _require_filename(value: str) -> None:
    _require_text("filename", value, MAX_ATTACHMENT_NAME)
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise MailCollectorError("MAIL_PROVIDER_RESPONSE", "mail attachment filename is invalid")


def _require_host(value: str) -> None:
    if value != GRAPH_HOST:
        raise ValueError("graph_host must be graph.microsoft.com")


def main() -> None:
    logger.error("mail provider is disabled", extra={"error_code": "MAIL_PROVIDER_DISABLED"})
    raise SystemExit(2)


if __name__ == "__main__":
    main()
