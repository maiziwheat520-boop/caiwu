"""What the Hermes private-message boundary admits, and what it refuses.

The module decides eligibility and nothing else -- it fetches nothing, stores
nothing, classifies nothing and deletes nothing -- so every case here is about
the shape of the envelope or the disposition it earns.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from ledgerbridge.hermes_message import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    MAX_MESSAGE_ID,
    MAX_PROFILE_REF,
    MAX_TEXT_BYTES,
    HermesMessageAttachment,
    HermesMessageDisposition,
    HermesMessageError,
    HermesMessageReason,
    HermesPrivateMessage,
    classify_private_message,
    utc_now,
)

PRIMARY = "telegram-8906289598"
ACTIVATED = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
SENT = ACTIVATED + timedelta(hours=1)


def _attachment(**overrides: Any) -> HermesMessageAttachment:
    fields: dict[str, Any] = {
        "filename": "receipt.png",
        "media_type": "image/png",
        "content": b"\x89PNG",
    }
    fields.update(overrides)
    return HermesMessageAttachment(**fields)


def _message(**overrides: Any) -> HermesPrivateMessage:
    """The smallest envelope the boundary admits, adjusted by the caller."""

    fields: dict[str, Any] = {
        "message_id": "m-1",
        "profile_ref": PRIMARY,
        "profile_kind": "primary",
        "chat_kind": "private",
        "sender_kind": "user",
        "sent_at": SENT,
        "text": "这是一张发票",
    }
    fields.update(overrides)
    return HermesPrivateMessage(**fields)


def _decide(message: HermesPrivateMessage) -> tuple[HermesMessageDisposition, HermesMessageReason]:
    decision = classify_private_message(
        message, primary_profile_ref=PRIMARY, activated_at=ACTIVATED
    )
    assert decision.ingest_channel == "HERMES"
    assert decision.source_system == "hermes_private_chat"
    return decision.disposition, decision.reason


def test_an_eligible_private_message_is_retained_for_triage() -> None:
    assert _decide(_message()) == (
        HermesMessageDisposition.RETAIN_FOR_TRIAGE,
        HermesMessageReason.ELIGIBLE_PRIVATE,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"profile_kind": "family"},
        {"profile_kind": "assistant"},
        {"profile_ref": "telegram-other"},
    ],
)
def test_a_message_outside_the_primary_profile_is_not_kept(overrides: dict[str, Any]) -> None:
    assert _decide(_message(**overrides)) == (
        HermesMessageDisposition.DELETE_TOMBSTONE,
        HermesMessageReason.NON_PRIMARY_PROFILE,
    )


def test_a_group_chat_is_not_kept() -> None:
    assert _decide(_message(chat_kind="group")) == (
        HermesMessageDisposition.DELETE_TOMBSTONE,
        HermesMessageReason.NON_PRIVATE_CHAT,
    )


@pytest.mark.parametrize("sender_kind", ["other", "assistant", "tool", "system"])
def test_a_message_the_user_did_not_send_is_not_kept(sender_kind: str) -> None:
    assert _decide(_message(sender_kind=sender_kind)) == (
        HermesMessageDisposition.DELETE_TOMBSTONE,
        HermesMessageReason.NON_USER_SENDER,
    )


def test_a_message_from_before_activation_is_ignored_rather_than_deleted() -> None:
    assert _decide(_message(sent_at=ACTIVATED - timedelta(seconds=1))) == (
        HermesMessageDisposition.IGNORE_HISTORY,
        HermesMessageReason.BEFORE_ACTIVATION,
    )


def test_a_message_sent_at_the_activation_instant_is_kept() -> None:
    assert _decide(_message(sent_at=ACTIVATED))[0] is HermesMessageDisposition.RETAIN_FOR_TRIAGE


def test_a_message_with_neither_text_nor_attachments_is_not_kept() -> None:
    assert _decide(_message(text="")) == (
        HermesMessageDisposition.DELETE_TOMBSTONE,
        HermesMessageReason.EMPTY_MESSAGE,
    )


def test_an_attachment_alone_is_content_enough_to_keep() -> None:
    message = _message(text="", attachments=(_attachment(),))

    assert message.has_content
    assert _decide(message)[0] is HermesMessageDisposition.RETAIN_FOR_TRIAGE


def test_a_message_with_no_content_reports_none() -> None:
    assert not _message(text="").has_content


def test_a_blank_primary_profile_ref_is_refused() -> None:
    with pytest.raises(HermesMessageError, match="primary_profile_ref is outside"):
        classify_private_message(_message(), primary_profile_ref="  ", activated_at=ACTIVATED)


def test_a_naive_activation_instant_is_refused() -> None:
    with pytest.raises(HermesMessageError, match="activated_at must be timezone-aware"):
        classify_private_message(
            _message(), primary_profile_ref=PRIMARY, activated_at=datetime(2026, 8, 25)
        )


@pytest.mark.parametrize("field", ["message_id", "profile_ref"])
def test_a_blank_identifier_is_refused(field: str) -> None:
    with pytest.raises(HermesMessageError, match=f"{field} is outside the allowed bounds"):
        _message(**{field: "   "})


@pytest.mark.parametrize(
    ("field", "maximum"),
    [("message_id", MAX_MESSAGE_ID), ("profile_ref", MAX_PROFILE_REF)],
)
def test_an_identifier_past_its_bound_is_refused(field: str, maximum: int) -> None:
    with pytest.raises(HermesMessageError, match=f"{field} is outside the allowed bounds"):
        _message(**{field: "a" * (maximum + 1)})


def test_text_past_the_byte_bound_is_refused() -> None:
    with pytest.raises(HermesMessageError, match="text is outside the allowed bounds"):
        _message(text="a" * (MAX_TEXT_BYTES + 1))


def test_the_text_bound_counts_bytes_rather_than_characters() -> None:
    with pytest.raises(HermesMessageError, match="text is outside the allowed bounds"):
        _message(text="发" * MAX_TEXT_BYTES)


def test_a_value_that_is_not_text_is_refused() -> None:
    with pytest.raises(HermesMessageError, match="message_id must be text"):
        _message(message_id=cast("str", 5))


@pytest.mark.parametrize("value", ["a\x00b", "a\ud800b"])
def test_text_that_cannot_be_stored_as_utf8_is_refused(value: str) -> None:
    with pytest.raises(HermesMessageError, match="message_id contains non-storable text"):
        _message(message_id=value)


def test_an_unknown_profile_kind_is_refused() -> None:
    with pytest.raises(HermesMessageError, match="profile_kind is invalid"):
        _message(profile_kind="stranger")


def test_an_unknown_chat_kind_is_refused() -> None:
    with pytest.raises(HermesMessageError, match="chat_kind is invalid"):
        _message(chat_kind="channel")


def test_an_unknown_sender_kind_is_refused() -> None:
    with pytest.raises(HermesMessageError, match="sender_kind is invalid"):
        _message(sender_kind="bot")


def test_a_naive_sent_at_is_refused() -> None:
    with pytest.raises(HermesMessageError, match="sent_at must be timezone-aware"):
        _message(sent_at=datetime(2026, 8, 25, 1, 0))


def test_more_attachments_than_the_bound_allows_is_refused() -> None:
    with pytest.raises(HermesMessageError, match="too many attachments"):
        _message(attachments=tuple(_attachment() for _ in range(MAX_ATTACHMENTS + 1)))


@pytest.mark.parametrize("filename", [".", "..", "a/b.png", "a\\b.png"])
def test_an_attachment_filename_that_could_escape_its_directory_is_refused(
    filename: str,
) -> None:
    with pytest.raises(HermesMessageError, match="attachment filename is invalid"):
        _attachment(filename=filename)


def test_a_blank_attachment_filename_is_refused() -> None:
    with pytest.raises(HermesMessageError, match=r"attachment\.filename is outside"):
        _attachment(filename="")


def test_a_blank_attachment_media_type_is_refused() -> None:
    with pytest.raises(HermesMessageError, match=r"attachment\.media_type is outside"):
        _attachment(media_type=" ")


def test_an_attachment_past_the_file_limit_is_refused() -> None:
    with pytest.raises(HermesMessageError, match="attachment exceeds the file limit"):
        _attachment(content=bytes(MAX_ATTACHMENT_BYTES + 1))


def test_the_clock_seam_answers_in_utc() -> None:
    assert utc_now().tzinfo is UTC
