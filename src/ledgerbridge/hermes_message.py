"""Fail-closed policy boundary for future Hermes private-message intake.

The module deliberately performs no network I/O, persistence, classification,
or deletion.  It only validates a bounded envelope and decides whether a
message is eligible for the later financial-triage pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from ledgerbridge.text import contains_unstorable_text

MAX_MESSAGE_ID = 300
MAX_PROFILE_REF = 200
MAX_TEXT_BYTES = 1_000_000
MAX_ATTACHMENTS = 32
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

ChatKind = Literal["private", "group"]
ProfileKind = Literal["primary", "family", "assistant", "tool", "system"]
SenderKind = Literal["user", "other", "assistant", "tool", "system"]


class HermesMessageError(ValueError):
    """A message envelope violated the bounded intake contract."""


class HermesMessageDisposition(StrEnum):
    """The only outcomes available before financial triage is authorized."""

    RETAIN_FOR_TRIAGE = "RETAIN_FOR_TRIAGE"
    IGNORE_HISTORY = "IGNORE_HISTORY"
    DELETE_TOMBSTONE = "DELETE_TOMBSTONE"


class HermesMessageReason(StrEnum):
    ELIGIBLE_PRIVATE = "ELIGIBLE_PRIVATE"
    NON_PRIMARY_PROFILE = "NON_PRIMARY_PROFILE"
    NON_PRIVATE_CHAT = "NON_PRIVATE_CHAT"
    NON_USER_SENDER = "NON_USER_SENDER"
    BEFORE_ACTIVATION = "BEFORE_ACTIVATION"
    EMPTY_MESSAGE = "EMPTY_MESSAGE"


@dataclass(frozen=True, slots=True)
class HermesMessageAttachment:
    """Bounded attachment bytes handed to the later artifact pipeline."""

    filename: str
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        _require_text("attachment.filename", self.filename, 255)
        if self.filename in {".", ".."} or "/" in self.filename or "\\" in self.filename:
            raise HermesMessageError("attachment filename is invalid")
        _require_text("attachment.media_type", self.media_type, 200)
        if len(self.content) > MAX_ATTACHMENT_BYTES:
            raise HermesMessageError("attachment exceeds the file limit")


@dataclass(frozen=True, slots=True)
class HermesPrivateMessage:
    """A provider-neutral, already-authenticated Hermes message envelope."""

    message_id: str
    profile_ref: str
    profile_kind: ProfileKind
    chat_kind: ChatKind
    sender_kind: SenderKind
    sent_at: datetime
    text: str
    attachments: tuple[HermesMessageAttachment, ...] = ()

    def __post_init__(self) -> None:
        _require_text("message_id", self.message_id, MAX_MESSAGE_ID)
        _require_text("profile_ref", self.profile_ref, MAX_PROFILE_REF)
        if self.profile_kind not in {"primary", "family", "assistant", "tool", "system"}:
            raise HermesMessageError("profile_kind is invalid")
        if self.chat_kind not in {"private", "group"}:
            raise HermesMessageError("chat_kind is invalid")
        if self.sender_kind not in {"user", "other", "assistant", "tool", "system"}:
            raise HermesMessageError("sender_kind is invalid")
        if self.sent_at.tzinfo is None:
            raise HermesMessageError("sent_at must be timezone-aware")
        _require_text("text", self.text, MAX_TEXT_BYTES, allow_empty=True)
        if len(self.attachments) > MAX_ATTACHMENTS:
            raise HermesMessageError("message has too many attachments")

    @property
    def has_content(self) -> bool:
        return bool(self.text or self.attachments)


@dataclass(frozen=True, slots=True)
class HermesMessageDecision:
    disposition: HermesMessageDisposition
    reason: HermesMessageReason
    ingest_channel: Literal["HERMES"] = "HERMES"
    source_system: Literal["hermes_private_chat"] = "hermes_private_chat"


def classify_private_message(
    message: HermesPrivateMessage,
    *,
    primary_profile_ref: str,
    activated_at: datetime,
) -> HermesMessageDecision:
    """Apply the D-015 private-chat boundary without making a financial decision."""

    _require_text("primary_profile_ref", primary_profile_ref, MAX_PROFILE_REF)
    if activated_at.tzinfo is None:
        raise HermesMessageError("activated_at must be timezone-aware")
    if message.profile_kind != "primary" or message.profile_ref != primary_profile_ref:
        return HermesMessageDecision(
            HermesMessageDisposition.DELETE_TOMBSTONE,
            HermesMessageReason.NON_PRIMARY_PROFILE,
        )
    if message.chat_kind != "private":
        return HermesMessageDecision(
            HermesMessageDisposition.DELETE_TOMBSTONE,
            HermesMessageReason.NON_PRIVATE_CHAT,
        )
    if message.sender_kind != "user":
        return HermesMessageDecision(
            HermesMessageDisposition.DELETE_TOMBSTONE,
            HermesMessageReason.NON_USER_SENDER,
        )
    if message.sent_at < activated_at:
        return HermesMessageDecision(
            HermesMessageDisposition.IGNORE_HISTORY,
            HermesMessageReason.BEFORE_ACTIVATION,
        )
    if not message.has_content:
        return HermesMessageDecision(
            HermesMessageDisposition.DELETE_TOMBSTONE,
            HermesMessageReason.EMPTY_MESSAGE,
        )
    return HermesMessageDecision(
        HermesMessageDisposition.RETAIN_FOR_TRIAGE,
        HermesMessageReason.ELIGIBLE_PRIVATE,
    )


def _require_text(field: str, value: str, maximum: int, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise HermesMessageError(f"{field} must be text")
    if (not allow_empty and not value.strip()) or len(value.encode("utf-8")) > maximum:
        raise HermesMessageError(f"{field} is outside the allowed bounds")
    if contains_unstorable_text(value):
        raise HermesMessageError(f"{field} contains non-storable text")


def utc_now() -> datetime:
    """Expose an explicit UTC clock seam for future adapters and demos."""

    return datetime.now(UTC)
