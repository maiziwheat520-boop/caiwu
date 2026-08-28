from __future__ import annotations

import base64
import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.app import SyntheticState, create_server
from server.auth import AuthManager, AuthStore
from server.persistence import SQLitePersistence


ORIGIN = "https://ledgerbridge.example.ts.net"
SETUP_CODE = "ABCD-EFGH-JKLM-NPQR-STUV-WXYZ-2345-6789"


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class AuthenticatedPreviewHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.root = root
        (root / "index.html").write_text("<html>authenticated preview</html>", encoding="utf-8")
        self.database = root / "state.sqlite3"
        self.persistence = SQLitePersistence(self.database)
        digest = hashlib.sha256(SETUP_CODE.replace("-", "").encode()).hexdigest()
        self.manager = AuthManager(
            AuthStore(self.database),
            rp_id="ledgerbridge.example.ts.net",
            expected_origin=ORIGIN,
            setup_code_sha256=digest,
            setup_code_expires_at=int(time.time()) + 600,
        )
        self.state = SyntheticState(persistence=self.persistence, actor="ledgerbridge-owner")
        self.server = create_server(
            "127.0.0.1",
            0,
            root,
            state=self.state,
            auth_manager=self.manager,
            mode="authenticated-preview",
            trusted_proxy_cidrs="127.0.0.1/32",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object] | None, http.client.HTTPMessage]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        encoded = json.dumps(body).encode() if body is not None else None
        request_headers = dict(headers or {})
        request_headers.setdefault("X-Forwarded-For", "192.0.2.10")
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        payload = json.loads(raw) if raw else None
        status = response.status
        response_headers = response.headers
        connection.close()
        return status, payload, response_headers

    @staticmethod
    def cookie_values(headers: http.client.HTTPMessage) -> dict[str, str]:
        values: dict[str, str] = {}
        for raw in headers.get_all("Set-Cookie", []):
            parsed = SimpleCookie()
            parsed.load(raw)
            for name, morsel in parsed.items():
                if morsel.value:
                    values[name] = morsel.value
        return values

    def test_registration_requires_origin_then_unlocks_persistent_api(self) -> None:
        status, initial, _ = self.request("GET", "/api/v1/auth/status")
        self.assertEqual(status, 200)
        self.assertTrue(initial["setup_required"])  # type: ignore[index]
        status, _, _ = self.request("GET", "/api/v1/session")
        self.assertEqual(status, 401)
        status, problem, _ = self.request(
            "POST",
            "/api/v1/auth/passkey/register/options",
            body={"setup_code": SETUP_CODE},
        )
        self.assertEqual(status, 403)
        self.assertEqual(problem["code"], "ORIGIN_VALIDATION_FAILED")  # type: ignore[index]

        status, options, option_headers = self.request(
            "POST",
            "/api/v1/auth/passkey/register/options",
            body={"setup_code": SETUP_CODE},
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(options["rp"]["id"], "ledgerbridge.example.ts.net")  # type: ignore[index]
        flow = self.cookie_values(option_headers)["__Host-ledgerbridge_auth_flow"]
        verified = SimpleNamespace(
            credential_id=b"credential-one",
            credential_public_key=b"public-key",
            sign_count=0,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )
        with patch("server.auth.verify_registration_response", return_value=verified):
            status, result, verify_headers = self.request(
                "POST",
                "/api/v1/auth/passkey/register/verify",
                body={"setup_code": SETUP_CODE, "credential": {"id": "credential-one"}},
                headers={
                    "Origin": ORIGIN,
                    "Sec-Fetch-Site": "same-origin",
                    "Cookie": f"__Host-ledgerbridge_auth_flow={flow}",
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(len(result["recovery_codes"]), 10)  # type: ignore[arg-type,index]
        session = self.cookie_values(verify_headers)["__Host-ledgerbridge_session"]
        cookie_header = {"Cookie": f"__Host-ledgerbridge_session={session}"}
        status, session_payload, _ = self.request("GET", "/api/v1/session", headers=cookie_header)
        self.assertEqual(status, 200)
        status, candidates, _ = self.request("GET", "/api/v1/candidates", headers=cookie_header)
        self.assertEqual(status, 200)
        self.assertEqual(len(candidates["items"]), 5)  # type: ignore[arg-type,index]

        candidate = candidates["items"][0]  # type: ignore[index]
        status, problem, _ = self.request(
            "POST",
            f"/api/v1/candidates/{candidate['id']}/decisions",  # type: ignore[index]
            body={"decision": "CONFIRM", "expected_revision": candidate["revision"], "reason": "origin test"},  # type: ignore[index]
            headers={
                **cookie_header,
                "Origin": "https://evil.example",
                "X-CSRF-Token": session_payload["csrf_token"],  # type: ignore[index]
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(problem["code"], "ORIGIN_VALIDATION_FAILED")  # type: ignore[index]

        reopened = SyntheticState(persistence=SQLitePersistence(self.database), actor="ledgerbridge-owner")
        self.assertEqual(len(reopened.list_candidates(status=None, month=None, cursor=None)["items"]), 5)  # type: ignore[arg-type]

    def test_hsts_and_secure_host_cookies_are_present(self) -> None:
        status, _, headers = self.request("GET", "/api/v1/auth/status")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Strict-Transport-Security"], "max-age=31536000")
        status, _, headers = self.request(
            "POST",
            "/api/v1/auth/passkey/register/options",
            body={"setup_code": SETUP_CODE},
            headers={"Origin": ORIGIN},
        )
        self.assertEqual(status, 200)
        flow_cookie = headers.get_all("Set-Cookie")[0]
        self.assertIn("__Host-ledgerbridge_auth_flow=", flow_cookie)
        self.assertIn("; Secure", flow_cookie)
        self.assertIn("; HttpOnly", flow_cookie)
        self.assertIn("; SameSite=Strict", flow_cookie)

    def test_trusted_proxy_identity_is_single_hop_and_isolates_setup_throttle(self) -> None:
        common_headers = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
        for _ in range(5):
            status, problem, _ = self.request(
                "POST",
                "/api/v1/auth/passkey/register/options",
                body={"setup_code": "wrong"},
                headers={**common_headers, "X-Forwarded-For": "192.0.2.20"},
            )
            self.assertEqual(status, 401)
            self.assertEqual(problem["code"], "SETUP_CODE_INVALID")  # type: ignore[index]
        status, problem, _ = self.request(
            "POST",
            "/api/v1/auth/passkey/register/options",
            body={"setup_code": SETUP_CODE},
            headers={**common_headers, "X-Forwarded-For": "192.0.2.20"},
        )
        self.assertEqual(status, 429)
        self.assertEqual(problem["code"], "AUTH_RATE_LIMITED")  # type: ignore[index]

        status, options, _ = self.request(
            "POST",
            "/api/v1/auth/passkey/register/options",
            body={"setup_code": SETUP_CODE},
            headers={**common_headers, "X-Forwarded-For": "192.0.2.21"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(options["rp"]["id"], "ledgerbridge.example.ts.net")  # type: ignore[index]

        status, problem, _ = self.request(
            "POST",
            "/api/v1/auth/passkey/register/options",
            body={"setup_code": SETUP_CODE},
            headers={**common_headers, "X-Forwarded-For": "192.0.2.21, 192.0.2.99"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "CLIENT_IDENTITY_INVALID")  # type: ignore[index]

    def test_trusted_proxy_configuration_rejects_non_host_networks(self) -> None:
        with self.assertRaisesRegex(ValueError, "IPv4 /32 or IPv6 /128"):
            create_server(
                "127.0.0.1",
                0,
                self.root,
                state=self.state,
                auth_manager=self.manager,
                mode="authenticated-preview",
                trusted_proxy_cidrs="127.0.0.0/24",
            )
        with self.assertRaisesRegex(ValueError, "at least one trusted proxy host"):
            create_server(
                "127.0.0.1",
                0,
                self.root,
                state=self.state,
                auth_manager=self.manager,
                mode="authenticated-preview",
                trusted_proxy_cidrs=" , ",
            )

    def test_recovery_registration_requires_its_csrf_token(self) -> None:
        recovery_codes = self.manager.store.register_credential(
            user_id=b"u" * 32,
            credential_id=b"credential-one",
            public_key=b"public-key",
            sign_count=0,
            device_type="multi_device",
            backed_up=True,
            initial=True,
        )
        status, recovery, recovery_headers = self.request(
            "POST",
            "/api/v1/auth/recovery",
            body={"recovery_code": recovery_codes[0]},
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(recovery["recovery_setup_required"])  # type: ignore[index]
        self.assertTrue(recovery["recovery_pending"])  # type: ignore[index]
        session = self.cookie_values(recovery_headers)["__Host-ledgerbridge_session"]
        headers = {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "Cookie": f"__Host-ledgerbridge_session={session}",
        }
        status, refreshed, _ = self.request(
            "GET",
            "/api/v1/auth/recovery/session",
            headers={"Cookie": f"__Host-ledgerbridge_session={session}"},
        )
        self.assertEqual(status, 200)
        self.assertIn("csrf_token", refreshed)  # type: ignore[operator]
        status, problem, _ = self.request(
            "POST",
            "/api/v1/auth/passkey/register/options",
            body={"setup_code": ""},
            headers=headers,
        )
        self.assertEqual(status, 403)
        self.assertEqual(problem["code"], "CSRF_VALIDATION_FAILED")  # type: ignore[index]
        status, _, _ = self.request(
            "POST",
            "/api/v1/auth/passkey/register/options",
            body={"setup_code": ""},
            headers={**headers, "X-CSRF-Token": refreshed["csrf_token"]},  # type: ignore[index]
        )
        self.assertEqual(status, 200)
        status, _, _ = self.request(
            "POST",
            "/api/v1/session/logout",
            body={},
            headers={**headers, "X-CSRF-Token": refreshed["csrf_token"]},  # type: ignore[index]
        )
        self.assertEqual(status, 204)

    def test_authenticated_passkey_addition_keeps_existing_devices_and_session(self) -> None:
        recovery_codes = self.manager.store.register_credential(
            user_id=b"u" * 32,
            credential_id=b"credential-one",
            public_key=b"public-key-one",
            sign_count=1,
            device_type="single_device",
            backed_up=False,
            initial=True,
        )
        session = self.manager.store.create_session("passkey")
        base_headers = {
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "Cookie": f"__Host-ledgerbridge_session={session.token}",
        }
        status, problem, _ = self.request(
            "POST",
            "/api/v1/auth/passkey/add/authorize/options",
            body={},
            headers=base_headers,
        )
        self.assertEqual(status, 403)
        self.assertEqual(problem["code"], "CSRF_VALIDATION_FAILED")  # type: ignore[index]

        headers = {**base_headers, "X-CSRF-Token": session.csrf_token}
        status, authorization_options, authorization_headers = self.request(
            "POST",
            "/api/v1/auth/passkey/add/authorize/options",
            body={},
            headers=headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(authorization_options["allowCredentials"]), 1)  # type: ignore[arg-type,index]
        authorization_flow = self.cookie_values(authorization_headers)["__Host-ledgerbridge_auth_flow"]
        authorization = SimpleNamespace(
            credential_id=b"credential-one",
            new_sign_count=2,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )
        with patch("server.auth.verify_authentication_response", return_value=authorization):
            status, registration_options, registration_headers = self.request(
                "POST",
                "/api/v1/auth/passkey/add/authorize/verify",
                body={"credential": {"id": b64url(b"credential-one")}},
                headers={
                    **headers,
                    "Cookie": (
                        f"__Host-ledgerbridge_session={session.token}; "
                        f"__Host-ledgerbridge_auth_flow={authorization_flow}"
                    ),
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(len(registration_options["excludeCredentials"]), 1)  # type: ignore[arg-type,index]
        registration_flow = self.cookie_values(registration_headers)["__Host-ledgerbridge_auth_flow"]
        registration = SimpleNamespace(
            credential_id=b"credential-two",
            credential_public_key=b"public-key-two",
            sign_count=0,
            credential_device_type=SimpleNamespace(value="single_device"),
            credential_backed_up=False,
        )
        with patch("server.auth.verify_registration_response", return_value=registration):
            status, result, _ = self.request(
                "POST",
                "/api/v1/auth/passkey/add/verify",
                body={"credential": {"id": b64url(b"credential-two")}},
                headers={
                    **headers,
                    "Cookie": (
                        f"__Host-ledgerbridge_session={session.token}; "
                        f"__Host-ledgerbridge_auth_flow={registration_flow}"
                    ),
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(result, {"added": True, "passkey_count": 2})
        self.assertIsNotNone(self.manager.store.credential(b"credential-one"))
        self.assertIsNotNone(self.manager.store.credential(b"credential-two"))
        self.assertIsNotNone(self.manager.store.session_payload(session.token, rotate_csrf=False))
        self.assertTrue(self.manager.store.consume_recovery_code(recovery_codes[0]))


if __name__ == "__main__":
    unittest.main()
