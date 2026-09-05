"""Reuse a derived read until the audit horizon moves.

Several workbench reads are pure functions of two things: which candidates a
principal may see, and the as-of audit horizon. That makes reuse exact rather
than a guess -- if the horizon has not advanced, the answer cannot have changed,
and any write anywhere in the system advances it.

The caller must read the horizon before building, and read it again afterwards:
a build that straddled a write is returned but never stored. Caches live at
module scope because the services that use them are constructed per request, so
instance state would never survive to be reused.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ledgerbridge.internal_read_contract import WorkloadPrincipal
from ledgerbridge.internal_read_cursor import grant_digest

DEFAULT_MAX_PRINCIPALS = 16


def horizon_cache_key(principal: WorkloadPrincipal) -> str:
    """Identify the principal by everything that changes what it may read.

    Grants decide which candidates are visible; the generation and principal
    ref keep two different identities from ever sharing an entry.
    """
    return "|".join(
        (
            principal.principal_ref,
            str(principal.policy_generation),
            grant_digest(principal),
        )
    )


@dataclass(frozen=True, slots=True)
class _Entry[T]:
    horizon_sequence: int
    horizon_hash: bytes
    value: T


class HorizonCache[T]:
    """One cached value per principal, valid only at the horizon it was built at."""

    def __init__(self, *, max_principals: int = DEFAULT_MAX_PRINCIPALS) -> None:
        self._entries: dict[str, _Entry[T]] = {}
        self._lock = threading.Lock()
        self._max_principals = max_principals

    def get(self, key: str, horizon_sequence: int, horizon_hash: bytes) -> T | None:
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.horizon_sequence != horizon_sequence or entry.horizon_hash != horizon_hash:
            return None
        return entry.value

    def store(self, key: str, horizon_sequence: int, horizon_hash: bytes, value: T) -> None:
        with self._lock:
            if key not in self._entries and len(self._entries) >= self._max_principals:
                self._entries.clear()
            self._entries[key] = _Entry(
                horizon_sequence=horizon_sequence,
                horizon_hash=horizon_hash,
                value=value,
            )

    def clear(self) -> None:
        """Drop every cached value. Used by tests; never on a read path."""
        with self._lock:
            self._entries.clear()
