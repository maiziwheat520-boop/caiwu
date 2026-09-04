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
from server.core_backend import (
    CoreBackedState,
    CoreBackendError,
    _validate_payroll_test_material_preview_payload,
)


ENTITY_ID = "10000000-0000-4000-8000-000000000001"
ASSERTION_KEY = b"synthetic-web-core-assertion-key-0001"
MATERIAL_ID = "material_live_2026_08"
TEST_BATCH_ID = "payroll_history_through_2026_08"
PROJECTION_REVISION = hashlib.sha256(b"payroll-live-7").hexdigest()
PROJECTION_ETAG = f'"{PROJECTION_REVISION}"'
VERIFICATION_EVIDENCE = (
    *(
        {
            "artifact_id": f"artifact_live_mybank_{index}_2026_08",
            "evidence_type": "MYBANK_STATEMENT",
        }
        for index in range(1, 6)
    ),
    {"artifact_id": "artifact_live_boc_2026_08", "evidence_type": "BOC_RECEIPT"},
    {
        "artifact_id": "artifact_live_wechat_2026_08",
        "evidence_type": "WECHAT_RECEIPT",
    },
)
VERIFICATION_EVIDENCE_IDS = [item["artifact_id"] for item in VERIFICATION_EVIDENCE]
TEST_PROJECTION_FACT_KEYS = (
    "data_scope",
    "test_batch_id",
    "company_id",
    "cutoff_date",
    "workspace_revision",
    "auto_test_ready",
    "payment_submission_supported",
    "payable",
    "submission_supported",
    "routing_counts",
    "materials",
)


