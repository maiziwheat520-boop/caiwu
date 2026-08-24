from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


SCHEMA_VERSION = 1
VALID_CANDIDATE_STATUSES = {
    "INCOMPLETE",
    "PENDING",
    "CONFLICTED",
    "CONFIRMED",
    "IGNORED",
    "SUPERSEDED",
}
VALID_DRAFT_STATUSES = {"QUEUED", "BUILDING", "NEEDS_REVIEW", "VERIFIED", "FAILED"}


class PersistenceError(RuntimeError):
    """Base error for persistence invariants."""


class SchemaVersionError(PersistenceError):
    pass


class SeedConflictError(PersistenceError):
    pass


class CandidateNotFoundError(PersistenceError):
    pass


class StaleRevisionError(PersistenceError):
    pass


class IdempotencyConflictError(PersistenceError):
    pass


class EventSequenceError(PersistenceError):
    pass


@dataclass(frozen=True)
class IdempotencyRecord:
    scope: str
    key: str
    fingerprint: str
    response_status: int
    response: dict[str, object]
    location: str | None = None


@dataclass(frozen=True)
class PersistenceResult:
    replayed: bool
    idempotency: IdempotencyRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_integer(value: object, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return value


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(raw: str) -> dict[str, object]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise PersistenceError("stored JSON is not an object")
    return value


def _validate_candidate(candidate: dict[str, object]) -> None:
    if not isinstance(candidate.get("id"), str) or not candidate["id"]:
        raise ValueError("candidate id is required")
    _require_integer(candidate.get("revision"), "candidate revision", minimum=1)
    _require_integer(candidate.get("amount_minor"), "candidate amount_minor")
    if candidate.get("status") not in VALID_CANDIDATE_STATUSES:
        raise ValueError("candidate status is invalid")


def _validate_event(event: dict[str, object]) -> None:
    for field in ("id", "candidate_id"):
        if not isinstance(event.get(field), str) or not event[field]:
            raise ValueError(f"review event {field} is required")
    sequence = _require_integer(event.get("sequence"), "event sequence", minimum=1)
    from_revision = _require_integer(event.get("from_revision"), "event from_revision", minimum=1)
    to_revision = _require_integer(event.get("to_revision"), "event to_revision", minimum=2)
    if to_revision != from_revision + 1:
        raise ValueError("review event revisions must be consecutive")
    if sequence < 1:
        raise ValueError("review event sequence is invalid")
    if not isinstance(event.get("changes"), list):
        raise ValueError("review event changes must be a list")


def _validate_draft(draft: dict[str, object]) -> None:
    if not isinstance(draft.get("id"), str) or not draft["id"]:
        raise ValueError("draft id is required")
    if not isinstance(draft.get("accounting_month"), str) or not draft["accounting_month"]:
        raise ValueError("draft accounting_month is required")
    _require_integer(draft.get("input_revision"), "draft input_revision", minimum=1)
    if draft.get("status") not in VALID_DRAFT_STATUSES:
        raise ValueError("draft status is invalid")


def _validate_idempotency(record: IdempotencyRecord) -> None:
    if not record.scope or not record.key or not record.fingerprint:
        raise ValueError("idempotency scope, key, and fingerprint are required")
    _require_integer(record.response_status, "idempotency response_status", minimum=100)
    if record.response_status > 599:
        raise ValueError("idempotency response_status must be a valid HTTP status")


class SQLitePersistence:
    """SQLite storage with connection-per-transaction thread safety."""

    def __init__(self, database_path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        raw_path = Path(database_path)
        if raw_path.is_symlink():
            raise ValueError("database_path must not be a symbolic link")
        self.path = raw_path.resolve()
        if self.path.exists() and self.path.is_dir():
            raise ValueError("database_path must be a file")
        _require_integer(busy_timeout_ms, "busy_timeout_ms", minimum=1)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version not in {0, SCHEMA_VERSION}:
                raise SchemaVersionError(
                    f"database schema version {current_version} is unsupported; expected {SCHEMA_VERSION}"
                )
            if current_version == 0:
                statements = (
                    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL) STRICT",
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT",
                    """
                    CREATE TABLE candidates (
                        candidate_id TEXT PRIMARY KEY,
                        revision INTEGER NOT NULL CHECK (revision >= 1),
                        status TEXT NOT NULL CHECK (status IN ('INCOMPLETE','PENDING','CONFLICTED','CONFIRMED','IGNORED','SUPERSEDED')),
                        amount_minor INTEGER NOT NULL,
                        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
                        updated_at TEXT NOT NULL,
                        CHECK (typeof(amount_minor) = 'integer')
                    ) STRICT
                    """,
                    """
                    CREATE TABLE review_events (
                        event_id TEXT PRIMARY KEY,
                        candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                        sequence INTEGER NOT NULL CHECK (sequence >= 1),
                        from_revision INTEGER NOT NULL CHECK (from_revision >= 1),
                        to_revision INTEGER NOT NULL CHECK (to_revision = from_revision + 1),
                        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
                        created_at TEXT NOT NULL,
                        UNIQUE (candidate_id, sequence)
                    ) STRICT
                    """,
                    """
                    CREATE TRIGGER review_events_no_update
                    BEFORE UPDATE ON review_events
                    BEGIN
                        SELECT RAISE(ABORT, 'review_events are append-only');
                    END
                    """,
                    """
                    CREATE TRIGGER review_events_no_delete
                    BEFORE DELETE ON review_events
                    BEGIN
                        SELECT RAISE(ABORT, 'review_events are append-only');
                    END
                    """,
                    """
                    CREATE TABLE workbook_drafts (
                        draft_id TEXT PRIMARY KEY,
                        accounting_month TEXT NOT NULL,
                        input_revision INTEGER NOT NULL CHECK (input_revision >= 1),
                        status TEXT NOT NULL CHECK (status IN ('QUEUED','BUILDING','NEEDS_REVIEW','VERIFIED','FAILED')),
                        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
                        updated_at TEXT NOT NULL
                    ) STRICT
                    """,
                    """
                    CREATE TABLE idempotency_responses (
                        scope TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        response_status INTEGER NOT NULL CHECK (response_status BETWEEN 100 AND 599),
                        response_json TEXT NOT NULL CHECK (json_valid(response_json)),
                        location TEXT,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (scope, idempotency_key)
                    ) STRICT
                    """,
                    "CREATE INDEX candidates_status_idx ON candidates(status)",
                    "CREATE INDEX review_events_candidate_idx ON review_events(candidate_id, sequence)",
                    "CREATE INDEX workbook_drafts_month_idx ON workbook_drafts(accounting_month)",
                )
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, _now()),
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            migration = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if migration is None or int(migration[0]) != SCHEMA_VERSION:
                raise SchemaVersionError("schema migration history is inconsistent")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @property
    def schema_version(self) -> int:
        with self.transaction() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def pragma_settings(self) -> dict[str, object]:
        with self.transaction() as connection:
            return {
                "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                "foreign_keys": int(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                "busy_timeout": int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
                "synchronous": int(connection.execute("PRAGMA synchronous").fetchone()[0]),
            }

    def seed_if_empty(
        self,
        candidates: Sequence[dict[str, object]],
        review_events: dict[str, Sequence[dict[str, object]]],
        *,
        seed_version: str,
    ) -> bool:
        if not seed_version:
            raise ValueError("seed_version is required")
        candidate_copies = [deepcopy(candidate) for candidate in candidates]
        event_copies = {
            candidate_id: [deepcopy(event) for event in events]
            for candidate_id, events in review_events.items()
        }
        candidate_by_id: dict[str, dict[str, object]] = {}
        for candidate in candidate_copies:
            _validate_candidate(candidate)
            candidate_id = str(candidate["id"])
            if candidate_id in candidate_by_id:
                raise ValueError("seed contains duplicate candidate ids")
            candidate_by_id[candidate_id] = candidate
        for candidate_id, events in event_copies.items():
            candidate = candidate_by_id.get(candidate_id)
            if candidate is None:
                raise ValueError("seed event references an unknown candidate")
            expected_sequence = 1
            previous_to: int | None = None
            for event in sorted(events, key=lambda item: int(item.get("sequence", 0))):
                _validate_event(event)
                if event["candidate_id"] != candidate_id or event["sequence"] != expected_sequence:
                    raise ValueError("seed review event sequence is inconsistent")
                if previous_to is not None and event["from_revision"] != previous_to:
                    raise ValueError("seed review event revision chain is inconsistent")
                previous_to = int(event["to_revision"])
                expected_sequence += 1
            if previous_to is not None and previous_to != candidate["revision"]:
                raise ValueError("seed candidate revision does not match its last event")

        with self.transaction(write=True) as connection:
            marker = connection.execute(
                "SELECT value FROM metadata WHERE key = 'seed_version'"
            ).fetchone()
            if marker is not None:
                if marker[0] != seed_version:
                    raise SeedConflictError(
                        f"database was seeded with {marker[0]!r}, not {seed_version!r}"
                    )
                return False
            existing = int(connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])
            if existing:
                raise SeedConflictError("candidate rows exist without a seed marker")
            now = _now()
            for candidate in candidate_copies:
                connection.execute(
                    """
                    INSERT INTO candidates(candidate_id, revision, status, amount_minor, payload_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate["id"],
                        candidate["revision"],
                        candidate["status"],
                        candidate["amount_minor"],
                        _canonical_json(candidate),
                        now,
                    ),
                )
            for candidate_id, events in event_copies.items():
                for event in sorted(events, key=lambda item: int(item["sequence"])):
                    self._insert_event(connection, event)
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('seed_version', ?)",
                (seed_version,),
            )
        return True

    def list_candidates(self) -> list[dict[str, object]]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM candidates ORDER BY candidate_id"
            ).fetchall()
        return [_json_object(row[0]) for row in rows]

    def get_candidate(self, candidate_id: str) -> dict[str, object] | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return _json_object(row[0]) if row is not None else None

    def get_review_events(self, candidate_id: str) -> list[dict[str, object]]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM review_events WHERE candidate_id = ? ORDER BY sequence",
                (candidate_id,),
            ).fetchall()
        return [_json_object(row[0]) for row in rows]

    def _insert_event(self, connection: sqlite3.Connection, event: dict[str, object]) -> None:
        connection.execute(
            """
            INSERT INTO review_events(
                event_id, candidate_id, sequence, from_revision, to_revision,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["id"],
                event["candidate_id"],
                event["sequence"],
                event["from_revision"],
                event["to_revision"],
                _canonical_json(event),
                event.get("created_at", _now()),
            ),
        )

    def _read_idempotency(
        self, connection: sqlite3.Connection, scope: str, key: str
    ) -> IdempotencyRecord | None:
        row = connection.execute(
            """
            SELECT scope, idempotency_key, fingerprint, response_status,
                   response_json, location
            FROM idempotency_responses
            WHERE scope = ? AND idempotency_key = ?
            """,
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        return IdempotencyRecord(
            scope=row["scope"],
            key=row["idempotency_key"],
            fingerprint=row["fingerprint"],
            response_status=row["response_status"],
            response=_json_object(row["response_json"]),
            location=row["location"],
        )

    def get_idempotency(self, scope: str, key: str) -> IdempotencyRecord | None:
        with self.transaction() as connection:
            return self._read_idempotency(connection, scope, key)

    def _insert_idempotency(
        self, connection: sqlite3.Connection, record: IdempotencyRecord
    ) -> None:
        connection.execute(
            """
            INSERT INTO idempotency_responses(
                scope, idempotency_key, fingerprint, response_status,
                response_json, location, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.scope,
                record.key,
                record.fingerprint,
                record.response_status,
                _canonical_json(record.response),
                record.location,
                _now(),
            ),
        )

    def remember_idempotency(self, record: IdempotencyRecord) -> PersistenceResult:
        _validate_idempotency(record)
        with self.transaction(write=True) as connection:
            existing = self._read_idempotency(connection, record.scope, record.key)
            if existing is not None:
                if existing.fingerprint != record.fingerprint:
                    raise IdempotencyConflictError("idempotency key fingerprint mismatch")
                return PersistenceResult(replayed=True, idempotency=existing)
            self._insert_idempotency(connection, record)
        return PersistenceResult(replayed=False, idempotency=record)

    def commit_candidate_transition(
        self,
        updated_candidate: dict[str, object],
        event: dict[str, object],
        idempotency: IdempotencyRecord,
    ) -> PersistenceResult:
        candidate = deepcopy(updated_candidate)
        audit_event = deepcopy(event)
        _validate_candidate(candidate)
        _validate_event(audit_event)
        _validate_idempotency(idempotency)
        if candidate["id"] != audit_event["candidate_id"]:
            raise ValueError("candidate and event ids do not match")
        if candidate["revision"] != audit_event["to_revision"]:
            raise ValueError("candidate revision must equal event to_revision")
        with self.transaction(write=True) as connection:
            existing_idempotency = self._read_idempotency(
                connection, idempotency.scope, idempotency.key
            )
            if existing_idempotency is not None:
                if existing_idempotency.fingerprint != idempotency.fingerprint:
                    raise IdempotencyConflictError("idempotency key fingerprint mismatch")
                return PersistenceResult(replayed=True, idempotency=existing_idempotency)
            current = connection.execute(
                "SELECT revision FROM candidates WHERE candidate_id = ?",
                (candidate["id"],),
            ).fetchone()
            if current is None:
                raise CandidateNotFoundError(str(candidate["id"]))
            if current["revision"] != audit_event["from_revision"]:
                raise StaleRevisionError(
                    f"expected revision {audit_event['from_revision']}, found {current['revision']}"
                )
            last_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM review_events WHERE candidate_id = ?",
                (candidate["id"],),
            ).fetchone()[0]
            if audit_event["sequence"] != last_sequence + 1:
                raise EventSequenceError(
                    f"expected event sequence {last_sequence + 1}, got {audit_event['sequence']}"
                )
            result = connection.execute(
                """
                UPDATE candidates
                SET revision = ?, status = ?, amount_minor = ?, payload_json = ?, updated_at = ?
                WHERE candidate_id = ? AND revision = ?
                """,
                (
                    candidate["revision"],
                    candidate["status"],
                    candidate["amount_minor"],
                    _canonical_json(candidate),
                    _now(),
                    candidate["id"],
                    audit_event["from_revision"],
                ),
            )
            if result.rowcount != 1:
                raise StaleRevisionError("candidate revision changed during transition")
            self._insert_event(connection, audit_event)
            self._insert_idempotency(connection, idempotency)
        return PersistenceResult(replayed=False, idempotency=idempotency)

    def save_draft(
        self, draft: dict[str, object], idempotency: IdempotencyRecord
    ) -> PersistenceResult:
        draft_copy = deepcopy(draft)
        _validate_draft(draft_copy)
        _validate_idempotency(idempotency)
        with self.transaction(write=True) as connection:
            existing = self._read_idempotency(
                connection, idempotency.scope, idempotency.key
            )
            if existing is not None:
                if existing.fingerprint != idempotency.fingerprint:
                    raise IdempotencyConflictError("idempotency key fingerprint mismatch")
                return PersistenceResult(replayed=True, idempotency=existing)
            connection.execute(
                """
                INSERT INTO workbook_drafts(
                    draft_id, accounting_month, input_revision, status,
                    payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_copy["id"],
                    draft_copy["accounting_month"],
                    draft_copy["input_revision"],
                    draft_copy["status"],
                    _canonical_json(draft_copy),
                    _now(),
                ),
            )
            self._insert_idempotency(connection, idempotency)
        return PersistenceResult(replayed=False, idempotency=idempotency)

    def get_draft(self, draft_id: str) -> dict[str, object] | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM workbook_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        return _json_object(row[0]) if row is not None else None
