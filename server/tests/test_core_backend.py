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
from collections.abc import Callable
from contextlib import closing
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

from server.app import COOKIE_NAME, create_server
from server.core_backend import (
    EVIDENCE_UNLOCK_CORE_PATH,
    CoreBackendError,
    CoreBackedState,
    sqlite_contains_business_facts,
)


CANDIDATE_ID = "30000000-0000-4000-8000-000000000003"
SECOND_CANDIDATE_ID = "30000000-0000-4000-8000-000000000004"
EVIDENCE_ID = "20000000-0000-4000-8000-000000000003"
ENTITY_ID = "10000000-0000-4000-8000-000000000001"
STATEMENT_ID = "70000000-0000-4000-8000-000000000007"
SECOND_STATEMENT_ID = "70000000-0000-4000-8000-000000000009"


def _decode_assertion(value: str) -> dict[str, object]:
    encoded = value.split(".")[1]
    padding = "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded + padding))
ACCOUNT_ID = "80000000-0000-4000-8000-000000000008"
SECOND_ACCOUNT_ID = "80000000-0000-4000-8000-000000000010"
ASSERTION_KEY = b"synthetic-web-core-assertion-key-0001"
CLASSIFICATION_GROUP_REF = "cg_0123456789abcdef0123456789abcdef"


def core_personal_finance() -> dict[str, object]:
    return {
        "contract_version": "ledgerbridge.personal-finance.v1",
        "snapshot_revision": "a" * 64,
        "owner_kind": "PERSON",
        "statement": {
            "statement_ref": STATEMENT_ID,
            "managed_account_ref": ACCOUNT_ID,
            "institution_code": "mybank",
            "account_suffix": "7968",
            "period_start": "2026-07-01",
            "period_end": "2026-07-02",
            "transaction_count": 2,
            "review_status": "CONFIRMED",
            "review_revision": 1,
        },
        "summary": {
            "currency": "CNY",
            "cash_inflow_minor": 10000,
            "cash_outflow_minor": 2500,
            "net_cash_flow_minor": 7500,
        },
        "items": [
            {
                "source_row_number": 2,
                "occurred_at": "2026-07-01T09:30:00+08:00",
                "amount_minor": 10000,
                "balance_minor": 20000,
                "currency": "CNY",
                "counterparty_name": "测试对方甲",
                "counterparty_account_masked": "******1234",
                "counterparty_institution": "测试银行",
                "transaction_name": "转入",
            },
            {
                "source_row_number": 3,
                "occurred_at": "2026-07-02T10:30:00+08:00",
                "amount_minor": -2500,
                "balance_minor": 17500,
                "currency": "CNY",
                "counterparty_name": "测试对方乙",
                "counterparty_account_masked": None,
                "counterparty_institution": None,
                "transaction_name": "消费",
            },
        ],
    }


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


def core_classification_group() -> dict[str, object]:
    return {
        "contract_version": "ledgerbridge.classification-group.v1",
        "group_ref": CLASSIFICATION_GROUP_REF,
        "accounting_month": "2026-08",
        "conditions": {
            "key_version": "ledgerbridge.classification-key.v1",
            "entity_ref": ENTITY_ID,
            "source_system": "alipay",
            "source_kind": "TRANSFER",
            "platform": "余额宝",
            "direction": "INFLOW",
            "transaction_type": "TRANSFER",
            "counterparty_key": "余额宝",
            "counterparty_label": "余额宝",
            "counterparty_basis": "EXACT_PLATFORM_SUMMARY_V1",
            "funding_instrument": "余额",
            "transaction_status": "SUCCESS",
            "currency": "CNY",
            "risk_signature": ["TRANSFER_REVIEW_REQUIRED"],
        },
        "members": [
            {
                "candidate_ref": candidate_ref,
                "short_id": f"C-{ordinal}",
                "revision": 1,
                "status": "PENDING",
                "amount_minor": amount_minor,
                "accounting_month": "2026-08",
                "confidence_basis_points": 9800,
                "review_risk_codes": ["TRANSFER_REVIEW_REQUIRED"],
                "amount_outlier": False,
                "batch_eligible": True,
                "one_click_eligible": False,
                "exclusion_codes": [],
            }
            for ordinal, (candidate_ref, amount_minor) in enumerate(
                ((CANDIDATE_ID, 12345), (SECOND_CANDIDATE_ID, 67890)),
                start=1,
            )
        ],
        "batch_member_count": 2,
        "one_click_member_count": 0,
        "terminal_statuses": [],
        "terminal_classifications": [],
        "rule_learning_eligible": False,
        "rule_learning_blocks": ["PROVISIONAL_BASIS", "REVIEW_RISK_PRESENT"],
        "active_rule": None,
    }


