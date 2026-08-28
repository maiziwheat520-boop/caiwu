from __future__ import annotations

import base64
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.auth import AuthError, AuthManager, AuthStore, FailureGate, RECOVERY_CODE_COUNT


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
    caller_key = "ip:192.0.2.10"

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
            self.manager.start_registration("wrong", None, caller_key=self.caller_key)
        self.assertEqual(wrong.exception.code, "SETUP_CODE_INVALID")
        options, flow = self.manager.start_registration(self.setup_code, None, caller_key=self.caller_key)
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
        _, flow = self.manager.start_login(caller_key=self.caller_key)
        verified = SimpleNamespace(
            credential_id=b"credential-one",
            new_sign_count=2,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )
        with patch("server.auth.verify_authentication_response", return_value=verified):
            passkey_session = self.manager.finish_login(flow, {"id": b64url(b"credential-one")})
        self.assertEqual(self.manager.store.credential(b"credential-one").sign_count, 2)  # type: ignore[union-attr]
        recovery_session = self.manager.recover(codes[0], caller_key=self.caller_key)
        self.assertIsNone(self.manager.store.session_payload(passkey_session.token, rotate_csrf=False))
        self.assertIsNotNone(self.manager.store.session_payload(recovery_session.token, rotate_csrf=False))
        self.assertFalse(self.manager.status(recovery_session.token)["authenticated"])
        self.assertTrue(self.manager.status(recovery_session.token)["recovery_setup_required"])
        self.assertIsNone(self.manager.session_payload(recovery_session.token))
        _, recovery_flow = self.manager.start_registration("", recovery_session.token, caller_key=self.caller_key)
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

    def test_authenticated_step_up_appends_independent_passkey(self) -> None:
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
        authorization_options, authorization_flow = self.manager.start_passkey_addition_authorization(
            session.token,
            caller_key=self.caller_key,
        )
        self.assertEqual(len(authorization_options["allowCredentials"]), 1)  # type: ignore[arg-type]
        authorization = SimpleNamespace(
            credential_id=b"credential-one",
            new_sign_count=2,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )
        with patch("server.auth.verify_authentication_response", return_value=authorization):
            registration_options, registration_flow = self.manager.finish_passkey_addition_authorization(
                authorization_flow,
                {"id": b64url(b"credential-one")},
                session_token=session.token,
            )
        self.assertEqual(len(registration_options["excludeCredentials"]), 1)  # type: ignore[arg-type]
        self.assertEqual(self.manager.store.credential(b"credential-one").sign_count, 2)  # type: ignore[union-attr]

        registration = SimpleNamespace(
            credential_id=b"credential-two",
            credential_public_key=b"public-key-two",
            sign_count=0,
            credential_device_type=SimpleNamespace(value="single_device"),
            credential_backed_up=False,
        )
        with patch("server.auth.verify_registration_response", return_value=registration):
            count = self.manager.finish_passkey_addition(
                registration_flow,
                {"id": b64url(b"credential-two")},
                session_token=session.token,
            )
        self.assertEqual(count, 2)
        self.assertIsNotNone(self.manager.store.credential(b"credential-one"))
        self.assertIsNotNone(self.manager.store.credential(b"credential-two"))
        self.assertIsNotNone(self.manager.store.session_payload(session.token, rotate_csrf=False))
        self.assertTrue(self.manager.store.consume_recovery_code(recovery_codes[0]))
        with self.assertRaises(AuthError) as replay:
            self.manager.finish_passkey_addition(
                registration_flow,
                {"id": b64url(b"credential-two")},
                session_token=session.token,
            )
        self.assertEqual(replay.exception.code, "AUTH_CEREMONY_EXPIRED")

    def test_passkey_addition_is_bound_to_authorizing_session(self) -> None:
        self.manager.store.register_credential(
            user_id=b"u" * 32,
            credential_id=b"credential-one",
            public_key=b"public-key-one",
            sign_count=1,
            device_type="single_device",
            backed_up=False,
            initial=True,
        )
        authorizing_session = self.manager.store.create_session("passkey")
        other_session = self.manager.store.create_session("passkey")
        _, flow = self.manager.start_passkey_addition_authorization(
            authorizing_session.token,
            caller_key=self.caller_key,
        )
        with self.assertRaises(AuthError) as mismatched:
            self.manager.finish_passkey_addition_authorization(
                flow,
                {"id": b64url(b"credential-one")},
                session_token=other_session.token,
            )
        self.assertEqual(mismatched.exception.code, "AUTH_CEREMONY_REVOKED")

    def test_expired_setup_code_fails_closed(self) -> None:
        manager = AuthManager(
            AuthStore(self.database),
            rp_id="ledgerbridge.example.ts.net",
            expected_origin="https://ledgerbridge.example.ts.net",
            setup_code_sha256="0" * 64,
            setup_code_expires_at=0,
        )
        with self.assertRaises(AuthError) as expired:
            manager.start_registration("anything", None, caller_key=self.caller_key)
        self.assertEqual(expired.exception.code, "SETUP_CODE_EXPIRED")

    def test_initial_registration_cannot_finish_after_setup_code_expires(self) -> None:
        _, flow = self.manager.start_registration(self.setup_code, None, caller_key=self.caller_key)
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
        recovery_session = self.manager.recover(codes[0], caller_key=self.caller_key)
        _, flow = self.manager.start_registration("", recovery_session.token, caller_key=self.caller_key)
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
        _, stale_flow = self.manager.start_login(caller_key=self.caller_key)
        recovery_session = self.manager.recover(codes[0], caller_key=self.caller_key)
        self.assertTrue(self.manager.status(recovery_session.token)["recovery_pending"])
        with self.assertRaises(AuthError) as blocked:
            self.manager.start_login(caller_key=self.caller_key)
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

    def test_failure_throttles_are_isolated_by_resolved_caller(self) -> None:
        attacker = "ip:192.0.2.20"
        legitimate = "ip:192.0.2.21"
        for _ in range(5):
            with self.assertRaises(AuthError) as invalid:
                self.manager.start_registration("wrong", None, caller_key=attacker)
            self.assertEqual(invalid.exception.code, "SETUP_CODE_INVALID")
        with self.assertRaises(AuthError) as limited:
            self.manager.start_registration(self.setup_code, None, caller_key=attacker)
        self.assertEqual(limited.exception.code, "AUTH_RATE_LIMITED")
        _, setup_flow = self.manager.start_registration(self.setup_code, None, caller_key=legitimate)
        self.assertTrue(setup_flow)
        with self.assertRaises(AuthError) as still_limited:
            self.manager.start_registration(self.setup_code, None, caller_key=attacker)
        self.assertEqual(still_limited.exception.code, "AUTH_RATE_LIMITED")

        codes = self.manager.store.register_credential(
            user_id=b"u" * 32,
            credential_id=b"credential-one",
            public_key=b"public-key",
            sign_count=0,
            device_type="multi_device",
            backed_up=True,
            initial=True,
        )
        for _ in range(5):
            with self.assertRaises(AuthError) as invalid:
                self.manager.recover("wrong", caller_key=attacker)
            self.assertEqual(invalid.exception.code, "RECOVERY_CODE_INVALID")
        recovery_session = self.manager.recover(codes[0], caller_key=legitimate)
        self.assertTrue(recovery_session.token)

    def test_failure_gate_admission_is_atomic_and_bounded(self) -> None:
        gate = FailureGate(attempts=5, window_seconds=900)
        admitted: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(40)

        def attempt() -> None:
            barrier.wait()
            try:
                gate.begin_attempt("setup:ip:192.0.2.20")
            except AuthError:
                result = False
            else:
                result = True
            with lock:
                admitted.append(result)

        threads = [threading.Thread(target=attempt) for _ in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(admitted), 5)
        self.assertEqual(len(gate._failures["setup:ip:192.0.2.20"]), 5)

    def test_login_admission_is_per_caller_and_retry_replaces_abandoned_flow(self) -> None:
        self.manager.store.register_credential(
            user_id=b"u" * 32,
            credential_id=b"credential-one",
            public_key=b"public-key",
            sign_count=0,
            device_type="multi_device",
            backed_up=True,
            initial=True,
        )
        attacker = "ip:192.0.2.20"
        for _ in range(5):
            self.manager.start_login(caller_key=attacker)
        with self.assertRaises(AuthError) as limited:
            self.manager.start_login(caller_key=attacker)
        self.assertEqual(limited.exception.code, "AUTH_RATE_LIMITED")

        legitimate = "ip:192.0.2.21"
        _, old_flow = self.manager.start_login(caller_key=legitimate)
        _, replacement_flow = self.manager.start_login(
            caller_key=legitimate,
            previous_flow_token=old_flow,
        )
        self.assertNotEqual(old_flow, replacement_flow)
        with self.assertRaises(AuthError) as replaced:
            self.manager.finish_login(old_flow, {"id": b64url(b"credential-one")})
        self.assertEqual(replaced.exception.code, "AUTH_CEREMONY_EXPIRED")

    def test_public_login_capacity_cannot_consume_recovery_registration_reserve(self) -> None:
        codes = self.manager.store.register_credential(
            user_id=b"u" * 32,
            credential_id=b"credential-one",
            public_key=b"public-key",
            sign_count=0,
            device_type="multi_device",
            backed_up=True,
            initial=True,
        )
        for index in range(120):
            self.manager.start_login(caller_key=f"ip:198.51.100.{index}")
        legitimate_flows = [
            self.manager.start_login(caller_key="ip:192.0.2.21")[1]
            for _ in range(5)
        ]
        self.assertTrue(legitimate_flows[-1])
        verified = SimpleNamespace(
            credential_id=b"credential-one",
            new_sign_count=1,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )
        with patch("server.auth.verify_authentication_response", return_value=verified):
            login_session = self.manager.finish_login(
                legitimate_flows[-1],
                {"id": b64url(b"credential-one")},
            )
        self.assertTrue(login_session.token)

        recovery_session = self.manager.recover(codes[0], caller_key="ip:192.0.2.21")
        _, recovery_flow = self.manager.start_registration(
            "",
            recovery_session.token,
            caller_key="ip:192.0.2.21",
        )
        self.assertTrue(recovery_flow)

    def test_mixed_full_capacity_still_admits_login_and_recovery_registration(self) -> None:
        registration_flows = [
            self.manager.start_registration(
                self.setup_code,
                None,
                caller_key=f"ip:192.0.2.{20 + (index // 5)}",
            )[1]
            for index in range(10)
        ]
        registered = SimpleNamespace(
            credential_id=b"credential-one",
            credential_public_key=b"public-key",
            sign_count=0,
            credential_device_type=SimpleNamespace(value="multi_device"),
            credential_backed_up=True,
        )
        with patch("server.auth.verify_registration_response", return_value=registered):
            _, codes = self.manager.finish_registration(
                registration_flows[0],
                {"id": b64url(b"credential-one")},
                setup_code=self.setup_code,
                session_token=None,
            )
        for index in range(119):
            self.manager.start_login(caller_key=f"ip:198.51.100.{index}")
        self.assertEqual(len(self.manager._ceremonies), 128)

        _, login_flow = self.manager.start_login(caller_key="ip:192.0.2.40")
        self.assertTrue(login_flow)
        self.assertEqual(len(self.manager._ceremonies), 128)
        recovery_session = self.manager.recover(codes[0], caller_key="ip:192.0.2.40")
        _, recovery_flow = self.manager.start_registration(
            "",
            recovery_session.token,
            caller_key="ip:192.0.2.40",
        )
        self.assertTrue(recovery_flow)
        self.assertEqual(len(self.manager._ceremonies), 128)


if __name__ == "__main__":
    unittest.main()
