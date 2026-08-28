from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from server.app import COOKIE_NAME, create_server
from server.core_backend import CoreBackedState, sqlite_contains_business_facts


CANDIDATE_ID = "30000000-0000-4000-8000-000000000003"
EVIDENCE_ID = "20000000-0000-4000-8000-000000000003"
ENTITY_ID = "10000000-0000-4000-8000-000000000001"
ASSERTION_KEY = b"synthetic-web-core-assertion-key-0001"


def core_candidate(*, status: str = "PENDING", revision: int = 1) -> dict[str, object]:
    return {
        "contract_version": "ledgerbridge.candidate.v1",
        "candidate_ref": CANDIDATE_ID,
        "short_id": "C-R0A003",
        "revision": revision,
        "status": status,
        "entity_ref": ENTITY_ID,
        "business_unit_ref": "unit-demo-a",
        "business_unit_label": "演示门店",
        "category_code": "SETTLEMENT",
        "category_label": "银行收款",
        "amount_minor": 12345,
        "currency": "CNY",
        "accounting_month": "2026-08",
        "summary": "合成中行邮件候选",
        "confidence_basis_points": 9500,
        "source": {
            "ingest_channel": "OUTLOOK",
            "source_system": "synthetic_boc_mail",
            "source_event_ref": "40000000-0000-4000-8000-000000000003",
            "display_label": "中行邮箱（合成）",
        },
        "evidence": [
            {
                "evidence_ref": EVIDENCE_ID,
                "kind": "ATTACHMENT",
                "media_type": "application/pdf",
                "display_name": "synthetic-boc.pdf",
                "download_available": True,
            }
        ],
        "blockers": [],
        "review_summary": {
            "event_count": revision - 1,
            "last_action": "CONFIRM" if revision > 1 else None,
            "last_decided_at": "2026-08-28T01:01:00Z" if revision > 1 else None,
            "current_revision": revision,
        },
        "created_at": "2026-08-28T01:00:00Z",
        "updated_at": "2026-08-28T01:01:00Z" if revision > 1 else "2026-08-28T01:00:00Z",
        "supersedes_candidate_ref": None,
        "superseded_by_candidate_ref": None,
    }


def core_event() -> dict[str, object]:
    prior = core_candidate()
    result = core_candidate(status="CONFIRMED", revision=2)
    return {
        "operation_id": "60000000-0000-4000-8000-000000000003",
        "command_fingerprint": "a" * 64,
        "candidate_ref": CANDIDATE_ID,
        "action": "CONFIRM",
        "from_revision": 1,
        "to_revision": 2,
        "from_status": "PENDING",
        "to_status": "CONFIRMED",
        "changes": [
            {
                "field": "status",
                "previous_value": "PENDING",
                "new_value": "CONFIRMED",
            }
        ],
        "resolved_conflicts": [],
        "reason": "合成网页复核",
        "actor_ref": "ledgerbridge-owner",
        "created_at": "2026-08-28T01:01:00Z",
        "derived_candidate_ref": None,
        "prior_projection": prior,
        "result_projection": result,
        "result_derived_candidate": None,
    }


class FakeCoreClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, path, body, dict(headers or {})))
        if method == "POST":
            return {
                "contract_version": "ledgerbridge.candidate-decision.v1",
                "operation_id": headers["Idempotency-Key"] if headers else "",
                "replayed": False,
                "candidate": core_candidate(status="CONFIRMED", revision=2),
                "events": [core_event()],
            }
        if path.startswith("/internal/v1/candidate-events"):
            return {"items": [core_event()], "next_cursor": None}
        if path.startswith(f"/internal/v1/candidates/{CANDIDATE_ID}"):
            return core_candidate()
        if path.startswith("/internal/v1/candidates?"):
            return {"items": [core_candidate()], "next_cursor": None}
        raise AssertionError(f"unexpected Core path: {path}")

    def evidence(self, path: str) -> dict[str, object]:
        self.calls.append(("GET", path, None, {}))
        return {
            "content": b"synthetic evidence",
            "content_type": "application/octet-stream",
            "disposition": "attachment",
            "filename": "evidence.bin",
        }


class FakeAuthStore:
    @staticmethod
    def validate_csrf(token: str, supplied: str) -> bool:
        return token == "session-token" and supplied == "csrf-token"


