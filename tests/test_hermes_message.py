"""The D-015 private-chat boundary, exercised at every branch it can refuse."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from ledgerbridge.hermes_message import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    HermesMessageAttachment,
    HermesMessageDisposition,
    HermesMessageError,
    HermesMessageReason,
    HermesPrivateMessage,
    classify_private_message,
    utc_now,
)

PRIMARY_PROFILE = "telegram-8906289598"
ACTIVATED_AT = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def _message(**overrides: object) -> HermesPrivateMessage:
    fields: dict[str, object] = {
        "message_id": "hermes-0001",
        "profile_ref": PRIMARY_PROFILE,
        "profile_kind": "primary",
        "chat_kind": "private",
        "sender_kind": "user",
        "sent_at": ACTIVATED_AT + timedelta(minutes=1),
        "text": "这是一张发票",
        "attachments": (),
    }
    fields.update(overrides)
    return HermesPrivateMessage(**fields)  # type: ignore[arg-type]


def _classify(
    message: HermesPrivateMessage,
) -> tuple[HermesMessageDisposition, HermesMessageReason]:
    decision = classify_private_message(
        message,
        primary_profile_ref=PRIMARY_PROFILE,
        activated_at=ACTIVATED_AT,
    )
    assert decision.ingest_channel == "HERMES"
    assert decision.source_system == "hermes_private_chat"
    return decision.disposition, decision.reason


def test_eligible_private_message_is_retained_for_triage() -> None:
    assert _classify(_message()) == (
        HermesMessageDisposition.RETAIN_FOR_TRIAGE,
        HermesMessageReason.ELIGIBLE_PRIVATE,
    )


def test_a_message_carrying_only_an_attachment_still_has_content() -> None:
    attachment = HermesMessageAttachment("receipt.pdf", "application/pdf", b"%PDF-1.7")
    message = _message(text="", attachments=(attachment,))
    assert message.has_content is True
    assert _classify(message)[0] is HermesMessageDisposition.RETAIN_FOR_TRIAGE


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"profile_kind": "family"}, HermesMessageReason.NON_PRIMARY_PROFILE),
        ({"profile_kind": "assistant"}, HermesMessageReason.NON_PRIMARY_PROFILE),
        ({"profile_ref": "telegram-someone-else"}, HermesMessageReason.NON_PRIMARY_PROFILE),
        ({"chat_kind": "group"}, HermesMessageReason.NON_PRIVATE_CHAT),
        ({"sender_kind": "other"}, HermesMessageReason.NON_USER_SENDER),
        ({"sender_kind": "assistant"}, HermesMessageReason.NON_USER_SENDER),
        ({"sender_kind": "tool"}, HermesMessageReason.NON_USER_SENDER),
        ({"sender_kind": "system"}, HermesMessageReason.NON_USER_SENDER),
        ({"text": "", "attachments": ()}, HermesMessageReason.EMPTY_MESSAGE),
    ],
)
def test_everything_outside_the_primary_private_chat_is_tombstoned(
    overrides: dict[str, object],
    reason: HermesMessageReason,
) -> None:
    disposition, actual = _classify(_message(**overrides))
    assert disposition is HermesMessageDisposition.DELETE_TOMBSTONE
    assert actual is reason


def test_history_before_activation_is_ignored_rather_than_deleted() -> None:
    # D-015 activates at a moment; older private mail is left alone, not erased.
    assert _classify(_message(sent_at=ACTIVATED_AT - timedelta(seconds=1))) == (
        HermesMessageDisposition.IGNORE_HISTORY,
        HermesMessageReason.BEFORE_ACTIVATION,
    )


def test_the_activation_instant_itself_is_admitted() -> None:
    assert (
        _classify(_message(sent_at=ACTIVATED_AT))[0] is HermesMessageDisposition.RETAIN_FOR_TRIAGE
    )


def test_the_profile_gate_is_checked_before_the_chat_and_sender_gates() -> None:
    # A group message from an assistant profile must report the profile refusal,
    # so the first boundary a message fails is the one that gets recorded.
    _, reason = _classify(_message(profile_kind="assistant", chat_kind="group", sender_kind="tool"))
    assert reason is HermesMessageReason.NON_PRIMARY_PROFILE


def test_activation_is_compared_across_time_zones_not_wall_clock() -> None:
    east_of_utc = timezone(timedelta(hours=8))
    # 2026-09-01 07:59 +08:00 is 2026-08-31 23:59Z, still before activation.
    before = _message(sent_at=datetime(2026, 9, 1, 7, 59, tzinfo=east_of_utc))
    assert _classify(before)[1] is HermesMessageReason.BEFORE_ACTIVATION
    after = _message(sent_at=datetime(2026, 9, 1, 8, 1, tzinfo=east_of_utc))
    assert _classify(after)[1] is HermesMessageReason.ELIGIBLE_PRIVATE


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"message_id": "   "}, "message_id is outside the allowed bounds"),
        ({"message_id": "m" * 301}, "message_id is outside the allowed bounds"),
        ({"profile_ref": ""}, "profile_ref is outside the allowed bounds"),
        ({"profile_ref": "p" * 201}, "profile_ref is outside the allowed bounds"),
        ({"message_id": "hermes\x00"}, "message_id contains non-storable text"),
        ({"profile_kind": "owner"}, "profile_kind is invalid"),
        ({"chat_kind": "channel"}, "chat_kind is invalid"),
        ({"sender_kind": "bot"}, "sender_kind is invalid"),
        ({"sent_at": datetime(2026, 9, 2, 0, 0)}, "sent_at must be timezone-aware"),
    ],
)
def test_the_envelope_refuses_what_it_cannot_bound(
    overrides: dict[str, object],
    expected: str,
) -> None:
    with pytest.raises(HermesMessageError, match=expected):
        _message(**overrides)


def test_an_empty_body_is_allowed_but_a_non_storable_one_is_not() -> None:
    assert _message(text="").text == ""
    with pytest.raises(HermesMessageError, match="text contains non-storable text"):
        _message(text="发票\x00")


def test_the_attachment_count_is_bounded() -> None:
    attachment = HermesMessageAttachment("receipt.pdf", "application/pdf", b"%PDF-1.7")
    filled = _message(attachments=(attachment,) * MAX_ATTACHMENTS)
    assert len(filled.attachments) == MAX_ATTACHMENTS
    with pytest.raises(HermesMessageError, match="too many attachments"):
        _message(attachments=(attachment,) * (MAX_ATTACHMENTS + 1))


@pytest.mark.parametrize("filename", [".", "..", "a/b.pdf", "a\\b.pdf"])
def test_an_attachment_filename_may_not_traverse(filename: str) -> None:
    with pytest.raises(HermesMessageError, match="attachment filename is invalid"):
        HermesMessageAttachment(filename, "application/pdf", b"%PDF-1.7")


def test_attachment_metadata_and_size_are_bounded() -> None:
    with pytest.raises(HermesMessageError, match=r"attachment\.filename is outside"):
        HermesMessageAttachment("f" * 256, "application/pdf", b"")
    with pytest.raises(HermesMessageError, match=r"attachment\.media_type is outside"):
        HermesMessageAttachment("receipt.pdf", "m" * 201, b"")
    with pytest.raises(HermesMessageError, match="attachment exceeds the file limit"):
        HermesMessageAttachment(
            "receipt.pdf", "application/pdf", b"\0" * (MAX_ATTACHMENT_BYTES + 1)
        )
    at_the_limit = HermesMessageAttachment(
        "receipt.pdf", "application/pdf", b"\0" * MAX_ATTACHMENT_BYTES
    )
    assert len(at_the_limit.content) == MAX_ATTACHMENT_BYTES


def test_a_non_text_field_is_refused_before_it_reaches_the_length_check() -> None:
    with pytest.raises(HermesMessageError, match="message_id must be text"):
        _message(message_id=cast(str, 1234))


def test_the_caller_supplied_activation_arguments_are_validated_too() -> None:
    with pytest.raises(HermesMessageError, match="primary_profile_ref is outside"):
        classify_private_message(_message(), primary_profile_ref="", activated_at=ACTIVATED_AT)
    with pytest.raises(HermesMessageError, match="activated_at must be timezone-aware"):
        classify_private_message(
            _message(),
            primary_profile_ref=PRIMARY_PROFILE,
            activated_at=datetime(2026, 9, 1, 0, 0),
        )


def test_the_envelope_is_frozen_so_a_decision_cannot_be_reused_after_edits() -> None:
    message = _message()
    with pytest.raises(AttributeError):
        message.text = "改写"  # type: ignore[misc]
    assert replace(message, text="改写").text == "改写"


def test_utc_now_is_the_timezone_aware_clock_seam() -> None:
    now = utc_now()
    assert now.tzinfo is UTC
    assert abs((now - datetime.now(UTC)).total_seconds()) < 5