def set_test_projection_revision(data: dict[str, object]) -> None:
    canonical = json.dumps(
        {key: data[key] for key in TEST_PROJECTION_FACT_KEYS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    revision = hashlib.sha256(canonical).hexdigest()
    data["projection_revision"] = revision
    data["etag"] = f'"{revision}"'


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
        self.test_workspace_data_updates: dict[str, object] = {}
        self.test_workspace_command_data_updates: dict[str, object] = {}
        self.test_material_preview_updates: dict[str, object] = {}
        self.legacy_workspace_updates: dict[str, object] = {}

    def test_workspace_projection(self) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": "payroll-ledgerbridge-test-projection/v1",
            "contract_version": "1.0.0",
            "data_scope": "TEST_ONLY",
            "test_batch_id": TEST_BATCH_ID,
            "company_id": "company_live_hotel",
            "cutoff_date": "2026-08-31",
            "workspace_revision": 1,
            "projection_revision": "",
            "etag": "",
            "generated_at": "2026-09-01T00:00:00.000Z",
            "auto_test_ready": True,
            "payment_submission_supported": False,
            "payable": False,
            "submission_supported": False,
            "routing_counts": {
                "auto_test": 4,
                "review_required": 0,
                "date_unknown": 0,
            },
            "materials": [
                {
                    "company_id": "company_live_hotel",
                    "material_id": "material_history_2026_08",
                    "routing_status": "AUTO_TEST",
                    "period": "2026-08",
                    "material_type": "PAYROLL_SHEET",
                    "payable": False,
                    "submission_supported": False,
                },
                {
                    "company_id": "company_live_hotel",
                    "material_id": "material_attendance",
                    "routing_status": "AUTO_TEST",
                    "period": "2026-07",
                    "material_type": "ATTENDANCE_SHEET",
                    "payable": False,
                    "submission_supported": False,
                },
                {
                    "company_id": "company_live_hotel",
                    "material_id": "material_aunt_attendance",
                    "routing_status": "AUTO_TEST",
                    "period": "2026-08",
                    "material_type": "AUNT_ATTENDANCE_SHEET",
                    "payable": False,
                    "submission_supported": False,
                },
                {
                    "company_id": "company_live_hotel",
                    "material_id": "material_authoritative_summary",
                    "routing_status": "AUTO_TEST",
                    "period": None,
                    "material_type": "PAYROLL_SUMMARY",
                    "payable": False,
                    "submission_supported": False,
                },
            ],
        }
        set_test_projection_revision(data)
        data.update(self.test_workspace_data_updates)
        return data

    def legacy_workspace(self) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": "payroll-legacy-feature-workspace/v1",
            "data_scope": "TEST_ONLY",
            "company_id": "company_live_hotel",
            "test_batch_id": TEST_BATCH_ID,
            "revision": 1,
            "active_period": "2026-08",
            "rules": {"revision": 0, "employees": []},
            "batches": [{
                "batch_id": f"{TEST_BATCH_ID}_2026_08",
                "period": "2026-08",
                "revision": 1,
                "main_material_id": "material_history_2026_08",
                "supporting_material_ids": {},
                "lines": [{
                    "source_row": 4,
                    "company_id": "company_live_hotel",
                    "employee_id": "emp_preview_001",
                    "employee_name": "示例员工甲",
                    "account_id": "acct_preview_001",
                    "account_masked": "****0138",
                    "payment_channel": "MYBANK",
                    "base_salary_cents": 500000,
                    "allowance_cents": 30000,
                    "bonus_cents": 20000,
                    "deduction_cents": 5000,
                    "social_insurance_cents": 18000,
                    "housing_fund_cents": 12000,
                    "individual_income_tax_cents": 15000,
                    "gross_pay_cents": 550000,
                    "net_pay_cents": 500000,
                    "notes": "脱敏测试材料",
                }],
                "adjustments": [],
                "source_exceptions": [],
                "drafts": [],
                "summary": None,
                "verification": None,
                "pending_items": [],
                "checks": None,
            }],
            "audit_events": [{
                "sequence": 1,
                "action": "payroll.main_filled",
                "period": "2026-08",
                "occurred_at": "2026-09-01T02:00:00.000Z",
                "reason": "受信工资表已进入网页测试主表",
            }],
            "payment_submission_supported": False,
            "payable": False,
            "submission_supported": False,
        }
        data.update(self.legacy_workspace_updates)
        return data

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
        if method == "POST" and path == "/internal/v1/payroll/test-workspaces":
            request = json.loads(body or b"{}")
            return {
                "contract_version": "ledgerbridge.payroll-test-workspace-command-result.v1",
                "entity_ref": ENTITY_ID,
                "company_id": "company_live_hotel",
                "action": "payroll.test_workspace.create",
                "resource_ref": request["test_batch_id"],
                "replayed": False,
                "data": self.test_workspace_projection(),
            }
        if (
            method == "POST"
            and path
            == (
                f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/materials/"
                "material_attendance/organize"
            )
        ):
            request = json.loads(body or b"{}")
            data: dict[str, object] = {
                "schema_version": "payroll-test-material-organize-result/v1",
                "data_scope": "TEST_ONLY",
                "test_batch_id": TEST_BATCH_ID,
                "company_id": "company_live_hotel",
                "workspace_revision": request["expected_workspace_revision"] + 1,
                "projection_revision": "a" * 64,
                "material": {
                    "company_id": "company_live_hotel",
                    "material_id": "material_attendance",
                    "routing_status": "AUTO_TEST",
                    "period": request["period"],
                    "material_type": request["material_type"],
                    "payable": False,
                    "submission_supported": False,
                },
                "payment_submission_supported": False,
                "payable": False,
                "submission_supported": False,
                "replayed": False,
            }
            data.update(self.test_workspace_command_data_updates)
            return {
                "contract_version": "ledgerbridge.payroll-test-workspace-command-result.v1",
                "entity_ref": ENTITY_ID,
                "company_id": "company_live_hotel",
                "action": "payroll.test_workspace.organize",
                "resource_ref": "material_attendance",
                "replayed": False,
                "data": data,
            }
        if (
            method == "POST"
            and path == f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/validate"
        ):
            request = json.loads(body or b"{}")
            return {
                "contract_version": "ledgerbridge.payroll-test-workspace-command-result.v1",
                "entity_ref": ENTITY_ID,
                "company_id": "company_live_hotel",
                "action": "payroll.test_workspace.validate",
                "resource_ref": TEST_BATCH_ID,
                "replayed": False,
                "data": {
                    "schema_version": "payroll-test-batch-validation-result/v1",
                    "data_scope": "TEST_ONLY",
                    "test_batch_id": TEST_BATCH_ID,
                    "company_id": "company_live_hotel",
                    "workspace_revision": request["expected_workspace_revision"],
                    "ready_batch_count": 1,
                    "blocked_material_count": 0,
                    "batches": [{
                        "batch_id": f"{TEST_BATCH_ID}_2026_08",
                        "period": "2026-08",
                        "material_count": 2,
                        "payroll_sheet_count": 1,
                        "supporting_material_count": 1,
                        "status": "READY_FOR_TEST_REVIEW",
                    }],
                    "payment_submission_supported": False,
                    "payable": False,
                    "submission_supported": False,
                    "replayed": False,
                },
            }
        if (
            method == "POST"
            and path
            == f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/legacy-features/commands"
        ):
            request = json.loads(body or b"{}")
            workspace = self.legacy_workspace()
            workspace["revision"] = request["expected_revision"] + 1
            return {
                "contract_version": "ledgerbridge.payroll-legacy-feature-command-result.v1",
                "entity_ref": ENTITY_ID,
                "company_id": "company_live_hotel",
                "action": "payroll.test_workspace.legacy.command",
                "resource_ref": TEST_BATCH_ID,
                "replayed": False,
                "data": {
                    "action": request["action"],
                    "replayed": False,
                    "workspace": workspace,
                },
            }
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
            if path == (
                f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/materials/"
                "material_authoritative_summary/preview"
            ):
                return {
                    "contract_version":
                        "ledgerbridge.payroll-test-material-preview-read.v1",
                    "entity_ref": self.response_entity_ref,
                    "company_id": "company_live_hotel",
                    "material_id": "material_authoritative_summary",
                    "data": {
                        "schema_version": "payroll-summary-authoritative-preview/v1",
                        "data_scope": "TEST_ONLY",
                        "test_batch_id": TEST_BATCH_ID,
                        "company_id": "company_live_hotel",
                        "material_id": "material_authoritative_summary",
                        "routing_status": "AUTO_TEST",
                        "source_of_truth": "PAYROLL_SUMMARY",
                        "authoritative": True,
                        "period_count": 1,
                        "latest_period": "2026-07",
                        "periods": [{
                            "period": "2026-07",
                            "store_count": 2,
                            "stores": [
                                {"store_name": "青居客", "net_pay_cents": 3_242_000},
                                {"store_name": "同富", "net_pay_cents": 14_019_198},
                            ],
                            "total_net_pay_cents": 17_261_198,
                            "total_source": "SUMMARY_TOTAL_ROW",
                            "total_matches_stores": True,
                        }],
                        "payment_submission_supported": False,
                        "payable": False,
                        "submission_supported": False,
                    },
                }
            if path == (
                f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/materials/"
                "material_attendance/preview"
            ):
                return {
                    "contract_version":
                        "ledgerbridge.payroll-test-material-preview-read.v1",
                    "entity_ref": self.response_entity_ref,
                    "company_id": "company_live_hotel",
                    "material_id": "material_attendance",
                    "data": {
                        "schema_version": "payroll-input-material-preview/v1",
                        "data_scope": "TEST_ONLY",
                        "test_batch_id": TEST_BATCH_ID,
                        "company_id": "company_live_hotel",
                        "material_id": "material_attendance",
                        "period": "2026-07",
                        "material_type": "ATTENDANCE_SHEET",
                        "detected_material_type": "AUNT_ATTENDANCE_SHEET",
                        "canonical_name": "2026.7_阿姨考勤表",
                        "selected_sheet": "阿姨考勤",
                        "sheet_names": ["阿姨考勤"],
                        "columns": ["姓名", "考勤天数", "工资合计"],
                        "record_count": 2,
                        "preview_rows": [
                            {"source_row": 2, "values": ["员工甲", "26", "5000"]},
                            {"source_row": 3, "values": ["员工乙", "25", "4800"]},
                        ],
                        "status": "READY_FOR_REVIEW",
                        "payment_submission_supported": False,
                        "payable": False,
                        "submission_supported": False,
                    },
                }
            if path == (
                f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/materials/"
                "material_history_2026_08/preview"
            ):
                data = {
                    "schema_version": "payroll-test-material-preview/v1",
                    "data_scope": "TEST_ONLY",
                    "test_batch_id": TEST_BATCH_ID,
                    "company_id": "company_live_hotel",
                    "material_id": "material_history_2026_08",
                    "period": "2026-08",
                    "routing_status": "AUTO_TEST",
                    "auto_batch_eligible": True,
                    "status": "READY_FOR_REVIEW",
                    "line_count": 1,
                    "total_net_pay_cents": 500000,
                    "lines": [{
                        "source_row": 4,
                        "company_id": "company_live_hotel",
                        "employee_id": "emp_preview_001",
                        "employee_name": "示例员工甲",
                        "account_id": "acct_preview_001",
                        "account_masked": "****0138",
                        "payment_channel": "MYBANK",
                        "base_salary_cents": 500000,
                        "allowance_cents": 30000,
                        "bonus_cents": 20000,
                        "deduction_cents": 5000,
                        "social_insurance_cents": 18000,
                        "housing_fund_cents": 12000,
                        "individual_income_tax_cents": 15000,
                        "gross_pay_cents": 550000,
                        "net_pay_cents": 500000,
                        "notes": "脱敏测试材料",
                    }],
                    "exceptions": [],
                    "payment_submission_supported": False,
                    "payable": False,
                    "submission_supported": False,
                }
                data.update(self.test_material_preview_updates)
                return {
                    "contract_version":
                        "ledgerbridge.payroll-test-material-preview-read.v1",
                    "entity_ref": self.response_entity_ref,
                    "company_id": "company_live_hotel",
                    "material_id": "material_history_2026_08",
                    "data": data,
                }
            if path == f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}":
                data = self.test_workspace_projection()
                return {
                    "contract_version": "ledgerbridge.payroll-test-workspace-read.v1",
                    "entity_ref": self.response_entity_ref,
                    "company_id": "company_live_hotel",
                    "data": data,
                }
            if path == f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/legacy-features":
                return {
                    "contract_version": "ledgerbridge.payroll-legacy-feature-read.v1",
                    "entity_ref": self.response_entity_ref,
                    "company_id": "company_live_hotel",
                    "data": self.legacy_workspace(),
                }
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
                            "source_artifact_ids": list(VERIFICATION_EVIDENCE_IDS),
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
                    available_evidence = [
                        {
                            "company_id": "company_live_hotel",
                            **evidence,
                            "period": "2026-08",
                            "status": "READY_FOR_MATCHING",
                            "display_label": f"{evidence['evidence_type']} · 2026-08",
                        }
                        for evidence in VERIFICATION_EVIDENCE
                    ]
                    available_evidence[0].update(self.verification_evidence_updates)
                    data["available_evidence"] = available_evidence
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
    payroll_test_workspace_enabled: bool = False,
    payroll_test_workspace_autocreate: bool = False,
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
        payroll_test_workspace_enabled=payroll_test_workspace_enabled,
        payroll_test_batch_id=TEST_BATCH_ID if payroll_test_workspace_enabled else None,
        payroll_test_workspace_autocreate=payroll_test_workspace_autocreate,
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
            state=build_state(self.client, payroll_test_workspace_enabled=True),
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

    def test_authenticated_session_reads_july_august_test_workspace(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/api/v1/payroll/test-workspace",
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)

        self.assertEqual(payload["data"]["data_scope"], "TEST_ONLY")
        self.assertEqual(
            payload["data"]["routing_counts"],
            {"auto_test": 4, "review_required": 0, "date_unknown": 0},
        )
        self.assertFalse(payload["data"]["payment_submission_supported"])
        method, path, body, headers = self.client.calls[-1]
        self.assertEqual(
            (method, path, body),
            (
                "GET",
                f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}",
                None,
            ),
        )
        _, encoded, _ = headers["X-LedgerBridge-User-Assertion"].split(".")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(claims["action"], "payroll.test_workspace.read")
        self.assertEqual(claims["resource_ref"], TEST_BATCH_ID)
        self.assertEqual(claims["canonical_path"], path)

    def test_authenticated_session_previews_masked_test_payroll_spreadsheet_lines(self) -> None:
        material_id = "material_history_2026_08"
        request = urllib.request.Request(
            (
                f"http://127.0.0.1:{self.server.server_port}"
                f"/api/v1/payroll/test-workspace/materials/{material_id}/preview"
            ),
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)

        self.assertEqual(
            payload["contract_version"],
            "ledgerbridge.payroll-test-material-preview-read.v1",
        )
        self.assertEqual(payload["data"]["line_count"], 1)
        self.assertEqual(payload["data"]["lines"][0]["account_masked"], "****0138")
        self.assertFalse(payload["data"]["payment_submission_supported"])
        method, path, body, headers = self.client.calls[-1]
        self.assertEqual(
            (method, path, body),
            (
                "GET",
                (
                    f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/materials/"
                    f"{material_id}/preview"
                ),
                None,
            ),
        )
        _, encoded, _ = headers["X-LedgerBridge-User-Assertion"].split(".")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(claims["action"], "payroll.test_workspace.read")
        self.assertEqual(claims["resource_ref"], material_id)

    def test_authenticated_session_previews_renamed_wage_input_rows(self) -> None:
        material_id = "material_attendance"
        request = urllib.request.Request(
            (
                f"http://127.0.0.1:{self.server.server_port}"
                f"/api/v1/payroll/test-workspace/materials/{material_id}/preview"
            ),
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)

        self.assertEqual(payload["data"]["canonical_name"], "2026.7_阿姨考勤表")
        self.assertEqual(payload["data"]["record_count"], 2)
        self.assertEqual(payload["data"]["preview_rows"][0]["values"][0], "员工甲")
        self.assertFalse(payload["data"]["submission_supported"])

    def test_authenticated_session_reads_authoritative_monthly_store_summary(self) -> None:
        material_id = "material_authoritative_summary"
        request = urllib.request.Request(
            (
                f"http://127.0.0.1:{self.server.server_port}"
                f"/api/v1/payroll/test-workspace/materials/{material_id}/preview"
            ),
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)

        self.assertEqual(payload["data"]["source_of_truth"], "PAYROLL_SUMMARY")
        self.assertEqual(payload["data"]["latest_period"], "2026-07")
        self.assertEqual(
            payload["data"]["periods"][0]["total_net_pay_cents"],
            17_261_198,
        )
        self.assertFalse(payload["data"]["submission_supported"])

    def test_test_payroll_preview_rejects_full_account_or_float_money(self) -> None:
        state = build_state(self.client, payroll_test_workspace_enabled=True)
        self.client.test_material_preview_updates = {"total_net_pay_cents": 512000.0}
        with self.assertRaises(CoreBackendError):
            state.payroll_test_material_preview(
                "session-token", "ledgerbridge-owner", "material_history_2026_08"
            )
        self.client.test_material_preview_updates = {}
        payload = self.client.json(
            "GET",
            (
                f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/materials/"
                "material_history_2026_08/preview"
            ),
        )
        payload["data"]["lines"][0]["account_masked"] = "6222000000000138"
        with self.assertRaises(CoreBackendError):
            _validate_payroll_test_material_preview_payload(
                payload,
                expected_entity_ref=ENTITY_ID,
                expected_batch_id=TEST_BATCH_ID,
                expected_material_id="material_history_2026_08",
            )

    def test_test_workspace_material_can_be_organized_and_batches_validated(self) -> None:
        organize_status, organized = self.post_payroll(
            "/api/v1/payroll/test-workspace/materials/material_attendance/organize",
            {
                "expected_workspace_revision": 1,
                "period": "2026-08",
                "material_type": "PAYROLL_SHEET",
            },
            idempotency_key="70000000-0000-4000-8000-000000000011",
        )
        self.assertEqual(organize_status, 200, organized)
        self.assertEqual(organized["action"], "payroll.test_workspace.organize")
        self.assertFalse(organized["data"]["payable"])

        validate_status, validated = self.post_payroll(
            "/api/v1/payroll/test-workspace/validate",
            {"expected_workspace_revision": 2},
            idempotency_key="70000000-0000-4000-8000-000000000012",
        )
        self.assertEqual(validate_status, 200, validated)
        self.assertEqual(validated["action"], "payroll.test_workspace.validate")
        self.assertEqual(validated["data"]["ready_batch_count"], 1)
        self.assertFalse(validated["data"]["payment_submission_supported"])

        organize_call, validate_call = self.client.calls[-2:]
        self.assertEqual(
            organize_call[:2],
            (
                "POST",
                f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/materials/material_attendance/organize",
            ),
        )
        self.assertEqual(
            validate_call[:2],
            ("POST", f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/validate"),
        )
        organize_request = json.loads(organize_call[2] or b"{}")
        validate_request = json.loads(validate_call[2] or b"{}")
        self.assertEqual(
            set(organize_request),
            {
                "schema_version",
                "expected_workspace_revision",
                "period",
                "material_type",
                "idempotency_key",
                "explicitly_confirmed",
            },
        )
        self.assertEqual(
            set(validate_request),
            {
                "schema_version",
                "expected_workspace_revision",
                "idempotency_key",
                "explicitly_confirmed",
            },
        )
        self.assertTrue(organize_request["explicitly_confirmed"])
        self.assertTrue(validate_request["explicitly_confirmed"])

        _, organize_encoded, _ = organize_call[3][
            "X-LedgerBridge-User-Assertion"
        ].split(".")
        organize_claims = json.loads(
            base64.urlsafe_b64decode(
                organize_encoded + "=" * (-len(organize_encoded) % 4)
            )
        )
        self.assertEqual(organize_claims["action"], "payroll.test_workspace.organize")
        self.assertEqual(organize_claims["resource_ref"], "material_attendance")

    def test_legacy_feature_workspace_fills_and_reloads_through_request_bound_bff(self) -> None:
        read_request = urllib.request.Request(
            (
                f"http://127.0.0.1:{self.server.server_port}"
                "/api/v1/payroll/legacy-workspace"
            ),
            headers={"Cookie": f"{COOKIE_NAME}=session-token"},
        )
        with urllib.request.urlopen(read_request, timeout=2) as response:
            read_payload = json.load(response)
        self.assertEqual(read_payload["data"]["revision"], 1)
        self.assertFalse(read_payload["data"]["payable"])

        status, command = self.post_payroll(
            "/api/v1/payroll/legacy-workspace/commands",
            {
                "action": "FILL_MAIN",
                "expected_revision": 1,
                "payload": {
                    "main_material_id": "material_history_2026_08",
                    "supporting_material_ids": {},
                    "adjustments": [],
                },
            },
            idempotency_key="70000000-0000-4000-8000-000000000021",
        )
        self.assertEqual(status, 200, command)
        self.assertEqual(command["data"]["workspace"]["revision"], 2)
        self.assertFalse(command["data"]["workspace"]["submission_supported"])
        method, path, body, headers = self.client.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(
            path,
            f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}/legacy-features/commands",
        )
        provider_request = json.loads(body or b"{}")
        self.assertEqual(
            set(provider_request),
            {
                "schema_version",
                "action",
                "expected_revision",
                "idempotency_key",
                "explicitly_confirmed",
                "payload",
            },
        )
        _, encoded, _ = headers["X-LedgerBridge-User-Assertion"].split(".")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(claims["action"], "payroll.test_workspace.legacy.command")
        self.assertEqual(claims["resource_ref"], TEST_BATCH_ID)
        self.assertEqual(claims["expected_revision"], 1)

    def test_legacy_feature_workspace_rejects_payment_mode_drift(self) -> None:
        state = build_state(self.client, payroll_test_workspace_enabled=True)
        self.client.legacy_workspace_updates = {"payable": True}
        with self.assertRaises(CoreBackendError) as raised:
            state.payroll_legacy_workspace("session-token", "ledgerbridge-owner")
        self.assertEqual(raised.exception.payload["code"], "PAYROLL_PAYMENT_MODE_NOT_ALLOWED")

    def test_legacy_feature_workspace_accepts_generated_batch_without_main_material(self) -> None:
        state = build_state(self.client, payroll_test_workspace_enabled=True)
        batches = self.client.legacy_workspace()["batches"]
        batches[0].pop("main_material_id")
        self.client.legacy_workspace_updates = {"batches": batches}

        workspace = state.payroll_legacy_workspace(
            "session-token", "ledgerbridge-owner"
        )

        self.assertNotIn("main_material_id", workspace["data"]["batches"][0])

    def test_legacy_feature_workspace_accepts_rules_before_first_batch(self) -> None:
        state = build_state(self.client, payroll_test_workspace_enabled=True)
        self.client.legacy_workspace_updates = {
            "rules": {
                "revision": 1,
                "employees": [],
                "review_rules": [{
                    "rule_id": "review_supporting_materials",
                    "name": "三类工资素材必须齐全",
                    "rule_type": "SUPPORTING_MATERIAL_REQUIRED",
                    "enabled": True,
                    "severity": "REVIEW",
                    "threshold_cents": 0,
                }],
            },
            "batches": [],
        }

        workspace = state.payroll_legacy_workspace(
            "session-token", "ledgerbridge-owner"
        )

        self.assertEqual(len(workspace["data"]["rules"]["review_rules"]), 1)
        self.assertEqual(workspace["data"]["batches"], [])

    def test_legacy_feature_workspace_accepts_canonical_opaque_account_id(self) -> None:
        state = build_state(self.client, payroll_test_workspace_enabled=True)
        workspace = self.client.legacy_workspace()
        workspace["batches"][0]["lines"][0]["account_id"] = (
            "account_123456789012345678901234"
        )
        self.client.legacy_workspace_updates = workspace

        result = state.payroll_legacy_workspace(
            "session-token", "ledgerbridge-owner"
        )

        self.assertEqual(
            result["data"]["batches"][0]["lines"][0]["account_id"],
            "account_123456789012345678901234",
        )

    def test_test_workspace_command_rejects_payment_mode_drift(self) -> None:
        self.client.test_workspace_command_data_updates = {"payable": True}
        status, payload = self.post_payroll(
            "/api/v1/payroll/test-workspace/materials/material_attendance/organize",
            {
                "expected_workspace_revision": 1,
                "period": "2026-08",
                "material_type": "PAYROLL_SHEET",
            },
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "CORE_CONTRACT_INVALID")

    def test_test_workspace_is_disabled_by_default(self) -> None:
        state = build_state(self.client)
        with self.assertRaises(CoreBackendError) as raised:
            state.payroll_test_workspace("session-token", "ledgerbridge-owner")
        self.assertEqual(
            (raised.exception.status, raised.exception.payload["code"]),
            (404, "PAYROLL_TEST_WORKSPACE_DISABLED"),
        )
        self.assertEqual(self.client.calls, [])

    def test_missing_test_workspace_accepts_core_direct_projection_on_create(self) -> None:
        self.client.failure = CoreBackendError(
            404,
            {"status": 404, "code": "PAYROLL_TEST_WORKSPACE_NOT_FOUND"},
        )
        self.client.failure_method = "GET"
        state = build_state(
            self.client,
            payroll_test_workspace_enabled=True,
            payroll_test_workspace_autocreate=True,
        )

        payload = state.payroll_test_workspace("session-token", "ledgerbridge-owner")

        self.assertEqual(payload["data"]["data_scope"], "TEST_ONLY")
        self.assertEqual([call[:2] for call in self.client.calls], [
            ("GET", f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}"),
            ("POST", "/internal/v1/payroll/test-workspaces"),
        ])
        request = json.loads(self.client.calls[1][2] or b"{}")
        self.assertEqual(request["test_batch_id"], TEST_BATCH_ID)
        self.assertEqual(request["cutoff_date"], "2026-08-31")
        self.assertEqual(request["expected_store_revision"], 0)
        _, encoded, _ = self.client.calls[1][3][
            "X-LedgerBridge-User-Assertion"
        ].split(".")
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual(claims["action"], "payroll.test_workspace.create")
        self.assertEqual(claims["operation_id"], request["idempotency_key"])
        self.assertEqual(claims["expected_revision"], 0)

    def test_autocreate_does_not_upgrade_an_unrelated_core_not_found(self) -> None:
        self.client.failure = CoreBackendError(
            404,
            {"status": 404, "code": "PAYROLL_TEST_WORKSPACE_DISABLED"},
        )
        self.client.failure_method = "GET"
        state = build_state(
            self.client,
            payroll_test_workspace_enabled=True,
            payroll_test_workspace_autocreate=True,
        )

        with self.assertRaises(CoreBackendError) as raised:
            state.payroll_test_workspace("session-token", "ledgerbridge-owner")

        self.assertEqual(raised.exception.payload["code"], "PAYROLL_TEST_WORKSPACE_DISABLED")
        self.assertEqual([call[:2] for call in self.client.calls], [
            ("GET", f"/internal/v1/payroll/test-workspaces/{TEST_BATCH_ID}"),
        ])

    def test_test_workspace_fails_closed_on_scope_routing_and_safety_drift(self) -> None:
        state = build_state(self.client, payroll_test_workspace_enabled=True)
        invalid_updates = [
            {"company_id": "company_other"},
            {"payment_submission_supported": True},
            {"auto_test_ready": False},
            {"workspace_revision": 0},
            {"routing_counts": {"auto_test": 3, "review_required": 0, "date_unknown": 0}},
            {
                "materials": [
                    {
                        "company_id": "company_live_hotel",
                        "material_id": "material_review_2026_09",
                        "routing_status": "AUTO_TEST",
                        "period": "2026-09",
                        "material_type": "PAYROLL_SHEET",
                        "payable": False,
                        "submission_supported": False,
                    }
                ],
                "routing_counts": {"auto_test": 1, "review_required": 0, "date_unknown": 0},
            },
        ]
        for update in invalid_updates:
            with self.subTest(update=update):
                self.client.test_workspace_data_updates = update
                with self.assertRaises(CoreBackendError) as raised:
                    state.payroll_test_workspace("session-token", "ledgerbridge-owner")
                self.assertEqual(
                    (raised.exception.status, raised.exception.payload["code"]),
                    (503, "CORE_CONTRACT_INVALID"),
                )
        self.client.test_workspace_data_updates = {}

    def test_summary_material_may_keep_a_safe_derived_period(self) -> None:
        projection = self.client.test_workspace_projection()
        materials = projection["materials"]
        assert isinstance(materials, list)
        assert isinstance(materials[3], dict)
        materials[3]["period"] = "2026-08"
        set_test_projection_revision(projection)
        self.client.test_workspace_data_updates = projection
        state = build_state(self.client, payroll_test_workspace_enabled=True)

        payload = state.payroll_test_workspace("session-token", "ledgerbridge-owner")

        self.assertEqual(payload["data"]["routing_counts"]["auto_test"], 4)

    def test_test_workspace_rejects_material_tampering_with_a_stale_revision(self) -> None:
        projection = self.client.test_workspace_projection()
        materials = projection["materials"]
        assert isinstance(materials, list)
        assert isinstance(materials[0], dict)
        materials[0]["material_id"] = "material_history_tampered"
        self.client.test_workspace_data_updates = projection
        state = build_state(self.client, payroll_test_workspace_enabled=True)

        with self.assertRaises(CoreBackendError) as raised:
            state.payroll_test_workspace("session-token", "ledgerbridge-owner")

        self.assertEqual(
            (raised.exception.status, raised.exception.payload["code"]),
            (503, "CORE_CONTRACT_INVALID"),
        )

    def test_test_workspace_rejects_revision_above_javascript_safe_integer(self) -> None:
        projection = self.client.test_workspace_projection()
        projection["workspace_revision"] = 9_007_199_254_740_992
        set_test_projection_revision(projection)
        self.client.test_workspace_data_updates = projection
        state = build_state(self.client, payroll_test_workspace_enabled=True)

        with self.assertRaises(CoreBackendError) as raised:
            state.payroll_test_workspace("session-token", "ledgerbridge-owner")

        self.assertEqual(
            (raised.exception.status, raised.exception.payload["code"]),
            (503, "CORE_CONTRACT_INVALID"),
        )

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
                                **evidence,
                                "period": "2026-08",
                                "status": "READY_FOR_MATCHING",
                                "display_label": f"{evidence['evidence_type']} · 2026-08",
                            }
                            for evidence in VERIFICATION_EVIDENCE
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
                "source_artifact_ids": list(VERIFICATION_EVIDENCE_IDS),
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
                "source_artifact_ids": list(VERIFICATION_EVIDENCE_IDS),
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
                "source_artifact_ids": list(VERIFICATION_EVIDENCE_IDS),
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
                "source_artifact_ids": [VERIFICATION_EVIDENCE_IDS[0]],
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(problem["code"], "VERIFICATION_EVIDENCE_SET_INCOMPLETE")
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
                "source_artifact_ids": list(VERIFICATION_EVIDENCE_IDS),
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
                "source_artifact_ids": list(VERIFICATION_EVIDENCE_IDS),
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
                "source_artifact_ids": list(VERIFICATION_EVIDENCE_IDS),
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
                "source_artifact_ids": list(VERIFICATION_EVIDENCE_IDS),
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
                "source_artifact_ids": list(VERIFICATION_EVIDENCE_IDS),
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
                "source_artifact_ids": list(VERIFICATION_EVIDENCE_IDS),
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
                "source_artifact_ids": list(VERIFICATION_EVIDENCE_IDS),
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