class FakeAuthManager:
    expected_origin = "https://ledgerbridge.test"
    store = FakeAuthStore()

    @staticmethod
    def status(token: str | None) -> dict[str, object]:
        return {
            "authenticated": token == "session-token",
            "setup_required": False,
            "passkey_registered": True,
            "recovery_setup_required": False,
            "recovery_pending": False,
        }

    @staticmethod
    def session_payload(token: str | None) -> dict[str, str] | None:
        if token != "session-token":
            return None
        return {
            "principal": "ledgerbridge-owner",
            "csrf_token": "csrf-token",
            "expires_at": "2026-08-29T00:00:00Z",
        }


def build_state(client: FakeCoreClient) -> CoreBackedState:
    return CoreBackedState(
        client,  # type: ignore[arg-type]
        assertion_key=ASSERTION_KEY,
        assertion_issuer="ledgerbridge-web-test",
        assertion_audience="ledgerbridge-core-test",
        workload_principal="ledgerbridge-web",
        policy_generation=21,
        user_subject="ledgerbridge-owner",
        authentication_generation=4,
        entity_ref=ENTITY_ID,
        business_unit_ref="unit-demo-a",
    )


class CoreBackedAdapterTests(unittest.TestCase):
    def test_maps_outlook_candidate_and_binds_exact_user_assertion(self) -> None:
        client = FakeCoreClient()
        state = build_state(client)

        page = state.list_candidates(status="PENDING", month="2026-08", cursor=None)
        candidate = page["items"][0]  # type: ignore[index]
        self.assertEqual(candidate["source_channel"], "outlook")
        self.assertEqual(candidate["business_unit"], "演示门店")
        self.assertIsNone(candidate["evidence"][0]["sha256"])

        operation = str(uuid.uuid4())
        request: dict[str, object] = {
            "decision": "CONFIRM",
            "expected_revision": 1,
            "reason": "合成网页复核",
        }
        status, payload = state.append_decision(CANDIDATE_ID, operation, request)
        self.assertEqual(status, 200)
        self.assertEqual(payload["candidate"]["status"], "CONFIRMED")
        self.assertEqual(payload["event"]["actor"], "ledgerbridge-owner")

        method, path, body, headers = client.calls[-1]
        self.assertEqual(method, "POST")
        self.assertNotIn("actor", json.loads(body))
        version, encoded, signature = headers["X-LedgerBridge-User-Assertion"].split(".")
        self.assertEqual(version, "v1")
        signed = f"v1.{encoded}".encode("ascii")
        expected = hmac.new(ASSERTION_KEY, signed, hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        self.assertTrue(hmac.compare_digest(expected, supplied))
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(claims["canonical_path"], path)
        self.assertEqual(claims["operation_id"], operation)
        self.assertEqual(claims["body_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(claims["subject"], "ledgerbridge-owner")

    def test_core_backed_bff_serves_and_reviews_without_local_business_store(self) -> None:
        client = FakeCoreClient()
        state = build_state(client)
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "index.html").write_text("<main>review</main>", encoding="utf-8")
            server = create_server(
                "127.0.0.1",
                0,
                temp_dir,
                state=state,
                auth_manager=FakeAuthManager(),  # type: ignore[arg-type]
                mode="core-backed",
                trusted_proxy_cidrs="127.0.0.1/32",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                cookie = f"{COOKIE_NAME}=session-token"
                request = urllib.request.Request(
                    f"{base_url}/api/v1/candidates?status=PENDING",
                    headers={"Cookie": cookie},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    page = json.load(response)
                self.assertEqual(page["items"][0]["source_channel"], "outlook")

                body = json.dumps(
                    {
                        "decision": "CONFIRM",
                        "expected_revision": 1,
                        "reason": "合成网页复核",
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"{base_url}/api/v1/candidates/{CANDIDATE_ID}/decisions",
                    data=body,
                    method="POST",
                    headers={
                        "Cookie": cookie,
                        "Origin": "https://ledgerbridge.test",
                        "Sec-Fetch-Site": "same-origin",
                        "Content-Type": "application/json",
                        "X-CSRF-Token": "csrf-token",
                        "Idempotency-Key": str(uuid.uuid4()),
                    },
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    result = json.load(response)
                self.assertEqual(result["candidate"]["status"], "CONFIRMED")
                self.assertEqual(response.headers["X-LedgerBridge-Mode"], "core-backed")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_core_backed_mode_rejects_sqlite_business_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir, "web.sqlite3")
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE auth_sessions (id TEXT PRIMARY KEY)")
                connection.commit()
            self.assertFalse(sqlite_contains_business_facts(database))
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE candidates (id TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO candidates (id) VALUES ('synthetic')")
                connection.commit()
            self.assertTrue(sqlite_contains_business_facts(database))


if __name__ == "__main__":
    unittest.main()