def core_classification_batch_receipt(operation_id: str) -> dict[str, object]:
    results: list[dict[str, object]] = []
    for ordinal, candidate_ref in enumerate((CANDIDATE_ID, SECOND_CANDIDATE_ID), start=3):
        candidate = core_candidate(status="CONFIRMED", revision=2)
        candidate["candidate_ref"] = candidate_ref
        candidate["short_id"] = f"C-R0A00{ordinal}"
        event = core_event()
        event["candidate_ref"] = candidate_ref
        event["prior_projection"] = deepcopy(candidate) | {"revision": 1, "status": "PENDING"}
        event["result_projection"] = deepcopy(candidate)
        member_operation_id = f"60000000-0000-4000-8000-00000000000{ordinal}"
        # Core returns the member decision receipt ID and the underlying state-event
        # operation ID as distinct, independently valid identifiers.
        event["operation_id"] = f"70000000-0000-4000-8000-00000000000{ordinal}"
        results.append(
            {
                "candidate_ref": candidate_ref,
                "operation_id": member_operation_id,
                "status": "APPLIED",
                "candidate": candidate,
                "events": [event],
            }
        )
    return {
        "contract_version": "ledgerbridge.classification-batch.v1",
        "operation_id": operation_id,
        "replayed": False,
        "group_ref": CLASSIFICATION_GROUP_REF,
        "accounting_month": "2026-08",
        "source_candidate_ref": CANDIDATE_ID,
        "target": {
            "business_unit_ref": "unit-demo-a",
            "category_code": "SETTLEMENT",
        },
        "acknowledged_risk_codes": ["TRANSFER_REVIEW_REQUIRED"],
        "results": results,
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


def core_company_report_composition(basis: str) -> dict[str, object]:
    if basis == "CONFIRMED_CANDIDATE":
        compositions = {
            "positive": {
                "total_minor": 800000,
                "fact_count": 3,
                "items": [
                    {
                        "category_code": "ROOM",
                        "category_label": "客房收入",
                        "amount_minor": 600000,
                        "fact_count": 2,
                    },
                    {
                        "category_code": "OTHER",
                        "category_label": "其他收入",
                        "amount_minor": 200000,
                        "fact_count": 1,
                    },
                ],
            },
            "negative": {
                "total_minor": 235000,
                "fact_count": 1,
                "items": [
                    {
                        "category_code": "SUPPLY",
                        "category_label": "经营物料",
                        "amount_minor": 235000,
                        "fact_count": 1,
                    }
                ],
            },
        }
    else:
        compositions = {
            "revenue": {
                "total_minor": 600000,
                "fact_count": 1,
                "items": [
                    {
                        "category_code": "ROOM",
                        "category_label": "客房收入",
                        "amount_minor": 600000,
                        "fact_count": 1,
                    }
                ],
            },
            "expense": {
                "total_minor": 100000,
                "fact_count": 1,
                "items": [
                    {
                        "category_code": "SUPPLY",
                        "category_label": "经营物料",
                        "amount_minor": 100000,
                        "fact_count": 1,
                    }
                ],
            },
        }
    return {
        "contract_version": "ledgerbridge.company-report-composition.v1",
        "basis": basis,
        "from_month": "2026-01",
        "to_month": "2026-08",
        "items": [
            {
                "company_ref": ENTITY_ID,
                "company_name": "演示公司",
                "currency": "CNY",
                "basis": basis,
                **compositions,
            }
        ],
    }
def core_original_reconciliation() -> dict[str, object]:
    columns = [
        {
            "column": chr(ord("A") + offset),
            "ordinal": offset + 1,
            "role": "SPACER" if offset in {5, 6} else "MAIN" if offset <= 4 else "DETAIL",
        }
        for offset in range(13)
    ]
    rows: list[dict[str, object]] = []
    for row_number in range(1, 41):
        cells: list[dict[str, object]] = []
        for column in (item["column"] for item in columns):
            cell: dict[str, object] = {
                "coordinate": f"{column}{row_number}",
                "column": column,
                "row_number": row_number,
                "kind": "BLANK",
                "label": None,
                "amount_minor": None,
                "currency": None,
                "gap_code": None,
                "source_fact_refs": [],
            }
            if row_number == 1 and column == "A":
                cell.update({"kind": "LABEL", "label": "示例科目"})
            elif row_number == 2 and column == "H":
                cell.update({
                    "kind": "AMOUNT",
                    "amount_minor": 12345,
                    "currency": "CNY",
                    "source_fact_refs": ["fact-confirmed-1"],
                })
            elif row_number == 2 and column == "I":
                cell.update({
                    "kind": "AMOUNT",
                    "amount_minor": -2345,
                    "currency": "CNY",
                    "source_fact_refs": ["fact-posted-1"],
                })
            elif row_number == 2 and column == "J":
                cell.update({
                    "kind": "GAP",
                    "label": None,
                    "gap_code": "MISSING_ECONOMIC_EFFECT",
                })
            elif row_number == 2 and column == "K":
                cell.update({
                    "kind": "AMOUNT",
                    "amount_minor": 10000,
                    "currency": "CNY",
                })
            cells.append(cell)
        rows.append({"row_number": row_number, "cells": cells})
    return {
        "contract_version": "ledgerbridge.original-reconciliation.v1",
        "taxonomy_version": "ledgerbridge.financial-foundation-blocker-taxonomy.v1",
        "layout_version": "ledgerbridge.original-reconciliation-layout.v1",
        "mapping_version": "ledgerbridge.original-reconciliation-mapping.v1",
        "is_complete": False,
        "posted_ledger_complete": True,
        "projection_gaps": ["MISSING_TIME_GRANULARITY"],
        "month": "2026-08",
        "scope": {"entity_ref": ENTITY_ID, "business_unit_ref": "unit-demo-a"},
        "columns": columns,
        "rows": rows,
        "totals": {
            "posted_income_minor": 12345,
            "posted_expense_minor": 2345,
            "posted_profit_minor": 10000,
            "opening_balance_minor": None,
            "closing_balance_minor": None,
            "mapped_cell_count": 2,
            "confirmed_candidate_amount_minor": 12345,
            "posted_amount_minor": -2345,
            "currency": "CNY",
        },
        "pending_review_count": 3,
        "confirmed_pending_posting_count": 2,
        "missing_material_count": 1,
        "unmapped_confirmed_count": 1,
        "sources": [
            {
                "source_kind": "CONFIRMED_CANDIDATE",
                "source_system": "synthetic_confirmed",
                "source_label": "已确认候选（脱敏）",
                "fact_count": 2,
                "mapped_fact_count": 1,
                "amount_minor": 12345,
            },
            {
                "source_kind": "POSTED_LEDGER",
                "source_system": "synthetic_posted",
                "source_label": "正式账簿（脱敏）",
                "fact_count": 1,
                "mapped_fact_count": 1,
                "amount_minor": -2345,
            },
            {
                "source_kind": "ACCOUNT_STATEMENT",
                "source_system": "synthetic_statement",
                "source_label": None,
                "fact_count": 1,
                "mapped_fact_count": 0,
                "amount_minor": 0,
            },
        ],
    }


def company_reports_bff() -> dict[str, object]:
    return {
        "contract_version": "ledgerbridge.company-reports-bff.v2",
        "from_month": "2026-01",
        "to_month": "2026-08",
        "posted_ledger_status": "AVAILABLE",
        "layers": [core_company_report_layer(basis) for basis in REPORT_BASES],
        "compositions": [
            core_company_report_composition(basis)
            for basis in ("CONFIRMED_CANDIDATE", "POSTED_LEDGER")
        ],
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
        self.company_report_composition_payloads = {
            basis: core_company_report_composition(basis)
            for basis in ("CONFIRMED_CANDIDATE", "POSTED_LEDGER")
        }
        self.personal_finance_payload = core_personal_finance()

    def json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, path, body, dict(headers or {})))
        if method == "POST" and path.startswith("/internal/v1/bank-statements/"):
            return {
                "contract_version": "ledgerbridge.bank-statement-review.v1",
                "statement_ref": STATEMENT_ID,
                "decision": "CONFIRMED",
                "revision": 2,
                "created": True,
            }
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
        if path.startswith("/internal/v1/company-reports?"):
            basis = next(
                value for value in REPORT_BASES if f"basis={value}" in path
            )
            return self.company_report_payloads[basis]
        if path.startswith("/internal/v1/company-report-composition?"):
            basis = next(
                value
                for value in ("CONFIRMED_CANDIDATE", "POSTED_LEDGER")
                if f"basis={value}" in path
            )
            return self.company_report_composition_payloads[basis]
        if path.startswith("/internal/v1/personal-finance?"):
            return self.personal_finance_payload
        if path.startswith("/internal/v1/original-reconciliations/"):
            return core_original_reconciliation()
        raise AssertionError(f"unexpected Core path: {path}")

    def evidence(self, path: str) -> dict[str, object]:
        self.calls.append(("GET", path, None, {}))
        return {
            "content": b"synthetic evidence",
            "content_type": "application/octet-stream",
            "disposition": "attachment",
            "filename": "evidence.bin",
        }


