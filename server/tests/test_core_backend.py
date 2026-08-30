from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import uuid
from contextlib import closing
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from server.app import COOKIE_NAME, create_server
from server.core_backend import (
    EVIDENCE_UNLOCK_CORE_PATH,
    CoreBackendError,
    CoreBackedState,
    sqlite_contains_business_facts,
)


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
        self.candidate_next_cursor: str | None = None
        self.candidate_payload = core_candidate()

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
        if path.startswith("/internal/v1/accounting-dimensions?"):
            return {
                "contract_version": "ledgerbridge.accounting-dimensions.v1",
                "entity_ref": ENTITY_ID,
                "business_units": [
                    {"ref": "unit-demo-a", "label": "演示门店"},
                    {"ref": "unit-demo-b", "label": "机场门店"},
                ],
                "categories": [
                    {"code": "OTHER", "label": "其他"},
                    {"code": "SETTLEMENT", "label": "银行收款"},
                ],
            }
        if path.startswith(f"/internal/v1/candidates/{CANDIDATE_ID}"):
            return self.candidate_payload
        if path.startswith("/internal/v1/candidates?"):
            return {"items": [self.candidate_payload], "next_cursor": self.candidate_next_cursor}
        raise AssertionError(f"unexpected Core path: {path}")

    def evidence(self, path: str) -> dict[str, object]:
        self.calls.append(("GET", path, None, {}))
        return {
            "content": b"synthetic evidence",
            "content_type": "application/octet-stream",
            "disposition": "attachment",
            "filename": "evidence.bin",
        }


class StableReferenceCoreClient(FakeCoreClient):
    """Model Core's fail-closed ref/code lookup for correction commands."""

    def json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        if method == "POST":
            assert body is not None
            corrections = json.loads(body)["corrections"]
            if corrections.get("business_unit") not in {"unit-demo-a", "unit-demo-b"} or corrections.get(
                "category"
            ) not in {"SETTLEMENT", "OTHER"}:
                raise CoreBackendError(404, {"code": "RESOURCE_NOT_VISIBLE"})
        return super().json(method, path, body=body, headers=headers)


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


