"""Replay a Thunderbird-synchronized mbox into the local staging gateway."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from ledgerbridge.mail_mbox import MboxMailProvider
from ledgerbridge.staging_replay import replay_message, require_loopback_gateway

DEFAULT_GATEWAY = "http://127.0.0.1:8653/v1/intake"


def run_self_check() -> dict[str, Any]:
    return {"mode": "synthetic", "messages_checked": 0, "network": False}


def run_staging(path: Path) -> dict[str, Any]:
    if os.environ.get("LEDGERBRIDGE_STAGING_NETWORK") != "1":
        raise RuntimeError("set LEDGERBRIDGE_STAGING_NETWORK=1 to enable mbox staging")
    mailbox_name = os.environ.get("LEDGERBRIDGE_STAGING_MAILBOX", "local-mbox")
    entity_raw = os.environ.get("LEDGERBRIDGE_STAGING_ENTITY_REF")
    if not entity_raw:
        raise RuntimeError("set LEDGERBRIDGE_STAGING_ENTITY_REF for mbox staging")
    try:
        entity_ref = UUID(entity_raw)
    except ValueError as exc:
        raise RuntimeError("LEDGERBRIDGE_STAGING_ENTITY_REF must be a UUID") from exc
    gateway_url = require_loopback_gateway(
        os.environ.get("LEDGERBRIDGE_STAGING_GATEWAY_URL", DEFAULT_GATEWAY)
    )
    provider = MboxMailProvider(path, max_messages=5)
    results = [
        replay_message(message, entity_ref=entity_ref, gateway_url=gateway_url)
        for message in provider.iter_messages()
    ]
    return {
        "mode": "mbox-staging",
        "mailbox": mailbox_name,
        "messages_replayed": len(results),
        "results": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="run a no-network self-check")
    parser.add_argument("--mbox", type=Path, help="Thunderbird mbox file")
    args = parser.parse_args()
    if args.check:
        result = run_self_check()
    elif args.mbox is None:
        raise SystemExit("--mbox is required unless --check is used")
    else:
        result = run_staging(args.mbox)
    print(json.dumps(result, sort_keys=True))
