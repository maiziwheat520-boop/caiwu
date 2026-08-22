"""Application-side access to the append-only database audit chain."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def append_audit_event(
    session: Session,
    *,
    actor: str,
    action: str,
    reason: str,
    payload: Mapping[str, object] | None = None,
    rule_version: str | None = None,
) -> UUID:
    """Append through the database-owned hash-chain function in the current transaction."""

    value = session.execute(
        text(
            """
            SELECT append_audit_event(
                :actor,
                :action,
                :reason,
                :rule_version,
                CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "actor": actor,
            "action": action,
            "reason": reason,
            "rule_version": rule_version,
            "payload": json.dumps(payload or {}, sort_keys=True, separators=(",", ":")),
        },
    ).scalar_one()
    return cast(UUID, value)
