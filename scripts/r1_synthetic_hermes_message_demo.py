"""Show the fail-closed Hermes private-message intake boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from ledgerbridge.hermes_message import (
    HermesMessageAttachment,
    HermesMessageDisposition,
    HermesPrivateMessage,
    classify_private_message,
)

PRIMARY_PROFILE = "profile:primary"
ACTIVATED_AT = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)


def run_self_check() -> dict[str, object]:
    messages = (
        HermesPrivateMessage(
            "msg-eligible",
            PRIMARY_PROFILE,
            "primary",
            "private",
            "user",
            ACTIVATED_AT + timedelta(minutes=1),
            "报销单据在附件中",
            (HermesMessageAttachment("receipt.jpg", "image/jpeg", b"synthetic"),),
        ),
        HermesPrivateMessage(
            "msg-group",
            PRIMARY_PROFILE,
            "primary",
            "group",
            "user",
            ACTIVATED_AT + timedelta(minutes=2),
            "群聊消息",
        ),
        HermesPrivateMessage(
            "msg-history",
            PRIMARY_PROFILE,
            "primary",
            "private",
            "user",
            ACTIVATED_AT - timedelta(days=1),
            "历史消息",
        ),
        HermesPrivateMessage(
            "msg-assistant",
            PRIMARY_PROFILE,
            "primary",
            "private",
            "assistant",
            ACTIVATED_AT + timedelta(minutes=3),
            "工具输出",
        ),
    )
    decisions = [
        classify_private_message(
            message,
            primary_profile_ref=PRIMARY_PROFILE,
            activated_at=ACTIVATED_AT,
        )
        for message in messages
    ]
    return {
        "mode": "synthetic",
        "messages_checked": len(messages),
        "dispositions": [decision.disposition for decision in decisions],
        "relevant_messages": sum(
            decision.disposition is HermesMessageDisposition.RETAIN_FOR_TRIAGE
            for decision in decisions
        ),
    }


if __name__ == "__main__":
    print(json.dumps(run_self_check(), ensure_ascii=False, sort_keys=True))
