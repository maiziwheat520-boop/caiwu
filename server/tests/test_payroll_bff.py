from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

from server.app import COOKIE_NAME, create_server
from server.core_backend import CoreBackedState, CoreBackendError


ENTITY_ID = "10000000-0000-4000-8000-000000000001"
ASSERTION_KEY = b"synthetic-web-core-assertion-key-0001"
MATERIAL_ID = "material_live_2026_08"
PROJECTION_REVISION = hashlib.sha256(b"payroll-live-7").hexdigest()
PROJECTION_ETAG = f'"{PROJECTION_REVISION}"'


class FakePayrollCoreClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.command_replays: dict[str, tuple[bytes, dict[str, object]]] = {}
        self.failure: CoreBackendError | None = None
        self.failure_method: str | None = None
        self.read_contract_version = "ledgerbridge.payroll-read.v1"
        self.response_entity_ref = ENTITY_ID
        self.payment_submission_supported = False
        self.live_data_ready = True
        self.core_commands_enabled = True
        self.status_data_updates: dict[str, object] = {}
        self.verification_evidence_updates: dict[str, object] = {}

    def json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, path, body, dict(headers or {})))
        if self.failure is not None and self.failure_method in {None, method}:
            raise self.failure
        if method == "POST" and path.startswith("/internal/v1/payroll/"):
            action = path.rsplit("/", 1)[-1]
            resource_ref = path.split("/")[-2]
            action_name = (
                "payroll.material.review"
                if path.endswith("/reviews")
                else f"payroll.batch.{action}"
            )
            response: dict[str, object] = {
                "contract_version": "ledgerbridge.payroll-command-result.v1",
                "entity_ref": ENTITY_ID,
                "company_id": "company_live_hotel",
                "action": action_name,
                "resource_ref": resource_ref,
                "replayed": False,
                "data": {
                    "schema_version": "payroll-ledgerbridge-command-receipt/v1",
                    "company_id": "company_live_hotel",
                    "resource_id": resource_ref,
                    "action": "payroll.receipts.verify",
                    "idempotency_key": (headers or {}).get("Idempotency-Key", ""),
                    "audit_event_id": "audit_verify_001",
                    "audit_hash": "c" * 64,
                    "occurred_at": "2026-08-30T08:00:00.000Z",
                    "replayed": False,
                    "audit_closure": {
                        "company_id": "company_live_hotel",
                        "resource_id": resource_ref,
                        "action": "payroll.receipts.verify",
                        "actor_subject": "ledgerbridge-owner",
                        "actor_id": "payroll_checker_001",
                        "audit_event_id": "audit_verify_001",
                        "audit_hash": "c" * 64,
                        "occurred_at": "2026-08-30T08:00:00.000Z",
                    },
                },
            }
            operation_id = (headers or {}).get("Idempotency-Key", "")
            existing = self.command_replays.get(operation_id)
            if existing is not None:
                if existing[0] != body:
                    raise CoreBackendError(
                        409,
                        {"code": "IDEMPOTENCY_CONFLICT", "status": 409},
                    )
                replay = json.loads(json.dumps(existing[1]))
                replay["replayed"] = True
                replay["data"]["replayed"] = True
                return replay
            self.command_replays[operation_id] = (body or b"", response)
            return response
        if method == "GET" and path.startswith("/internal/v1/payroll/"):
            data: dict[str, object]
            if path == "/internal/v1/payroll/status":
                data = {
                    "schema_version": "ledgerbridge.payroll-status.v1",
                    "live_data_ready": self.live_data_ready,
                    "live_projection_schema": "payroll-ledgerbridge-live-projection/v1",
                    "payment_operations_exposed": False,
                    "projection_revision": PROJECTION_REVISION,
                    "etag": PROJECTION_ETAG,
                    "setup_summary": {
                        "provider_connected": True,
                        "runtime_mode": "live-provider",
                        "unassigned_material_count": 0 if self.live_data_ready else 2,
                        "ready_material_count": 3 if self.live_data_ready else 0,
                        "company_mapped_material_count": 3 if self.live_data_ready else 1,
                        "blocking_reason_codes": []
                        if self.live_data_ready
                        else [
                            "UNASSIGNED_MATERIALS",
                            "MATERIAL_REVIEW_REQUIRED",
                            "PAYROLL_BATCH_REQUIRED",
                            "LIVE_DATA_NOT_READY",
                        ],
                    },
                    "capabilities": {
                        "commands_enabled": self.core_commands_enabled,
                        "allowed_actions": ["VERIFY_RECEIPTS"]
                        if self.core_commands_enabled
                        else [],
                    },
                }
                data.update(self.status_data_updates)
            elif path == "/internal/v1/payroll/dashboard":
                data = {
                    "schema_version": "ledgerbridge.payroll-dashboard.v1",
                    "projection_revision": PROJECTION_REVISION,
                    "etag": PROJECTION_ETAG,
                    "generated_at": "2026-08-30T08:00:00.000Z",
                    "live_data_ready": self.live_data_ready,
                    "setup_summary": {
                        "provider_connected": True,
                        "runtime_mode": "live-provider",
                        "unassigned_material_count": 0,
                        "ready_material_count": 1,
                        "company_mapped_material_count": 1,
                        "blocking_reason_codes": [],
                    },
                }
                if self.live_data_ready:
                    data["dashboard"] = {
                        "batch_count": 1,
                        "material_count": 1,
                        "materials_needing_review_count": 0,
                        "verification_attention_count": 0,
                        "unassigned_material_count": 0,
                        "net_pay_minor": 524000,
                    }
            elif path == "/internal/v1/payroll/materials":
                data = {
                    "schema_version": "ledgerbridge.payroll-material-list.v1",
                    "projection_revision": PROJECTION_REVISION,
                    "etag": PROJECTION_ETAG,
                    "generated_at": "2026-08-30T08:00:00.000Z",
                    "items": [
                        {
                            "company_id": "company_live_hotel",
                            "material_id": MATERIAL_ID,
                            "material_type": "PAYROLL_SHEET",
                            "period": "2026-08",
                            "status": "REVIEWED",
                            "review_revision": 1,
                            "payable": False,
                            "submission_supported": False,
                        }
                    ],
                }
            elif path == "/internal/v1/payroll/batches":
                data = {
                    "schema_version": "ledgerbridge.payroll-batch-list.v1",
                    "projection_revision": PROJECTION_REVISION,
                    "etag": PROJECTION_ETAG,
                    "generated_at": "2026-08-30T08:00:00.000Z",
                    "items": [
                        {
                            "company_id": "company_live_hotel",
                            "batch_id": "batch_live_2026_08",
                            "pay_period": "2026-08",
                            "revision": 7,
                            "status": "APPROVED",
                            "payable": False,
                            "submission_supported": False,
                            "payment_submission_supported": self.payment_submission_supported,
                            "lines": [
                                {
                                    "company_id": "company_live_hotel",
                                    "employee_id": "employee_live_001",
                                    "employee_display": "员*工",
                                    "account_id": "account_live_001",
                                    "account_display": "****1234",
                                    "net_pay_minor": 524000,
                                }
                            ],
                            "audit_closure": {
                                "audit_event_id": "audit_live_001",
                                "audit_hash": "b" * 64,
                            },
                        }
                    ],
                }
            elif path == "/internal/v1/payroll/verification":
                data = {
                    "schema_version": "ledgerbridge.payroll-verification-list.v1",
                    "projection_revision": PROJECTION_REVISION,
                    "etag": PROJECTION_ETAG,
                    "generated_at": "2026-08-30T08:00:00.000Z",
                    "items": [
                        {
                            "company_id": "company_live_hotel",
                            "verification_id": "verification_live_001",
                            "batch_id": "batch_live_2026_08",
                            "status": "MATCHED",
                            "source_artifact_ids": ["artifact_live_statement_2026_08"],
                            "results": [
                                {
                                    "company_id": "company_live_hotel",
                                    "employee_id": "employee_live_001",
                                    "employee_display": "员*工",
                                    "account_id": "account_live_001",
                                    "account_display": "****1234",
                                    "status": "MATCHED",
                                }
                            ],
                            "payable": False,
                            "submission_supported": False,
                            "payment_submission_supported": False,
                        }
                    ],
                }
                if path == "/internal/v1/payroll/verification":
                    evidence = {
                        "company_id": "company_live_hotel",
                        "artifact_id": "artifact_live_statement_2026_08",
                        "period": "2026-08",
                        "evidence_type": "BANK_RECEIPT",
                        "status": "READY_FOR_MATCHING",
                        "display_label": "BANK_RECEIPT · 2026-08",
                    }
                    evidence.update(self.verification_evidence_updates)
                    data["available_evidence"] = [evidence]
            else:
                raise AssertionError(f"unexpected Core payroll read: {path}")
            return {
                "contract_version": self.read_contract_version,
                "entity_ref": self.response_entity_ref,
                "company_id": "company_live_hotel",
                "data": data,
            }
        raise AssertionError(f"unexpected Core request: {method} {path}")


