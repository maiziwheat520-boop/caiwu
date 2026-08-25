"""Explicitly enabled, non-production Microsoft Graph -> local staging replay.

The script reads a short-lived access token from the process environment only,
fetches at most five inbox messages from Graph, and posts bounded JSON to the
loopback synthetic gateway. It never writes the token or source bytes to disk.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

from ledgerbridge.mail_collector import (
    GRAPH_HOST,
    AccessTokenProvider,
    GraphTransport,
    MailMessage,
    MicrosoftGraphMailProvider,
)
from ledgerbridge.mail_credentials import (
    CredentialFileTokenProvider,
    WindowsCredentialTokenProvider,
)

MAX_STAGING_EVIDENCE_BYTES = 1_048_576
DEFAULT_GATEWAY = "http://127.0.0.1:8653/v1/intake"


class EnvironmentTokenProvider:
    def __init__(self, token: str) -> None:
        self._token = token

    def get_access_token(self) -> str:
        return self._token


class UrllibGraphTransport(GraphTransport):
    def get_json(self, path: str, *, authorization: str) -> Mapping[str, object]:
        request = Request(
            f"https://{GRAPH_HOST}{path}",
            headers={"Authorization": authorization, "Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise RuntimeError(f"Graph returned HTTP {response.status}")
                payload = json.loads(response.read(MAX_STAGING_EVIDENCE_BYTES + 1))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise RuntimeError("Graph staging request failed") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Graph staging response is not an object")
        return payload


def replay_message(message: MailMessage, *, entity_ref: UUID, gateway_url: str) -> dict[str, Any]:
    received_at = _parse_received_at(message.received_at)
    evidence = []
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
    if not isinstance(result, dict):
        raise RuntimeError("local staging gateway response is invalid")
    return {
        "source_message_id": result.get("source_message_id"),
        "source_subject": result.get("source_subject"),
        "triage_action": result.get("triage_action"),
        "candidate_ref": result.get("candidate_ref"),
        "evidence_count": len(result.get("evidence", [])),
        "writes_posting": result.get("writes_posting"),
    }


def run_staging() -> dict[str, Any]:
    if os.environ.get("LEDGERBRIDGE_STAGING_NETWORK") != "1":
        raise RuntimeError(
            "set LEDGERBRIDGE_STAGING_NETWORK=1 to enable Graph staging network access"
        )
    token = os.environ.get("LEDGERBRIDGE_STAGING_ACCESS_TOKEN")
    credential_target = os.environ.get("LEDGERBRIDGE_STAGING_CREDENTIAL_TARGET")
    credential_file = os.environ.get("LEDGERBRIDGE_STAGING_CREDENTIAL_FILE")
    mailbox = os.environ.get("LEDGERBRIDGE_STAGING_MAILBOX")
    entity_raw = os.environ.get("LEDGERBRIDGE_STAGING_ENTITY_REF")
    if (
        sum(bool(value) for value in (token, credential_target, credential_file)) != 1
        or not mailbox
        or not entity_raw
    ):
        raise RuntimeError(
            "set exactly one credential source (_ACCESS_TOKEN, _CREDENTIAL_TARGET, "
            "or _CREDENTIAL_FILE), plus _MAILBOX and _ENTITY_REF"
        )
    try:
        entity_ref = UUID(entity_raw)
    except ValueError as exc:
        raise RuntimeError("LEDGERBRIDGE_STAGING_ENTITY_REF must be a UUID") from exc
    gateway_url = _require_loopback_gateway(
        os.environ.get("LEDGERBRIDGE_STAGING_GATEWAY_URL", DEFAULT_GATEWAY)
    )
    token_provider: AccessTokenProvider
    if token is not None:
        token_provider = EnvironmentTokenProvider(token)
    elif credential_target is not None:
        token_provider = WindowsCredentialTokenProvider(credential_target)
    else:
        token_provider = CredentialFileTokenProvider(Path(credential_file or ""))
    provider = MicrosoftGraphMailProvider(
        UrllibGraphTransport(),
        token_provider,
        mailbox=mailbox,
        page_size=5,
        max_pages=1,
    )
    results = [
        replay_message(message, entity_ref=entity_ref, gateway_url=gateway_url)
        for message in provider.iter_messages()
    ]
    return {"mode": "staging", "messages_replayed": len(results), "results": results}


def _parse_received_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _require_loopback_gateway(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("staging gateway must be an http loopback URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("staging gateway URL must not contain credentials or query data")
    return value


def run_self_check() -> dict[str, Any]:
    class FakeTransport:
        def get_json(self, path: str, *, authorization: str) -> Mapping[str, object]:
            assert authorization == "Bearer staging-token"
            if "/messages?" in path and "%24select=" in path:
                return {
                    "value": [
                        {
                            "id": "message-1",
                            "subject": "synthetic invoice",
                            "receivedDateTime": "2026-08-25T02:00:00Z",
                            "bodyPreview": "please process invoice",
                            "hasAttachments": False,
                        }
                    ]
                }
            raise AssertionError(path)

    provider = MicrosoftGraphMailProvider(
        FakeTransport(),
        EnvironmentTokenProvider("staging-token"),
        mailbox="staging@example.test",
        page_size=5,
        max_pages=1,
    )
    messages = tuple(provider.iter_messages())
    assert len(messages) == 1 and messages[0].body_preview == "please process invoice"
    return {"mode": "synthetic", "messages_checked": len(messages), "network": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="run a no-network provider self-check")
    args = parser.parse_args()
    print(json.dumps(run_self_check() if args.check else run_staging(), sort_keys=True))
