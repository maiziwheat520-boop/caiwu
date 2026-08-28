from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

from server.persistence import (
    SCHEMA_VERSION,
    IdempotencyConflictError,
    IdempotencyRecord,
    SQLitePersistence,
    SchemaVersionError,
)
from server.synthetic_data import initial_candidates, initial_review_events


class SQLitePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name, "ledgerbridge-preview.sqlite3")
        self.store = SQLitePersistence(self.database_path, busy_timeout_ms=4_000)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def seed(self) -> None:
        seeded = self.store.seed_if_empty(
            initial_candidates(),
            initial_review_events(),
            seed_version="synthetic-v1",
        )
        self.assertTrue(seeded)

    def transition_for(
        self,
        candidate_id: str,
        *,
        event_id: str | None = None,
        key: str | None = None,
        fingerprint: str = "f" * 64,
    ) -> tuple[dict[str, object], dict[str, object], IdempotencyRecord]:
        current = self.store.get_candidate(candidate_id)
        assert current is not None
        events = self.store.get_review_events(candidate_id)
        updated = deepcopy(current)
        updated["revision"] = int(current["revision"]) + 1
        updated["status"] = "CONFIRMED"
        event = {
            "id": event_id or str(uuid.uuid4()),
            "candidate_id": candidate_id,
            "sequence": len(events) + 1,
            "from_revision": current["revision"],
            "to_revision": updated["revision"],
            "decision": "CONFIRM",
            "actor": "persistence-test",
            "reason": "synthetic transition",
            "changes": [
                {
                    "field": "status",
                    "previous_value": current["status"],
                    "new_value": "CONFIRMED",
                }
            ],
            "conflict_resolution": None,
            "created_at": "2026-08-24T03:00:00+00:00",
        }
        response = {"candidate": deepcopy(updated), "event": deepcopy(event)}
        idempotency = IdempotencyRecord(
            scope="candidate-decision",
            key=key or str(uuid.uuid4()),
            fingerprint=fingerprint,
            response_status=200,
            response=response,
        )
        return updated, event, idempotency

    def test_schema_pragmas_and_explicit_version(self) -> None:
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)
        settings = self.store.pragma_settings()
        self.assertEqual(settings["journal_mode"], "wal")
        self.assertEqual(settings["foreign_keys"], 1)
        self.assertEqual(settings["busy_timeout"], 4_000)
        with self.store.transaction() as connection:
            migration = connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchone()[0]
        self.assertEqual(migration, SCHEMA_VERSION)

    def test_unsupported_schema_version_fails_closed(self) -> None:
        other_path = Path(self.temp_dir.name, "future.sqlite3")
        other_store = SQLitePersistence(other_path)
        with other_store.transaction(write=True) as connection:
            connection.execute("PRAGMA user_version = 99")
        with self.assertRaises(SchemaVersionError):
            SQLitePersistence(other_path)

    def test_seed_is_atomic_idempotent_and_survives_reopen(self) -> None:
        invalid = initial_candidates()
        invalid[1]["amount_minor"] = "2148.00"
        with self.assertRaises(ValueError):
            self.store.seed_if_empty(
                invalid,
                initial_review_events(),
                seed_version="synthetic-v1",
            )
        self.assertEqual(self.store.list_candidates(), [])
        self.seed()
        self.assertFalse(
            self.store.seed_if_empty(
                initial_candidates(),
                initial_review_events(),
                seed_version="synthetic-v1",
            )
        )
        reopened = SQLitePersistence(self.database_path)
        self.assertEqual(len(reopened.list_candidates()), 5)
        confirmed_id = "f16cef2e-321f-431d-b73c-e865ae2249e3"
        self.assertEqual(len(reopened.get_review_events(confirmed_id)), 1)

    def test_seed_rolls_back_all_rows_on_mid_transaction_failure(self) -> None:
        atomic_path = Path(self.temp_dir.name, "atomic-seed.sqlite3")
        atomic_store = SQLitePersistence(atomic_path)
        candidates = initial_candidates()
        events = initial_review_events()
        duplicate = deepcopy(next(iter(events.values()))[0])
        first_id = str(candidates[0]["id"])
        candidates[0]["revision"] = 2
        candidates[0]["status"] = "CONFIRMED"
        duplicate["candidate_id"] = first_id
        events[first_id] = [duplicate]
        with self.assertRaises(sqlite3.IntegrityError):
            atomic_store.seed_if_empty(
                candidates,
                events,
                seed_version="broken-synthetic-v1",
            )
        self.assertEqual(atomic_store.list_candidates(), [])
        with atomic_store.transaction() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0],
                0,
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT value FROM metadata WHERE key = 'seed_version'"
                ).fetchone()
            )

    def test_candidate_transition_event_and_idempotency_survive_reopen(self) -> None:
        self.seed()
        candidate_id = "2d0d0cb9-d4ab-4e3f-9879-7812882b8f21"
        updated, event, idempotency = self.transition_for(candidate_id)
        result = self.store.commit_candidate_transition(updated, event, idempotency)
        self.assertFalse(result.replayed)
        reopened = SQLitePersistence(self.database_path)
        self.assertEqual(reopened.get_candidate(candidate_id), updated)
        self.assertEqual(reopened.get_review_events(candidate_id), [event])
        self.assertEqual(reopened.get_idempotency(idempotency.scope, idempotency.key), idempotency)
        replay = reopened.commit_candidate_transition(updated, event, idempotency)
        self.assertTrue(replay.replayed)
        mismatched = IdempotencyRecord(
            scope=idempotency.scope,
            key=idempotency.key,
            fingerprint="0" * 64,
            response_status=200,
            response=idempotency.response,
        )
        with self.assertRaises(IdempotencyConflictError):
            reopened.commit_candidate_transition(updated, event, mismatched)
        self.assertEqual(len(reopened.get_review_events(candidate_id)), 1)

    def test_review_events_are_append_only_even_through_raw_transaction(self) -> None:
        self.seed()
        candidate_id = "f16cef2e-321f-431d-b73c-e865ae2249e3"
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            with self.store.transaction(write=True) as connection:
                connection.execute(
                    "UPDATE review_events SET sequence = 2 WHERE candidate_id = ?",
                    (candidate_id,),
                )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            with self.store.transaction(write=True) as connection:
                connection.execute(
                    "DELETE FROM review_events WHERE candidate_id = ?",
                    (candidate_id,),
                )
        self.assertEqual(len(self.store.get_review_events(candidate_id)), 1)

    def test_review_events_can_be_listed_newest_first(self) -> None:
        self.seed()
        events = self.store.list_review_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["decision"], "CONFIRM")

    def test_failed_event_insert_rolls_back_projection_and_idempotency(self) -> None:
        self.seed()
        candidate_id = "cf8efc6d-5955-4f48-b52c-6bfa2e547a64"
        original = self.store.get_candidate(candidate_id)
        duplicate_event_id = "428cf469-f596-4716-af00-b910552a3021"
        updated, event, idempotency = self.transition_for(
            candidate_id,
            event_id=duplicate_event_id,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.commit_candidate_transition(updated, event, idempotency)
        self.assertEqual(self.store.get_candidate(candidate_id), original)
        self.assertEqual(self.store.get_review_events(candidate_id), [])
        self.assertIsNone(self.store.get_idempotency(idempotency.scope, idempotency.key))

    def test_integer_amount_is_enforced_in_python_and_sqlite(self) -> None:
        self.seed()
        candidate_id = "2d0d0cb9-d4ab-4e3f-9879-7812882b8f21"
        updated, event, idempotency = self.transition_for(candidate_id)
        updated["amount_minor"] = 6380.5
        with self.assertRaisesRegex(ValueError, "amount_minor"):
            self.store.commit_candidate_transition(updated, event, idempotency)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction(write=True) as connection:
                connection.execute(
                    "UPDATE candidates SET amount_minor = ? WHERE candidate_id = ?",
                    (6380.5, candidate_id),
                )

    def test_draft_and_idempotency_survive_reopen(self) -> None:
        draft_id = str(uuid.uuid4())
        draft = {
            "id": draft_id,
            "accounting_month": "2026-08",
            "input_revision": 9,
            "status": "NEEDS_REVIEW",
            "verification": None,
            "monitor_url": f"/api/v1/workbook-drafts/{draft_id}",
            "output_sha256": None,
            "verification_detail": "synthetic only",
        }
        idempotency = IdempotencyRecord(
            scope="workbook-draft",
            key=str(uuid.uuid4()),
            fingerprint="d" * 64,
            response_status=202,
            response=draft,
            location=draft["monitor_url"],
        )
        self.assertFalse(self.store.save_draft(draft, idempotency).replayed)
        reopened = SQLitePersistence(self.database_path)
        self.assertEqual(reopened.get_draft(draft_id), draft)
        self.assertEqual(reopened.get_idempotency(idempotency.scope, idempotency.key), idempotency)
        self.assertTrue(reopened.save_draft(draft, idempotency).replayed)

    def test_concurrent_writers_use_independent_connections(self) -> None:
        def write_one(index: int) -> bool:
            record = IdempotencyRecord(
                scope="concurrency-test",
                key=f"key-{index}",
                fingerprint=f"fingerprint-{index}",
                response_status=200,
                response={"index": index},
            )
            return self.store.remember_idempotency(record).replayed

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(write_one, range(32)))
        self.assertEqual(results, [False] * 32)
        reopened = SQLitePersistence(self.database_path)
        for index in range(32):
            record = reopened.get_idempotency("concurrency-test", f"key-{index}")
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.response, {"index": index})


if __name__ == "__main__":
    unittest.main()
