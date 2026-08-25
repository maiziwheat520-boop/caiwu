"""Explicitly enabled, non-production Outlook.com IMAP -> local staging replay."""

from __future__ import annotations

import argparse
import imaplib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from ledgerbridge.mail_credentials import CredentialFileSecretProvider
from ledgerbridge.mail_imap import ImapMailProvider, ImapTransport, parse_imap_message
from ledgerbridge.staging_replay import replay_message, require_loopback_gateway

DEFAULT_GATEWAY = "http://127.0.0.1:8653/v1/intake"
DEFAULT_CREDENTIAL_FILE = Path("G:/我的云端硬盘/凭据/home-infra-credentials.md")


class ImapSslTransport(ImapTransport):
    def __init__(self, *, host: str = "outlook.office365.com", port: int = 993) -> None:
        self._host = host
        self._port = port
        self._client: imaplib.IMAP4_SSL | None = None

    def authenticate(self, mailbox: str, secret: str, *, mode: str) -> None:
        self._client = imaplib.IMAP4_SSL(self._host, self._port, timeout=30)
        if mode == "xoauth2":
            auth = f"user={mailbox}\x01auth=Bearer {secret}\x01\x01".encode()
            self._client.authenticate("XOAUTH2", lambda _: auth)
        elif mode == "password":
            self._client.login(mailbox, secret)
        else:
            raise ValueError("unsupported IMAP auth mode")

    def select_inbox(self) -> None:
        if self._client is None:
            raise RuntimeError("IMAP client is not authenticated")
        status, _ = self._client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("IMAP inbox selection failed")

    def search_all(self) -> tuple[str, ...]:
        if self._client is None:
            raise RuntimeError("IMAP client is not authenticated")
        status, data = self._client.uid("SEARCH", "", "ALL")
        if status != "OK" or not data or not isinstance(data[0], bytes):
            raise RuntimeError("IMAP search failed")
        return tuple(value.decode("ascii") for value in data[0].split() if value.isdigit())

    def fetch_rfc822(self, message_id: str) -> bytes:
        if self._client is None:
            raise RuntimeError("IMAP client is not authenticated")
        status, data = self._client.uid("FETCH", message_id, "(BODY.PEEK[])")
        if status != "OK":
            raise RuntimeError("IMAP fetch failed")
        chunks = [item[1] for item in data if isinstance(item, tuple) and len(item) > 1]
        if not chunks or not all(isinstance(chunk, bytes) for chunk in chunks):
            raise RuntimeError("IMAP message payload is invalid")
        return b"".join(chunks)

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._client.close()
        finally:
            try:
                self._client.logout()
            finally:
                self._client = None


def run_self_check() -> dict[str, Any]:
    raw = (
        b"Message-ID: <synthetic-imap@example.test>\r\n"
        b"Subject: synthetic invoice\r\n"
        b"Date: Tue, 25 Aug 2026 02:00:00 +0000\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"invoice body\r\n"
    )
    message = parse_imap_message(raw, fallback_id="1")
    if message.subject != "synthetic invoice" or message.body_preview != "invoice body":
        raise RuntimeError("IMAP parser self-check failed")
    return {"mode": "synthetic", "messages_checked": 1, "network": False}


def run_staging() -> dict[str, Any]:
    if os.environ.get("LEDGERBRIDGE_STAGING_NETWORK") != "1":
        raise RuntimeError("set LEDGERBRIDGE_STAGING_NETWORK=1 to enable IMAP staging")
    mailbox = os.environ.get("LEDGERBRIDGE_STAGING_MAILBOX")
    entity_raw = os.environ.get("LEDGERBRIDGE_STAGING_ENTITY_REF")
    auth_mode = os.environ.get("LEDGERBRIDGE_STAGING_IMAP_AUTH", "xoauth2")
    credential_file = Path(
        os.environ.get("LEDGERBRIDGE_STAGING_CREDENTIAL_FILE", str(DEFAULT_CREDENTIAL_FILE))
    )
    credential_key = (
        "LEDGERBRIDGE_STAGING_IMAP_ACCESS_TOKEN"
        if auth_mode == "xoauth2"
        else "LEDGERBRIDGE_STAGING_IMAP_APP_PASSWORD"
    )
    if not mailbox or not entity_raw:
        raise RuntimeError("set _MAILBOX and _ENTITY_REF for IMAP staging")
    try:
        entity_ref = UUID(entity_raw)
    except ValueError as exc:
        raise RuntimeError("LEDGERBRIDGE_STAGING_ENTITY_REF must be a UUID") from exc
    provider = ImapMailProvider(
        ImapSslTransport(),
        CredentialFileSecretProvider(credential_file, key=credential_key),
        mailbox=mailbox,
        auth_mode=auth_mode,
        max_messages=5,
    )
    gateway_url = require_loopback_gateway(
        os.environ.get("LEDGERBRIDGE_STAGING_GATEWAY_URL", DEFAULT_GATEWAY)
    )
    results = [
        replay_message(message, entity_ref=entity_ref, gateway_url=gateway_url)
        for message in provider.iter_messages()
    ]
    return {"mode": "imap-staging", "messages_replayed": len(results), "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="run a no-network parser self-check")
    args = parser.parse_args()
    print(json.dumps(run_self_check() if args.check else run_staging(), sort_keys=True))
