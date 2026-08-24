from __future__ import annotations

import base64
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.auth import AuthError, AuthManager, AuthStore, RECOVERY_CODE_COUNT


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class AuthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "auth.sqlite3"
        self.store = AuthStore(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_initial_registration_recovery_and_reopen_are_persistent(self) -> None:
        codes = self.store.register_credential(
            user_id=b"u" * 32,
            credential_id=b"credential-one",
            public_key=b"public-key",
            sign_count=0,
            device_type="multi_device",
            backed_up=True,
            initial=True,
        )
        self.assertEqual(len(codes), RECOVERY_CODE_COUNT)
        self.assertTrue(all(len(code.replace("-", "")) == 32 for code in codes))
        reopened = AuthStore(self.database)
        self.assertTrue(reopened.initialized())
        self.assertEqual(reopened.credential(b"credential-one").public_key, b"public-key")  # type: ignore[union-attr]
        self.assertTrue(reopened.consume_recovery_code(codes[0]))
        self.assertFalse(reopened.consume_recovery_code(codes[0]))

    def test_session_tokens_are_hashed_rotatable_and_revocable(self) -> None:
        session = self.store.create_session("passkey")
        self.assertIsNotNone(self.store.session_payload(session.token, rotate_csrf=False))
        payload = self.store.session_payload(session.token)
        self.assertIsNotNone(payload)
        self.assertTrue(self.store.validate_csrf(session.token, payload["csrf_token"]))  # type: ignore[index]
        self.assertTrue(self.store.validate_csrf(session.token, session.csrf_token))
        second_payload = self.store.session_payload(session.token)
        self.assertIsNotNone(second_payload)
        self.assertTrue(self.store.validate_csrf(session.token, payload["csrf_token"]))  # type: ignore[index]
        self.assertFalse(self.store.validate_csrf(session.token, session.csrf_token))
        self.assertFalse(self.store.validate_csrf(session.token, "wrong"))
        with self.store.connection() as connection:
            stored = bytes(connection.execute("SELECT token_hash FROM auth_sessions").fetchone()[0])
        self.assertNotEqual(stored, session.token.encode())
        self.store.revoke_all_sessions()
        self.assertIsNone(self.store.session_payload(session.token, rotate_csrf=False))

    def test_missing_auth_state_fails_closed_on_reopen(self) -> None:
        with self.store.connection() as connection:
            connection.execute("DELETE FROM auth_state")
        with self.assertRaisesRegex(RuntimeError, "authentication state row is missing"):
            AuthStore(self.database)


class AuthManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "auth.sqlite3"
        self.setup_code = "ABCD-EFGH-JKLM-NPQR-STUV-WXYZ-2345-6789"
        digest = hashlib.sha256(self.setup_code.replace("-", "").encode()).hexdigest()
        self.manager = AuthManager(
            AuthStore(self.database),
            rp_id="ledgerbridge.example.ts.net",
            expected_origin="https://ledgerbridge.example.ts.net",
            setup_code_sha256=digest,
            setup_code_expires_at=int(time.time()) + 600,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_setup_code_is_required_and_registration_challenge_is_one_use(self) -> None:
        with self.assertRaises(AuthError) as wrong:
            self.manager.start_registration("wrong", None)
        self.assertEqual(wrong.exception.code, "SETUP_CODE_INVALID")
        options, flow = self.manager.start_registration(self.setup_code, None)
        self.assertEqual(options["rp"]["id"], "ledgerbridge.example.ts.net")
        verified = SimpleNamespace(
            credential_id=b"credential-one",
            credential_public_key=b"public-key",
            sign_count=0,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )
        with patch("server.auth.verify_registration_response", return_value=verified):
            session, codes = self.manager.finish_registration(
                flow,
                {"id": b64url(b"credential-one")},
                setup_code=self.setup_code,
                session_token=None,
            )
        self.assertEqual(len(codes), RECOVERY_CODE_COUNT)
        self.assertIsNotNone(self.manager.store.session_payload(session.token, rotate_csrf=False))
        with self.assertRaises(AuthError) as replay:
            self.manager.finish_registration(
                flow,
                {"id": b64url(b"credential-one")},
                setup_code=self.setup_code,
                session_token=None,
            )
        self.assertEqual(replay.exception.code, "AUTH_CEREMONY_EXPIRED")

    def test_login_updates_counter_and_recovery_revokes_existing_session(self) -> None:
        codes = self.manager.store.register_credential(
            user_id=b"u" * 32,
            credential_id=b"credential-one",
            public_key=b"public-key",
            sign_count=1,
            device_type="single_device",
            backed_up=False,
            initial=True,
        )
        _, flow = self.manager.start_login()
        verified = SimpleNamespace(
            credential_id=b"credential-one",
            new_sign_count=2,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )
        with patch("server.auth.verify_authentication_response", return_value=verified):
            passkey_session = self.manager.finish_login(flow, {"id": b64url(b"credential-one")})
        self.assertEqual(self.manager.store.credential(b"credential-one").sign_count, 2)  # type: ignore[union-attr]
        recovery_session = self.manager.recover(codes[0])
        self.assertIsNone(self.manager.store.session_payload(passkey_session.token, rotate_csrf=False))
        self.assertIsNotNone(self.manager.store.session_payload(recovery_session.token, rotate_csrf=False))
        self.assertFalse(self.manager.status(recovery_session.token)["authenticated"])
        self.assertTrue(self.manager.status(recovery_session.token)["recovery_setup_required"])
        self.assertIsNone(self.manager.session_payload(recovery_session.token))
        _, recovery_flow = self.manager.start_registration("", recovery_session.token)
        replacement = SimpleNamespace(
            credential_id=b"credential-two",
            credential_public_key=b"public-key-two",
            sign_count=0,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )
        with patch("server.auth.verify_registration_response", return_value=replacement):
            replacement_session, replacement_codes = self.manager.finish_registration(
                recovery_flow,
                {"id": b64url(b"credential-two")},
                setup_code="",
                session_token=recovery_session.token,
            )
        self.assertEqual(len(replacement_codes), RECOVERY_CODE_COUNT)
        self.assertTrue(self.manager.status(replacement_session.token)["authenticated"])
        self.assertIsNone(self.manager.store.credential(b"credential-one"))
        self.assertIsNotNone(self.manager.store.credential(b"credential-two"))

    def test_expired_setup_code_fails_closed(self) -> None:
        manager = AuthManager(
            AuthStore(self.database),
            rp_id="ledgerbridge.example.ts.net",
            expected_origin="https://ledgerbridge.example.ts.net",
            setup_code_sha256="0" * 64,
            setup_code_expires_at=0,
        )
        with self.assertRaises(AuthError) as expired:
            manager.start_registration("anything", None)
        self.assertEqual(expired.exception.code, "SETUP_CODE_EXPIRED")

    def test_initial_registration_cannot_finish_after_setup_code_expires(self) -> None:
        _, flow = self.manager.start_registration(self.setup_code, None)
        self.manager.setup_code_expires_at = 0
        with self.assertRaises(AuthError) as expired:
            self.manager.finish_registration(
                flow,
                {"id": b64url(b"credential-one")},
                setup_code=self.setup_code,
                session_token=None,
            )
        self.assertEqual(expired.exception.code, "SETUP_CODE_EXPIRED")
        self.assertFalse(self.manager.store.initialized())

    def test_recovery_registration_is_bound_to_its_live_authorizing_session(self) -> None:
        codes = self.manager.store.register_credential(
            user_id=b"u" * 32,
            credential_id=b"credential-one",
            public_key=b"public-key",
            sign_count=1,
            device_type="single_device",
            backed_up=False,
            initial=True,
        )
        recovery_session = self.manager.recover(codes[0])
        _, flow = self.manager.start_registration("", recovery_session.token)
        self.manager.store.logout(recovery_session.token)
        replacement = SimpleNamespace(
            credential_id=b"credential-two",
            credential_public_key=b"public-key-two",
            sign_count=0,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )
        with patch("server.auth.verify_registration_response", return_value=replacement):
            with self.assertRaises(AuthError) as revoked:
                self.manager.finish_registration(
                    flow,
                    {"id": b64url(b"credential-two")},
                    setup_code="",
                    session_token=recovery_session.token,
                )
        self.assertEqual(revoked.exception.code, "AUTH_CEREMONY_REVOKED")
        self.assertIsNotNone(self.manager.store.credential(b"credential-one"))
        self.assertIsNone(self.manager.store.credential(b"credential-two"))

    def test_recovery_freezes_old_passkey_and_revokes_pending_login(self) -> None:
        codes = self.manager.store.register_credential(
            user_id=b"u" * 32,
            credential_id=b"credential-one",
            public_key=b"public-key",
            sign_count=1,
            device_type="single_device",
            backed_up=False,
            initial=True,
        )
        _, stale_flow = self.manager.start_login()
        recovery_session = self.manager.recover(codes[0])
        self.assertTrue(self.manager.status(recovery_session.token)["recovery_pending"])
        with self.assertRaises(AuthError) as blocked:
            self.manager.start_login()
        self.assertEqual(blocked.exception.code, "RECOVERY_IN_PROGRESS")
        verified = SimpleNamespace(
            credential_id=b"credential-one",
            new_sign_count=2,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )
        with patch("server.auth.verify_authentication_response", return_value=verified):
            with self.assertRaises(AuthError) as revoked:
                self.manager.finish_login(stale_flow, {"id": b64url(b"credential-one")})
        self.assertEqual(revoked.exception.code, "AUTH_CEREMONY_REVOKED")
        self.assertFalse(self.manager.status(None)["authenticated"])


if __name__ == "__main__":
    unittest.main()
