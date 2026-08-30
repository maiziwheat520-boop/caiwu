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
from copy import deepcopy
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


REPORT_BASES = (
    "CONFIRMED_CANDIDATE",
    "ACCOUNT_STATEMENT",
    "POSTED_LEDGER",
)


def core_report_metrics(basis: str) -> dict[str, object]:
    if basis == "CONFIRMED_CANDIDATE":
        return {
            "basis": basis,
            "confirmed_positive_minor": 800000,
            "confirmed_negative_minor": -235000,
            "confirmed_net_minor": 565000,
            "confirmed_count": 3,
            "source_count": 2,
        }
    if basis == "ACCOUNT_STATEMENT":
        return {
            "basis": basis,
            "cash_inflow_minor": 700000,
            "cash_outflow_minor": 200000,
            "net_cash_flow_minor": 500000,
            "confirmed_transaction_count": 2,
            "statement_count": 1,
        }
    return {
        "basis": basis,
        "revenue_minor": 600000,
        "expense_minor": 100000,
        "profit_minor": 500000,
        "posted_entry_count": 2,
        "source_count": 2,
    }


def core_company_report_layer(basis: str) -> dict[str, object]:
    common = {
        "metrics": core_report_metrics(basis),
        "pending_review_count": 4 if basis == "CONFIRMED_CANDIDATE" else 0,
        "attribution_pending_count": 2 if basis == "ACCOUNT_STATEMENT" else 1 if basis == "CONFIRMED_CANDIDATE" else 0,
        "missing_material_count": None,
        "taxonomy_version": None,
        "balance": {
            "balance_basis": "UNAVAILABLE",
            "opening_balance_minor": None,
            "closing_balance_minor": None,
            "gap": "AUTHORITATIVE_BALANCE_UNAVAILABLE",
        },
    }
    return {
        "contract_version": "ledgerbridge.company-report.v1",
        "basis": basis,
        "from_month": "2026-01",
        "to_month": "2026-08",
        "items": [
            {
                "company_ref": ENTITY_ID,
                "company_name": "演示公司",
                "currency": "CNY",
                "business_unit_breakdown_status": "AVAILABLE",
                **common,
                "months": [
                    {
                        "month": "2026-08",
                        **common,
                        "business_unit_breakdown_status": "AVAILABLE",
                        "business_units": [
                            {
                                "business_unit_ref": "unit-demo-a",
                                "business_unit_label": "演示门店",
                                **common,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def company_reports_bff() -> dict[str, object]:
    return {
        "contract_version": "ledgerbridge.company-reports-bff.v1",
        "from_month": "2026-01",
        "to_month": "2026-08",
        "layers": [core_company_report_layer(basis) for basis in REPORT_BASES],
    }


class FakeCoreClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.candidate_next_cursor: str | None = None
        self.candidate_payload = core_candidate()
        self.company_report_payloads = {
            basis: core_company_report_layer(basis)
            for basis in REPORT_BASES
        }

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
            return self.candidate_payload
        if path.startswith("/internal/v1/candidates?"):
            return {"items": [self.candidate_payload], "next_cursor": self.candidate_next_cursor}
        if path.startswith("/internal/v1/company-reports?"):
            basis = next(
                value for value in REPORT_BASES if f"basis={value}" in path
            )
            return self.company_report_payloads[basis]
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
    def test_company_reports_preserve_authoritative_company_and_business_unit_scope(self) -> None:
        client = FakeCoreClient()

        report = build_state(client).company_reports("2026-01", "2026-08")

        self.assertEqual(report, company_reports_bff())
        self.assertEqual(
            client.calls[-3:],
            [
                (
                    "GET",
                    f"/internal/v1/company-reports?from_month=2026-01&to_month=2026-08&basis={basis}",
                    None,
                    {},
                )
                for basis in REPORT_BASES
            ],
        )

    def test_company_reports_reject_private_fields_instead_of_silently_dropping_them(self) -> None:
        client = FakeCoreClient()
        payload = client.company_report_payloads["CONFIRMED_CANDIDATE"]
        payload["internal_scope"] = "must-not-leak"

        with self.assertRaises(CoreBackendError) as raised:
            build_state(client).company_reports("2026-01", "2026-08")

        self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_reject_invalid_financial_integers_at_every_rollup_level(self) -> None:
        invalid_values = (
            ("CONFIRMED_CANDIDATE", "candidate net", lambda payload: payload["items"][0]["metrics"].update({"confirmed_net_minor": 1})),  # type: ignore[index,union-attr]
            ("CONFIRMED_CANDIDATE", "month boolean", lambda payload: payload["items"][0]["months"][0]["metrics"].update({"confirmed_negative_minor": True})),  # type: ignore[index,union-attr]
            ("CONFIRMED_CANDIDATE", "business unit unsafe", lambda payload: payload["items"][0]["months"][0]["business_units"][0]["metrics"].update({"confirmed_positive_minor": 2**53})),  # type: ignore[index,union-attr]
            ("ACCOUNT_STATEMENT", "cash net", lambda payload: payload["items"][0]["metrics"].update({"net_cash_flow_minor": 1})),  # type: ignore[index,union-attr]
            ("POSTED_LEDGER", "posted profit", lambda payload: payload["items"][0]["metrics"].update({"profit_minor": 1})),  # type: ignore[index,union-attr]
            ("CONFIRMED_CANDIDATE", "negative count", lambda payload: payload["items"][0].update({"pending_review_count": -1})),  # type: ignore[index,union-attr]
        )
        for basis, label, mutate in invalid_values:
            with self.subTest(label=label):
                client = FakeCoreClient()
                mutate(client.company_report_payloads[basis])

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).company_reports("2026-01", "2026-08")

                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_reject_malformed_contracts_and_more_than_fifty_companies(self) -> None:
        invalid_values = (
            ("version", lambda payload: payload.update({"contract_version": "ledgerbridge.company-report.v2"})),
            ("basis", lambda payload: payload.update({"basis": "PENDING_CANDIDATE"})),
            ("range", lambda payload: payload.update({"from_month": "2025-01"})),
            ("item limit", lambda payload: payload.update({"items": payload["items"] * 51})),  # type: ignore[operator]
            ("missing company field", lambda payload: payload["items"][0].pop("company_name")),  # type: ignore[index,union-attr]
            ("months shape", lambda payload: payload["items"][0].update({"months": {}})),  # type: ignore[index,union-attr]
            ("business units shape", lambda payload: payload["items"][0]["months"][0].update({"business_units": None})),  # type: ignore[index,union-attr]
            ("metrics discriminator", lambda payload: payload["items"][0]["metrics"].update({"basis": "ACCOUNT_STATEMENT"})),  # type: ignore[index,union-attr]
            ("fabricated balance", lambda payload: payload["items"][0]["balance"].update({"opening_balance_minor": 0})),  # type: ignore[index,union-attr]
            ("missing material shape", lambda payload: payload["items"][0].update({"missing_material_count": "unknown"})),  # type: ignore[index,union-attr]
        )
        for label, mutate in invalid_values:
            with self.subTest(label=label):
                client = FakeCoreClient()
                mutate(client.company_report_payloads["CONFIRMED_CANDIDATE"])

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).company_reports("2026-01", "2026-08")

                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_require_exact_fields_at_every_contract_boundary(self) -> None:
        def report_nodes(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
            company = payload["items"][0]  # type: ignore[index]
            month = company["months"][0]  # type: ignore[index]
            business_unit = month["business_units"][0]  # type: ignore[index]
            return (
                payload,
                company,
                month,
                business_unit,
                company["metrics"],  # type: ignore[index]
                company["balance"],  # type: ignore[index]
            )

        invalid_shapes = (
            ("layer extra", 0, "internal_scope", "private"),
            ("layer missing", 0, "items", None),
            ("company extra", 1, "internal_note", "private"),
            ("company missing", 1, "months", None),
            ("company breakdown status missing", 1, "business_unit_breakdown_status", None),
            ("month extra", 2, "candidate_refs", []),
            ("month missing", 2, "business_units", None),
            ("breakdown status missing", 2, "business_unit_breakdown_status", None),
            ("business unit extra", 3, "bank_account", "private"),
            ("business unit missing", 3, "business_unit_label", None),
            ("metrics extra", 4, "income_minor", 1),
            ("metrics missing", 4, "source_count", None),
            ("balance extra", 5, "derived", True),
            ("balance missing", 5, "gap", None),
        )
        for label, node_index, field, replacement in invalid_shapes:
            with self.subTest(label=label):
                client = FakeCoreClient()
                node = report_nodes(
                    client.company_report_payloads["CONFIRMED_CANDIDATE"]
                )[node_index]
                if replacement is None:
                    node.pop(field)
                else:
                    node[field] = replacement

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).company_reports("2026-01", "2026-08")

                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_fail_closed_on_cross_layer_company_identity_mismatch(self) -> None:
        client = FakeCoreClient()
        company = client.company_report_payloads["ACCOUNT_STATEMENT"]["items"][0]  # type: ignore[index]
        company["currency"] = "USD"  # type: ignore[index]

        with self.assertRaises(CoreBackendError) as raised:
            build_state(client).company_reports("2026-01", "2026-08")

        self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_require_the_same_company_refs_in_all_three_layers(self) -> None:
        client = FakeCoreClient()
        client.company_report_payloads["ACCOUNT_STATEMENT"]["items"] = []

        with self.assertRaises(CoreBackendError) as raised:
            build_state(client).company_reports("2026-01", "2026-08")

        self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_preserve_explicit_business_unit_unavailability(self) -> None:
        client = FakeCoreClient()
        expected = {
            "CONFIRMED_CANDIDATE": ("EMPTY", []),
            "ACCOUNT_STATEMENT": ("UNAVAILABLE_ATTRIBUTION_PENDING", None),
            "POSTED_LEDGER": ("UNAVAILABLE_MISSING_SNAPSHOT", None),
        }
        for basis, (status, business_units) in expected.items():
            company = client.company_report_payloads[basis]["items"][0]  # type: ignore[index]
            company["business_unit_breakdown_status"] = status  # type: ignore[index]
            month = client.company_report_payloads[basis]["items"][0]["months"][0]  # type: ignore[index]
            month["business_unit_breakdown_status"] = status  # type: ignore[index]
            month["business_units"] = business_units  # type: ignore[index]

        reports = build_state(client).company_reports("2026-01", "2026-08")

        for layer in reports["layers"]:  # type: ignore[union-attr]
            company = layer["items"][0]
            month = layer["items"][0]["months"][0]
            status, business_units = expected[layer["basis"]]
            self.assertEqual(company["business_unit_breakdown_status"], status)
            self.assertEqual(month["business_unit_breakdown_status"], status)
            self.assertEqual(month["business_units"], business_units)

    def test_company_reports_preserve_company_breakdown_status_priority(self) -> None:
        client = FakeCoreClient()

        candidate = client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]  # type: ignore[index]
        candidate_empty = deepcopy(candidate["months"][0])  # type: ignore[index]
        candidate_empty["month"] = "2026-07"
        candidate_empty["business_unit_breakdown_status"] = "EMPTY"
        candidate_empty["business_units"] = []
        candidate["months"] = [candidate_empty, candidate["months"][0]]  # type: ignore[index]
        candidate["business_unit_breakdown_status"] = "AVAILABLE"  # type: ignore[index]

        statement = client.company_report_payloads["ACCOUNT_STATEMENT"]["items"][0]  # type: ignore[index]
        statement_available = deepcopy(statement["months"][0])  # type: ignore[index]
        statement_available["month"] = "2026-06"
        statement_missing = deepcopy(statement["months"][0])  # type: ignore[index]
        statement_missing["month"] = "2026-07"
        statement_missing["business_unit_breakdown_status"] = "UNAVAILABLE_MISSING_SNAPSHOT"
        statement_missing["business_units"] = None
        statement_pending = deepcopy(statement["months"][0])  # type: ignore[index]
        statement_pending["business_unit_breakdown_status"] = "UNAVAILABLE_ATTRIBUTION_PENDING"
        statement_pending["business_units"] = None
        statement["months"] = [statement_available, statement_missing, statement_pending]  # type: ignore[index]
        statement["business_unit_breakdown_status"] = "UNAVAILABLE_ATTRIBUTION_PENDING"  # type: ignore[index]

        posted = client.company_report_payloads["POSTED_LEDGER"]["items"][0]  # type: ignore[index]
        posted_available = deepcopy(posted["months"][0])  # type: ignore[index]
        posted_available["month"] = "2026-07"
        posted_missing = deepcopy(posted["months"][0])  # type: ignore[index]
        posted_missing["business_unit_breakdown_status"] = "UNAVAILABLE_MISSING_SNAPSHOT"
        posted_missing["business_units"] = None
        posted["months"] = [posted_available, posted_missing]  # type: ignore[index]
        posted["business_unit_breakdown_status"] = "UNAVAILABLE_MISSING_SNAPSHOT"  # type: ignore[index]

        reports = build_state(client).company_reports("2026-01", "2026-08")

        self.assertEqual(
            [layer["items"][0]["business_unit_breakdown_status"] for layer in reports["layers"]],  # type: ignore[index,union-attr]
            ["AVAILABLE", "UNAVAILABLE_ATTRIBUTION_PENDING", "UNAVAILABLE_MISSING_SNAPSHOT"],
        )

    def test_company_reports_reject_company_breakdown_status_that_does_not_summarize_months(self) -> None:
        def no_months_but_available(client: FakeCoreClient) -> None:
            company = client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]  # type: ignore[index]
            company["months"] = []  # type: ignore[index]
            company["business_unit_breakdown_status"] = "AVAILABLE"  # type: ignore[index]

        def candidate_empty_but_available(client: FakeCoreClient) -> None:
            company = client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]  # type: ignore[index]
            month = company["months"][0]  # type: ignore[index]
            month["business_unit_breakdown_status"] = "EMPTY"  # type: ignore[index]
            month["business_units"] = []  # type: ignore[index]
            company["business_unit_breakdown_status"] = "AVAILABLE"  # type: ignore[index]

        def statement_pending_but_missing(client: FakeCoreClient) -> None:
            company = client.company_report_payloads["ACCOUNT_STATEMENT"]["items"][0]  # type: ignore[index]
            month = company["months"][0]  # type: ignore[index]
            month["business_unit_breakdown_status"] = "UNAVAILABLE_ATTRIBUTION_PENDING"  # type: ignore[index]
            month["business_units"] = None  # type: ignore[index]
            company["business_unit_breakdown_status"] = "UNAVAILABLE_MISSING_SNAPSHOT"  # type: ignore[index]

        def statement_missing_but_available(client: FakeCoreClient) -> None:
            company = client.company_report_payloads["ACCOUNT_STATEMENT"]["items"][0]  # type: ignore[index]
            month = company["months"][0]  # type: ignore[index]
            month["business_unit_breakdown_status"] = "UNAVAILABLE_MISSING_SNAPSHOT"  # type: ignore[index]
            month["business_units"] = None  # type: ignore[index]
            company["business_unit_breakdown_status"] = "AVAILABLE"  # type: ignore[index]

        def posted_missing_but_available(client: FakeCoreClient) -> None:
            company = client.company_report_payloads["POSTED_LEDGER"]["items"][0]  # type: ignore[index]
            month = company["months"][0]  # type: ignore[index]
            month["business_unit_breakdown_status"] = "UNAVAILABLE_MISSING_SNAPSHOT"  # type: ignore[index]
            month["business_units"] = None  # type: ignore[index]
            company["business_unit_breakdown_status"] = "AVAILABLE"  # type: ignore[index]

        for label, mutate in (
            ("no months", no_months_but_available),
            ("candidate empty", candidate_empty_but_available),
            ("statement pending priority", statement_pending_but_missing),
            ("statement missing priority", statement_missing_but_available),
            ("posted missing priority", posted_missing_but_available),
        ):
            with self.subTest(label=label):
                client = FakeCoreClient()
                mutate(client)

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).company_reports("2026-01", "2026-08")

                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_reject_inconsistent_business_unit_breakdown_shapes(self) -> None:
        invalid_shapes = (
            ("candidate unavailable", "CONFIRMED_CANDIDATE", "UNAVAILABLE_ATTRIBUTION_PENDING", None),
            ("candidate available empty", "CONFIRMED_CANDIDATE", "AVAILABLE", []),
            ("candidate empty null", "CONFIRMED_CANDIDATE", "EMPTY", None),
            ("statement unavailable list", "ACCOUNT_STATEMENT", "UNAVAILABLE_ATTRIBUTION_PENDING", []),
            ("statement wrong unavailable status", "ACCOUNT_STATEMENT", "UNAVAILABLE_MISSING_SNAPSHOT", None),
            ("posted unavailable list", "POSTED_LEDGER", "UNAVAILABLE_MISSING_SNAPSHOT", []),
            ("posted wrong unavailable status", "POSTED_LEDGER", "UNAVAILABLE_ATTRIBUTION_PENDING", None),
            ("unknown status", "POSTED_LEDGER", "UNKNOWN", None),
        )
        for label, basis, status, business_units in invalid_shapes:
            with self.subTest(label=label):
                client = FakeCoreClient()
                month = client.company_report_payloads[basis]["items"][0]["months"][0]  # type: ignore[index]
                month["business_unit_breakdown_status"] = status  # type: ignore[index]
                month["business_units"] = business_units  # type: ignore[index]

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).company_reports("2026-01", "2026-08")

                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_require_strictly_sorted_unique_aggregates(self) -> None:
        def append_company(payload: dict[str, object], company_ref: str) -> None:
            company = deepcopy(payload["items"][0])  # type: ignore[index]
            company["company_ref"] = company_ref
            payload["items"].append(company)  # type: ignore[union-attr]

        invalid_mutations = (
            (
                "duplicate company",
                lambda client: [
                    client.company_report_payloads[basis]["items"].append(  # type: ignore[union-attr]
                        deepcopy(client.company_report_payloads[basis]["items"][0])  # type: ignore[index]
                    )
                    for basis in REPORT_BASES
                ],
            ),
            (
                "unsorted company",
                lambda client: [
                    append_company(
                        client.company_report_payloads[basis],
                        "00000000-0000-4000-8000-000000000001",
                    )
                    for basis in REPORT_BASES
                ],
            ),
            (
                "duplicate month",
                lambda client: client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]["months"].append(  # type: ignore[index,union-attr]
                    deepcopy(client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]["months"][0])  # type: ignore[index]
                ),
            ),
            (
                "unsorted month",
                lambda client: client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]["months"].append(  # type: ignore[index,union-attr]
                    {
                        **deepcopy(client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]["months"][0]),  # type: ignore[index]
                        "month": "2026-07",
                    }
                ),
            ),
            (
                "duplicate business unit",
                lambda client: client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]["months"][0]["business_units"].append(  # type: ignore[index,union-attr]
                    deepcopy(client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]["months"][0]["business_units"][0])  # type: ignore[index]
                ),
            ),
            (
                "unsorted business unit",
                lambda client: client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]["months"][0]["business_units"].append(  # type: ignore[index,union-attr]
                    {
                        **deepcopy(client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]["months"][0]["business_units"][0]),  # type: ignore[index]
                        "business_unit_ref": "a-unit",
                    }
                ),
            ),
        )
        for label, mutate in invalid_mutations:
            with self.subTest(label=label):
                client = FakeCoreClient()
                mutate(client)

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).company_reports("2026-01", "2026-08")

                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_enforce_nested_cardinality_limits(self) -> None:
        def fifty_one_companies(client: FakeCoreClient) -> tuple[str, str]:
            for basis in REPORT_BASES:
                original = client.company_report_payloads[basis]["items"][0]  # type: ignore[index]
                client.company_report_payloads[basis]["items"] = [
                    {**deepcopy(original), "company_ref": f"10000000-0000-4000-8000-{index:012d}"}
                    for index in range(1, 52)
                ]
            return "2026-01", "2026-08"

        def twenty_five_months(client: FakeCoreClient) -> tuple[str, str]:
            month_values = [
                f"{year}-{month:02d}"
                for year in range(2024, 2027)
                for month in range(1, 13)
                if "2024-08" <= f"{year}-{month:02d}" <= "2026-08"
            ]
            for payload in client.company_report_payloads.values():
                payload["from_month"] = "2024-08"
                original = payload["items"][0]["months"][0]  # type: ignore[index]
                payload["items"][0]["months"] = [  # type: ignore[index]
                    {**deepcopy(original), "month": month}
                    for month in month_values
                ]
            return "2024-08", "2026-08"

        def fifty_one_business_units(client: FakeCoreClient) -> tuple[str, str]:
            payload = client.company_report_payloads["CONFIRMED_CANDIDATE"]
            month = payload["items"][0]["months"][0]  # type: ignore[index]
            original = month["business_units"][0]  # type: ignore[index]
            month["business_units"] = [  # type: ignore[index]
                {**deepcopy(original), "business_unit_ref": f"unit-{index:03d}"}
                for index in range(1, 52)
            ]
            return "2026-01", "2026-08"

        for label, mutate in (
            ("companies", fifty_one_companies),
            ("months", twenty_five_months),
            ("business units", fifty_one_business_units),
        ):
            with self.subTest(label=label):
                client = FakeCoreClient()
                from_month, to_month = mutate(client)

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).company_reports(from_month, to_month)

                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_enforce_fact_source_count_relationships(self) -> None:
        invalid_counts = (
            ("CONFIRMED_CANDIDATE", "source_count", 4),
            ("ACCOUNT_STATEMENT", "statement_count", 3),
            ("POSTED_LEDGER", "source_count", 3),
        )
        for basis, field, value in invalid_counts:
            with self.subTest(basis=basis):
                client = FakeCoreClient()
                metrics = client.company_report_payloads[basis]["items"][0]["metrics"]  # type: ignore[index]
                metrics[field] = value  # type: ignore[index]

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).company_reports("2026-01", "2026-08")

                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_require_material_count_and_taxonomy_version_as_a_pair(self) -> None:
        for label, missing_material_count, taxonomy_version in (
            ("count only", 1, None),
            ("taxonomy only", None, "taxonomy-v1"),
        ):
            with self.subTest(label=label):
                client = FakeCoreClient()
                company = client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]  # type: ignore[index]
                company["missing_material_count"] = missing_material_count  # type: ignore[index]
                company["taxonomy_version"] = taxonomy_version  # type: ignore[index]

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).company_reports("2026-01", "2026-08")

                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

        client = FakeCoreClient()
        company = client.company_report_payloads["CONFIRMED_CANDIDATE"]["items"][0]  # type: ignore[index]
        company["missing_material_count"] = 0  # type: ignore[index]
        company["taxonomy_version"] = "taxonomy-v1"  # type: ignore[index]

        reports = build_state(client).company_reports("2026-01", "2026-08")

        candidate_company = reports["layers"][0]["items"][0]  # type: ignore[index]
        self.assertEqual(candidate_company["missing_material_count"], 0)
        self.assertEqual(candidate_company["taxonomy_version"], "taxonomy-v1")

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
                    f"{base_url}/api/v1/company-reports",
                    headers={"Cookie": cookie},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    company_reports = json.load(response)
                self.assertEqual(company_reports, company_reports_bff())
                self.assertEqual(
                    client.calls[-1][1],
                    "/internal/v1/company-reports?from_month=2026-01&to_month=2026-08&basis=POSTED_LEDGER",
                )

                forwarded_call_count = len(client.calls)
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.server_port,
                    timeout=3,
                )
                connection.request(
                    "GET",
                    f"/api/v1/company-reports?company_ref={ENTITY_ID}",
                    headers={"Cookie": cookie},
                )
                rejected = connection.getresponse()
                problem = json.loads(rejected.read())
                connection.close()
                self.assertEqual(rejected.status, 400)
                self.assertEqual(problem["code"], "INVALID_COMPANY_REPORT_QUERY")
                self.assertEqual(len(client.calls), forwarded_call_count)

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
