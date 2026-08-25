"""Opt-in, metadata-only persistence for the loopback synthetic gateway."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from ledgerbridge.candidate_contract import CandidateAggregate


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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_aggregate (
                    candidate_ref TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )

    def save(
        self, output: Mapping[str, object], *, aggregate: CandidateAggregate | None = None
    ) -> None:
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
                    if aggregate is not None:
                        connection.execute(
                            "INSERT OR IGNORE INTO candidate_aggregate "
                            "(candidate_ref, payload) VALUES (?, ?)",
                            (
                                candidate_ref,
                                json.dumps(aggregate.model_dump(mode="json"), sort_keys=True),
                            ),
                        )
                    return
                connection.execute(
                    "INSERT INTO candidate_projection "
                    "(candidate_ref, source_event_ref, payload, created_at) VALUES (?, ?, ?, ?)",
                    (candidate_ref, source_event_ref, payload, created_at),
                )
                if aggregate is not None:
                    connection.execute(
                        "INSERT INTO candidate_aggregate (candidate_ref, payload) VALUES (?, ?)",
                        (
                            candidate_ref,
                            json.dumps(aggregate.model_dump(mode="json"), sort_keys=True),
                        ),
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

    def get_aggregate(self, candidate_ref: str) -> CandidateAggregate | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM candidate_aggregate WHERE candidate_ref = ?",
                    (candidate_ref,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise SyntheticPersistenceError("staging persistence is unavailable") from exc
        if row is None:
            return None
        try:
            return CandidateAggregate.model_validate(json.loads(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SyntheticPersistenceError(
                "staging persistence contains invalid aggregate"
            ) from exc

    def update_aggregate(self, aggregate: CandidateAggregate) -> None:
        payload = json.dumps(aggregate.model_dump(mode="json"), sort_keys=True)
        try:
            with self._connect() as connection:
                result = connection.execute(
                    "UPDATE candidate_aggregate SET payload = ? WHERE candidate_ref = ?",
                    (payload, str(aggregate.projection.candidate_ref)),
                )
                if result.rowcount != 1:
                    raise SyntheticPersistenceError("staging candidate aggregate is missing")
        except sqlite3.Error as exc:
            raise SyntheticPersistenceError("staging persistence is unavailable") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection


def _required_key(output: Mapping[str, object], field: str) -> str:
    value = output.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SyntheticPersistenceError(f"staging output {field} is required")
    return value
