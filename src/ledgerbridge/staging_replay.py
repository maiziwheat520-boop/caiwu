"""Shared bounded projection from mail providers into the loopback demo gateway."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

from ledgerbridge.mail_collector import MailMessage

MAX_STAGING_EVIDENCE_BYTES = 1_048_576


def replay_message(message: MailMessage, *, entity_ref: UUID, gateway_url: str) -> dict[str, Any]:
    received_at = _parse_received_at(message.received_at)
    evidence: list[dict[str, object]] = []
    for attachment in message.attachments:
        if len(attachment.content) > MAX_STAGING_EVIDENCE_BYTES:
            raise RuntimeError("staging gateway accepts at most 1 MiB per evidence item")
        evidence.append(
            {
                "evidence_ref": str(
                    uuid5(
                        NAMESPACE_URL,
                        f"ledgerbridge:graph:evidence:{message.message_id}:{attachment.attachment_id}",
                    )
                ),
                "media_type": attachment.media_type,
                "content_base64": base64.b64encode(attachment.content).decode("ascii"),
                "business_unit_ref": None,
            }
        )
    if not evidence:
        body = message.body_preview.encode("utf-8") or message.subject.encode("utf-8")
        evidence.append(
            {
                "evidence_ref": str(
                    uuid5(NAMESPACE_URL, f"ledgerbridge:graph:evidence:{message.message_id}:body")
                ),
                "media_type": "text/plain",
                "content_base64": base64.b64encode(body).decode("ascii"),
                "business_unit_ref": None,
            }
        )
    payload = {
        "message_id": message.message_id,
        "source_event_ref": str(
            uuid5(NAMESPACE_URL, f"ledgerbridge:graph:event:{message.message_id}")
        ),
        "entity_ref": str(entity_ref),
        "sent_at": received_at.isoformat(),
        "activation_at": received_at.isoformat(),
        "text": f"{message.subject}\n{message.body_preview}".strip(),
        "evidence": evidence,
    }
    request = Request(
        gateway_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read(MAX_STAGING_EVIDENCE_BYTES))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError("local staging gateway request failed") from exc
    if not isinstance(result, Mapping):
        raise RuntimeError("local staging gateway response is invalid")
    return {
        "source_message_id": result.get("source_message_id"),
        "source_subject": result.get("source_subject"),
        "triage_action": result.get("triage_action"),
        "candidate_ref": result.get("candidate_ref"),
        "evidence_count": len(result.get("evidence", [])),
        "writes_posting": result.get("writes_posting"),
    }


def _parse_received_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def require_loopback_gateway(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("staging gateway must be an http loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("staging gateway URL must not contain credentials or query data")
    return value