class SecretSafeUnlockCoreClient(FakeCoreClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_unlock = False
        self.unlock_calls = 0
        self.password_was_present = False
        self.unlock_source_ref: str | None = None
        self.unlock_headers: dict[str, str] = {}
        self.unlock_body_sha256: str | None = None

    def json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        if method == "POST" and path == EVIDENCE_UNLOCK_CORE_PATH:
            assert body is not None
            request = json.loads(body)
            password = request.pop("password", None)
            self.unlock_calls += 1
            self.password_was_present = isinstance(password, str) and bool(password)
            self.unlock_source_ref = request.get("source_ref")
            self.unlock_headers = dict(headers or {})
            self.unlock_body_sha256 = hashlib.sha256(body).hexdigest()
            if self.fail_unlock:
                raise CoreBackendError(
                    422,
                    {"code": "CORE_PASSWORD_REJECTED", "detail": password},
                )
            return {
                "contract_version": "ledgerbridge.evidence-unlock-result.v1",
                "source_ref": self.unlock_source_ref,
                "unlock_status": "UNLOCKED",
            }
        return super().json(method, path, body=body, headers=headers)


def build_state(
    client: FakeCoreClient,
    *,
    evidence_unlock_path: str | None = None,
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
        evidence_unlock_path=evidence_unlock_path,
    )


class CoreBackedAdapterTests(unittest.TestCase):
    def test_review_event_merges_dimension_identity_and_label_without_exposing_identifiers(self) -> None:
        client = FakeCoreClient()
        event = core_event()
        prior = core_candidate()
        result = core_candidate(status="CONFIRMED", revision=2)
        prior.update(
            {
                "business_unit_ref": "unit-demo-a",
                "business_unit_label": "同名门店",
                "category_code": "SETTLEMENT",
                "category_label": "银行收款",
            }
        )
        result.update(
            {
                "business_unit_ref": "unit-demo-b",
                "business_unit_label": "同名门店",
                "category_code": "OTHER",
                "category_label": "其他",
            }
        )
        event.update(
            {
                "action": "COMPLETE_FIELDS",
                "prior_projection": prior,
                "result_projection": result,
                "changes": [
                    {
                        "field": "business_unit_ref",
                        "previous_value": "unit-demo-a",
                        "new_value": "unit-demo-b",
                    },
                    {
                        "field": "category_code",
                        "previous_value": "SETTLEMENT",
                        "new_value": "OTHER",
                    },
                    {
                        "field": "category_label",
                        "previous_value": "银行收款",
                        "new_value": "其他",
                    },
                    {
                        "field": "status",
                        "previous_value": "PENDING",
                        "new_value": "CONFIRMED",
                    },
                ],
            }
        )
        original_json = client.json

        def identity_event(*args: object, **kwargs: object) -> dict[str, object]:
            if str(args[1]).startswith("/internal/v1/candidate-events"):
                return {"items": [event], "next_cursor": None}
            return original_json(*args, **kwargs)  # type: ignore[arg-type]

        client.json = identity_event  # type: ignore[method-assign]

        mapped = build_state(client).list_review_events(cursor=None)["items"][0]  # type: ignore[index]

        self.assertEqual(
            mapped["changes"],
            [
                {
                    "field": "business_unit",
                    "previous_value": "同名门店",
                    "new_value": "同名门店",
                    "identity_changed": True,
                },
                {
                    "field": "category",
                    "previous_value": "银行收款",
                    "new_value": "其他",
                    "identity_changed": True,
                },
                {
                    "field": "status",
                    "previous_value": "PENDING",
                    "new_value": "CONFIRMED",
                    "identity_changed": False,
                },
            ],
        )
        public_json = json.dumps(mapped, ensure_ascii=False)
        self.assertNotIn("unit-demo-a", public_json)
        self.assertNotIn("unit-demo-b", public_json)
        self.assertNotIn("SETTLEMENT", public_json)
        self.assertNotIn("OTHER", public_json)

    def test_candidate_rejects_invalid_or_unpaired_dimension_identity_and_label(self) -> None:
        invalid_values: tuple[tuple[str, object], ...] = (
            ("business_unit_ref", 7),
            ("category_code", "x" * 101),
            ("business_unit_label", None),
            ("category_label", "x" * 201),
        )
        for field, value in invalid_values:
            with self.subTest(field=field):
                client = FakeCoreClient()
                client.candidate_payload[field] = value

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).list_candidates(status=None, month=None, cursor=None)

                self.assertEqual(raised.exception.status, 503)
                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_accounting_dimensions_are_scoped_to_the_configured_entity(self) -> None:
        client = FakeCoreClient()

        options = build_state(client).accounting_dimensions()

        self.assertEqual(
            client.calls[-1][1],
            f"/internal/v1/accounting-dimensions?entity_ref={ENTITY_ID}",
        )
        self.assertEqual(options["business_units"][1], {"ref": "unit-demo-b", "label": "机场门店"})
        self.assertEqual(options["categories"][0], {"code": "OTHER", "label": "其他"})

    def test_accounting_dimensions_reject_a_mismatched_entity_contract(self) -> None:
        client = FakeCoreClient()
        original_json = client.json

        def mismatched_entity(*args: object, **kwargs: object) -> dict[str, object]:
            payload = original_json(*args, **kwargs)  # type: ignore[arg-type]
            if str(args[1]).startswith("/internal/v1/accounting-dimensions?"):
                payload["entity_ref"] = "10000000-0000-4000-8000-000000000099"
            return payload

        client.json = mismatched_entity  # type: ignore[method-assign]

        with self.assertRaises(CoreBackendError) as raised:
            build_state(client).accounting_dimensions()
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.payload["code"], "ACCOUNTING_DIMENSIONS_INVALID")

    def test_accounting_dimensions_reject_an_unsorted_core_contract(self) -> None:
        client = FakeCoreClient()
        original_json = client.json

        def unsorted_dimensions(*args: object, **kwargs: object) -> dict[str, object]:
            payload = original_json(*args, **kwargs)  # type: ignore[arg-type]
            if str(args[1]).startswith("/internal/v1/accounting-dimensions?"):
                payload["business_units"] = list(reversed(payload["business_units"]))  # type: ignore[arg-type]
            return payload

        client.json = unsorted_dimensions  # type: ignore[method-assign]

        with self.assertRaises(CoreBackendError) as raised:
            build_state(client).accounting_dimensions()
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.payload["code"], "ACCOUNTING_DIMENSIONS_INVALID")

    def test_accounting_dimensions_reject_extra_top_level_fields_and_duplicate_labels(self) -> None:
        for mutation in ("extra", "duplicate-label"):
            with self.subTest(mutation=mutation):
                client = FakeCoreClient()
                original_json = client.json

                def invalid_dimensions(*args: object, **kwargs: object) -> dict[str, object]:
                    payload = original_json(*args, **kwargs)  # type: ignore[arg-type]
                    if str(args[1]).startswith("/internal/v1/accounting-dimensions?"):
                        if mutation == "extra":
                            payload["internal_id"] = "must-not-cross-bff"
                        else:
                            payload["business_units"] = [
                                {"ref": "unit-demo-a", "label": "同名门店"},
                                {"ref": "unit-demo-b", "label": "同名门店"},
                            ]
                    return payload

                client.json = invalid_dimensions  # type: ignore[method-assign]

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).accounting_dimensions()
                self.assertEqual(raised.exception.status, 503)
                self.assertEqual(raised.exception.payload["code"], "ACCOUNTING_DIMENSIONS_INVALID")

    def test_accounting_dimensions_normalize_upstream_errors_without_leaking_payloads(self) -> None:
        client = FakeCoreClient()
        original_json = client.json

        def missing_dimensions(*args: object, **kwargs: object) -> dict[str, object]:
            if str(args[1]).startswith("/internal/v1/accounting-dimensions?"):
                raise CoreBackendError(
                    404,
                    {"code": "RESOURCE_NOT_FOUND", "detail": "secret upstream entity metadata"},
                )
            return original_json(*args, **kwargs)  # type: ignore[arg-type]

        client.json = missing_dimensions  # type: ignore[method-assign]

        with self.assertRaises(CoreBackendError) as raised:
            build_state(client).accounting_dimensions()
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(raised.exception.payload["code"], "ACCOUNTING_DIMENSIONS_NOT_FOUND")
        self.assertNotIn("secret", json.dumps(raised.exception.payload))

        def unexpected_dimensions(*args: object, **kwargs: object) -> dict[str, object]:
            if str(args[1]).startswith("/internal/v1/accounting-dimensions?"):
                raise CoreBackendError(418, {"code": "UPSTREAM_INTERNAL", "detail": "secret"})
            return original_json(*args, **kwargs)  # type: ignore[arg-type]

        client.json = unexpected_dimensions  # type: ignore[method-assign]
        with self.assertRaises(CoreBackendError) as raised:
            build_state(client).accounting_dimensions()
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.payload["code"], "ACCOUNTING_DIMENSIONS_UNAVAILABLE")
        self.assertNotIn("secret", json.dumps(raised.exception.payload))

    def test_legacy_display_label_corrections_are_rejected_even_when_labels_match(self) -> None:
        client = StableReferenceCoreClient()
        state = build_state(client)

        status, payload = state.append_decision(
            CANDIDATE_ID,
            str(uuid.uuid4()),
            {
                "decision": "CORRECT_AND_CONFIRM",
                "expected_revision": 1,
                "reason": "人工更正金额和月份",
                "corrections": {
                    "business_unit": "演示门店",
                    "category": "银行收款",
                    "amount_minor": 23456,
                    "accounting_month": "2026-07",
                },
            },
        )

        self.assertEqual(status, 422, payload)
        self.assertEqual(payload["code"], "INVALID_CORRECTIONS")
        self.assertFalse(any(call[0] == "POST" for call in client.calls))

    def test_correction_forwards_explicit_new_stable_references(self) -> None:
        client = StableReferenceCoreClient()
        state = build_state(client)

        status, payload = state.append_decision(
            CANDIDATE_ID,
            str(uuid.uuid4()),
            {
                "decision": "CORRECT_AND_CONFIRM",
                "expected_revision": 1,
                "reason": "人工更正营业单元和科目",
                "corrections": {
                    "business_unit_ref": "unit-demo-b",
                    "category_code": "OTHER",
                },
            },
        )

        self.assertEqual(status, 200, payload)
        _, _, body, _ = client.calls[-1]
        assert body is not None
        corrections = json.loads(body)["corrections"]
        self.assertEqual(corrections, {"business_unit": "unit-demo-b", "category": "OTHER"})

    def test_unknown_explicit_reference_remains_core_fail_closed(self) -> None:
        client = StableReferenceCoreClient()

        status, payload = build_state(client).append_decision(
            CANDIDATE_ID,
            str(uuid.uuid4()),
            {
                "decision": "CORRECT_AND_CONFIRM",
                "expected_revision": 1,
                "reason": "未知营业单元不得写入",
                "corrections": {
                    "business_unit_ref": "unit-unknown",
                    "category_code": "SETTLEMENT",
                },
            },
        )

        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "RESOURCE_NOT_VISIBLE")

    def test_legacy_display_label_cannot_select_a_different_dimension(self) -> None:
        client = StableReferenceCoreClient()

        status, payload = build_state(client).append_decision(
            CANDIDATE_ID,
            str(uuid.uuid4()),
            {
                "decision": "CORRECT_AND_CONFIRM",
                "expected_revision": 1,
                "reason": "不得按显示名称反查新维度",
                "corrections": {
                    "business_unit": "机场门店",
                    "category": "其他",
                },
            },
        )

        self.assertEqual(status, 422)
        self.assertEqual(payload["code"], "INVALID_CORRECTIONS")
        self.assertFalse(any(call[0] == "POST" for call in client.calls))

    def test_maps_only_valid_structured_evidence_unlock_state(self) -> None:
        source_ref = "21000000-0000-4000-8000-000000000001"
        client = FakeCoreClient()
        client.candidate_payload["evidence"][0].update(  # type: ignore[index,union-attr]
            {"unlock_status": "PASSWORD_REQUIRED", "source_ref": source_ref}
        )

        evidence = build_state(client).list_candidates(status=None, month=None, cursor=None)["items"][0]["evidence"][0]  # type: ignore[index]
        self.assertEqual(evidence["unlock_status"], "PASSWORD_REQUIRED")
        self.assertEqual(evidence["source_ref"], source_ref)

        client.candidate_payload["evidence"][0]["source_ref"] = "../private/archive.zip"  # type: ignore[index]
        with self.assertRaises(CoreBackendError) as raised:
            build_state(client).list_candidates(status=None, month=None, cursor=None)
        self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_unlock_adapter_is_fail_closed_and_drops_core_failure_text(self) -> None:
        source_ref = "22000000-0000-4000-8000-000000000001"
        operation = str(uuid.uuid4())
        unavailable_status, unavailable = build_state(SecretSafeUnlockCoreClient()).unlock_evidence_source(
            source_ref,
            "temporary-password",
            operation,
        )
        self.assertEqual(unavailable_status, 503)
        self.assertEqual(unavailable["code"], "EVIDENCE_UNLOCK_UNAVAILABLE")

        client = SecretSafeUnlockCoreClient()
        client.fail_unlock = True
        status, problem = build_state(
            client,
            evidence_unlock_path=EVIDENCE_UNLOCK_CORE_PATH,
        ).unlock_evidence_source(source_ref, "must-not-leak", operation)
        self.assertEqual(status, 422)
        self.assertEqual(problem["code"], "EVIDENCE_UNLOCK_FAILED")
        self.assertNotIn("must-not-leak", json.dumps(problem))
        self.assertFalse(hasattr(client, "unlock_body"))

    def test_unlock_adapter_binds_source_body_digest_and_operation(self) -> None:
        source_ref = "23000000-0000-4000-8000-000000000001"
        operation = str(uuid.uuid4())
        client = SecretSafeUnlockCoreClient()
        status, result = build_state(
            client,
            evidence_unlock_path=EVIDENCE_UNLOCK_CORE_PATH,
        ).unlock_evidence_source(source_ref, "temporary-password", operation)
        self.assertEqual(status, 200)
        self.assertEqual(result, {"unlocked": True})
        self.assertTrue(client.password_was_present)
        self.assertEqual(client.unlock_source_ref, source_ref)
        self.assertEqual(client.unlock_headers["Idempotency-Key"], operation)
        version, encoded, _ = client.unlock_headers["X-LedgerBridge-User-Assertion"].split(".")
        self.assertEqual(version, "v1")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(claims["resource_ref"], source_ref)
        self.assertEqual(claims["body_sha256"], client.unlock_body_sha256)
        self.assertEqual(claims["operation_id"], operation)

    def test_missing_reconciliation_snapshot_does_not_hide_imported_candidates(self) -> None:
        client = FakeCoreClient()

        def missing_snapshot(
            method: str,
            path: str,
            *,
            body: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> dict[str, object]:
            if path.startswith("/internal/v1/reconciliations/"):
                raise CoreBackendError(404, {"code": "RESOURCE_NOT_VISIBLE"})
            return FakeCoreClient.json(client, method, path, body=body, headers=headers)

        client.json = missing_snapshot  # type: ignore[method-assign]
        reconciliation = build_state(client).reconciliation("2026-08")

        self.assertEqual(reconciliation["accounting_month"], "2026-08")
        self.assertEqual(reconciliation["revision"], 0)
        self.assertFalse(reconciliation["ready"])
        self.assertEqual(reconciliation["business_units"], [])
        self.assertEqual(
            reconciliation["blockers"][0]["code"],  # type: ignore[index]
            "RECONCILIATION_SNAPSHOT_MISSING",
        )

    def test_maps_outlook_candidate_and_binds_exact_user_assertion(self) -> None:
        client = FakeCoreClient()
        state = build_state(client)

        page = state.list_candidates(status="PENDING", month="2026-08", cursor=None)
        candidate = page["items"][0]  # type: ignore[index]
        self.assertEqual(candidate["source_channel"], "outlook")
        self.assertEqual(candidate["business_unit"], "演示门店")
        self.assertEqual(candidate["business_unit_ref"], "unit-demo-a")
        self.assertEqual(candidate["category_code"], "SETTLEMENT")
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
        client.candidate_next_cursor = "eNpF.payload.signature"
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
                    f"{base_url}/api/v1/session",
                    headers={"Cookie": cookie},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    session = json.load(response)
                self.assertEqual(session["runtime_mode"], "core-backed")
                request = urllib.request.Request(
                    f"{base_url}/api/v1/candidates?status=PENDING",
                    headers={"Cookie": cookie},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    page = json.load(response)
                self.assertEqual(page["items"][0]["source_channel"], "outlook")
                self.assertEqual(page["next_cursor"], client.candidate_next_cursor)
                request = urllib.request.Request(
                    f"{base_url}/api/v1/candidates?cursor={client.candidate_next_cursor}",
                    headers={"Cookie": cookie},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    next_page = json.load(response)
                self.assertEqual(next_page["items"][0]["source_channel"], "outlook")
                self.assertIn(
                    f"cursor={client.candidate_next_cursor}",
                    client.calls[-1][1],
                )

                request = urllib.request.Request(
                    f"{base_url}/api/v1/accounting-dimensions",
                    headers={"Cookie": cookie},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    dimensions = json.load(response)
                self.assertEqual(dimensions["business_units"][0]["ref"], "unit-demo-a")
                self.assertEqual(dimensions["categories"][1]["code"], "SETTLEMENT")

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


class CoreBackedUnlockBffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SecretSafeUnlockCoreClient()
        self.temp_dir = tempfile.TemporaryDirectory()
        Path(self.temp_dir.name, "index.html").write_text("<main>review</main>", encoding="utf-8")
        self.server = create_server(
            "127.0.0.1",
            0,
            self.temp_dir.name,
            state=build_state(self.client, evidence_unlock_path=EVIDENCE_UNLOCK_CORE_PATH),
            auth_manager=FakeAuthManager(),  # type: ignore[arg-type]
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

    def request(
        self,
        body: dict[str, object],
        *,
        authenticated: bool = True,
        csrf: str = "csrf-token",
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {
            "Origin": "https://ledgerbridge.test",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            "Idempotency-Key": str(uuid.uuid4()),
        }
        if authenticated:
            headers["Cookie"] = f"{COOKIE_NAME}=session-token"
        connection.request(
            "POST",
            "/api/v1/evidence/unlocks",
            body=json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        status = response.status
        connection.close()
        return status, payload

    def test_requires_session_csrf_and_opaque_source_reference(self) -> None:
        request = {
            "source_ref": "24000000-0000-4000-8000-000000000001",
            "password": "temporary-password",
        }
        status, problem = self.request(request, authenticated=False)
        self.assertEqual(status, 401)
        self.assertEqual(problem["code"], "AUTHENTICATION_REQUIRED")
        status, problem = self.request(request, csrf="wrong")
        self.assertEqual(status, 403)
        self.assertEqual(problem["code"], "CSRF_VALIDATION_FAILED")
        status, problem = self.request({**request, "source_ref": "../private/archive.zip"})
        self.assertEqual(status, 422)
        self.assertEqual(problem["code"], "INVALID_SOURCE_REF")
        self.assertEqual(self.client.unlock_calls, 0)

    def test_rejects_every_malformed_unlock_shape_without_forwarding_or_echoing_passwords(self) -> None:
        source_ref = "24500000-0000-4000-8000-000000000001"
        cases = [
            (
                {"source_ref": source_ref, "password": "extra-field-secret", "extra": True},
                "INVALID_EVIDENCE_UNLOCK_REQUEST",
                "extra-field-secret",
            ),
            (
                {"password": "missing-reference-secret"},
                "INVALID_EVIDENCE_UNLOCK_REQUEST",
                "missing-reference-secret",
            ),
            (
                {"source_ref": "../private/archive.zip", "password": "invalid-reference-secret"},
                "INVALID_SOURCE_REF",
                "invalid-reference-secret",
            ),
            (
                {"source_ref": source_ref, "password": "nul-secret\x00suffix"},
                "INVALID_EVIDENCE_PASSWORD",
                "nul-secret",
            ),
            (
                {"source_ref": source_ref, "password": "x" * 1025},
                "INVALID_EVIDENCE_PASSWORD",
                "x" * 64,
            ),
        ]
        capture = io.StringIO()
        with redirect_stdout(capture):
            for body, expected_code, secret_fragment in cases:
                with self.subTest(expected_code=expected_code):
                    status, problem = self.request(body)
                    self.assertEqual(status, 422)
                    self.assertEqual(problem["code"], expected_code)
                    self.assertNotIn(secret_fragment, json.dumps(problem))
        self.assertEqual(self.client.unlock_calls, 0)
        for _, _, secret_fragment in cases:
            self.assertNotIn(secret_fragment, capture.getvalue())

    def test_core_failure_is_generic_and_password_is_not_logged_or_returned(self) -> None:
        self.client.fail_unlock = True
        capture = io.StringIO()
        with redirect_stdout(capture):
            status, problem = self.request(
                {
                    "source_ref": "25000000-0000-4000-8000-000000000001",
                    "password": "must-not-leak",
                }
            )
        self.assertEqual(status, 422)
        self.assertEqual(problem["code"], "EVIDENCE_UNLOCK_FAILED")
        self.assertNotIn("must-not-leak", json.dumps(problem))
        self.assertNotIn("must-not-leak", capture.getvalue())

    def test_success_returns_only_unlock_flag(self) -> None:
        status, payload = self.request(
            {
                "source_ref": "26000000-0000-4000-8000-000000000001",
                "password": "temporary-password",
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"unlocked": True})
        self.assertEqual(self.client.unlock_calls, 1)


if __name__ == "__main__":
    unittest.main()
