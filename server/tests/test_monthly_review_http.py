from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server.app import COOKIE_NAME, create_server
from server.tests.test_monthly_review import package
from server.tests.test_payroll_bff import FakeAuthManager, FakePayrollCoreClient, build_state


class MonthlyReviewHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.site = self.root / "site"
        self.site.mkdir()
        (self.site / "index.html").write_text("<main>synthetic</main>")
        self.evidence = self.root / "private-review.json"
        self.evidence.write_text(json.dumps(package(), ensure_ascii=False), encoding="utf-8")
        self.env = patch.dict("os.environ", {"MONTHLY_RECONCILIATION_REVIEW_FILE": str(self.evidence)})
        self.env.start()
        self.manager = FakeAuthManager()
        self.client = FakePayrollCoreClient()
        self.server = create_server("127.0.0.1", 0, self.site,
            state=build_state(self.client), auth_manager=self.manager, mode="core-backed",
            trusted_proxy_cidrs="127.0.0.1/32")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.stop()
        self.directory.cleanup()

    def get(self, suffix="2026-09", authenticated=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"X-Forwarded-For": "192.0.2.10"}
        if authenticated:
            headers["Cookie"] = f"{COOKIE_NAME}=session-token"
        connection.request("GET", "/api/v1/monthly-reconciliation-reviews/" + suffix, headers=headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read()), response.getheader("Cache-Control")
        connection.close()
        return result

    def test_owner_read_is_private_no_store_and_does_not_touch_core(self):
        before = self.evidence.read_bytes()
        status, result, cache = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(cache, "no-store")
        self.assertFalse(result["production_posted"])
        self.assertEqual(result["authority"], "NON_AUTHORITATIVE_REFERENCE")
        self.assertEqual(result["accounting_month"], "2026-09")
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.evidence.read_bytes(), before)

    def test_anonymous_is_denied_before_reading_package(self):
        with patch("server.app.load_monthly_review") as load:
            self.assertEqual(self.get(authenticated=False)[0], 401)
            load.assert_not_called()

    def test_different_authenticated_subject_is_denied(self):
        self.manager.session_subject = "other-subject"
        with patch("server.app.load_monthly_review") as load:
            self.assertEqual(self.get()[0], 403)
            load.assert_not_called()

    def test_recovery_style_identity_is_denied(self):
        with patch.object(self.manager, "payroll_session_subject", return_value=None):
            self.assertEqual(self.get()[0], 403)

    def test_invalid_month_and_scope_or_path_query_are_denied(self):
        for suffix in ["2026-13", "2026-09?path=secret", "2026-09?entity=another", "2026-9"]:
            with self.subTest(suffix=suffix):
                self.assertEqual(self.get(suffix)[0], 400)

    def test_missing_or_invalid_evidence_is_unavailable_without_private_error(self):
        self.evidence.write_text("private sensitive invalid input", encoding="utf-8")
        status, result, _ = self.get()
        self.assertEqual(status, 503)
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertEqual(result["code"], "MONTHLY_REVIEW_UNAVAILABLE")

    def test_private_report_is_not_served_as_a_static_asset(self):
        for path in ("/private-review.json", "/monthly-reconciliation-review.json", "/config/monthly-reconciliation-review.json"):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            connection.request("GET", path, headers={"X-Forwarded-For": "192.0.2.10"})
            response = connection.getresponse()
            body = response.read()
            self.assertNotIn(b"ledgerbridge.monthly-review-package.v1", body)
            self.assertNotEqual(body, self.evidence.read_bytes())
            connection.close()


if __name__ == "__main__":
    unittest.main()