class MultipleStatementsCoreClient(FakeCoreClient):
    def json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        if method == "GET" and path.startswith("/internal/v1/personal-finance?") and (
            f"statement_ref={SECOND_STATEMENT_ID}" in path
        ):
            self.calls.append((method, path, body, dict(headers or {})))
            payload = deepcopy(core_personal_finance())
            statement = payload["statement"]
            assert isinstance(statement, dict)
            statement.update(
                {
                    "statement_ref": SECOND_STATEMENT_ID,
                    "managed_account_ref": SECOND_ACCOUNT_ID,
                    "institution_code": "ccb",
                    "account_suffix": "7564",
                }
            )
            return payload
        return super().json(method, path, body=body, headers=headers)


class ClassificationCoreClient(FakeCoreClient):
    def json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        if method == "GET" and path == "/internal/v1/candidate-classification-groups":
            self.calls.append((method, path, body, dict(headers or {})))
            return {
                "contract_version": "ledgerbridge.classification-groups.v1",
                "items": [core_classification_group()],
                "next_cursor": None,
            }
        if method == "POST" and path == (
            f"/internal/v1/candidate-classification-groups/{CLASSIFICATION_GROUP_REF}/decisions"
        ):
            assert body is not None
            assert headers is not None
            self.calls.append((method, path, body, dict(headers or {})))
            return core_classification_batch_receipt(str(headers["Idempotency-Key"]))
        return super().json(method, path, body=body, headers=headers)


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


class UnavailableCompanyReportCoreClient(FakeCoreClient):
    def __init__(self, basis: str, status: int = 503) -> None:
        super().__init__()
        self.unavailable_basis = basis
        self.unavailable_status = status

    def json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        if (
            method == "GET"
            and path.startswith("/internal/v1/company-reports?")
            and f"basis={self.unavailable_basis}" in path
        ):
            self.calls.append((method, path, body, dict(headers or {})))
            raise CoreBackendError(
                self.unavailable_status,
                {"code": "CORE_COMPANY_REPORT_UNAVAILABLE"},
            )
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
    personal_finance_enabled: bool = True,
    personal_finance_statement_refs: tuple[str, ...] | None = None,
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
        personal_finance_entity_ref=ENTITY_ID if personal_finance_enabled else None,
        personal_finance_statement_ref=(
            STATEMENT_ID
            if personal_finance_enabled and personal_finance_statement_refs is None
            else None
        ),
        personal_finance_statement_refs=(
            personal_finance_statement_refs if personal_finance_enabled else None
        ),
        evidence_unlock_path=evidence_unlock_path,
    )


