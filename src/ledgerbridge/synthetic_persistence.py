"""Opt-in, metadata-only persistence for the loopback synthetic gateway."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import cast


class SyntheticPersistenceError(RuntimeError):
    """The local staging projection cannot be persisted safely."""


class SyntheticOutputStore:
    """Small SQLite store for candidate projections, never raw evidence bytes."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute() or path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            raise ValueError("synthetic persistence path must be an absolute SQLite file")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_projection (
                    candidate_ref TEXT PRIMARY KEY,
                    source_event_ref TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def save(self, output: Mapping[str, object]) -> None:
        candidate_ref = _required_key(output, "candidate_ref")
        source_event_ref = _required_key(output, "source_event_ref")
        payload = json.dumps(
            dict(output), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        created_at = str(output.get("source_received_at", ""))
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT candidate_ref, payload FROM candidate_projection "
                    "WHERE candidate_ref = ? OR source_event_ref = ?",
                    (candidate_ref, source_event_ref),
                ).fetchone()
                if existing is not None:
                    if existing[1] != payload:
                        raise SyntheticPersistenceError(
                            "staging source identity already has different output"
                        )
                    return
                connection.execute(
                    "INSERT INTO candidate_projection "
                    "(candidate_ref, source_event_ref, payload, created_at) VALUES (?, ?, ?, ?)",
                    (candidate_ref, source_event_ref, payload, created_at),
                )
        except sqlite3.Error as exc:
            raise SyntheticPersistenceError("staging persistence is unavailable") from exc

    def list(self) -> list[dict[str, object]]:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT payload FROM candidate_projection ORDER BY created_at, candidate_ref"
                ).fetchall()
        except sqlite3.Error as exc:
            raise SyntheticPersistenceError("staging persistence is unavailable") from exc
        values: list[dict[str, object]] = []
        for (payload,) in rows:
            parsed = json.loads(payload)
            if not isinstance(parsed, dict):
                raise SyntheticPersistenceError("staging persistence contains invalid output")
            values.append(cast(dict[str, object], parsed))
        return values

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection


def _required_key(output: Mapping[str, object], field: str) -> str:
    value = output.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SyntheticPersistenceError(f"staging output {field} is required")
    return value
