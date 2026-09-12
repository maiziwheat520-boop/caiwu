from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from server.app import (
    LOCAL_CORE_BASE_URL,
    _build_local_single_user_state,
    _verify_local_scope,
    create_server,
    run,
)
from server.core_backend import CoreBackendError, CoreHttpClient

ENTITY_REF = "10000000-0000-4000-8000-000000000001"


def _local_env(**overrides: str) -> dict[str, str]:
    """A minimal environment that starts local mode, before the test breaks it."""
    env = {
        "LEDGERBRIDGE_MODE": "local-single-user",
        "BIND_ADDRESS": "127.0.0.1",
        "PORT": "0",
        "CORE_ENTITY_REF": ENTITY_REF,
        "CORE_BUSINESS_UNIT_REF": "unit-local",
    }
    env.update(overrides)
    return {name: value for name, value in env.items() if value}


class LocalSingleUserBindTests(unittest.TestCase):
    """The bind address is this mode's only access control, so it is tested first."""

    def setUp(self) -> None:
        env = patch.dict("os.environ", _local_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_every_interface_is_refused(self) -> None:
        for address in ("0.0.0.0", "::", "192.168.1.20", "example.invalid"):
            with self.subTest(address=address):
                with self.assertRaisesRegex(SystemExit, "non-loopback BIND_ADDRESS"):
                    _build_local_single_user_state(address)

    def test_empty_bind_address_is_refused(self) -> None:
        # An empty BIND_ADDRESS is every interface, not a missing value, so it
        # must fail for the same reason 0.0.0.0 does.
        with self.assertRaisesRegex(SystemExit, "every interface"):
            _build_local_single_user_state("")

    def test_loopback_forms_are_accepted(self) -> None:
        for address in ("127.0.0.1", "127.0.0.53", "::1", "localhost"):
            with self.subTest(address=address):
                state = _build_local_single_user_state(address)
                self.assertEqual(state.entity_ref, ENTITY_REF)

    def test_run_refuses_a_non_loopback_bind(self) -> None:
        with tempfile.TemporaryDirectory() as site_root:
            Path(site_root, "index.html").write_text("<main>local</main>", encoding="utf-8")
            env = _local_env(BIND_ADDRESS="0.0.0.0", SITE_ROOT=site_root)
            with patch.dict("os.environ", env, clear=True):
                with self.assertRaisesRegex(SystemExit, "non-loopback BIND_ADDRESS"):
                    run()


class LocalSingleUserSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict("os.environ", _local_env(), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_deployed_path_settings_are_refused_rather_than_ignored(self) -> None:
        for name in (
            "CORE_CA_FILE",
            "CORE_CERT_FILE",
            "CORE_KEY_FILE",
            "CORE_USER_ASSERTION_KEY",
            "CORE_WORKLOAD_PRINCIPAL",
            "CORE_POLICY_GENERATION",
            "TRUSTED_PROXY_CIDRS",
        ):
            with self.subTest(name=name):
                with patch.dict("os.environ", {name: "set-by-operator"}):
                    with self.assertRaisesRegex(SystemExit, name):
                        _build_local_single_user_state("127.0.0.1")

    def test_payroll_commands_are_refused(self) -> None:
        with patch.dict("os.environ", {"PAYROLL_COMMANDS_ENABLED": "1"}):
            with self.assertRaisesRegex(SystemExit, "payroll commands"):
                _build_local_single_user_state("127.0.0.1")

    def test_missing_scope_is_refused(self) -> None:
        for name in ("CORE_ENTITY_REF", "CORE_BUSINESS_UNIT_REF"):
            with self.subTest(name=name):
                with patch.dict("os.environ", {name: ""}):
                    with self.assertRaisesRegex(SystemExit, name):
                        _build_local_single_user_state("127.0.0.1")

    def test_a_remote_core_base_url_is_refused(self) -> None:
        for base_url in ("http://ledger.example.com:8661", "https://127.0.0.1:8661"):
            with self.subTest(base_url=base_url):
                with patch.dict("os.environ", {"CORE_BASE_URL": base_url}):
                    with self.assertRaisesRegex(SystemExit, "invalid Core settings"):
                        _build_local_single_user_state("127.0.0.1")

    def test_the_default_core_base_url_is_local_core(self) -> None:
        self.assertEqual(LOCAL_CORE_BASE_URL, "http://127.0.0.1:8661")
        state = _build_local_single_user_state("127.0.0.1")
        self.assertTrue(state.local_session)


class PlaintextCoreClientTests(unittest.TestCase):
    def test_plaintext_requires_http_on_loopback(self) -> None:
        for base_url in (
            "https://127.0.0.1:8661",
            "http://core.example.com:8661",
            "http://127.0.0.1:8661/internal",
            "http://user:pw@127.0.0.1:8661",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    CoreHttpClient(base_url=base_url, plaintext_loopback=True)

    def test_plaintext_refuses_tls_material(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot carry TLS material"):
            CoreHttpClient(
                base_url=LOCAL_CORE_BASE_URL,
                certificate_file="core.crt",
                private_key_file="core.key",
                plaintext_loopback=True,
            )

    def test_mtls_still_requires_certificates(self) -> None:
        # Without this the new optional arguments would let the deployed path
        # start with no client certificate at all.
        with self.assertRaisesRegex(ValueError, "requires a CA, a certificate and a key"):
            CoreHttpClient(base_url="https://core.internal:8445")

    def test_plaintext_client_is_bounded_to_the_internal_surface(self) -> None:
        client = CoreHttpClient(base_url=LOCAL_CORE_BASE_URL, plaintext_loopback=True)
        with self.assertRaises(ValueError):
            client.json("GET", "/admin/v1/anything")


class LocalSingleUserServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        Path(self.temp_dir.name, "index.html").write_text("<main>local</main>", encoding="utf-8")
        env = patch.dict("os.environ", _local_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.state = _build_local_single_user_state("127.0.0.1")
        # No TRUSTED_PROXY_CIDRS: local mode has no reverse proxy in front of it,
        # and requiring one would be a setting with nothing behind it.
        self.server = create_server(
            "127.0.0.1",
            0,
            self.temp_dir.name,
            state=self.state,
            mode="local-single-user",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def test_session_is_served_without_a_passkey(self) -> None:
        response = urllib.request.urlopen(f"{self.base_url}/api/v1/session", timeout=2)
        payload = json.load(response)
        self.assertEqual(payload["runtime_mode"], "local-single-user")
        self.assertEqual(payload["principal"], "local-single-user")
        self.assertTrue(payload["csrf_token"])

    def test_the_session_cookie_is_not_secure_on_loopback(self) -> None:
        # A Secure cookie would never come back over plain HTTP, so the session
        # would silently never establish.
        response = urllib.request.urlopen(f"{self.base_url}/api/v1/session", timeout=2)
        cookie = response.headers["Set-Cookie"]
        self.assertTrue(cookie.startswith("ledgerbridge_local_session="))
        self.assertNotIn("; Secure", cookie)
        self.assertIn("HttpOnly", cookie)

    def test_the_served_mode_is_announced(self) -> None:
        response = urllib.request.urlopen(f"{self.base_url}/api/v1/session", timeout=2)
        self.assertEqual(response.headers["X-LedgerBridge-Mode"], "local-single-user")


class CoreBackedSessionUnchangedTests(unittest.TestCase):
    def test_core_backed_still_refuses_to_mint_its_own_session(self) -> None:
        # local_session defaults off, so the deployed path keeps requiring the
        # Passkey AuthManager for a browser session.
        with patch.dict("os.environ", _local_env(), clear=True):
            state = _build_local_single_user_state("127.0.0.1")
        state.local_session = False
        self.assertFalse(state.session_active())
        with self.assertRaises(CoreBackendError) as caught:
            state.session_payload()
        self.assertEqual(caught.exception.status, 503)


class LocalSingleUserScopeTests(unittest.TestCase):
    """A book that does not exist must be a refusal, never an empty queue."""

    def _state(self, business_unit_ref: str = "unit-local"):
        env = _local_env(CORE_BUSINESS_UNIT_REF=business_unit_ref)
        with patch.dict("os.environ", env, clear=True):
            return _build_local_single_user_state("127.0.0.1")

    @staticmethod
    def _dimensions(*refs: str) -> dict[str, object]:
        return {
            "contract_version": "ledgerbridge.accounting-dimensions.v1",
            "business_units": [{"ref": ref, "label": ref} for ref in refs],
            "categories": [],
        }

    def test_a_book_core_knows_is_accepted(self) -> None:
        state = self._state()
        with patch.object(
            type(state),
            "accounting_dimensions",
            return_value=self._dimensions("unit-local", "unit-other"),
        ):
            _verify_local_scope(state)

    def test_a_uuid_where_the_stable_ref_belongs_is_refused(self) -> None:
        # The commonest mistake: CORE_ENTITY_REF is a UUID, so this looks like
        # one too. Core answers every read and returns nothing, which reads on
        # screen as a book with nothing left to review.
        state = self._state("9eaf7bd5-2115-e3b4-d237-c603d265452c")
        with patch.object(
            type(state),
            "accounting_dimensions",
            return_value=self._dimensions("book-08"),
        ):
            with self.assertRaisesRegex(SystemExit, "not a book of this entity"):
                _verify_local_scope(state)

    def test_the_refusal_names_the_books_that_do_exist(self) -> None:
        state = self._state("wrong")
        with patch.object(
            type(state),
            "accounting_dimensions",
            return_value=self._dimensions("book-08", "book-03"),
        ):
            with self.assertRaises(SystemExit) as caught:
                _verify_local_scope(state)
        self.assertIn("book-08", str(caught.exception))
        self.assertIn("book-03", str(caught.exception))

    def test_an_entity_with_no_books_is_refused_rather_than_served_empty(self) -> None:
        state = self._state()
        with patch.object(
            type(state), "accounting_dimensions", return_value=self._dimensions()
        ):
            with self.assertRaisesRegex(SystemExit, r"\(none\)"):
                _verify_local_scope(state)

    def test_core_being_down_is_refused_at_startup_not_shown_as_a_blank_page(self) -> None:
        state = self._state()
        with patch.object(
            type(state),
            "accounting_dimensions",
            side_effect=CoreBackendError(503, {"status": 503}),
        ):
            with self.assertRaisesRegex(SystemExit, "local Core did not answer"):
                _verify_local_scope(state)


if __name__ == "__main__":
    unittest.main()