class CoreBackedAdapterTests(unittest.TestCase):
    def test_classification_group_scope_and_atomic_batch_cross_the_core_boundary(self) -> None:
        client = ClassificationCoreClient()
        state = build_state(client)

        page = state.candidate_classification_groups()

        self.assertEqual(client.calls[-1][:2], ("GET", "/internal/v1/candidate-classification-groups"))
        self.assertEqual(page["items"][0]["group_ref"], CLASSIFICATION_GROUP_REF)  # type: ignore[index]
        self.assertEqual(page["items"][0]["batch_member_count"], 2)  # type: ignore[index]
        operation_id = str(uuid.uuid4())
        request: dict[str, object] = {
            "source_candidate_ref": CANDIDATE_ID,
            "accounting_month": "2026-08",
            "target": {
                "business_unit_ref": "unit-demo-a",
                "category_code": "SETTLEMENT",
            },
            "members": [
                {"candidate_ref": CANDIDATE_ID, "expected_revision": 1},
                {"candidate_ref": SECOND_CANDIDATE_ID, "expected_revision": 1},
            ],
            "reason": "逐笔核对相似交易后整组确认",
            "acknowledged_risk_codes": ["TRANSFER_REVIEW_REQUIRED"],
        }

        status, receipt = state.apply_candidate_classification_batch(
            CLASSIFICATION_GROUP_REF,
            operation_id,
            request,
        )

        self.assertEqual(status, 200, receipt)
        self.assertEqual(receipt["acknowledged_risk_codes"], ["TRANSFER_REVIEW_REQUIRED"])
        self.assertEqual(len(receipt["results"]), 2)
        method, path, body, headers = client.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(
            path,
            f"/internal/v1/candidate-classification-groups/{CLASSIFICATION_GROUP_REF}/decisions",
        )
        self.assertEqual(json.loads(body), request)
        self.assertEqual(headers["Idempotency-Key"], operation_id)
        version, encoded, signature = headers["X-LedgerBridge-User-Assertion"].split(".")
        self.assertEqual(version, "v1")
        signed = f"v1.{encoded}".encode("ascii")
        expected = hmac.new(ASSERTION_KEY, signed, hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        self.assertTrue(hmac.compare_digest(expected, supplied))
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(claims["canonical_path"], path)
        self.assertEqual(claims["resource_ref"], CANDIDATE_ID)
        self.assertEqual(claims["expected_revision"], 1)
        self.assertEqual(claims["body_sha256"], hashlib.sha256(body).hexdigest())

    def test_classification_group_preserves_core_created_at_order_when_uuid_order_differs(
        self,
    ) -> None:
        client = ClassificationCoreClient()
        original_json = client.json

        def core_created_at_order(*args: object, **kwargs: object) -> dict[str, object]:
            payload = original_json(*args, **kwargs)
            if args[0] == "GET":
                # Core orders by created_at first; that timestamp is not repeated on members.
                payload["items"][0]["members"].reverse()  # type: ignore[index]
            return payload

        client.json = core_created_at_order  # type: ignore[method-assign]
        state = build_state(client)

        page = state.candidate_classification_groups()

        self.assertEqual(
            [member["candidate_ref"] for member in page["items"][0]["members"]],  # type: ignore[index]
            [SECOND_CANDIDATE_ID, CANDIDATE_ID],
        )

    def test_classification_batch_accepts_a_self_contained_idempotent_replay(self) -> None:
        client = ClassificationCoreClient()
        original_json = client.json

        def replayed_contract(*args: object, **kwargs: object) -> dict[str, object]:
            payload = original_json(*args, **kwargs)
            if args[0] == "POST":
                payload["replayed"] = True
                for result in payload["results"]:  # type: ignore[index]
                    result["status"] = "REPLAYED"
            return payload

        client.json = replayed_contract  # type: ignore[method-assign]
        state = build_state(client)
        status, receipt = state.apply_candidate_classification_batch(
            CLASSIFICATION_GROUP_REF,
            str(uuid.uuid4()),
            {
                "source_candidate_ref": CANDIDATE_ID,
                "accounting_month": "2026-08",
                "target": {
                    "business_unit_ref": "unit-demo-a",
                    "category_code": "SETTLEMENT",
                },
                "members": [
                    {"candidate_ref": CANDIDATE_ID, "expected_revision": 1},
                    {"candidate_ref": SECOND_CANDIDATE_ID, "expected_revision": 1},
                ],
                "reason": "逐笔核对相似交易后整组确认",
                "acknowledged_risk_codes": ["TRANSFER_REVIEW_REQUIRED"],
            },
        )

        self.assertEqual(status, 200, receipt)
        self.assertIs(receipt["replayed"], True)
        self.assertEqual(receipt["acknowledged_risk_codes"], ["TRANSFER_REVIEW_REQUIRED"])
        self.assertEqual(
            [result["status"] for result in receipt["results"]],  # type: ignore[index]
            ["REPLAYED", "REPLAYED"],
        )

    def test_classification_group_adapter_rejects_malformed_scope_and_receipt(self) -> None:
        for mutation in (
            "wrong-entity",
            "active-rule",
            "unknown-risk",
            "non-string-risk",
            "mismatched-member-risk",
            "duplicate-member-within-group",
            "duplicate-member-across-groups",
            "missing-acknowledgements",
            "wrong-operation",
            "wrong-result-member",
            "wrong-event-binding",
            "invalid-event-operation",
            "duplicate-event-operation",
            "wrong-result-target",
            "broken-event-revision-chain",
        ):
            with self.subTest(mutation=mutation):
                client = ClassificationCoreClient()
                original_json = client.json

                def invalid_contract(
                    *args: object,
                    _mutation: str = mutation,
                    _original_json: Callable[..., dict[str, object]] = original_json,
                    **kwargs: object,
                ) -> dict[str, object]:
                    payload = _original_json(*args, **kwargs)
                    if _mutation == "wrong-entity" and args[0] == "GET":
                        payload["items"][0]["conditions"]["entity_ref"] = (  # type: ignore[index]
                            "10000000-0000-4000-8000-000000000099"
                        )
                    elif _mutation == "active-rule" and args[0] == "GET":
                        payload["items"][0]["active_rule"] = {  # type: ignore[index]
                            "contract_version": "ledgerbridge.classification-rule.v1",
                            "group_ref": CLASSIFICATION_GROUP_REF,
                        }
                    elif _mutation == "unknown-risk" and args[0] == "GET":
                        payload["items"][0]["conditions"]["risk_signature"] = [  # type: ignore[index]
                            "UNVERSIONED_FUTURE_RISK"
                        ]
                        for member in payload["items"][0]["members"]:  # type: ignore[index]
                            member["review_risk_codes"] = ["UNVERSIONED_FUTURE_RISK"]
                    elif _mutation == "non-string-risk" and args[0] == "GET":
                        payload["items"][0]["conditions"]["risk_signature"] = [[]]  # type: ignore[index]
                        for member in payload["items"][0]["members"]:  # type: ignore[index]
                            member["review_risk_codes"] = [[]]
                    elif _mutation == "mismatched-member-risk" and args[0] == "GET":
                        payload["items"][0]["members"][1]["review_risk_codes"] = []  # type: ignore[index]
                    elif _mutation == "duplicate-member-within-group" and args[0] == "GET":
                        payload["items"][0]["members"][1]["candidate_ref"] = CANDIDATE_ID  # type: ignore[index]
                    elif _mutation == "duplicate-member-across-groups" and args[0] == "GET":
                        duplicate = deepcopy(payload["items"][0])  # type: ignore[index]
                        duplicate["group_ref"] = f"cg_{'b' * 32}"
                        payload["items"].append(duplicate)  # type: ignore[union-attr]
                    elif _mutation == "missing-acknowledgements" and args[0] == "POST":
                        del payload["acknowledged_risk_codes"]
                    elif _mutation == "wrong-operation" and args[0] == "POST":
                        payload["operation_id"] = "70000000-0000-4000-8000-000000000007"
                    elif _mutation == "wrong-result-member" and args[0] == "POST":
                        unexpected_ref = "30000000-0000-4000-8000-000000000099"
                        result = payload["results"][1]  # type: ignore[index]
                        result["candidate_ref"] = unexpected_ref
                        result["candidate"]["candidate_ref"] = unexpected_ref
                        result["events"][0]["candidate_ref"] = unexpected_ref
                    elif _mutation == "wrong-event-binding" and args[0] == "POST":
                        first = payload["results"][0]  # type: ignore[index]
                        second = payload["results"][1]  # type: ignore[index]
                        first["events"][0]["candidate_ref"] = second["candidate_ref"]
                        first["events"][0]["operation_id"] = second["operation_id"]
                    elif _mutation == "invalid-event-operation" and args[0] == "POST":
                        payload["results"][0]["events"][0]["operation_id"] = "not-a-uuid"  # type: ignore[index]
                    elif _mutation == "duplicate-event-operation" and args[0] == "POST":
                        payload["results"][1]["events"][0]["operation_id"] = (  # type: ignore[index]
                            payload["results"][0]["events"][0]["operation_id"]  # type: ignore[index]
                        )
                    elif _mutation == "wrong-result-target" and args[0] == "POST":
                        payload["results"][0]["candidate"]["category_code"] = "OTHER"  # type: ignore[index]
                    elif _mutation == "broken-event-revision-chain" and args[0] == "POST":
                        payload["results"][0]["events"][0]["to_revision"] = 9  # type: ignore[index]
                    return payload

                client.json = invalid_contract  # type: ignore[method-assign]
                state = build_state(client)
                if mutation in {
                    "wrong-entity",
                    "active-rule",
                    "unknown-risk",
                        "non-string-risk",
                        "mismatched-member-risk",
                        "duplicate-member-within-group",
                        "duplicate-member-across-groups",
                }:
                    with self.assertRaises(CoreBackendError) as raised:
                        state.candidate_classification_groups()
                    self.assertEqual(raised.exception.status, 503)
                else:
                    status, payload = state.apply_candidate_classification_batch(
                        CLASSIFICATION_GROUP_REF,
                        str(uuid.uuid4()),
                        {
                            "source_candidate_ref": CANDIDATE_ID,
                            "accounting_month": "2026-08",
                            "target": {
                                "business_unit_ref": "unit-demo-a",
                                "category_code": "SETTLEMENT",
                            },
                            "members": [
                                {"candidate_ref": CANDIDATE_ID, "expected_revision": 1},
                                {
                                    "candidate_ref": SECOND_CANDIDATE_ID,
                                    "expected_revision": 1,
                                },
                            ],
                            "reason": "逐笔核对相似交易后整组确认",
                            "acknowledged_risk_codes": ["TRANSFER_REVIEW_REQUIRED"],
                        },
                    )
                    self.assertEqual(status, 503)
                    self.assertEqual(payload["code"], "CORE_CONTRACT_INVALID")

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

    def test_candidate_rejects_an_invalid_source_system(self) -> None:
        client = FakeCoreClient()
        client.candidate_payload["source"]["source_system"] = "../private"  # type: ignore[index]

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

    def test_personal_bank_transactions_use_only_server_bound_scope(self) -> None:
        client = FakeCoreClient()

        result = build_state(client).personal_bank_transactions()

        self.assertEqual(
            client.calls[-1][:2],
            (
                "GET",
                f"/internal/v1/personal-finance?statement_ref={STATEMENT_ID}&entity_ref={ENTITY_ID}",
            ),
        )
        self.assertEqual(
            result["contract_version"],
            "ledgerbridge.personal-bank-transactions-bff.v2",
        )
        self.assertEqual(result["statements"], [core_personal_finance()["statement"]])
        self.assertEqual(
            result["summary"],
            {
                **core_personal_finance()["summary"],
                "statement_count": 1,
                "transaction_count": 2,
            },
        )
        self.assertEqual(
            result["items"],
            [
                {"statement_ref": STATEMENT_ID, **item}
                for item in reversed(core_personal_finance()["items"])
            ],
        )

    def test_bank_statement_review_binds_server_entity_and_signed_revision(self) -> None:
        client = FakeCoreClient()
        operation_id = str(uuid.uuid4())

        status, payload = build_state(client).review_bank_statement(
            STATEMENT_ID,
            operation_id,
            {
                "expected_revision": 1,
                "decision": "CONFIRMED",
                "reason": "人工确认正式账单",
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["revision"], 2)
        method, path, body, headers = client.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, f"/internal/v1/bank-statements/{STATEMENT_ID}/reviews")
        self.assertEqual(headers["Idempotency-Key"], operation_id)
        assert body is not None
        self.assertEqual(json.loads(body)["entity_ref"], ENTITY_ID)
        claims = _decode_assertion(headers["X-LedgerBridge-User-Assertion"])
        self.assertEqual(claims["resource_ref"], STATEMENT_ID)
        self.assertEqual(claims["expected_revision"], 1)

    def test_personal_bank_transactions_merge_multiple_server_bound_statements(self) -> None:
        client = MultipleStatementsCoreClient()

        result = build_state(
            client,
            personal_finance_statement_refs=(STATEMENT_ID, SECOND_STATEMENT_ID),
        ).personal_bank_transactions()

        personal_calls = [
            call for call in client.calls if call[1].startswith("/internal/v1/personal-finance?")
        ]
        self.assertEqual(len(personal_calls), 2)
        self.assertEqual(result["summary"]["statement_count"], 2)  # type: ignore[index]
        self.assertEqual(result["summary"]["transaction_count"], 4)  # type: ignore[index]
        self.assertEqual(
            {statement["statement_ref"] for statement in result["statements"]},  # type: ignore[union-attr]
            {STATEMENT_ID, SECOND_STATEMENT_ID},
        )
        self.assertEqual(
            {item["statement_ref"] for item in result["items"]},  # type: ignore[union-attr]
            {STATEMENT_ID, SECOND_STATEMENT_ID},
        )

    def test_personal_bank_transactions_accept_a_statement_over_200_rows(self) -> None:
        client = FakeCoreClient()
        template = client.personal_finance_payload["items"][0]  # type: ignore[index]
        assert isinstance(template, dict)
        items = [
            {
                **template,
                "source_row_number": row_number,
                "amount_minor": 1,
                "balance_minor": row_number,
            }
            for row_number in range(1, 212)
        ]
        client.personal_finance_payload["items"] = items
        client.personal_finance_payload["statement"]["transaction_count"] = len(items)  # type: ignore[index]
        client.personal_finance_payload["summary"].update(  # type: ignore[union-attr]
            {
                "cash_inflow_minor": len(items),
                "cash_outflow_minor": 0,
                "net_cash_flow_minor": len(items),
            }
        )

        result = build_state(client).personal_bank_transactions()

        self.assertEqual(result["summary"]["transaction_count"], 211)  # type: ignore[index]
        self.assertEqual(len(result["items"]), 211)  # type: ignore[arg-type]

    def test_personal_bank_transactions_fail_closed_without_server_binding(self) -> None:
        client = FakeCoreClient()

        with self.assertRaises(CoreBackendError) as raised:
            build_state(
                client,
                personal_finance_enabled=False,
            ).personal_bank_transactions()

        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(
            raised.exception.payload["code"],
            "PERSONAL_BANK_FACTS_UNAVAILABLE",
        )
        self.assertFalse(any("personal-finance" in call[1] for call in client.calls))

    def test_personal_bank_transactions_reject_incomplete_or_inconsistent_core_facts(self) -> None:
        mutations: list[tuple[str, Callable[[dict[str, object]], None]]] = [
            (
                "private field",
                lambda payload: payload["items"][0].__setitem__("transaction_serial", "secret"),  # type: ignore[index,union-attr]
            ),
            (
                "total mismatch",
                lambda payload: payload["summary"].__setitem__("cash_inflow_minor", 9999),  # type: ignore[union-attr]
            ),
            (
                "unexpected duplicate count",
                lambda payload: payload["summary"].__setitem__("transaction_count", 2),  # type: ignore[union-attr]
            ),
            (
                "duplicate row",
                lambda payload: payload["items"][1].__setitem__("source_row_number", 2),  # type: ignore[index,union-attr]
            ),
            (
                "unmasked account",
                lambda payload: payload["items"][0].__setitem__("counterparty_account_masked", "62221234"),  # type: ignore[index,union-attr]
            ),
            (
                "invalid statement date",
                lambda payload: payload["statement"].__setitem__("period_end", "2026-02-31"),  # type: ignore[union-attr]
            ),
        ]
        for label, mutate in mutations:
            with self.subTest(label=label):
                client = FakeCoreClient()
                mutate(client.personal_finance_payload)
                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).personal_bank_transactions()
                self.assertEqual(raised.exception.status, 503)
                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_company_reports_preserve_authoritative_company_and_business_unit_scope(self) -> None:
        client = FakeCoreClient()

        report = build_state(client).company_reports("2026-01", "2026-08")

        self.assertEqual(report, company_reports_bff())
        self.assertEqual(
            client.calls[-5:],
            [
                (
                    "GET",
                    f"/internal/v1/company-reports?from_month=2026-01&to_month=2026-08&basis={basis}",
                    None,
                    {},
                )
                for basis in REPORT_BASES
            ]
            + [
                (
                    "GET",
                    f"/internal/v1/company-report-composition?from_month=2026-01&to_month=2026-08&basis={basis}",
                    None,
                    {},
                )
                for basis in ("CONFIRMED_CANDIDATE", "POSTED_LEDGER")
            ],
        )

    def test_company_reports_preserve_other_layers_when_posted_ledger_is_unavailable(self) -> None:
        for status in (404, 503):
            with self.subTest(status=status):
                client = UnavailableCompanyReportCoreClient("POSTED_LEDGER", status)

                report = build_state(client).company_reports("2026-01", "2026-08")

                self.assertEqual(report["posted_ledger_status"], "UNAVAILABLE")
                self.assertEqual(
                    [layer["basis"] for layer in report["layers"]],  # type: ignore[index]
                    ["CONFIRMED_CANDIDATE", "ACCOUNT_STATEMENT"],
                )
                self.assertEqual(
                    [item["basis"] for item in report["compositions"]],  # type: ignore[index]
                    ["CONFIRMED_CANDIDATE"],
                )

    def test_company_report_composition_must_reconcile_to_summary_totals(self) -> None:
        client = FakeCoreClient()
        candidate = client.company_report_composition_payloads[
            "CONFIRMED_CANDIDATE"
        ]["items"][0]  # type: ignore[index]
        candidate["positive"]["total_minor"] = 799999  # type: ignore[index]

        with self.assertRaises(CoreBackendError) as raised:
            build_state(client).company_reports("2026-01", "2026-08")

        self.assertEqual(raised.exception.status, 503)

    def test_company_report_composition_rejects_private_or_unsorted_categories(self) -> None:
        for mutation in ("private", "unsorted"):
            with self.subTest(mutation=mutation):
                client = FakeCoreClient()
                candidate = client.company_report_composition_payloads[
                    "CONFIRMED_CANDIDATE"
                ]["items"][0]  # type: ignore[index]
                positive = candidate["positive"]  # type: ignore[index]
                if mutation == "private":
                    positive["source_record_ids"] = ["must-not-leak"]  # type: ignore[index]
                else:
                    positive["items"].reverse()  # type: ignore[index]

                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).company_reports("2026-01", "2026-08")

                self.assertEqual(raised.exception.status, 503)

    def test_company_reports_do_not_mask_non_posted_layer_failures(self) -> None:
        client = UnavailableCompanyReportCoreClient("ACCOUNT_STATEMENT")

        with self.assertRaises(CoreBackendError) as raised:
            build_state(client).company_reports("2026-01", "2026-08")

        self.assertEqual(raised.exception.status, 503)

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

    def test_original_reconciliation_forwards_only_configured_scope_and_preserves_signed_minor_amounts(self) -> None:
        client = FakeCoreClient()

        projection = build_state(client).original_reconciliation(
            "2026-08",
            entity_ref=ENTITY_ID,
            business_unit_ref="unit-demo-a",
        )

        self.assertEqual(projection["totals"]["posted_income_minor"], 12345)  # type: ignore[index]
        self.assertEqual(projection["totals"]["posted_expense_minor"], 2345)  # type: ignore[index]
        self.assertEqual(projection["totals"]["mapped_cell_count"], 2)  # type: ignore[index]
        self.assertEqual(projection["projection_gaps"], ["MISSING_TIME_GRANULARITY"])
        derived_cell = projection["rows"][1]["cells"][10]  # type: ignore[index]
        self.assertEqual(derived_cell["kind"], "AMOUNT")
        self.assertEqual(derived_cell["source_fact_refs"], [])
        gap_cell = projection["rows"][1]["cells"][9]  # type: ignore[index]
        self.assertEqual(gap_cell["gap_code"], "MISSING_ECONOMIC_EFFECT")
        self.assertIsNone(gap_cell["label"])
        self.assertEqual(gap_cell["source_fact_refs"], [])
        self.assertEqual(projection["sources"][2]["source_kind"], "ACCOUNT_STATEMENT")  # type: ignore[index]
        method, path, _, _ = client.calls[-1]
        self.assertEqual(method, "GET")
        self.assertIn(f"entity_ref={ENTITY_ID}", path)
        self.assertIn("business_unit=unit-demo-a", path)

    def test_original_reconciliation_rejects_invalid_posted_totals_and_false_complete_claims(self) -> None:
        for mutation in (
            "posted_profit",
            "complete_with_gaps",
            "taxonomy",
            "missing_layout",
            "missing_mapping",
            "unsupported_gap",
            "gap_label",
            "unsupported_source",
            "incomplete_posted_with_totals",
            "invalid_column_role",
            "zero_fact_source",
            "unsupported_projection_gap",
            "extra_contract_field",
            "confirmed_count_mismatch",
            "unmapped_too_high",
            "complete_with_unmapped_posted",
        ):
            with self.subTest(mutation=mutation):
                client = FakeCoreClient()

                def invalid_projection(
                    method: str,
                    path: str,
                    *,
                    body: bytes | None = None,
                    headers: dict[str, str] | None = None,
                ) -> dict[str, object]:
                    if path.startswith("/internal/v1/original-reconciliations/"):
                        payload = core_original_reconciliation()
                        if mutation == "posted_profit":
                            payload["totals"]["posted_profit_minor"] = 99999  # type: ignore[index]
                        elif mutation == "complete_with_gaps":
                            payload["is_complete"] = True
                        elif mutation == "taxonomy":
                            payload["taxonomy_version"] = "untrusted-taxonomy.v1"
                        elif mutation == "missing_layout":
                            payload.pop("layout_version")
                        elif mutation == "missing_mapping":
                            payload.pop("mapping_version")
                        elif mutation == "unsupported_gap":
                            payload["rows"][1]["cells"][9]["gap_code"] = "UNKNOWN_GAP"  # type: ignore[index]
                        elif mutation == "gap_label":
                            payload["rows"][1]["cells"][9]["label"] = "GAP 不得携带标签"  # type: ignore[index]
                        elif mutation == "unsupported_source":
                            payload["sources"][0]["source_kind"] = "POSTED"  # type: ignore[index]
                        elif mutation == "invalid_column_role":
                            payload["columns"][1]["role"] = "DETAIL"  # type: ignore[index]
                        elif mutation == "zero_fact_source":
                            payload["sources"][0]["fact_count"] = 0  # type: ignore[index]
                        elif mutation == "unsupported_projection_gap":
                            payload["projection_gaps"] = ["FUTURE_UNKNOWN_GAP"]
                        elif mutation == "extra_contract_field":
                            payload["unexpected"] = "must fail closed"
                        elif mutation == "confirmed_count_mismatch":
                            payload["confirmed_pending_posting_count"] = 1
                        elif mutation == "unmapped_too_high":
                            payload["unmapped_confirmed_count"] = 3
                        elif mutation == "complete_with_unmapped_posted":
                            payload["is_complete"] = True
                            payload["projection_gaps"] = []
                            payload["pending_review_count"] = 0
                            payload["confirmed_pending_posting_count"] = 0
                            payload["missing_material_count"] = 0
                            payload["unmapped_confirmed_count"] = 0
                            payload["totals"]["opening_balance_minor"] = 0  # type: ignore[index]
                            payload["totals"]["closing_balance_minor"] = 0  # type: ignore[index]
                            payload["sources"] = [payload["sources"][1]]  # type: ignore[index]
                            payload["sources"][0]["mapped_fact_count"] = 0  # type: ignore[index]
                            gap = payload["rows"][1]["cells"][9]  # type: ignore[index]
                            gap.update(
                                {
                                    "kind": "BLANK",
                                    "gap_code": None,
                                    "source_fact_refs": [],
                                }
                            )
                        else:
                            payload["posted_ledger_complete"] = False
                        return payload
                    return FakeCoreClient.json(client, method, path, body=body, headers=headers)

                client.json = invalid_projection  # type: ignore[method-assign]
                with self.assertRaises(CoreBackendError) as raised:
                    build_state(client).original_reconciliation(
                        "2026-08",
                        entity_ref=None,
                        business_unit_ref=None,
                    )
                self.assertEqual(raised.exception.payload["code"], "CORE_CONTRACT_INVALID")

    def test_original_reconciliation_allows_unavailable_posted_ledger_without_zero_fallback(self) -> None:
        client = FakeCoreClient()

        def unavailable_posted_ledger(
            method: str,
            path: str,
            *,
            body: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> dict[str, object]:
            if path.startswith("/internal/v1/original-reconciliations/"):
                payload = core_original_reconciliation()
                payload["posted_ledger_complete"] = False
                for field in (
                    "posted_income_minor",
                    "posted_expense_minor",
                    "posted_profit_minor",
                    "posted_amount_minor",
                ):
                    payload["totals"][field] = None  # type: ignore[index]
                payload["rows"][1]["cells"][9].update(  # type: ignore[index]
                    {"gap_code": "POSTED_LEDGER_UNAVAILABLE", "label": None}
                )
                return payload
            return FakeCoreClient.json(client, method, path, body=body, headers=headers)

        client.json = unavailable_posted_ledger  # type: ignore[method-assign]
        projection = build_state(client).original_reconciliation(
            "2026-08",
            entity_ref=None,
            business_unit_ref=None,
        )

        self.assertFalse(projection["posted_ledger_complete"])
        self.assertIsNone(projection["totals"]["posted_income_minor"])  # type: ignore[index]
        self.assertEqual(
            projection["rows"][1]["cells"][9]["gap_code"],  # type: ignore[index]
            "POSTED_LEDGER_UNAVAILABLE",
        )

    def test_original_reconciliation_preserves_zero_for_an_available_empty_posted_ledger(self) -> None:
        client = FakeCoreClient()

        def empty_posted_ledger(
            method: str,
            path: str,
            *,
            body: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> dict[str, object]:
            if path.startswith("/internal/v1/original-reconciliations/"):
                payload = core_original_reconciliation()
                payload["posted_ledger_complete"] = True
                for field in (
                    "posted_income_minor",
                    "posted_expense_minor",
                    "posted_profit_minor",
                    "posted_amount_minor",
                ):
                    payload["totals"][field] = 0  # type: ignore[index]
                return payload
            return FakeCoreClient.json(client, method, path, body=body, headers=headers)

        client.json = empty_posted_ledger  # type: ignore[method-assign]
        projection = build_state(client).original_reconciliation(
            "2026-08",
            entity_ref=None,
            business_unit_ref=None,
        )

        self.assertTrue(projection["posted_ledger_complete"])
        self.assertEqual(projection["totals"]["posted_income_minor"], 0)  # type: ignore[index]
        self.assertEqual(projection["totals"]["posted_amount_minor"], 0)  # type: ignore[index]

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
        self.assertEqual(candidate["source_system"], "synthetic_boc_mail")
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
                request = urllib.request.Request(
                    f"{base_url}/api/v1/company-reports?from_month=2026-01&to_month=2026-08",
                    headers={"Cookie": cookie},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    company_reports = json.load(response)
                self.assertEqual(company_reports, company_reports_bff())
                self.assertEqual(
                    client.calls[-1][1],
                    "/internal/v1/company-report-composition?"
                    "from_month=2026-01&to_month=2026-08&basis=POSTED_LEDGER",
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

    def test_core_backed_bff_serves_original_reconciliation_and_rejects_cross_scope(self) -> None:
        client = FakeCoreClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "index.html").write_text("<main>review</main>", encoding="utf-8")
            server = create_server(
                "127.0.0.1",
                0,
                temp_dir,
                state=build_state(client),
                auth_manager=FakeAuthManager(),  # type: ignore[arg-type]
                mode="core-backed",
                trusted_proxy_cidrs="127.0.0.1/32",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                cookie = f"{COOKIE_NAME}=session-token"
                path = (
                    f"/api/v1/original-reconciliations/2026-08?"
                    f"entity_ref={ENTITY_ID}&business_unit=unit-demo-a"
                )
                request = urllib.request.Request(f"{base_url}{path}", headers={"Cookie": cookie})
                with urllib.request.urlopen(request, timeout=2) as response:
                    projection = json.load(response)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(projection["contract_version"], "ledgerbridge.original-reconciliation.v1")
                self.assertEqual(projection["columns"][0]["column"], "A")
                self.assertEqual(projection["columns"][-1]["column"], "M")

                other_entity = "10000000-0000-4000-8000-000000000099"
                request = urllib.request.Request(
                    f"{base_url}/api/v1/original-reconciliations/2026-08?entity_ref={other_entity}&business_unit=unit-demo-a",
                    headers={"Cookie": cookie},
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(caught.exception.code, 403)
                self.assertEqual(caught.exception.headers["Cache-Control"], "no-store")
                self.assertEqual(json.load(caught.exception)["code"], "SCOPE_NOT_AUTHORIZED")
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
        target: str = "/api/v1/evidence/unlocks",
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
            target,
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

    def test_rejects_unlock_query_without_logging_its_value(self) -> None:
        secret = "url-query-secret"
        capture = io.StringIO()
        with redirect_stdout(capture):
            status, problem = self.request(
                {
                    "source_ref": "24600000-0000-4000-8000-000000000001",
                    "password": "body-only-secret",
                },
                target=f"/api/v1/evidence/unlocks?password={secret}",
            )
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "INVALID_EVIDENCE_UNLOCK_REQUEST")
        self.assertEqual(self.client.unlock_calls, 0)
        self.assertNotIn(secret, capture.getvalue())
        self.assertNotIn("body-only-secret", capture.getvalue())

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
