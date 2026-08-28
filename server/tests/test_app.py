from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

from server.app import SyntheticState, create_server, run


class SyntheticBffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        Path(self.temp_dir.name, "index.html").write_text("<main>preview</main>", encoding="utf-8")
        self.state = SyntheticState(cookie_secure=False)
        self.server = create_server("127.0.0.1", 0, self.temp_dir.name, state=self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        session_response = urllib.request.urlopen(f"{self.base_url}/api/v1/session", timeout=2)
        self.session = json.load(session_response)
        self.set_cookie = session_response.headers["Set-Cookie"]
        self.cookie = self.set_cookie.split(";", 1)[0]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> tuple[int, dict[str, object], object]:
        request_headers = dict(headers or {})
        if authenticated:
            request_headers["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=request_headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error), error
        return response.status, json.load(response), response

    def decision_headers(self, key: str | None = None) -> dict[str, str]:
        return {"X-CSRF-Token": self.session["csrf_token"], "Idempotency-Key": key or str(uuid.uuid4())}

    def test_session_health_head_and_security_headers(self) -> None:
        self.assertEqual(self.session["principal"], "prototype-single-user")
        self.assertGreaterEqual(len(self.session["csrf_token"]), 32)
        self.assertTrue(self.set_cookie.startswith("ledgerbridge_preview_session="))
        self.assertIn("HttpOnly", self.set_cookie)
        self.assertIn("SameSite=Strict", self.set_cookie)
        self.assertNotIn("Secure", self.set_cookie)
        response = urllib.request.urlopen(f"{self.base_url}/healthz", timeout=2)
        self.assertEqual(response.read(), b"ok\n")
        self.assertEqual(response.headers["X-LedgerBridge-Mode"], "synthetic-preview")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        request = urllib.request.Request(f"{self.base_url}/healthz", method="HEAD")
        head = urllib.request.urlopen(request, timeout=2)
        self.assertEqual(head.status, 200)
        self.assertEqual(head.headers["Content-Length"], "3")
        self.assertEqual(head.read(), b"")

    def test_api_requires_session_after_bootstrap(self) -> None:
        status, problem, _ = self.request("/api/v1/candidates", authenticated=False)
        self.assertEqual(status, 401)
        self.assertEqual(problem["code"], "AUTHENTICATION_REQUIRED")

    def test_candidate_filters_detail_and_read_projections(self) -> None:
        status, payload, _ = self.request("/api/v1/candidates?status=PENDING&accounting_month=2026-08")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 2)
        candidate = payload["items"][0]
        self.assertIsInstance(candidate["amount_minor"], int)
        self.assertNotIn("review_events", candidate)
        status, detail, _ = self.request(f"/api/v1/candidates/{candidate['id']}")
        self.assertEqual(status, 200)
        self.assertIn("review_events", detail)
        status, reconciliation, _ = self.request("/api/v1/reconciliations/2026-08")
        self.assertEqual(status, 200)
        self.assertFalse(reconciliation["ready"])
        status, connections, _ = self.request("/api/v1/connections")
        self.assertEqual(status, 200)
        self.assertEqual(len(connections["items"]), 4)
        status, events, _ = self.request("/api/v1/review-events")
        self.assertEqual(status, 200)
        self.assertEqual(len(events["items"]), 1)
        self.assertEqual(events["items"][0]["decision"], "CONFIRM")
        self.assertIsNone(events["next_cursor"])
        status, problem, _ = self.request("/api/v1/review-events?cursor=invalid")
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "INVALID_CURSOR")
        status, problem, _ = self.request("/api/v1/review-events?cursor=%C2%B2")
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "INVALID_CURSOR")
        status, problem, _ = self.request(f"/api/v1/review-events?cursor={'1' * 513}")
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "INVALID_CURSOR")

    def test_review_event_history_is_cursor_paginated(self) -> None:
        candidate_id, seeded = next(iter(self.state.review_events.items()))
        template = seeded[0]
        with self.state.lock:
            self.state.review_events = {
                candidate_id: [
                    template | {"id": str(uuid.uuid4()), "sequence": sequence}
                    for sequence in range(1, 52)
                ]
            }

        status, first_page, _ = self.request("/api/v1/review-events")
        self.assertEqual(status, 200)
        self.assertEqual(len(first_page["items"]), 50)
        self.assertEqual(first_page["items"][0]["sequence"], 51)
        self.assertEqual(first_page["next_cursor"], "50")

        status, second_page, _ = self.request("/api/v1/review-events?cursor=50")
        self.assertEqual(status, 200)
        self.assertEqual(len(second_page["items"]), 1)
        self.assertEqual(second_page["items"][0]["sequence"], 1)
        self.assertIsNone(second_page["next_cursor"])

    def test_evidence_content_is_authenticated_safe_and_digest_matched(self) -> None:
        message_id = "1dedc967-753a-4c02-8409-e51c02e6cc18"
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/evidence/{message_id}/content",
            headers={"Cookie": self.cookie},
        )
        response = urllib.request.urlopen(request, timeout=2)
        content = response.read()
        self.assertIn("合成原文".encode(), content)
        self.assertEqual(response.headers.get_content_type(), "text/plain")
        self.assertEqual(int(response.headers["Content-Length"]), len(content))
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-LedgerBridge-Mode"], "synthetic-preview")
        self.assertRegex(response.headers["Content-Disposition"], r'^inline; filename="[A-Za-z0-9.-]+"$')
        status, detail, _ = self.request("/api/v1/candidates/2d0d0cb9-d4ab-4e3f-9879-7812882b8f21")
        self.assertEqual(status, 200)
        self.assertEqual(detail["evidence"][0]["sha256"], hashlib.sha256(content).hexdigest())

        attachment_id = "5715f313-93d0-4b3d-8f58-180d14ba5a7a"
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/evidence/{attachment_id}/content",
            headers={"Cookie": self.cookie},
        )
        attachment = urllib.request.urlopen(request, timeout=2)
        attachment_content = attachment.read()
        self.assertIn("合成附件占位".encode(), attachment_content)
        self.assertRegex(attachment.headers["Content-Disposition"], r'^attachment; filename="[A-Za-z0-9.-]+"$')
        self.assertTrue(attachment.headers["Content-Digest"].startswith("sha-256=:"))

        status, problem, _ = self.request(
            f"/api/v1/evidence/{message_id}/content", authenticated=False
        )
        self.assertEqual(status, 401)
        self.assertEqual(problem["code"], "AUTHENTICATION_REQUIRED")
        status, problem, _ = self.request(
            "/api/v1/evidence/00000000-0000-0000-0000-000000000000/content"
        )
        self.assertEqual(status, 404)
        self.assertEqual(problem["code"], "EVIDENCE_NOT_FOUND")

    def test_csrf_idempotency_and_immutable_revision_delta(self) -> None:
        candidate_id = "2d0d0cb9-d4ab-4e3f-9879-7812882b8f21"
        body = {"decision": "CONFIRM", "expected_revision": 1, "reason": "已核对合成附件"}
        key = str(uuid.uuid4())
        status, problem, _ = self.request(
            f"/api/v1/candidates/{candidate_id}/decisions", method="POST", body=body,
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(status, 403)
        self.assertEqual(problem["code"], "CSRF_VALIDATION_FAILED")
        status, first, _ = self.request(
            f"/api/v1/candidates/{candidate_id}/decisions", method="POST", body=body,
            headers=self.decision_headers(key),
        )
        self.assertEqual(status, 200)
        event_snapshot = json.dumps(first["event"], sort_keys=True, ensure_ascii=False)
        self.assertEqual(first["event"]["from_revision"], 1)
        self.assertEqual(first["event"]["to_revision"], 2)
        self.assertEqual(first["event"]["changes"][-1]["field"], "status")
        status, events, _ = self.request("/api/v1/review-events")
        self.assertEqual(status, 200)
        self.assertEqual(events["items"][0]["id"], first["event"]["id"])
        status, replay, _ = self.request(
            f"/api/v1/candidates/{candidate_id}/decisions", method="POST", body=body,
            headers=self.decision_headers(key),
        )
        self.assertEqual(status, 200)
        self.assertEqual(replay, first)
        self.assertEqual(json.dumps(self.state.review_events[candidate_id][0], sort_keys=True, ensure_ascii=False), event_snapshot)
        changed = {"decision": "IGNORE", "expected_revision": 1, "reason": "正文不同"}
        status, problem, _ = self.request(
            f"/api/v1/candidates/{candidate_id}/decisions", method="POST", body=changed,
            headers=self.decision_headers(key),
        )
        self.assertEqual(status, 409)
        self.assertEqual(problem["code"], "IDEMPOTENCY_KEY_REUSED")

    def test_decision_types_constrain_corrections_and_blockers(self) -> None:
        complete_id = "2d0d0cb9-d4ab-4e3f-9879-7812882b8f21"
        status, problem, _ = self.request(
            f"/api/v1/candidates/{complete_id}/decisions", method="POST",
            body={"decision": "CONFIRM", "expected_revision": 1, "reason": "不应带更正", "corrections": {"amount_minor": 1}},
            headers=self.decision_headers(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(problem["code"], "INVALID_DECISION")
        incomplete_id = "430d322d-461d-41e9-89ba-7e8ed04d62d9"
        status, problem, _ = self.request(
            f"/api/v1/candidates/{incomplete_id}/decisions", method="POST",
            body={"decision": "CONFIRM", "expected_revision": 1, "reason": "尝试跳过月份"},
            headers=self.decision_headers(),
        )
        self.assertEqual(status, 422)
        self.assertEqual(problem["code"], "CANDIDATE_INCOMPLETE")
        conflicted_id = "d92f2482-d0a6-46de-809c-e68f9c735b17"
        status, problem, _ = self.request(
            f"/api/v1/candidates/{conflicted_id}/decisions", method="POST",
            body={"decision": "CONFIRM", "expected_revision": 1, "reason": "尝试跳过冲突"},
            headers=self.decision_headers(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(problem["code"], "UNRESOLVED_CONFLICT")

    def test_correct_and_resolve_append_complete_audit_events(self) -> None:
        incomplete_id = "430d322d-461d-41e9-89ba-7e8ed04d62d9"
        status, payload, _ = self.request(
            f"/api/v1/candidates/{incomplete_id}/decisions", method="POST",
            body={"decision": "CORRECT_AND_CONFIRM", "expected_revision": 1, "reason": "人工确认月份", "corrections": {"accounting_month": "2026-08"}},
            headers=self.decision_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["event"]["changes"][0]["previous_value"], None)
        conflicted_id = "d92f2482-d0a6-46de-809c-e68f9c735b17"
        status, payload, _ = self.request(
            f"/api/v1/candidates/{conflicted_id}/decisions", method="POST",
            body={"decision": "RESOLVE_CONFLICT", "expected_revision": 1, "reason": "核对流水", "conflict_resolution": "以银行电子回单金额为准"},
            headers=self.decision_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["event"]["conflict_resolution"], "以银行电子回单金额为准")
        status, reconciliation, _ = self.request("/api/v1/reconciliations/2026-08")
        self.assertEqual(status, 200)
        self.assertTrue(reconciliation["ready"])

    def test_workbook_draft_creation_guards_idempotency_and_polling(self) -> None:
        draft_path = "/api/v1/reconciliations/2026-08/drafts"
        status, problem, _ = self.request(
            draft_path,
            method="POST",
            body={"expected_revision": 7},
            headers=self.decision_headers(),
            authenticated=False,
        )
        self.assertEqual(status, 401)
        self.assertEqual(problem["code"], "AUTHENTICATION_REQUIRED")
        status, problem, _ = self.request(
            draft_path,
            method="POST",
            body={"expected_revision": 7},
            headers={"X-CSRF-Token": self.session["csrf_token"], "Idempotency-Key": "not-a-uuid"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "INVALID_IDEMPOTENCY_KEY")
        status, problem, _ = self.request(
            draft_path,
            method="POST",
            body={"expected_revision": 7},
            headers=self.decision_headers(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(problem["code"], "RECONCILIATION_BLOCKED")

        status, problem, _ = self.request(
            draft_path,
            method="POST",
            body={"expected_revision": 7},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self.assertEqual(status, 403)
        self.assertEqual(problem["code"], "CSRF_VALIDATION_FAILED")

        incomplete_id = "430d322d-461d-41e9-89ba-7e8ed04d62d9"
        self.request(
            f"/api/v1/candidates/{incomplete_id}/decisions",
            method="POST",
            body={"decision": "CORRECT_AND_CONFIRM", "expected_revision": 1, "reason": "人工确认月份", "corrections": {"accounting_month": "2026-08"}},
            headers=self.decision_headers(),
        )
        conflicted_id = "d92f2482-d0a6-46de-809c-e68f9c735b17"
        self.request(
            f"/api/v1/candidates/{conflicted_id}/decisions",
            method="POST",
            body={"decision": "RESOLVE_CONFLICT", "expected_revision": 1, "reason": "核对流水", "conflict_resolution": "以合成银行回单为准"},
            headers=self.decision_headers(),
        )
        status, reconciliation, _ = self.request("/api/v1/reconciliations/2026-08")
        self.assertEqual(status, 200)
        self.assertTrue(reconciliation["ready"])
        self.assertEqual(reconciliation["revision"], 9)

        status, problem, _ = self.request(
            draft_path,
            method="POST",
            body={"expected_revision": 8},
            headers=self.decision_headers(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(problem["code"], "STALE_REVISION")

        idempotency_key = str(uuid.uuid4())
        status, draft, response = self.request(
            draft_path,
            method="POST",
            body={"expected_revision": 9},
            headers=self.decision_headers(idempotency_key),
        )
        self.assertEqual(status, 202)
        location = response.headers["Location"]
        self.assertEqual(location, draft["monitor_url"])
        self.assertEqual(draft["status"], "NEEDS_REVIEW")
        self.assertIsNone(draft["verification"])
        self.assertIn("合成预览", draft["verification_detail"])

        status, polled, _ = self.request(location)
        self.assertEqual(status, 200)
        self.assertEqual(polled, draft)
        status, replay, replay_response = self.request(
            draft_path,
            method="POST",
            body={"expected_revision": 9},
            headers=self.decision_headers(idempotency_key),
        )
        self.assertEqual(status, 202)
        self.assertEqual(replay, draft)
        self.assertEqual(replay_response.headers["Location"], location)
        status, problem, _ = self.request(
            draft_path,
            method="POST",
            body={"expected_revision": 10},
            headers=self.decision_headers(idempotency_key),
        )
        self.assertEqual(status, 409)
        self.assertEqual(problem["code"], "IDEMPOTENCY_KEY_REUSED")

        fresh_state = SyntheticState(cookie_secure=False)
        self.assertIsNone(fresh_state.get_draft(draft["id"]))
        status, problem, _ = self.request(
            "/api/v1/workbook-drafts/00000000-0000-0000-0000-000000000000"
        )
        self.assertEqual(status, 404)
        self.assertEqual(problem["code"], "WORKBOOK_DRAFT_NOT_FOUND")

    def test_spa_fallback_and_symlink_are_safe(self) -> None:
        response = urllib.request.urlopen(f"{self.base_url}/review", timeout=2)
        self.assertEqual(response.read(), b"<main>preview</main>")
        outside = Path(self.temp_dir.name).parent / f"outside-{uuid.uuid4().hex}.txt"
        outside.write_text("secret", encoding="utf-8")
        link = Path(self.temp_dir.name, "linked.txt")
        try:
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks unavailable on this platform")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"{self.base_url}/linked.txt", timeout=2)
            self.assertEqual(caught.exception.code, 404)
        finally:
            outside.unlink(missing_ok=True)

    def test_real_mode_fails_closed(self) -> None:
        tls_state = SyntheticState(cookie_secure=True)
        self.assertEqual(tls_state.cookie_name, "__Host-ledgerbridge_session")
        tls_server = create_server("127.0.0.1", 0, self.temp_dir.name, state=tls_state)
        tls_thread = threading.Thread(target=tls_server.serve_forever, daemon=True)
        tls_thread.start()
        try:
            response = urllib.request.urlopen(
                f"http://127.0.0.1:{tls_server.server_port}/api/v1/session", timeout=2
            )
            secure_cookie = response.headers["Set-Cookie"]
            self.assertTrue(secure_cookie.startswith("__Host-ledgerbridge_session="))
            self.assertIn("; Secure", secure_cookie)
        finally:
            tls_server.shutdown()
            tls_server.server_close()
            tls_thread.join(timeout=2)
        with patch.dict(os.environ, {"LEDGERBRIDGE_MODE": "real", "SITE_ROOT": self.temp_dir.name}, clear=False):
            with self.assertRaisesRegex(SystemExit, "unsupported LedgerBridge mode"):
                run()


if __name__ == "__main__":
    unittest.main()