class FakeAuthStore:
    @staticmethod
    def validate_csrf(token: str, supplied: str) -> bool:
        return token == "session-token" and supplied == "csrf-token"


class FakeAuthManager:
    expected_origin = "https://ledgerbridge.test"
    store = FakeAuthStore()

    def __init__(self, *, session_subject: str = "ledgerbridge-owner") -> None:
        self.session_subject = session_subject

    @staticmethod
    def status(token: str | None) -> dict[str, object]:
        return {
            "authenticated": token == "session-token",
            "setup_required": False,
            "passkey_registered": True,
            "recovery_setup_required": False,
            "recovery_pending": False,
        }

    def session_payload(self, token: str | None) -> dict[str, str] | None:
        if token != "session-token":
            return None
        return {
            "principal": self.session_subject,
            "csrf_token": "csrf-token",
            "expires_at": "2026-08-30T12:00:00Z",
        }

    def payroll_session_subject(self, token: str | None) -> str | None:
        return self.session_subject if token == "session-token" else None


def build_state(
    client: FakePayrollCoreClient,
    *,
    payroll_commands_enabled: bool = False,
    payroll_roles: frozenset[str] = frozenset(),
) -> CoreBackedState:
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
        payroll_commands_enabled=payroll_commands_enabled,
        payroll_role_bindings={"ledgerbridge-owner": payroll_roles},
    )


class PayrollBffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakePayrollCoreClient()
        self.auth_manager = FakeAuthManager()
        self.temp_dir = tempfile.TemporaryDirectory()
        Path(self.temp_dir.name, "index.html").write_text("<main>review</main>", encoding="utf-8")
        self.server = create_server(
            "127.0.0.1",
            0,
            self.temp_dir.name,
            state=build_state(self.client),
            auth_manager=self.auth_manager,  # type: ignore[arg-type]
            mode="core-backed",
            trusted_proxy_cidrs="127.0.0.1/32",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def post_payroll(
        self,
        path: str,
        body: dict[str, object],
        *,
        idempotency_key: str | None = None,
        csrf: str = "csrf-token",
        authenticated: bool = True,
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {
            "Origin": "https://ledgerbridge.test",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": idempotency_key or "70000000-0000-4000-8000-000000000001",
        }
        if authenticated:
            headers["Cookie"] = f"{COOKIE_NAME}=session-token"
        connection.request("POST", path, body=json.dumps(body).encode("utf-8"), headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        status = response.status
        connection.close()
        return status, payload

    def test_authenticated_session_reads_truthful_payroll_status(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/status",
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)

        self.assertTrue(payload["data"]["live_data_ready"])
        self.assertNotIn("provider", payload["data"])
        self.assertEqual(payload["data"]["projection_revision"], PROJECTION_REVISION)
        self.assertEqual(payload["data"]["etag"], PROJECTION_ETAG)
        self.assertEqual(
            payload["data"]["capabilities"],
            {"commands_enabled": False, "allowed_actions": []},
        )
        self.assertEqual(len(self.client.calls), 1)
        method, path, body, headers = self.client.calls[0]
        self.assertEqual((method, path, body), ("GET", "/internal/v1/payroll/status", None))
        version, encoded, signature = headers["X-LedgerBridge-User-Assertion"].split(".")
        self.assertEqual(version, "v1")
        expected_signature = hmac.new(
            ASSERTION_KEY,
            f"v1.{encoded}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        supplied_signature = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        self.assertTrue(hmac.compare_digest(expected_signature, supplied_signature))
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(claims["version"], "ledgerbridge.payroll-bff-user-assertion.v1")
        self.assertEqual(claims["subject"], "ledgerbridge-owner")
        self.assertEqual(claims["entity_ref"], ENTITY_ID)
        self.assertEqual(claims["action"], "payroll.status.read")
        self.assertEqual(claims["resource_ref"], "payroll-status")
        self.assertEqual(claims["method"], "GET")
        self.assertEqual(claims["canonical_path"], path)
        self.assertEqual(claims["body_sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(len(claims["session_ref"]), 64)
        self.assertIsNone(claims["expected_revision"])
        self.assertIsNone(claims["operation_id"])
        self.assertNotIn("session-token", json.dumps(claims))
        self.assertNotIn("required_role", claims)

    def test_status_exposes_only_safe_setup_summary_when_live_data_is_not_ready(self) -> None:
        self.client.live_data_ready = False
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/status",
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)

        self.assertFalse(payload["data"]["live_data_ready"])
        self.assertEqual(
            payload["data"]["setup_summary"],
            {
                "provider_connected": True,
                "runtime_mode": "live-provider",
                "unassigned_material_count": 2,
                "ready_material_count": 0,
                "company_mapped_material_count": 1,
                "blocking_reason_codes": [
                    "UNASSIGNED_MATERIALS",
                    "MATERIAL_REVIEW_REQUIRED",
                    "PAYROLL_BATCH_REQUIRED",
                    "LIVE_DATA_NOT_READY",
                ],
            },
        )
        self.assertEqual(
            payload["data"]["capabilities"],
            {"commands_enabled": False, "allowed_actions": []},
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("employee_name", "filename", "file_path", "account_number"):
            self.assertNotIn(forbidden, serialized)

    def test_status_fails_closed_on_unknown_or_malformed_setup_projection_fields(self) -> None:
        cases: list[dict[str, object]] = [
            {"filename": "unsafe-provider-upload.xlsx"},
            {"projection_revision": 7},
            {
                "setup_summary": {
                    "provider_connected": True,
                    "runtime_mode": "live-provider",
                    "unassigned_material_count": 2,
                    "ready_material_count": 0,
                    "company_mapped_material_count": 1,
                    "blocking_reason_codes": [
                        "LIVE_DATA_NOT_READY",
                        "UNASSIGNED_MATERIALS",
                    ],
                }
            },
            {
                "setup_summary": {
                    "provider_connected": True,
                    "runtime_mode": "live-provider",
                    "unassigned_material_count": -1,
                    "ready_material_count": 0,
                    "company_mapped_material_count": 1,
                    "blocking_reason_codes": ["LIVE_DATA_NOT_READY"],
                }
            },
            {
                "capabilities": {
                    "commands_enabled": False,
                    "allowed_actions": ["VERIFY_RECEIPTS"],
                }
            },
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                self.client.status_data_updates = updates
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/status",
                    headers={"Cookie": f"{COOKIE_NAME}=session-token"},
                )
                with self.assertRaises(HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(raised.exception.code, 503)
                self.assertEqual(json.load(raised.exception)["code"], "CORE_CONTRACT_INVALID")

    def test_payroll_read_rejects_browser_supplied_scope_before_core(self) -> None:
        for query in (
            "company_id=company_attacker",
            "actor_id=attacker",
            "role=approver",
            "payment_submission_allowed=false",
        ):
            with self.subTest(query=query):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/status?{query}",
                    headers={"Cookie": f"{COOKIE_NAME}=session-token"},
                )
                with self.assertRaises(HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(raised.exception.code, 400)
                problem = json.load(raised.exception)
                self.assertEqual(problem["code"], "INVALID_PAYROLL_QUERY")

        self.assertEqual(self.client.calls, [])

    def test_payroll_rejects_authenticated_session_subject_mismatch(self) -> None:
        self.auth_manager.session_subject = "different-ledgerbridge-user"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/status",
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with self.assertRaises(HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 403)
        problem = json.load(raised.exception)
        self.assertEqual(problem["code"], "PAYROLL_SESSION_SCOPE_MISMATCH")
        self.assertEqual(self.client.calls, [])

    def test_payroll_requires_one_server_controlled_entity_selection(self) -> None:
        self.server.state.entity_ref = ""  # type: ignore[attr-defined]
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/status",
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with self.assertRaises(HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 409)
        problem = json.load(raised.exception)
        self.assertEqual(problem["code"], "ENTITY_SELECTION_REQUIRED")
        self.assertEqual(self.client.calls, [])

    def test_reads_only_versioned_core_payroll_views_with_session_bound_actions(self) -> None:
        cases = [
            ("/api/v1/payroll/dashboard", "/internal/v1/payroll/dashboard", "payroll.dashboard.read", "payroll-dashboard"),
            ("/api/v1/payroll/materials", "/internal/v1/payroll/materials", "payroll.materials.list", "payroll-materials"),
            ("/api/v1/payroll/batches", "/internal/v1/payroll/batches", "payroll.batches.list", "payroll-batches"),
            (
                "/api/v1/payroll/verification",
                "/internal/v1/payroll/verification",
                "payroll.verification.list",
                "payroll-verification",
            ),
        ]

        for public_path, core_path, expected_action, expected_resource in cases:
            with self.subTest(public_path=public_path):
                before = len(self.client.calls)
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.server.server_port}{public_path}",
                    headers={"Cookie": f"{COOKIE_NAME}=session-token"},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    payload = json.load(response)
                self.assertEqual(payload["contract_version"], "ledgerbridge.payroll-read.v1")
                self.assertEqual(payload["entity_ref"], ENTITY_ID)
                self.assertEqual(payload["company_id"], "company_live_hotel")
                serialized = json.dumps(payload["data"])
                self.assertNotIn('"payable": true', serialized.lower())
                self.assertNotIn('"payment_submission_supported": true', serialized.lower())
                self.assertEqual(payload["data"]["projection_revision"], PROJECTION_REVISION)
                self.assertEqual(payload["data"]["etag"], PROJECTION_ETAG)

                self.assertEqual(len(self.client.calls), before + 1)
                method, actual_path, body, headers = self.client.calls[-1]
                self.assertEqual((method, actual_path, body), ("GET", core_path, None))
                _, encoded, _ = headers["X-LedgerBridge-User-Assertion"].split(".")
                claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
                self.assertEqual(claims["action"], expected_action)
                self.assertEqual(claims["resource_ref"], expected_resource)
                self.assertEqual(claims["canonical_path"], core_path)
                self.assertIsNone(claims["expected_revision"])
                self.assertIsNone(claims["operation_id"])
                if public_path == "/api/v1/payroll/verification":
                    self.assertEqual(
                        payload["data"]["available_evidence"],
                        [
                            {
                                "company_id": "company_live_hotel",
                                "artifact_id": "artifact_live_statement_2026_08",
                                "period": "2026-08",
                                "evidence_type": "BANK_RECEIPT",
                                "status": "READY_FOR_MATCHING",
                                "display_label": "BANK_RECEIPT · 2026-08",
                            }
                        ],
                    )

    def test_material_detail_read_is_not_a_public_bff_route(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/materials/{MATERIAL_ID}",
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with self.assertRaises(HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(json.load(raised.exception)["code"], "API_ROUTE_NOT_FOUND")
        self.assertEqual(self.client.calls, [])

    def test_only_verify_command_is_exposed_and_it_defaults_unavailable(self) -> None:
        status, problem = self.post_payroll(
            "/api/v1/payroll/batches/batch_live_2026_08/submit-review",
            {"expected_revision": 7},
        )
        self.assertEqual((status, problem["code"]), (404, "API_ROUTE_NOT_FOUND"))

        status, problem = self.post_payroll(
            "/api/v1/payroll/batches/batch_live_2026_08/verify-receipts",
            {
                "expected_revision": 7,
                "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
                "source_artifact_ids": ["artifact_live_statement_2026_08"],
            },
        )

        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "PAYROLL_COMMAND_UNAVAILABLE")
        self.assertEqual(self.client.calls, [])

    def test_payroll_read_fails_closed_on_unknown_version_scope_or_payment_mode(self) -> None:
        cases = [
            ("version", "CORE_CONTRACT_INVALID"),
            ("scope", "CORE_CONTRACT_INVALID"),
            ("payment", "PAYROLL_PAYMENT_MODE_NOT_ALLOWED"),
        ]
        for kind, expected_code in cases:
            with self.subTest(kind=kind):
                self.client.read_contract_version = "ledgerbridge.payroll-read.v1"
                self.client.response_entity_ref = ENTITY_ID
                self.client.payment_submission_supported = False
                if kind == "version":
                    self.client.read_contract_version = "ledgerbridge.payroll-read.v2"
                elif kind == "scope":
                    self.client.response_entity_ref = "10000000-0000-4000-8000-000000000099"
                else:
                    self.client.payment_submission_supported = True
                view = "batches" if kind == "payment" else "dashboard"
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/{view}",
                    headers={"Cookie": f"{COOKIE_NAME}=session-token"},
                )
                with self.assertRaises(HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(raised.exception.code, 503)
                self.assertEqual(json.load(raised.exception)["code"], expected_code)

    def test_payroll_core_unavailable_error_is_preserved_without_fake_data(self) -> None:
        self.client.failure = CoreBackendError(
            503,
            {"type": "about:blank", "title": "unavailable", "status": 503, "code": "PAYROLL_PROVIDER_UNAVAILABLE"},
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/dashboard",
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with self.assertRaises(HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        problem = json.load(raised.exception)
        self.assertEqual(raised.exception.code, 503)
        self.assertEqual(problem["code"], "PAYROLL_PROVIDER_UNAVAILABLE")
        self.assertNotIn("items", problem)

    def test_verification_view_rejects_uncontrolled_evidence_display_text(self) -> None:
        self.client.verification_evidence_updates = {
            "display_label": "2026-08 employee-bank-statement.xlsx"
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/verification",
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with self.assertRaises(HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 503)
        self.assertEqual(json.load(raised.exception)["code"], "CORE_CONTRACT_INVALID")


class EnabledPayrollCommandBffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakePayrollCoreClient()
        self.auth_manager = FakeAuthManager()
        self.temp_dir = tempfile.TemporaryDirectory()
        Path(self.temp_dir.name, "index.html").write_text("<main>review</main>", encoding="utf-8")
        self._start_server(frozenset({"checker"}))

    def _start_server(self, roles: frozenset[str]) -> None:
        self.server = create_server(
            "127.0.0.1",
            0,
            self.temp_dir.name,
            state=build_state(
                self.client,
                payroll_commands_enabled=True,
                payroll_roles=roles,
            ),
            auth_manager=self.auth_manager,  # type: ignore[arg-type]
            mode="core-backed",
            trusted_proxy_cidrs="127.0.0.1/32",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _restart_server(self, roles: frozenset[str]) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._start_server(roles)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def post(
        self,
        path: str,
        body: dict[str, object],
        *,
        authenticated: bool = True,
        csrf: str = "csrf-token",
        idempotency_key: str = "70000000-0000-4000-8000-000000000002",
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {
            "Origin": "https://ledgerbridge.test",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": idempotency_key,
        }
        if authenticated:
            headers["Cookie"] = f"{COOKIE_NAME}=session-token"
        connection.request(
            "POST",
            path,
            body=json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        status = response.status
        connection.close()
        return status, payload

    def test_non_verification_commands_are_not_public_routes(self) -> None:
        cases = [
            (f"/api/v1/payroll/materials/{MATERIAL_ID}/review", {"expected_review_revision": 0}),
            ("/api/v1/payroll/batches/batch_live_2026_08/submit-review", {"expected_revision": 7}),
            ("/api/v1/payroll/batches/batch_live_2026_08/review", {"expected_revision": 7}),
            ("/api/v1/payroll/batches/batch_live_2026_08/approve", {"expected_revision": 7}),
        ]
        for path, body in cases:
            with self.subTest(path=path):
                status, problem = self.post(path, body)
                self.assertEqual((status, problem["code"]), (404, "API_ROUTE_NOT_FOUND"))
        self.assertEqual(self.client.calls, [])

    def test_status_advertises_only_core_and_web_authorized_capability_intersection(self) -> None:
        self._restart_server(frozenset({"checker"}))
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/status",
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(
            payload["data"]["capabilities"],
            {"commands_enabled": True, "allowed_actions": ["VERIFY_RECEIPTS"]},
        )

        self.client.calls.clear()
        self.client.core_commands_enabled = False
        status, problem = self.post(
            "/api/v1/payroll/batches/batch_live_2026_08/verify-receipts",
            {
                "expected_revision": 7,
                "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
                "source_artifact_ids": ["artifact_live_statement_2026_08"],
            },
        )
        self.assertEqual((status, problem["code"]), (403, "PAYROLL_ACTION_NOT_AUTHORIZED"))
        self.assertEqual(
            [(method, core_path) for method, core_path, _, _ in self.client.calls],
            [("GET", "/internal/v1/payroll/status")],
        )

    def test_checker_verifies_only_explicit_nonempty_projection_evidence(self) -> None:
        self._restart_server(frozenset({"checker"}))
        path = "/api/v1/payroll/batches/batch_live_2026_08/verify-receipts"
        status, problem = self.post(
            path,
            {
                "expected_revision": 7,
                "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
                "source_artifact_ids": [],
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(problem["code"], "VERIFICATION_EVIDENCE_REQUIRED")
        self.assertEqual(self.client.calls, [])

        status, problem = self.post(
            path,
            {
                "expected_revision": 7,
                "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
                "source_artifact_ids": ["artifact_live_statement_2026_08"],
                "receipts": [{"receipt_id": "receipt_demo_fake"}],
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(problem["code"], "INVALID_PAYROLL_COMMAND")
        self.assertEqual(self.client.calls, [])

        status, problem = self.post(
            path,
            {
                "expected_revision": 7,
                "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
                "source_artifact_ids": ["artifact_live_not_in_current_projection"],
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(problem["code"], "PAYROLL_VERIFICATION_EVIDENCE_NOT_AVAILABLE")
        self.assertEqual(
            [(method, core_path) for method, core_path, _, _ in self.client.calls],
            [
                ("GET", "/internal/v1/payroll/status"),
                ("GET", "/internal/v1/payroll/verification"),
            ],
        )
        self.assertFalse(any(call[0] == "POST" for call in self.client.calls))
        self.client.calls.clear()

        status, problem = self.post(
            path,
            {
                "expected_revision": 7,
                "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
                "source_artifact_ids": ["artifact_demo_2026_08"],
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(problem["code"], "INVALID_PAYROLL_VERIFICATION_EVIDENCE")
        self.assertEqual(self.client.calls, [])

        self.client.verification_evidence_updates = {
            "display_label": "2026-08 employee-bank-statement.xlsx"
        }
        status, problem = self.post(
            path,
            {
                "expected_revision": 7,
                "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
                "source_artifact_ids": ["artifact_live_statement_2026_08"],
            },
        )
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "CORE_CONTRACT_INVALID")
        self.assertEqual(len(self.client.calls), 2)
        self.assertFalse(any(call[0] == "POST" for call in self.client.calls))
        self.client.verification_evidence_updates = {}
        self.client.calls.clear()

        status, payload = self.post(
            path,
            {
                "expected_revision": 7,
                "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
                "source_artifact_ids": ["artifact_live_statement_2026_08"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["action"], "payroll.batch.verify-receipts")
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(self.client.calls[0][1], "/internal/v1/payroll/status")
        self.assertEqual(self.client.calls[1][1], "/internal/v1/payroll/verification")
        method, core_path, body, headers = next(
            call for call in self.client.calls if call[0] == "POST"
        )
        self.assertEqual(method, "POST")
        self.assertEqual(
            core_path,
            "/internal/v1/payroll/batches/batch_live_2026_08/verify-receipts",
        )
        self.assertEqual(
            json.loads(body or b"{}"),
            {
                "contract_version": "ledgerbridge.payroll-receipt-verification-command.v1",
                "expected_revision": 7,
                "explicitly_confirmed": True,
                "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
                "source_artifact_ids": ["artifact_live_statement_2026_08"],
            },
        )
        _, encoded, _ = headers["X-LedgerBridge-User-Assertion"].split(".")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(claims["action"], "payroll.batch.verify-receipts")
        self.assertEqual(claims["expected_revision"], 7)

    def test_verification_role_is_server_bound_and_core_errors_are_not_relabelled_success(self) -> None:
        self._restart_server(frozenset({"maker"}))
        request = {
            "expected_revision": 7,
            "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
            "source_artifact_ids": ["artifact_live_statement_2026_08"],
        }
        status, problem = self.post(
            "/api/v1/payroll/batches/batch_live_2026_08/verify-receipts",
            request,
        )
        self.assertEqual(status, 403)
        self.assertEqual(problem["code"], "PAYROLL_ROLE_NOT_AUTHORIZED")
        self.assertEqual(self.client.calls, [])

        self._restart_server(frozenset({"checker"}))
        self.client.failure = CoreBackendError(
            409,
            {"code": "VERSION_CONFLICT", "status": 409, "recovery": "refresh"},
        )
        self.client.failure_method = "POST"
        status, problem = self.post(
            "/api/v1/payroll/batches/batch_live_2026_08/verify-receipts",
            request,
        )
        self.assertEqual(status, 409)
        self.assertEqual(problem["code"], "VERSION_CONFLICT")
        self.assertNotIn("replayed", problem)

    def test_browser_identity_payment_and_freeform_fields_never_reach_core(self) -> None:
        forbidden = {
            "company_id": "company_attacker",
            "actor_id": "attacker",
            "role": "approver",
            "payment_submission_allowed": False,
            "note": "browser supplied",
            "explicitly_confirmed": True,
        }
        for field, value in forbidden.items():
            with self.subTest(field=field):
                status, problem = self.post(
                    "/api/v1/payroll/batches/batch_live_2026_08/verify-receipts",
                    {
                        "expected_revision": 7,
                        "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
                        "source_artifact_ids": ["artifact_live_statement_2026_08"],
                        field: value,
                    },
                )
                self.assertEqual(status, 422)
                self.assertEqual(problem["code"], "INVALID_PAYROLL_COMMAND")
        self.assertEqual(self.client.calls, [])

    def test_payroll_command_requires_full_session_csrf_and_canonical_operation_uuid(self) -> None:
        body = {
            "expected_revision": 7,
            "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
            "source_artifact_ids": ["artifact_live_statement_2026_08"],
        }
        path = "/api/v1/payroll/batches/batch_live_2026_08/verify-receipts"
        status, problem = self.post(path, body, authenticated=False)
        self.assertEqual((status, problem["code"]), (401, "AUTHENTICATION_REQUIRED"))
        status, problem = self.post(path, body, csrf="wrong")
        self.assertEqual((status, problem["code"]), (403, "CSRF_VALIDATION_FAILED"))
        status, problem = self.post(path, body, idempotency_key="not-a-uuid")
        self.assertEqual((status, problem["code"]), (400, "INVALID_IDEMPOTENCY_KEY"))
        self.assertEqual(self.client.calls, [])

    def test_core_idempotency_replay_is_preserved_exactly(self) -> None:
        path = "/api/v1/payroll/batches/batch_live_2026_08/verify-receipts"
        body = {
            "expected_revision": 7,
            "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
            "source_artifact_ids": ["artifact_live_statement_2026_08"],
        }
        first_status, first = self.post(path, body)
        replay_status, replay = self.post(path, body)
        self.assertEqual((first_status, first["replayed"]), (200, False))
        self.assertEqual((replay_status, replay["replayed"]), (200, True))
        post_calls = [call for call in self.client.calls if call[0] == "POST"]
        self.assertEqual(len(post_calls), 2)
        self.assertEqual(post_calls[0][2], post_calls[1][2])
        self.assertEqual(
            post_calls[0][3]["Idempotency-Key"],
            post_calls[1][3]["Idempotency-Key"],
        )


if __name__ == "__main__":
    unittest.main()
