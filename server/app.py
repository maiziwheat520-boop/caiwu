from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .auth import AuthError, AuthManager, AuthStore
from .core_backend import (
    CLASSIFICATION_RISK_CODES,
    PAYROLL_TEST_MATERIAL_TYPES,
    CoreBackedState,
    CoreBackendError,
    CoreHttpClient,
    sqlite_contains_business_facts,
)
from .evidence_preview import EvidencePreviewError, build_evidence_preview
from .persistence import (
    IdempotencyConflictError,
    IdempotencyRecord,
    SQLitePersistence,
    StaleRevisionError,
)
from .synthetic_data import (
    SYNTHETIC_CONNECTIONS,
    SYNTHETIC_EVIDENCE_CONTENT,
    SYNTHETIC_RECONCILIATION,
    initial_candidates,
    initial_review_events,
)


COOKIE_NAME = "__Host-ledgerbridge_session"
FLOW_COOKIE_NAME = "__Host-ledgerbridge_auth_flow"
MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
CANDIDATE_PATH = re.compile(r"^/api/v1/candidates/([0-9a-f-]{36})$")
DECISION_PATH = re.compile(r"^/api/v1/candidates/([0-9a-f-]{36})/decisions$")
BANK_STATEMENT_REVIEW_PATH = re.compile(
    r"^/api/v1/personal-finance/bank-statements/([0-9a-f-]{36})/reviews$"
)
COMPANY_BANK_STATEMENT_REVIEW_PATH = re.compile(
    r"^/api/v1/company-bank-statements/([0-9a-f-]{36})/reviews$"
)
COMPANY_TRANSACTION_CLASSIFICATION_REVIEW_PATH = re.compile(
    r"^/api/v1/company-transaction-classifications/([0-9a-f-]{36})/reviews$"
)
CLASSIFICATION_BATCH_PATH = re.compile(
    r"^/api/v1/candidate-classification-groups/(cg_[0-9a-f]{32})/decisions$"
)
RECONCILIATION_PATH = re.compile(r"^/api/v1/reconciliations/([^/]+)$")
ORIGINAL_RECONCILIATION_PATH = re.compile(r"^/api/v1/original-reconciliations/([^/]+)$")
CASH_RECONCILIATION_PATH = re.compile(r"^/api/v1/cash-reconciliations/([^/]+)$")
DRAFT_CREATE_PATH = re.compile(r"^/api/v1/reconciliations/([^/]+)/drafts$")
DRAFT_PATH = re.compile(r"^/api/v1/workbook-drafts/([0-9a-f-]{36})$")
EVIDENCE_PATH = re.compile(r"^/api/v1/evidence/([0-9a-f-]{36})/content$")
EVIDENCE_PREVIEW_PATH = re.compile(r"^/api/v1/evidence/([0-9a-f-]{36})/preview$")
EVIDENCE_UNLOCK_PATH = "/api/v1/evidence/unlocks"
PAYROLL_BATCH_COMMAND_PATH = re.compile(
    r"^/api/v1/payroll/batches/([A-Za-z0-9][A-Za-z0-9._-]{0,127})/verify-receipts$"
)
PAYROLL_TEST_MATERIAL_ORGANIZE_PATH = re.compile(
    r"^/api/v1/payroll/test-workspace/materials/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})/organize$"
)
PAYROLL_TEST_MATERIAL_PREVIEW_PATH = re.compile(
    r"^/api/v1/payroll/test-workspace/materials/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})/preview$"
)
PAYROLL_TEST_VALIDATE_PATH = "/api/v1/payroll/test-workspace/validate"
PAYROLL_LEGACY_WORKSPACE_PATH = "/api/v1/payroll/legacy-workspace"
PAYROLL_LEGACY_COMMAND_PATH = "/api/v1/payroll/legacy-workspace/commands"
PAYROLL_LEGACY_ACTIONS = {
    "FILL_MAIN",
    "GENERATE_MONTHLY_PAYROLL",
    "GENERATE_NORMAL_DRAFT",
    "GENERATE_SUPPLEMENTAL_DRAFT",
    "UPDATE_SUMMARY",
    "SAVE_RULES",
    "CHECK_RULES_AND_HISTORY",
    "VERIFY_CURRENT_PAID",
    "VERIFY_AND_UPDATE_SUMMARY",
    "CHECK_PREVIOUS_PENDING",
}
MAX_REQUEST_BYTES = 64 * 1024
MAX_CURSOR_LENGTH = 512
# A multi-unit Core cursor wraps one bounded Core cursor in a small JSON envelope
# and base64url-encodes it.  Keep a separate closed bound for that BFF envelope.
MAX_WRAPPED_CURSOR_LENGTH = 1024
JSON_SAFE_INTEGER = 9_007_199_254_740_991
STATUSES = {"INCOMPLETE", "PENDING", "CONFLICTED", "CONFIRMED", "IGNORED", "SUPERSEDED"}
COMPANY_TRANSACTION_CATEGORIES = {
    "PLATFORM_ROOM_REVENUE",
    "RELATED_PARTY_CURRENT",
    "PAYROLL",
    "FINANCING",
    "BOTTLED_WATER",
    "INTERNAL_TRANSFER",
    "RENT",
    "RENTAL_INCOME",
    "BANK_INTEREST",
    "LINEN_LAUNDRY",
    "OPERATING_FEE",
}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_company_report_client(
    *,
    default_base_url: str,
    default_ca_file: str,
    timeout_seconds: float,
) -> CoreHttpClient | None:
    certificate_file = os.environ.get("CORE_COMPANY_REPORT_CERT_FILE", "").strip()
    private_key_file = os.environ.get("CORE_COMPANY_REPORT_KEY_FILE", "").strip()
    if not certificate_file or not private_key_file:
        return None
    try:
        return CoreHttpClient(
            base_url=os.environ.get(
                "CORE_COMPANY_REPORT_BASE_URL", default_base_url
            ).strip()
            or default_base_url,
            ca_file=os.environ.get(
                "CORE_COMPANY_REPORT_CA_FILE", default_ca_file
            ).strip()
            or default_ca_file,
            certificate_file=certificate_file,
            private_key_file=private_key_file,
            timeout_seconds=timeout_seconds,
        )
    except (OSError, ValueError):
        return None


def _build_company_bank_review_client(
    *,
    default_ca_file: str,
    timeout_seconds: float,
) -> CoreHttpClient | None:
    certificate_file = os.environ.get("CORE_COMPANY_BANK_REVIEW_CERT_FILE", "").strip()
    private_key_file = os.environ.get("CORE_COMPANY_BANK_REVIEW_KEY_FILE", "").strip()
    if not certificate_file or not private_key_file:
        return None
    try:
        return CoreHttpClient(
            base_url=os.environ.get(
                "CORE_COMPANY_BANK_REVIEW_BASE_URL",
                "https://internal-ingress:8445",
            ).strip()
            or "https://internal-ingress:8445",
            ca_file=os.environ.get(
                "CORE_COMPANY_BANK_REVIEW_CA_FILE", default_ca_file
            ).strip()
            or default_ca_file,
            certificate_file=certificate_file,
            private_key_file=private_key_file,
            timeout_seconds=timeout_seconds,
        )
    except (OSError, ValueError):
        return None


def _company_bank_statement_mappings() -> tuple[tuple[str, str, str], ...]:
    mapping_file = os.environ.get("CORE_COMPANY_BANK_STATEMENTS_FILE", "").strip()
    source = "CORE_COMPANY_BANK_STATEMENTS_JSON"
    if mapping_file:
        source = "CORE_COMPANY_BANK_STATEMENTS_FILE"
        try:
            raw = Path(mapping_file).read_text(encoding="utf-8")
        except OSError as error:
            raise SystemExit(f"Refusing unreadable {source}") from error
    else:
        raw = os.environ.get("CORE_COMPANY_BANK_STATEMENTS_JSON", "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not 1 <= len(parsed) <= 32:
            raise ValueError
        result: list[tuple[str, str, str]] = []
        for index, item in enumerate(parsed, start=1):
            if not isinstance(item, dict) or not set(item).issubset(
                {"statement_ref", "entity_ref", "company_name"}
            ) or not {"statement_ref", "entity_ref"}.issubset(item):
                raise ValueError
            if not isinstance(item["statement_ref"], str) or not isinstance(item["entity_ref"], str):
                raise ValueError
            company_name = item.get("company_name", f"公司 {index}")
            if not isinstance(company_name, str) or not company_name.strip():
                raise ValueError
            result.append(
                (
                    str(uuid.UUID(item["statement_ref"])),
                    str(uuid.UUID(item["entity_ref"])),
                    company_name.strip(),
                )
            )
        return tuple(result)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"Refusing invalid {source}") from error


def _problem(status: int, code: str, title: str, detail: str = "") -> dict[str, object]:
    payload: dict[str, object] = {"type": "about:blank", "title": title, "status": status, "code": code}
    if detail:
        payload["detail"] = detail
    return payload


def _synthetic_dimension_identity(kind: str, label: object) -> str:
    if not isinstance(label, str) or not label:
        raise ValueError("synthetic accounting dimensions require non-empty labels")
    digest = hashlib.sha256(f"{kind}\n{label}".encode("utf-8")).hexdigest()[:16]
    return f"synthetic-{kind}-{digest}"


def _add_synthetic_dimension_identities(candidate: dict[str, object]) -> dict[str, object]:
    candidate["business_unit_ref"] = _synthetic_dimension_identity(
        "bu", candidate.get("business_unit")
    )
    candidate["category_code"] = _synthetic_dimension_identity(
        "category", candidate.get("category")
    )
    return candidate


def _add_synthetic_event_identity_flags(event: dict[str, object]) -> dict[str, object]:
    changes = event.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if isinstance(change, dict):
                change.setdefault("identity_changed", False)
    return event


class SyntheticState:
    """Synthetic projections backed by memory or an optional durable SQLite store."""

    def __init__(
        self,
        *,
        cookie_secure: bool = True,
        persistence: SQLitePersistence | None = None,
        actor: str = "prototype-single-user",
    ) -> None:
        self.lock = threading.RLock()
        self.persistence = persistence
        self.actor = actor
        seeded_candidates = [
            _add_synthetic_dimension_identities(candidate)
            for candidate in initial_candidates()
        ]
        seeded_events = {
            candidate_id: [
                _add_synthetic_event_identity_flags(event) for event in events
            ]
            for candidate_id, events in initial_review_events().items()
        }
        if persistence is not None:
            persistence.seed_if_empty(seeded_candidates, seeded_events, seed_version="synthetic-v1")
            seeded_candidates = [
                _add_synthetic_dimension_identities(candidate)
                for candidate in persistence.list_candidates()
            ]
            seeded_events = {
                str(candidate["id"]): [
                    _add_synthetic_event_identity_flags(event)
                    for event in persistence.get_review_events(str(candidate["id"]))
                ]
                for candidate in seeded_candidates
            }
        self.candidates = {str(item["id"]): item for item in seeded_candidates}
        self.review_events = seeded_events
        self.idempotency: dict[str, tuple[str, dict[str, object]]] = {}
        self.draft_idempotency: dict[str, tuple[str, dict[str, object], str]] = {}
        self.drafts: dict[str, dict[str, object]] = {}
        self.session_id = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
        self.cookie_secure = cookie_secure
        self.cookie_name = COOKIE_NAME if cookie_secure else "ledgerbridge_preview_session"
        self.reconciliation_revision = int(SYNTHETIC_RECONCILIATION["revision"])

    def _candidate_values(self) -> list[dict[str, object]]:
        candidates = self.persistence.list_candidates() if self.persistence is not None else list(self.candidates.values())
        return [_add_synthetic_dimension_identities(candidate) for candidate in candidates]

    def _reconciliation_revision(self) -> int:
        if self.persistence is None:
            return self.reconciliation_revision
        initial_revisions = {str(item["id"]): int(item["revision"]) for item in initial_candidates()}
        delta = sum(
            max(0, int(item["revision"]) - initial_revisions.get(str(item["id"]), int(item["revision"])))
            for item in self.persistence.list_candidates()
        )
        return int(SYNTHETIC_RECONCILIATION["revision"]) + delta

    def session_payload(self) -> dict[str, str]:
        return {
            "principal": "prototype-single-user",
            "csrf_token": self.csrf_token,
            "expires_at": self.expires_at.isoformat(timespec="seconds"),
        }

    def session_active(self) -> bool:
        return datetime.now(timezone.utc) < self.expires_at

    def evidence(self, evidence_id: str) -> dict[str, object] | None:
        evidence = SYNTHETIC_EVIDENCE_CONTENT.get(evidence_id)
        return deepcopy(evidence) if evidence is not None else None

    def unlock_evidence_source(
        self,
        source_ref: str,
        password: str,
        idempotency_key: str,
    ) -> tuple[int, dict[str, object]]:
        del source_ref, password, idempotency_key
        return 503, _problem(503, "EVIDENCE_UNLOCK_UNAVAILABLE", "账单解锁服务尚未配置")

    def connections(self) -> list[dict[str, str]]:
        return deepcopy(SYNTHETIC_CONNECTIONS)

    def candidate_detail(self, candidate_id: str) -> dict[str, object] | None:
        with self.lock:
            candidate = self.persistence.get_candidate(candidate_id) if self.persistence is not None else self.candidates.get(candidate_id)
            if candidate is None:
                return None
            detail = _add_synthetic_dimension_identities(deepcopy(candidate))
            events = self.persistence.get_review_events(candidate_id) if self.persistence is not None else self.review_events.get(candidate_id, [])
            detail["review_events"] = [
                _add_synthetic_event_identity_flags(deepcopy(event)) for event in events
            ]
            return detail

    def accounting_dimensions(self) -> dict[str, object]:
        with self.lock:
            candidates = self._candidate_values()
        business_units = sorted(
            {
                (str(candidate["business_unit_ref"]), str(candidate["business_unit"]))
                for candidate in candidates
                if candidate.get("business_unit_ref") and candidate.get("business_unit")
            },
            key=lambda item: item[0],
        )
        categories = sorted(
            {
                (str(candidate["category_code"]), str(candidate["category"]))
                for candidate in candidates
                if candidate.get("category_code") and candidate.get("category")
            },
            key=lambda item: item[0],
        )
        return {
            "contract_version": "ledgerbridge.accounting-dimensions.v1",
            "business_units": [{"ref": ref, "label": label} for ref, label in business_units],
            "categories": [{"code": code, "label": label} for code, label in categories],
        }

    def candidate_classification_groups(self) -> dict[str, object]:
        return {
            "contract_version": "ledgerbridge.classification-groups.v1",
            "items": [],
            "next_cursor": None,
        }

    def apply_candidate_classification_batch(
        self,
        group_ref: str,
        idempotency_key: str,
        request: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        del group_ref, idempotency_key, request
        return 503, _problem(
            503,
            "CLASSIFICATION_BATCH_UNAVAILABLE",
            "相似交易批量处理仅在 Core 模式可用",
        )

    def list_candidates(self, *, status: str | None, month: str | None, cursor: str | None) -> dict[str, object]:
        offset = int(cursor) if cursor is not None else 0
        with self.lock:
            items = [deepcopy(item) for item in self._candidate_values()]
        items.sort(key=lambda item: str(item["received_at"]), reverse=True)
        if status is not None:
            items = [item for item in items if item["status"] == status]
        if month is not None:
            items = [item for item in items if item["accounting_month"] == month]
        page_size = 50
        page = items[offset : offset + page_size]
        next_offset = offset + len(page)
        return {"items": page, "next_cursor": str(next_offset) if next_offset < len(items) else None}

    def list_review_events(self, *, cursor: str | None) -> dict[str, object]:
        offset = int(cursor) if cursor is not None else 0
        with self.lock:
            if self.persistence is not None:
                items = self.persistence.list_review_events()
            else:
                items = [
                    _add_synthetic_event_identity_flags(deepcopy(event))
                    for events in self.review_events.values()
                    for event in events
                ]
            if self.persistence is not None:
                items = [
                    _add_synthetic_event_identity_flags(event) for event in items
                ]
        items.sort(
            key=lambda item: (
                str(item["created_at"]),
                str(item["candidate_id"]),
                int(item["sequence"]),
            ),
            reverse=True,
        )
        page_size = 50
        page = items[offset : offset + page_size]
        next_offset = offset + len(page)
        return {"items": page, "next_cursor": str(next_offset) if next_offset < len(items) else None}

    def company_reports(self, from_month: str, to_month: str) -> dict[str, object]:
        return {
            "contract_version": "ledgerbridge.company-reports-bff.v2",
            "from_month": from_month,
            "to_month": to_month,
            "posted_ledger_status": "AVAILABLE",
            "layers": [
                {
                    "contract_version": "ledgerbridge.company-report.v1",
                    "basis": basis,
                    "from_month": from_month,
                    "to_month": to_month,
                    "items": [],
                }
                for basis in (
                    "CONFIRMED_CANDIDATE",
                    "ACCOUNT_STATEMENT",
                    "POSTED_LEDGER",
                )
            ],
            "compositions": [
                {
                    "contract_version": "ledgerbridge.company-report-composition.v1",
                    "basis": basis,
                    "from_month": from_month,
                    "to_month": to_month,
                    "items": [],
                }
                for basis in ("CONFIRMED_CANDIDATE", "POSTED_LEDGER")
            ],
        }

    def personal_bank_transactions(self) -> dict[str, object]:
        return {
            "contract_version": "ledgerbridge.personal-bank-transactions-bff.v2",
            "snapshot_revision": "0" * 64,
            "owner_kind": "PERSON",
            "statements": [],
            "summary": {
                "currency": "CNY",
                "statement_count": 0,
                "transaction_count": 0,
                "cash_inflow_minor": 0,
                "cash_outflow_minor": 0,
                "net_cash_flow_minor": 0,
            },
            "items": [],
        }

    def reconciliation(self, month: str) -> dict[str, object]:
        if month != SYNTHETIC_RECONCILIATION["accounting_month"]:
            return {"accounting_month": month, "revision": 1, "ready": True, "blockers": [], "business_units": []}
        with self.lock:
            payload = deepcopy(SYNTHETIC_RECONCILIATION)
            blockers: list[dict[str, str]] = []
            candidates = self._candidate_values()
            if any(item["status"] == "CONFLICTED" for item in candidates):
                blockers.append({"code": "BUSINESS_KEY_CONFLICT", "message": "城南店银行收款候选存在金额冲突。"})
            if any(item["status"] == "INCOMPLETE" for item in candidates):
                blockers.append({"code": "MISSING_ACCOUNTING_MONTH", "message": "机场店水费候选尚未确认归属月份。"})
            payload["revision"] = self._reconciliation_revision()
            payload["blockers"] = blockers
            payload["ready"] = not blockers
            return payload

    def append_decision(
        self, candidate_id: str, idempotency_key: str, request: dict[str, object]
    ) -> tuple[int, dict[str, object]]:
        fingerprint = hashlib.sha256(
            (candidate_id + "\n" + json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))).encode()
        ).hexdigest()
        scope = f"candidate-decision:{candidate_id}"
        with self.lock:
            if self.persistence is not None:
                replay_record = self.persistence.get_idempotency(scope, idempotency_key)
                if replay_record is not None:
                    if not hmac.compare_digest(replay_record.fingerprint, fingerprint):
                        return 409, _problem(409, "IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他请求")
                    return replay_record.response_status, deepcopy(replay_record.response)
            replay = self.idempotency.get(idempotency_key)
            if replay is not None:
                old_fingerprint, old_response = replay
                if not hmac.compare_digest(old_fingerprint, fingerprint):
                    return 409, _problem(409, "IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他请求")
                return 200, deepcopy(old_response)

            candidate = self.persistence.get_candidate(candidate_id) if self.persistence is not None else self.candidates.get(candidate_id)
            if candidate is None:
                return 404, _problem(404, "CANDIDATE_NOT_FOUND", "候选不存在")
            candidate = _add_synthetic_dimension_identities(candidate)
            if request["expected_revision"] != candidate["revision"]:
                return 409, _problem(409, "STALE_REVISION", "候选版本已变化，请刷新后重试")
            if candidate["status"] in {"CONFIRMED", "IGNORED", "SUPERSEDED"}:
                return 409, _problem(409, "TERMINAL_CANDIDATE", "候选已完成审核")

            decision = str(request["decision"])
            corrections = deepcopy(request.get("corrections") or {})
            if {"business_unit", "category"} & corrections.keys():
                return 422, _problem(422, "INVALID_CORRECTIONS", "显示标签不属于更正合约")
            dimensions = self.accounting_dimensions()
            stable_fields = {
                "business_unit_ref": (
                    "business_unit",
                    {item["ref"]: item["label"] for item in dimensions["business_units"]},  # type: ignore[index]
                ),
                "category_code": (
                    "category",
                    {item["code"]: item["label"] for item in dimensions["categories"]},  # type: ignore[index]
                ),
            }
            selected_dimensions: dict[str, tuple[str, str, str]] = {}
            for stable_field, (candidate_field, labels) in stable_fields.items():
                if stable_field not in corrections:
                    continue
                stable_value = corrections.pop(stable_field)
                if not isinstance(stable_value, str):
                    return 422, _problem(422, "INVALID_CORRECTION_REFERENCE", "会计维度标识无效")
                label = labels.get(stable_value)
                if label is None:
                    return 422, _problem(422, "INVALID_CORRECTION_REFERENCE", "会计维度不在授权目录中")
                selected_dimensions[stable_field] = (candidate_field, stable_value, label)
            updated = deepcopy(candidate)
            changes: list[dict[str, object]] = []
            for stable_field, (candidate_field, stable_value, label) in selected_dimensions.items():
                if updated.get(stable_field) == stable_value and updated.get(candidate_field) == label:
                    continue
                changes.append(
                    {
                        "field": candidate_field,
                        "previous_value": updated.get(candidate_field),
                        "new_value": label,
                        "identity_changed": updated.get(stable_field) != stable_value,
                    }
                )
                updated[stable_field] = stable_value
                updated[candidate_field] = label
            correction_fields = {
                "amount_minor": "amount_minor",
                "accounting_month": "accounting_month",
            }
            for supplied_field, candidate_field in correction_fields.items():
                if supplied_field in corrections and updated[candidate_field] != corrections[supplied_field]:
                    changes.append(
                        {
                            "field": candidate_field,
                            "previous_value": updated[candidate_field],
                            "new_value": corrections[supplied_field],
                            "identity_changed": False,
                        }
                    )
                    updated[candidate_field] = corrections[supplied_field]

            if updated.get("accounting_month") is not None:
                updated["blockers"] = [
                    blocker for blocker in updated["blockers"] if blocker["code"] != "MISSING_ACCOUNTING_MONTH"
                ]

            if decision == "IGNORE":
                updated["status"] = "IGNORED"
            elif decision == "RESOLVE_CONFLICT":
                if not any(blocker["code"] == "BUSINESS_KEY_CONFLICT" for blocker in candidate["blockers"]):
                    return 409, _problem(409, "NO_CONFLICT_TO_RESOLVE", "候选没有可解决的业务键冲突")
                updated["blockers"] = [
                    blocker for blocker in updated["blockers"] if blocker["code"] != "BUSINESS_KEY_CONFLICT"
                ]
                if updated["blockers"]:
                    return 422, _problem(422, "CANDIDATE_INCOMPLETE", "候选仍有未解决阻断项")
                updated["status"] = "CONFIRMED"
            else:
                if any(blocker["code"] in {"BUSINESS_KEY_CONFLICT", "DUPLICATE_MESSAGE", "DUPLICATE_ATTACHMENT"} for blocker in updated["blockers"]):
                    return 409, _problem(409, "UNRESOLVED_CONFLICT", "候选仍有未解决冲突")
                if updated["blockers"]:
                    return 422, _problem(422, "CANDIDATE_INCOMPLETE", "候选仍有未解决阻断项")
                updated["status"] = "CONFIRMED"

            if candidate["status"] != updated["status"]:
                changes.append(
                    {
                        "field": "status",
                        "previous_value": candidate["status"],
                        "new_value": updated["status"],
                        "identity_changed": False,
                    }
                )
            from_revision = int(candidate["revision"])
            to_revision = from_revision + 1
            updated["revision"] = to_revision
            events = self.persistence.get_review_events(candidate_id) if self.persistence is not None else self.review_events.setdefault(candidate_id, [])
            event: dict[str, object] = {
                "id": str(uuid.uuid4()),
                "candidate_id": candidate_id,
                "sequence": len(events) + 1,
                "from_revision": from_revision,
                "to_revision": to_revision,
                "decision": decision,
                "actor": self.actor,
                "reason": request["reason"],
                "changes": changes,
                "conflict_resolution": request.get("conflict_resolution"),
                "created_at": _utc_timestamp(),
            }
            response = {"candidate": deepcopy(updated), "event": deepcopy(event)}
            if self.persistence is not None:
                try:
                    result = self.persistence.commit_candidate_transition(
                        updated,
                        event,
                        IdempotencyRecord(scope, idempotency_key, fingerprint, 200, response),
                    )
                except StaleRevisionError:
                    return 409, _problem(409, "STALE_REVISION", "候选版本已变化，请刷新后重试")
                except IdempotencyConflictError:
                    return 409, _problem(409, "IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他请求")
                if result.replayed:
                    return result.idempotency.response_status, deepcopy(result.idempotency.response)
            else:
                events.append(deepcopy(event))
                self.candidates[candidate_id] = updated
                self.reconciliation_revision += 1
            self.idempotency[idempotency_key] = (fingerprint, deepcopy(response))
            return 200, response

    def create_draft(
        self, month: str, idempotency_key: str, expected_revision: int
    ) -> tuple[int, dict[str, object], str | None]:
        fingerprint = hashlib.sha256(f"{month}\n{expected_revision}".encode()).hexdigest()
        scope = f"workbook-draft:{month}"
        with self.lock:
            if self.persistence is not None:
                replay_record = self.persistence.get_idempotency(scope, idempotency_key)
                if replay_record is not None:
                    if not hmac.compare_digest(replay_record.fingerprint, fingerprint):
                        return 409, _problem(409, "IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他请求"), None
                    return replay_record.response_status, deepcopy(replay_record.response), replay_record.location
            replay = self.draft_idempotency.get(idempotency_key)
            if replay is not None:
                old_fingerprint, old_response, old_location = replay
                if not hmac.compare_digest(old_fingerprint, fingerprint):
                    return 409, _problem(409, "IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他请求"), None
                return 202, deepcopy(old_response), old_location
            reconciliation = self.reconciliation(month)
            if expected_revision != reconciliation["revision"]:
                return 409, _problem(409, "STALE_REVISION", "对账版本已变化，请刷新后重试"), None
            if not reconciliation["ready"]:
                return 409, _problem(409, "RECONCILIATION_BLOCKED", "对账仍有阻断项，不能生成草稿"), None
            draft_id = str(uuid.uuid4())
            location = f"/api/v1/workbook-drafts/{draft_id}"
            draft: dict[str, object] = {
                "id": draft_id,
                "accounting_month": month,
                "input_revision": expected_revision,
                "status": "NEEDS_REVIEW",
                "verification": None,
                "monitor_url": location,
                "output_sha256": None,
                "verification_detail": "合成预览已创建内存草稿状态，不会生成或覆盖真实工作簿。",
            }
            if self.persistence is not None:
                try:
                    result = self.persistence.save_draft(
                        draft,
                        IdempotencyRecord(scope, idempotency_key, fingerprint, 202, draft, location),
                    )
                except IdempotencyConflictError:
                    return 409, _problem(409, "IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他请求"), None
                if result.replayed:
                    return result.idempotency.response_status, deepcopy(result.idempotency.response), result.idempotency.location
            else:
                self.drafts[draft_id] = deepcopy(draft)
            self.draft_idempotency[idempotency_key] = (fingerprint, deepcopy(draft), location)
            return 202, draft, location

    def get_draft(self, draft_id: str) -> dict[str, object] | None:
        with self.lock:
            if self.persistence is not None:
                return self.persistence.get_draft(draft_id)
            draft = self.drafts.get(draft_id)
            return deepcopy(draft) if draft is not None else None


class PreviewHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LedgerBridgePreview"
    sys_version = ""

    @property
    def preview_server(self) -> "PreviewServer":
        return self.server  # type: ignore[return-value]

    def __init__(self, *args: object, **kwargs: object) -> None:
        httpd = args[2] if len(args) > 2 else None
        site_root = getattr(httpd, "site_root", Path("/site"))
        super().__init__(*args, directory=str(site_root), **kwargs)

    def _send_json(
        self,
        status: int,
        payload: dict[str, object],
        *,
        cookie: bool = False,
        head: bool = False,
        headers: dict[str, str] | None = None,
        cookies: list[str] | None = None,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        content_type = "application/problem+json" if status >= 400 else "application/json"
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if cookie:
            secure = "; Secure" if self.preview_server.state.cookie_secure else ""
            self.send_header("Set-Cookie", f"{self.preview_server.state.cookie_name}={self.preview_server.state.session_id}; Path=/; HttpOnly; SameSite=Strict{secure}")
        for value in cookies or []:
            self.send_header("Set-Cookie", value)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(encoded)

    def _send_empty(self, status: int, *, cookies: list[str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for value in cookies or []:
            self.send_header("Set-Cookie", value)
        self.end_headers()

    def _send_evidence(self, evidence: dict[str, object]) -> None:
        content = evidence["content"]
        if not isinstance(content, bytes):
            self._send_json(500, _problem(500, "INVALID_SYNTHETIC_EVIDENCE", "合成证据配置无效"))
            return
        filename = str(evidence["filename"])
        disposition = str(evidence["disposition"])
        digest = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", str(evidence["content_type"]))
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'{disposition}; filename="{filename}"')
        self.send_header("Content-Digest", f"sha-256=:{digest}:")
        self.end_headers()
        self.wfile.write(content)

    def _cookie_value(self, name: str) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(name)
        return morsel.value if morsel else None

    def _session_token(self) -> str | None:
        name = COOKIE_NAME if self.preview_server.auth_manager is not None else self.preview_server.state.cookie_name
        return self._cookie_value(name)

    def _authenticated(self) -> bool:
        if self.preview_server.auth_manager is not None:
            token = self._session_token()
            return bool(token and self.preview_server.auth_manager.status(token)["authenticated"])
        morsel_value = self._session_token()
        return bool(
            morsel_value
            and self.preview_server.state.session_active()
            and hmac.compare_digest(morsel_value, self.preview_server.state.session_id)
        )

    def _require_session(self) -> bool:
        if self._authenticated():
            return True
        self._send_json(401, _problem(401, "AUTHENTICATION_REQUIRED", "需要先完成身份验证"))
        return False

    def _payroll_session_identity(self) -> tuple[str, str] | None:
        manager = self.preview_server.auth_manager
        session_token = self._session_token()
        if manager is None or session_token is None:
            self._send_json(401, _problem(401, "AUTHENTICATION_REQUIRED", "工资接口需要完整认证会话"))
            return None
        session_subject = manager.payroll_session_subject(session_token)
        configured_subject = getattr(self.preview_server.state, "user_subject", None)
        if session_subject is None:
            self._send_json(401, _problem(401, "AUTHENTICATION_REQUIRED", "工资接口需要完整认证会话"))
            return None
        if not isinstance(configured_subject, str) or not hmac.compare_digest(
            session_subject.encode("utf-8"),
            configured_subject.encode("utf-8"),
        ):
            self._send_json(
                403,
                _problem(403, "PAYROLL_SESSION_SCOPE_MISMATCH", "当前会话不属于已配置的工资主体"),
            )
            return None
        configured_entity_ref = getattr(self.preview_server.state, "entity_ref", None)
        try:
            canonical_entity_ref = str(uuid.UUID(configured_entity_ref))
        except (AttributeError, TypeError, ValueError):
            canonical_entity_ref = ""
        if not canonical_entity_ref or configured_entity_ref != canonical_entity_ref:
            self._send_json(
                409,
                _problem(409, "ENTITY_SELECTION_REQUIRED", "工资接口需要唯一的服务器端公司选择"),
            )
            return None
        return session_token, session_subject

    def _require_same_origin(self) -> bool:
        manager = self.preview_server.auth_manager
        if manager is None:
            return True
        if self.headers.get("Origin", "") != manager.expected_origin:
            self._send_json(403, _problem(403, "ORIGIN_VALIDATION_FAILED", "请求来源不受信任"))
            return False
        fetch_site = self.headers.get("Sec-Fetch-Site")
        if fetch_site and fetch_site != "same-origin":
            self._send_json(403, _problem(403, "CROSS_SITE_REQUEST_REJECTED", "拒绝跨站状态变更请求"))
            return False
        return True

    def _require_csrf(self) -> bool:
        supplied = self.headers.get("X-CSRF-Token", "")
        manager = self.preview_server.auth_manager
        if manager is not None:
            token = self._session_token()
            valid = bool(token and manager.store.validate_csrf(token, supplied))
        else:
            valid = hmac.compare_digest(supplied, self.preview_server.state.csrf_token)
        if not valid:
            self._send_json(403, _problem(403, "CSRF_VALIDATION_FAILED", "CSRF 令牌缺失或无效"))
            return False
        return True

    @staticmethod
    def _session_cookie(session_id: str) -> str:
        return f"{COOKIE_NAME}={session_id}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=43200"

    @staticmethod
    def _flow_cookie(flow_token: str) -> str:
        return f"{FLOW_COOKIE_NAME}={flow_token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=300"

    @staticmethod
    def _clear_cookie(name: str) -> str:
        return f"{name}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0"

    def _read_json_object(self) -> dict[str, object] | None:
        if self.headers.get_content_type() != "application/json":
            self._send_json(415, _problem(415, "UNSUPPORTED_MEDIA_TYPE", "请求必须使用 application/json"))
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json(400, _problem(400, "INVALID_CONTENT_LENGTH", "Content-Length 无效"))
            return None
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_json(413, _problem(413, "INVALID_REQUEST_SIZE", "请求正文为空或过大"))
            return None
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, _problem(400, "INVALID_JSON", "JSON 正文无法解析"))
            return None
        if not isinstance(value, dict):
            self._send_json(400, _problem(400, "INVALID_REQUEST", "JSON 正文必须是对象"))
            return None
        return value

    def _invalid_decision(self, code: str, detail: str) -> None:
        self._send_json(422, _problem(422, code, "审核请求不符合合约", detail))

    def _validate_corrections(self, corrections: Any) -> bool:
        supported = {
            "business_unit_ref",
            "category_code",
            "amount_minor",
            "accounting_month",
        }
        if not isinstance(corrections, dict) or not corrections or set(corrections) - supported:
            self._invalid_decision("INVALID_CORRECTIONS", "corrections 必须包含至少一个受支持字段")
            return False
        for field in ("business_unit_ref", "category_code"):
            if field in corrections and (not isinstance(corrections[field], str) or not 1 <= len(corrections[field]) <= 100):
                self._invalid_decision("INVALID_CORRECTIONS", f"{field} 无效")
                return False
        if "amount_minor" in corrections:
            amount_minor = corrections["amount_minor"]
            if (
                isinstance(amount_minor, bool)
                or not isinstance(amount_minor, int)
                or not -JSON_SAFE_INTEGER <= amount_minor <= JSON_SAFE_INTEGER
            ):
                self._invalid_decision("INVALID_CORRECTIONS", "amount_minor 必须是 JSON 安全整数")
                return False
        if "accounting_month" in corrections and (not isinstance(corrections["accounting_month"], str) or not MONTH_PATTERN.fullmatch(corrections["accounting_month"])):
            self._invalid_decision("INVALID_CORRECTIONS", "accounting_month 格式无效")
            return False
        return True

    def _validate_decision(self, value: dict[str, object]) -> dict[str, object] | None:
        decision = value.get("decision")
        base = {"decision", "expected_revision", "reason"}
        allowed_by_decision = {
            "CONFIRM": base,
            "IGNORE": base,
            "CORRECT_AND_CONFIRM": base | {"corrections"},
            "RESOLVE_CONFLICT": base | {"corrections", "conflict_resolution"},
        }
        allowed = allowed_by_decision.get(decision)
        if allowed is None or not base.issubset(value) or set(value) - allowed:
            self._invalid_decision("INVALID_DECISION", "decision 字段或对应字段组合无效")
            return None
        revision = value["expected_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            self._invalid_decision("INVALID_REVISION", "expected_revision 必须是正整数")
            return None
        reason = value["reason"]
        if not isinstance(reason, str) or not 1 <= len(reason) <= 1000:
            self._invalid_decision("INVALID_REASON", "reason 长度必须为 1 到 1000")
            return None
        if decision == "CORRECT_AND_CONFIRM" and not self._validate_corrections(value.get("corrections")):
            return None
        if decision == "RESOLVE_CONFLICT":
            conflict_resolution = value.get("conflict_resolution")
            if not isinstance(conflict_resolution, str) or not 1 <= len(conflict_resolution) <= 1000:
                self._invalid_decision("INVALID_CONFLICT_RESOLUTION", "conflict_resolution 长度必须为 1 到 1000")
                return None
            if "corrections" in value and not self._validate_corrections(value["corrections"]):
                return None
        return value

    def _validate_classification_batch(
        self,
        value: dict[str, object],
    ) -> dict[str, object] | None:
        expected = {
            "source_candidate_ref",
            "accounting_month",
            "target",
            "members",
            "reason",
            "acknowledged_risk_codes",
        }
        if set(value) != expected:
            self._invalid_decision(
                "INVALID_CLASSIFICATION_BATCH",
                "相似交易批量请求字段不符合合约",
            )
            return None
        source_ref = value.get("source_candidate_ref")
        try:
            canonical_source_ref = (
                str(uuid.UUID(source_ref)) if isinstance(source_ref, str) else ""
            )
        except ValueError:
            canonical_source_ref = ""
        if source_ref != canonical_source_ref:
            self._invalid_decision(
                "INVALID_CLASSIFICATION_SOURCE",
                "source_candidate_ref 必须是规范 UUID",
            )
            return None
        month = value.get("accounting_month")
        target = value.get("target")
        if not isinstance(month, str) or MONTH_PATTERN.fullmatch(month) is None:
            self._invalid_decision(
                "INVALID_ACCOUNTING_MONTH",
                "accounting_month 格式无效",
            )
            return None
        if not isinstance(target, dict) or set(target) != {
            "business_unit_ref",
            "category_code",
        }:
            self._invalid_decision(
                "INVALID_CLASSIFICATION_TARGET",
                "target 必须包含稳定业务单元和分类标识",
            )
            return None
        if any(
            not isinstance(target.get(field), str)
            or not 1 <= len(str(target[field])) <= 100
            for field in ("business_unit_ref", "category_code")
        ):
            self._invalid_decision(
                "INVALID_CLASSIFICATION_TARGET",
                "target 会计维度标识无效",
            )
            return None
        members = value.get("members")
        member_refs: list[str] = []
        if not isinstance(members, list) or not 2 <= len(members) <= 100:
            self._invalid_decision(
                "INVALID_CLASSIFICATION_MEMBERS",
                "members 必须包含 2 到 100 个候选",
            )
            return None
        for member in members:
            if not isinstance(member, dict) or set(member) != {
                "candidate_ref",
                "expected_revision",
            }:
                self._invalid_decision(
                    "INVALID_CLASSIFICATION_MEMBERS",
                    "member 字段不符合合约",
                )
                return None
            candidate_ref = member.get("candidate_ref")
            revision = member.get("expected_revision")
            try:
                canonical_ref = (
                    str(uuid.UUID(candidate_ref))
                    if isinstance(candidate_ref, str)
                    else ""
                )
            except ValueError:
                canonical_ref = ""
            if (
                candidate_ref != canonical_ref
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                self._invalid_decision(
                    "INVALID_CLASSIFICATION_MEMBERS",
                    "member 候选或版本无效",
                )
                return None
            member_refs.append(canonical_ref)
        if len(member_refs) != len(set(member_refs)) or canonical_source_ref not in member_refs:
            self._invalid_decision(
                "INVALID_CLASSIFICATION_MEMBERS",
                "members 必须唯一并包含来源候选",
            )
            return None
        reason = value.get("reason")
        risks = value.get("acknowledged_risk_codes")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 1000:
            self._invalid_decision(
                "INVALID_REASON",
                "reason 长度必须为 1 到 1000",
            )
            return None
        if (
            not isinstance(risks, list)
            or len(risks) > 6
            or any(
                not isinstance(risk, str)
                or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", risk) is None
                or risk not in CLASSIFICATION_RISK_CODES
                for risk in risks
            )
            or risks != sorted(set(risks))
        ):
            self._invalid_decision(
                "INVALID_CLASSIFICATION_RISK_ACKNOWLEDGEMENT",
                "风险确认码无效",
            )
            return None
        return value

    def _api_get(self, path: str, query: str) -> None:
        state = self.preview_server.state
        manager = self.preview_server.auth_manager
        if path == "/api/v1/auth/status":
            if manager is None:
                self._send_json(200, {"authenticated": True, "setup_required": False, "passkey_registered": False, "recovery_setup_required": False, "recovery_pending": False, "principal": "prototype-single-user"}, cookie=True)
            else:
                payload = manager.status(self._session_token())
                if payload["authenticated"]:
                    payload["principal"] = "ledgerbridge-owner"
                self._send_json(200, payload)
            return
        if path == "/api/v1/auth/recovery/session":
            if manager is None:
                self._send_json(404, _problem(404, "API_ROUTE_NOT_FOUND", "API 路径不存在"))
                return
            token = self._session_token()
            payload = manager.recovery_session_payload(token) if token else None
            if payload is None:
                self._send_json(401, _problem(401, "RECOVERY_SESSION_REQUIRED", "需要有效的恢复会话"))
            else:
                self._send_json(200, payload)
            return
        if path == "/api/v1/session":
            if manager is None:
                self._send_json(
                    200,
                    {**state.session_payload(), "runtime_mode": self.preview_server.mode},
                    cookie=True,
                )
                return
            token = self._session_token()
            payload = manager.session_payload(token) if token else None
            if payload is None:
                self._send_json(401, _problem(401, "AUTHENTICATION_REQUIRED", "需要先完成身份验证"))
            else:
                self._send_json(
                    200,
                    {**payload, "runtime_mode": self.preview_server.mode},
                )
            return
        if not self._require_session():
            return
        payroll_reads = {
            "/api/v1/payroll/status": "payroll_status",
            "/api/v1/payroll/test-workspace": "payroll_test_workspace",
            PAYROLL_LEGACY_WORKSPACE_PATH: "payroll_legacy_workspace",
            "/api/v1/payroll/dashboard": "payroll_dashboard",
            "/api/v1/payroll/materials": "payroll_materials",
            "/api/v1/payroll/batches": "payroll_batches",
            "/api/v1/payroll/verification": "payroll_verification",
        }
        payroll_method = payroll_reads.get(path)
        if payroll_method is not None:
            if query:
                self._send_json(400, _problem(400, "INVALID_PAYROLL_QUERY", "工资接口不接受浏览器作用域参数"))
                return
            identity = self._payroll_session_identity()
            if identity is None:
                return
            session_token, session_subject = identity
            if not hasattr(state, payroll_method):
                self._send_json(503, _problem(503, "PAYROLL_INTEGRATION_UNAVAILABLE", "工资服务尚未配置"))
                return
            self._send_json(200, getattr(state, payroll_method)(session_token, session_subject))
            return
        payroll_preview = PAYROLL_TEST_MATERIAL_PREVIEW_PATH.fullmatch(path)
        if payroll_preview:
            if query:
                self._send_json(
                    400,
                    _problem(
                        400,
                        "INVALID_PAYROLL_QUERY",
                        "工资材料预览不接受浏览器作用域参数",
                    ),
                )
                return
            identity = self._payroll_session_identity()
            if identity is None:
                return
            if not hasattr(state, "payroll_test_material_preview"):
                self._send_json(
                    503,
                    _problem(503, "PAYROLL_INTEGRATION_UNAVAILABLE", "工资服务尚未配置"),
                )
                return
            session_token, session_subject = identity
            self._send_json(
                200,
                state.payroll_test_material_preview(
                    session_token,
                    session_subject,
                    payroll_preview.group(1),
                ),
            )
            return
        if path == "/api/v1/accounting-dimensions":
            if query:
                self._send_json(
                    400,
                    _problem(400, "INVALID_ACCOUNTING_DIMENSIONS_QUERY", "会计维度不接受浏览器实体参数"),
                )
                return
            self._send_json(200, state.accounting_dimensions())
            return
        if path == "/api/v1/candidate-classification-groups":
            if query:
                self._send_json(
                    400,
                    _problem(
                        400,
                        "INVALID_CLASSIFICATION_GROUP_QUERY",
                        "相似交易分组范围由当前授权会话决定",
                    ),
                )
                return
            self._send_json(200, state.candidate_classification_groups())
            return
        if path == "/api/v1/personal-finance/bank-transactions":
            if query:
                self._send_json(
                    400,
                    _problem(
                        400,
                        "INVALID_PERSONAL_BANK_QUERY",
                        "个人正式流水范围由服务端受保护配置决定",
                    ),
                )
                return
            self._send_json(200, state.personal_bank_transactions())
            return
        if path == "/api/v1/company-bank-statements":
            if query:
                self._send_json(
                    400,
                    _problem(
                        400,
                        "INVALID_COMPANY_BANK_QUERY",
                        "公司账单范围由服务端受保护配置决定",
                    ),
                )
                return
            self._send_json(200, state.company_bank_statements())
            return
        if path == "/api/v1/company-transaction-classifications":
            if query:
                self._send_json(
                    400,
                    _problem(
                        400,
                        "INVALID_COMPANY_CLASSIFICATION_QUERY",
                        "公司流水分类范围由服务端专用授权决定",
                    ),
                )
                return
            self._send_json(200, state.company_transaction_classifications())
            return
        if path == "/api/v1/company-reports":
            params = parse_qs(query, keep_blank_values=True)
            if (
                not set(params).issubset({"from_month", "to_month"})
                or any(len(values) != 1 for values in params.values())
            ):
                self._send_json(
                    400,
                    _problem(
                        400,
                        "INVALID_COMPANY_REPORT_QUERY",
                        "公司报表范围由当前授权会话决定",
                    ),
                )
                return
            now = datetime.now(timezone.utc)
            to_month = params.get("to_month", [now.strftime("%Y-%m")])[0]
            from_month = params.get("from_month", [f"{now.year:04d}-01"])[0]
            if (
                MONTH_PATTERN.fullmatch(from_month) is None
                or MONTH_PATTERN.fullmatch(to_month) is None
                or from_month > to_month
                or (int(to_month[:4]) * 12 + int(to_month[5:]))
                - (int(from_month[:4]) * 12 + int(from_month[5:]))
                >= 24
            ):
                self._send_json(
                    400,
                    _problem(
                        400,
                        "INVALID_COMPANY_REPORT_QUERY",
                        "公司报表月份范围无效或超过 24 个月",
                    ),
                )
                return
            self._send_json(200, state.company_reports(from_month, to_month))
            return
        if path == "/api/v1/candidates":
            params = parse_qs(query, keep_blank_values=True)
            status = params.get("status", [None])[0]
            month = params.get("accounting_month", [None])[0]
            cursor = params.get("cursor", [None])[0]
            if status is not None and status not in STATUSES:
                self._send_json(400, _problem(400, "INVALID_STATUS", "候选状态无效"))
                return
            if month is not None and not MONTH_PATTERN.fullmatch(month):
                self._send_json(400, _problem(400, "INVALID_ACCOUNTING_MONTH", "归属月份格式无效"))
                return
            opaque_cursors = bool(getattr(state, "opaque_cursors", False))
            cursor_length_limit = (
                MAX_WRAPPED_CURSOR_LENGTH if opaque_cursors else MAX_CURSOR_LENGTH
            )
            if cursor is not None and (
                len(cursor) > cursor_length_limit
                or not cursor.isascii()
                or (not opaque_cursors and not cursor.isdigit())
                or (opaque_cursors and re.fullmatch(r"[A-Za-z0-9._-]+", cursor) is None)
            ):
                self._send_json(400, _problem(400, "INVALID_CURSOR", "分页游标无效"))
                return
            self._send_json(200, state.list_candidates(status=status, month=month, cursor=cursor))
            return
        if path == "/api/v1/review-events":
            params = parse_qs(query, keep_blank_values=True)
            cursor = params.get("cursor", [None])[0]
            if cursor is not None and (
                len(cursor) > MAX_CURSOR_LENGTH
                or not cursor.isascii()
                or not cursor.isdigit()
            ):
                self._send_json(400, _problem(400, "INVALID_CURSOR", "分页游标无效"))
                return
            self._send_json(200, state.list_review_events(cursor=cursor))
            return
        match = CANDIDATE_PATH.fullmatch(path)
        if match:
            detail = state.candidate_detail(match.group(1))
            self._send_json(200, detail) if detail else self._send_json(404, _problem(404, "CANDIDATE_NOT_FOUND", "候选不存在"))
            return
        match = EVIDENCE_PATH.fullmatch(path)
        if match:
            evidence = state.evidence(match.group(1))
            if evidence is None:
                self._send_json(404, _problem(404, "EVIDENCE_NOT_FOUND", "证据不存在"))
            else:
                self._send_evidence(evidence)
            return
        match = EVIDENCE_PREVIEW_PATH.fullmatch(path)
        if match:
            params = parse_qs(query, keep_blank_values=True)
            reference = params.get("reference", [None])[0]
            if set(params) - {"reference"} or reference == "":
                self._send_json(400, _problem(400, "INVALID_EVIDENCE_PREVIEW_QUERY", "证据预览参数无效"))
                return
            evidence = state.evidence(match.group(1))
            if evidence is None:
                self._send_json(404, _problem(404, "EVIDENCE_NOT_FOUND", "证据不存在"))
                return
            try:
                preview = build_evidence_preview(evidence, reference=reference)
            except EvidencePreviewError as error:
                self._send_json(400, _problem(400, "INVALID_EVIDENCE_PREVIEW_QUERY", "证据预览参数无效", str(error)))
                return
            self._send_json(200, preview, headers={"Cache-Control": "no-store"})
            return
        match = RECONCILIATION_PATH.fullmatch(path)
        if match:
            month = match.group(1)
            if not MONTH_PATTERN.fullmatch(month):
                self._send_json(400, _problem(400, "INVALID_ACCOUNTING_MONTH", "归属月份格式无效"))
            else:
                self._send_json(200, state.reconciliation(month))
            return
        match = ORIGINAL_RECONCILIATION_PATH.fullmatch(path)
        if match:
            month = match.group(1)
            params = parse_qs(query, keep_blank_values=True)
            if not MONTH_PATTERN.fullmatch(month):
                self._send_json(400, _problem(400, "INVALID_ACCOUNTING_MONTH", "归属月份格式无效"))
                return
            if set(params) - {"entity_ref", "business_unit"} or any(len(values) != 1 for values in params.values()):
                self._send_json(400, _problem(400, "INVALID_ORIGINAL_RECONCILIATION_SCOPE", "原口径对账范围参数无效"))
                return
            entity_ref = params.get("entity_ref", [None])[0]
            business_unit_ref = params.get("business_unit", [None])[0]
            if entity_ref == "" or business_unit_ref == "":
                self._send_json(400, _problem(400, "INVALID_ORIGINAL_RECONCILIATION_SCOPE", "原口径对账范围参数无效"))
                return
            read_projection = getattr(state, "original_reconciliation", None)
            if read_projection is None:
                self._send_json(503, _problem(503, "ORIGINAL_RECONCILIATION_UNAVAILABLE", "原口径对账投影尚未连接"))
                return
            self._send_json(
                200,
                read_projection(
                    month,
                    entity_ref=entity_ref,
                    business_unit_ref=business_unit_ref,
                ),
                headers={"Cache-Control": "no-store"},
            )
            return
        match = CASH_RECONCILIATION_PATH.fullmatch(path)
        if match:
            month = match.group(1)
            if not MONTH_PATTERN.fullmatch(month):
                self._send_json(400, _problem(400, "INVALID_ACCOUNTING_MONTH", "归属月份格式无效"))
                return
            read_projection = getattr(state, "cash_reconciliation", None)
            if read_projection is None:
                self._send_json(503, _problem(503, "CASH_RECONCILIATION_UNAVAILABLE", "流水自动生成尚未连接"))
                return
            self._send_json(200, read_projection(month), headers={"Cache-Control": "no-store"})
            return
        match = DRAFT_PATH.fullmatch(path)
        if match:
            draft = state.get_draft(match.group(1))
            if draft is None:
                self._send_json(404, _problem(404, "WORKBOOK_DRAFT_NOT_FOUND", "工作簿草稿不存在"))
            else:
                self._send_json(200, draft)
            return
        if path == "/api/v1/connections":
            self._send_json(200, {"items": state.connections()})
            return
        self._send_json(404, _problem(404, "API_ROUTE_NOT_FOUND", "API 路径不存在"))

    def _static_has_symlink(self, request_path: str) -> bool:
        decoded = unquote(request_path).split("?", 1)[0].split("#", 1)[0]
        current = self.preview_server.site_root
        for part in PurePosixPath(decoded).parts:
            if part in {"/", "", ".", ".."}:
                continue
            current = current / part
            if current.is_symlink():
                return True
        return False

    def _use_spa_fallback(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz" or path.startswith("/api/"):
            return
        relative = path.lstrip("/")
        target = (self.preview_server.site_root / relative).resolve()
        if self.preview_server.site_root not in target.parents and target != self.preview_server.site_root:
            return
        if not target.exists() and "." not in Path(relative).name:
            self.path = "/index.html"

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        if split.path == "/healthz":
            payload = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if split.path.startswith("/api/"):
            try:
                self._api_get(split.path, split.query)
            except CoreBackendError as error:
                self._send_json(error.status, error.payload)
            return
        self._use_spa_fallback()
        if self._static_has_symlink(urlsplit(self.path).path):
            self.send_error(404, "Not found")
            return
        super().do_GET()

    def _auth_post(self, path: str) -> bool:
        manager = self.preview_server.auth_manager
        auth_paths = {
            "/api/v1/auth/passkey/register/options",
            "/api/v1/auth/passkey/register/verify",
            "/api/v1/auth/passkey/login/options",
            "/api/v1/auth/passkey/login/verify",
            "/api/v1/auth/passkey/add/authorize/options",
            "/api/v1/auth/passkey/add/authorize/verify",
            "/api/v1/auth/passkey/add/verify",
            "/api/v1/auth/recovery",
            "/api/v1/session/logout",
        }
        if path not in auth_paths:
            return False
        if manager is None:
            self._send_json(404, _problem(404, "API_ROUTE_NOT_FOUND", "API 路径不存在"))
            return True
        if not self._require_same_origin():
            return True
        try:
            if path == "/api/v1/session/logout":
                token = self._session_token()
                if not token or manager.store.session_method(token) is None:
                    self._send_json(401, _problem(401, "AUTHENTICATION_REQUIRED", "需要有效会话"))
                    return True
                if not self._require_csrf():
                    return True
                manager.store.logout(token)
                self._send_empty(204, cookies=[self._clear_cookie(COOKIE_NAME)])
                return True
            request = self._read_json_object()
            if request is None:
                return True
            if path.startswith("/api/v1/auth/passkey/add/"):
                session_token = self._session_token()
                if not session_token or manager.store.session_method(session_token) in {None, "recovery-code"}:
                    raise AuthError(401, "AUTHENTICATION_REQUIRED", "需要有效的通行密钥登录会话")
                if not self._require_csrf():
                    return True
                if path == "/api/v1/auth/passkey/add/authorize/options":
                    if request:
                        raise AuthError(422, "INVALID_AUTH_REQUEST", "新增通行密钥授权请求必须为空对象")
                    options, flow_token = manager.start_passkey_addition_authorization(
                        session_token,
                        caller_key=self._auth_caller_key(),
                        previous_flow_token=self._cookie_value(FLOW_COOKIE_NAME),
                    )
                    self._send_json(200, options, cookies=[self._flow_cookie(flow_token)])
                    return True
                if path == "/api/v1/auth/passkey/add/authorize/verify":
                    if set(request) != {"credential"} or not isinstance(request["credential"], dict):
                        raise AuthError(422, "INVALID_AUTH_REQUEST", "通行密钥授权响应无效")
                    flow_token = self._cookie_value(FLOW_COOKIE_NAME)
                    if not flow_token:
                        raise AuthError(400, "AUTH_CEREMONY_EXPIRED", "认证请求已过期，请重新开始")
                    options, registration_flow_token = manager.finish_passkey_addition_authorization(
                        flow_token,
                        request["credential"],
                        session_token=session_token,
                    )
                    self._send_json(200, options, cookies=[self._flow_cookie(registration_flow_token)])
                    return True
                if set(request) != {"credential"} or not isinstance(request["credential"], dict):
                    raise AuthError(422, "INVALID_AUTH_REQUEST", "新增通行密钥登记响应无效")
                flow_token = self._cookie_value(FLOW_COOKIE_NAME)
                if not flow_token:
                    raise AuthError(400, "AUTH_CEREMONY_EXPIRED", "认证请求已过期，请重新开始")
                passkey_count = manager.finish_passkey_addition(
                    flow_token,
                    request["credential"],
                    session_token=session_token,
                )
                self._send_json(
                    200,
                    {"added": True, "passkey_count": passkey_count},
                    cookies=[self._clear_cookie(FLOW_COOKIE_NAME)],
                )
                return True
            if path == "/api/v1/auth/passkey/register/options":
                if set(request) != {"setup_code"} or not isinstance(request["setup_code"], str):
                    raise AuthError(422, "INVALID_AUTH_REQUEST", "setup_code 字段无效")
                if manager.store.initialized() and not self._require_csrf():
                    return True
                options, flow_token = manager.start_registration(
                    request["setup_code"],
                    self._session_token(),
                    caller_key=self._auth_caller_key(),
                    previous_flow_token=self._cookie_value(FLOW_COOKIE_NAME),
                )
                self._send_json(200, options, cookies=[self._flow_cookie(flow_token)])
                return True
            if path == "/api/v1/auth/passkey/register/verify":
                if set(request) != {"setup_code", "credential"} or not isinstance(request["setup_code"], str) or not isinstance(request["credential"], dict):
                    raise AuthError(422, "INVALID_AUTH_REQUEST", "通行密钥登记响应无效")
                if manager.store.initialized() and not self._require_csrf():
                    return True
                flow_token = self._cookie_value(FLOW_COOKIE_NAME)
                if not flow_token:
                    raise AuthError(400, "AUTH_CEREMONY_EXPIRED", "认证请求已过期，请重新开始")
                session, recovery_codes = manager.finish_registration(
                    flow_token,
                    request["credential"],
                    setup_code=request["setup_code"],
                    session_token=self._session_token(),
                )
                payload: dict[str, object] = {
                    "authenticated": True,
                    "setup_required": False,
                    "passkey_registered": True,
                    "recovery_setup_required": False,
                    "recovery_pending": False,
                    "principal": "ledgerbridge-owner",
                    "csrf_token": session.csrf_token,
                    "expires_at": session.expires_at,
                }
                if recovery_codes:
                    payload["recovery_codes"] = recovery_codes
                self._send_json(200, payload, cookies=[self._session_cookie(session.token), self._clear_cookie(FLOW_COOKIE_NAME)])
                return True
            if path == "/api/v1/auth/passkey/login/options":
                if request:
                    raise AuthError(422, "INVALID_AUTH_REQUEST", "登录选项请求必须为空对象")
                options, flow_token = manager.start_login(
                    caller_key=self._auth_caller_key(),
                    previous_flow_token=self._cookie_value(FLOW_COOKIE_NAME),
                )
                self._send_json(200, options, cookies=[self._flow_cookie(flow_token)])
                return True
            if path == "/api/v1/auth/passkey/login/verify":
                if set(request) != {"credential"} or not isinstance(request["credential"], dict):
                    raise AuthError(422, "INVALID_AUTH_REQUEST", "通行密钥登录响应无效")
                flow_token = self._cookie_value(FLOW_COOKIE_NAME)
                if not flow_token:
                    raise AuthError(400, "AUTH_CEREMONY_EXPIRED", "认证请求已过期，请重新开始")
                session = manager.finish_login(flow_token, request["credential"])
                self._send_json(200, {"authenticated": True, "setup_required": False, "passkey_registered": True, "recovery_setup_required": False, "recovery_pending": False, "principal": "ledgerbridge-owner", "csrf_token": session.csrf_token, "expires_at": session.expires_at}, cookies=[self._session_cookie(session.token), self._clear_cookie(FLOW_COOKIE_NAME)])
                return True
            if set(request) != {"recovery_code"} or not isinstance(request["recovery_code"], str):
                raise AuthError(422, "INVALID_AUTH_REQUEST", "恢复码请求无效")
            session = manager.recover(request["recovery_code"], caller_key=self._auth_caller_key())
            self._send_json(200, {"authenticated": False, "setup_required": False, "passkey_registered": True, "recovery_setup_required": True, "recovery_pending": True, "csrf_token": session.csrf_token, "expires_at": session.expires_at}, cookies=[self._session_cookie(session.token)])
            return True
        except AuthError as error:
            self._send_json(error.status, _problem(error.status, error.code, error.detail))
            return True

    def _auth_caller_key(self) -> str:
        try:
            peer = ipaddress.ip_address(self.client_address[0])
        except ValueError as error:
            raise AuthError(400, "CLIENT_IDENTITY_INVALID", "无法识别认证请求来源") from error
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
            peer = peer.ipv4_mapped
        if any(peer in network for network in self.preview_server.trusted_proxy_networks):
            forwarded_values = self.headers.get_all("X-Forwarded-For", [])
            if len(forwarded_values) != 1 or "," in forwarded_values[0]:
                raise AuthError(400, "CLIENT_IDENTITY_INVALID", "受信代理必须提供单一客户端地址")
            try:
                peer = ipaddress.ip_address(forwarded_values[0].strip())
            except ValueError as error:
                raise AuthError(400, "CLIENT_IDENTITY_INVALID", "受信代理提供的客户端地址无效") from error
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
            peer = peer.ipv4_mapped
        return f"ip:{peer.compressed}"

    def do_POST(self) -> None:
        # A rejected POST can leave an unread body. Close the connection so those
        # bytes can never be interpreted as a second HTTP request.
        self.close_connection = True
        split = urlsplit(self.path)
        path = split.path
        if self._auth_post(path):
            return
        decision_match = DECISION_PATH.fullmatch(path)
        bank_statement_review = BANK_STATEMENT_REVIEW_PATH.fullmatch(path)
        company_bank_statement_review = COMPANY_BANK_STATEMENT_REVIEW_PATH.fullmatch(path)
        company_transaction_review = (
            COMPANY_TRANSACTION_CLASSIFICATION_REVIEW_PATH.fullmatch(path)
        )
        classification_batch_match = CLASSIFICATION_BATCH_PATH.fullmatch(path)
        draft_match = DRAFT_CREATE_PATH.fullmatch(path)
        evidence_unlock = path == EVIDENCE_UNLOCK_PATH
        payroll_batch_command = PAYROLL_BATCH_COMMAND_PATH.fullmatch(path)
        payroll_test_organize = PAYROLL_TEST_MATERIAL_ORGANIZE_PATH.fullmatch(path)
        payroll_test_validate = path == PAYROLL_TEST_VALIDATE_PATH
        payroll_legacy_command = path == PAYROLL_LEGACY_COMMAND_PATH
        if (
            decision_match is None
            and bank_statement_review is None
            and company_bank_statement_review is None
            and company_transaction_review is None
            and classification_batch_match is None
            and draft_match is None
            and not evidence_unlock
            and payroll_batch_command is None
            and payroll_test_organize is None
            and not payroll_test_validate
            and not payroll_legacy_command
        ):
            self._send_json(404 if path.startswith("/api/") else 405, _problem(404 if path.startswith("/api/") else 405, "API_ROUTE_NOT_FOUND" if path.startswith("/api/") else "METHOD_NOT_ALLOWED", "路径不接受该请求"))
            return
        if evidence_unlock and split.query:
            self._send_json(400, _problem(400, "INVALID_EVIDENCE_UNLOCK_REQUEST", "解锁请求不接受查询参数"))
            return
        if classification_batch_match is not None and split.query:
            self._send_json(
                400,
                _problem(
                    400,
                    "INVALID_CLASSIFICATION_BATCH_QUERY",
                    "相似交易批量请求不接受查询参数",
                ),
            )
            return
        if company_transaction_review is not None and split.query:
            self._send_json(
                400,
                _problem(
                    400,
                    "INVALID_COMPANY_CLASSIFICATION_QUERY",
                    "公司流水分类审批不接受查询参数",
                ),
            )
            return
        if not self._require_session():
            return
        if not self._require_same_origin() or not self._require_csrf():
            return
        idempotency_key = self.headers.get("Idempotency-Key", "")
        try:
            parsed_key = uuid.UUID(idempotency_key)
        except ValueError:
            self._send_json(400, _problem(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 必须是 UUID"))
            return
        if str(parsed_key) != idempotency_key.lower():
            self._send_json(400, _problem(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key 必须使用规范 UUID 格式"))
            return
        payroll_identity: tuple[str, str] | None = None
        if (
            payroll_batch_command is not None
            or payroll_test_organize is not None
            or payroll_test_validate
            or payroll_legacy_command
        ):
            payroll_identity = self._payroll_session_identity()
            if payroll_identity is None:
                return
        if payroll_batch_command is not None and payroll_identity is not None and not bool(
            getattr(self.preview_server.state, "payroll_commands_enabled", False)
        ):
            self._send_json(
                503,
                _problem(503, "PAYROLL_COMMAND_UNAVAILABLE", "工资写操作尚未通过可信授权闸门"),
            )
            return
        request = self._read_json_object()
        if request is None:
            return
        if payroll_legacy_command:
            if payroll_identity is None:
                self._send_json(401, _problem(401, "AUTH_REQUIRED", "需要登录"))
                return
            if not bool(
                getattr(self.preview_server.state, "payroll_test_workspace_enabled", False)
            ):
                self._send_json(
                    404,
                    _problem(404, "PAYROLL_TEST_WORKSPACE_DISABLED", "工资功能工作区未启用"),
                )
                return
            if set(request) != {"action", "expected_revision", "payload"}:
                self._send_json(
                    422,
                    _problem(422, "INVALID_PAYROLL_LEGACY_COMMAND", "工资功能请求字段无效"),
                )
                return
            action = request.get("action")
            expected_revision = request.get("expected_revision")
            command_payload = request.get("payload")
            if (
                action not in PAYROLL_LEGACY_ACTIONS
                or isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or not 0 <= expected_revision <= JSON_SAFE_INTEGER
                or not isinstance(command_payload, dict)
            ):
                self._send_json(
                    422,
                    _problem(422, "INVALID_PAYROLL_LEGACY_COMMAND", "工资功能动作或版本无效"),
                )
                return
            if not hasattr(self.preview_server.state, "payroll_legacy_workspace_command"):
                self._send_json(
                    503,
                    _problem(503, "PAYROLL_TEST_COMMAND_UNAVAILABLE", "工资功能写入尚未配置"),
                )
                return
            session_token, session_subject = payroll_identity
            status, payload = self.preview_server.state.payroll_legacy_workspace_command(
                session_token=session_token,
                session_subject=session_subject,
                action=str(action),
                expected_revision=expected_revision,
                payload=command_payload,
                idempotency_key=idempotency_key.lower(),
            )
            self._send_json(status, payload)
            return
        if payroll_test_organize is not None or payroll_test_validate:
            if payroll_identity is None:
                self._send_json(401, _problem(401, "AUTH_REQUIRED", "需要登录"))
                return
            if not bool(
                getattr(self.preview_server.state, "payroll_test_workspace_enabled", False)
            ):
                self._send_json(
                    404,
                    _problem(404, "PAYROLL_TEST_WORKSPACE_DISABLED", "工资测试整理功能未启用"),
                )
                return
            expected_fields = {
                "expected_workspace_revision",
                "period",
                "material_type",
            } if payroll_test_organize is not None else {"expected_workspace_revision"}
            if set(request) != expected_fields:
                self._send_json(
                    422,
                    _problem(422, "INVALID_PAYROLL_TEST_COMMAND", "工资材料整理请求字段无效"),
                )
                return
            expected_revision = request.get("expected_workspace_revision")
            if (
                isinstance(expected_revision, bool)
                or not isinstance(expected_revision, int)
                or not 1 <= expected_revision <= JSON_SAFE_INTEGER
            ):
                self._send_json(
                    422,
                    _problem(422, "INVALID_PAYROLL_VERSION", "测试工作区版本必须是正整数"),
                )
                return
            session_token, session_subject = payroll_identity
            if payroll_test_organize is not None:
                period = request.get("period")
                material_type = request.get("material_type")
                if (
                    not isinstance(period, str)
                    or MONTH_PATTERN.fullmatch(period) is None
                    or material_type not in PAYROLL_TEST_MATERIAL_TYPES
                ):
                    self._send_json(
                        422,
                        _problem(422, "INVALID_PAYROLL_MATERIAL_CLASSIFICATION", "期间或材料类型无效"),
                    )
                    return
                if not hasattr(self.preview_server.state, "payroll_test_workspace_organize"):
                    self._send_json(503, _problem(503, "PAYROLL_TEST_COMMAND_UNAVAILABLE", "工资材料整理尚未配置"))
                    return
                status, payload = self.preview_server.state.payroll_test_workspace_organize(
                    session_token=session_token,
                    session_subject=session_subject,
                    material_id=payroll_test_organize.group(1),
                    expected_workspace_revision=expected_revision,
                    period=period,
                    material_type=str(material_type),
                    idempotency_key=idempotency_key.lower(),
                )
            else:
                if not hasattr(self.preview_server.state, "payroll_test_workspace_validate"):
                    self._send_json(503, _problem(503, "PAYROLL_TEST_COMMAND_UNAVAILABLE", "工资批次验证尚未配置"))
                    return
                status, payload = self.preview_server.state.payroll_test_workspace_validate(
                    session_token=session_token,
                    session_subject=session_subject,
                    expected_workspace_revision=expected_revision,
                    idempotency_key=idempotency_key.lower(),
                )
            self._send_json(status, payload)
            return
        if payroll_batch_command is not None:
            if payroll_identity is None or not hasattr(self.preview_server.state, "payroll_batch_command"):
                self._send_json(503, _problem(503, "PAYROLL_COMMAND_UNAVAILABLE", "工资写操作尚未配置"))
                return
            session_token, session_subject = payroll_identity
            command = "verify-receipts"
            expected_fields = {
                "expected_revision",
                "reason_code",
                "source_artifact_ids",
            }
            if set(request) != expected_fields:
                self._send_json(422, _problem(422, "INVALID_PAYROLL_COMMAND", "工资批次请求字段不符合合约"))
                return
            expected_revision = request.get("expected_revision")
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
                self._send_json(422, _problem(422, "INVALID_PAYROLL_VERSION", "expected_revision 必须是正整数"))
                return
            source_artifact_ids = request.get("source_artifact_ids")
            if not isinstance(source_artifact_ids, list) or not source_artifact_ids:
                self._send_json(422, _problem(422, "VERIFICATION_EVIDENCE_REQUIRED", "必须选择非空的发放证据"))
                return
            if (
                len(source_artifact_ids) > 100
                or len({value for value in source_artifact_ids if isinstance(value, str)})
                != len(source_artifact_ids)
                or any(
                    not isinstance(value, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None
                    or value.startswith(("artifact_demo_", "receipt_demo_"))
                    for value in source_artifact_ids
                )
            ):
                self._send_json(422, _problem(422, "INVALID_PAYROLL_VERIFICATION_EVIDENCE", "发放证据标识无效"))
                return
            if request.get("reason_code") != "MANUAL_DISBURSEMENT_VERIFICATION":
                self._send_json(422, _problem(422, "INVALID_PAYROLL_VERIFICATION_REASON", "发放验证原因码无效"))
                return
            command_request = {
                "contract_version": "ledgerbridge.payroll-receipt-verification-command.v1",
                "expected_revision": expected_revision,
                "explicitly_confirmed": True,
                "reason_code": "MANUAL_DISBURSEMENT_VERIFICATION",
                "source_artifact_ids": source_artifact_ids,
            }
            status, payload = self.preview_server.state.payroll_batch_command(
                session_token=session_token,
                session_subject=session_subject,
                batch_ref=payroll_batch_command.group(1),
                command=command,
                idempotency_key=idempotency_key.lower(),
                request=command_request,
            )
            self._send_json(status, payload)
            return
        if evidence_unlock:
            try:
                password = request.get("password")
                if set(request) != {"source_ref", "password"}:
                    self._send_json(422, _problem(422, "INVALID_EVIDENCE_UNLOCK_REQUEST", "解锁请求字段不符合合约"))
                    return
                source_ref = request.get("source_ref")
                try:
                    canonical_source_ref = str(uuid.UUID(source_ref)) if isinstance(source_ref, str) else ""
                except ValueError:
                    canonical_source_ref = ""
                if not isinstance(source_ref, str) or source_ref != canonical_source_ref:
                    self._send_json(422, _problem(422, "INVALID_SOURCE_REF", "账单来源引用无效"))
                    return
                if not isinstance(password, str) or not 1 <= len(password) <= 1024 or "\x00" in password:
                    self._send_json(422, _problem(422, "INVALID_EVIDENCE_PASSWORD", "解锁密码无效"))
                    return
                status, payload = self.preview_server.state.unlock_evidence_source(
                    canonical_source_ref,
                    password,
                    idempotency_key.lower(),
                )
            finally:
                if "password" in request:
                    request["password"] = ""
                password = ""
            self._send_json(status, payload)
            return
        if classification_batch_match is not None:
            validated_batch = self._validate_classification_batch(request)
            if validated_batch is None:
                return
            status, payload = self.preview_server.state.apply_candidate_classification_batch(
                classification_batch_match.group(1),
                idempotency_key.lower(),
                validated_batch,
            )
            self._send_json(status, payload)
            return
        if decision_match is not None:
            validated = self._validate_decision(request)
            if validated is None:
                return
            status, payload = self.preview_server.state.append_decision(
                decision_match.group(1), idempotency_key.lower(), validated
            )
            self._send_json(status, payload)
            return
        if bank_statement_review is not None:
            if set(request) != {"expected_revision", "decision", "reason"}:
                self._send_json(422, _problem(422, "INVALID_BANK_STATEMENT_REVIEW", "账单审核请求字段无效"))
                return
            revision = request.get("expected_revision")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or request.get("decision") not in {"CONFIRMED", "REJECTED"}
                or not isinstance(request.get("reason"), str)
                or not str(request["reason"]).strip()
            ):
                self._send_json(422, _problem(422, "INVALID_BANK_STATEMENT_REVIEW", "账单审核内容无效"))
                return
            status, payload = self.preview_server.state.review_bank_statement(
                bank_statement_review.group(1), idempotency_key.lower(), request
            )
            self._send_json(status, payload)
            return
        if company_bank_statement_review is not None:
            if set(request) != {"expected_revision", "decision", "reason"}:
                self._send_json(422, _problem(422, "INVALID_BANK_STATEMENT_REVIEW", "账单审核请求字段无效"))
                return
            revision = request.get("expected_revision")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or request.get("decision") not in {"CONFIRMED", "REJECTED"}
                or not isinstance(request.get("reason"), str)
                or not str(request["reason"]).strip()
            ):
                self._send_json(422, _problem(422, "INVALID_BANK_STATEMENT_REVIEW", "账单审核内容无效"))
                return
            status, payload = self.preview_server.state.review_company_bank_statement(
                company_bank_statement_review.group(1), idempotency_key.lower(), request
            )
            self._send_json(status, payload)
            return
        if company_transaction_review is not None:
            if set(request) != {
                "entity_ref",
                "expected_revision",
                "category_code",
                "reason",
            }:
                self._send_json(
                    422,
                    _problem(
                        422,
                        "INVALID_COMPANY_CLASSIFICATION_REVIEW",
                        "公司流水分类审批字段无效",
                    ),
                )
                return
            revision = request.get("expected_revision")
            reason = request.get("reason")
            try:
                entity_ref = str(uuid.UUID(str(request.get("entity_ref"))))
            except (TypeError, ValueError):
                entity_ref = ""
            if (
                request.get("entity_ref") != entity_ref
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or request.get("category_code") not in COMPANY_TRANSACTION_CATEGORIES
                or not isinstance(reason, str)
                or not 1 <= len(reason.strip()) <= 1000
            ):
                self._send_json(
                    422,
                    _problem(
                        422,
                        "INVALID_COMPANY_CLASSIFICATION_REVIEW",
                        "公司流水分类审批内容无效",
                    ),
                )
                return
            request["reason"] = reason.strip()
            status, payload = (
                self.preview_server.state.review_company_transaction_classification(
                    company_transaction_review.group(1),
                    idempotency_key.lower(),
                    request,
                )
            )
            self._send_json(status, payload)
            return
        month = draft_match.group(1)
        if not MONTH_PATTERN.fullmatch(month):
            self._send_json(400, _problem(400, "INVALID_ACCOUNTING_MONTH", "归属月份格式无效"))
            return
        if set(request) != {"expected_revision"}:
            self._send_json(422, _problem(422, "INVALID_DRAFT_REQUEST", "草稿请求字段不符合合约"))
            return
        expected_revision = request["expected_revision"]
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            self._send_json(422, _problem(422, "INVALID_REVISION", "expected_revision 必须是正整数"))
            return
        status, payload, location = self.preview_server.state.create_draft(
            month, idempotency_key.lower(), expected_revision
        )
        self._send_json(status, payload, headers={"Location": location} if location is not None else None)

    def do_HEAD(self) -> None:
        path = urlsplit(self.path).path
        if path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "3")
            self.end_headers()
            return
        if path.startswith("/api/"):
            self._send_json(405, _problem(405, "METHOD_NOT_ALLOWED", "API 不提供 HEAD"), head=True)
            return
        self._use_spa_fallback()
        if self._static_has_symlink(urlsplit(self.path).path):
            self.send_error(404, "Not found")
            return
        super().do_HEAD()

    def end_headers(self) -> None:
        self.send_header("X-LedgerBridge-Mode", self.preview_server.mode)
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Permitted-Cross-Domain-Policies", "none")
        if self.preview_server.auth_manager is not None:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if urlsplit(self.path).path.startswith("/assets/") else "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        safe_args = args
        if urlsplit(self.path).path == EVIDENCE_UNLOCK_PATH:
            safe_request_line = f"{self.command} {EVIDENCE_UNLOCK_PATH} {self.request_version}"
            safe_args = tuple(safe_request_line if value == self.requestline else value for value in args)
        print(f"{self.client_address[0]} {format % safe_args}", flush=True)


class PreviewServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        site_root: Path,
        state: SyntheticState | CoreBackedState | None = None,
        *,
        auth_manager: AuthManager | None = None,
        mode: str = "synthetic-preview",
        trusted_proxy_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (),
    ) -> None:
        self.site_root = site_root.resolve()
        self.state = state or SyntheticState()
        self.auth_manager = auth_manager
        self.mode = mode
        self.trusted_proxy_networks = trusted_proxy_networks
        super().__init__(server_address, PreviewHandler)

    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def create_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    site_root: str | Path = "/site",
    *,
    state: SyntheticState | CoreBackedState | None = None,
    auth_manager: AuthManager | None = None,
    mode: str = "synthetic-preview",
    trusted_proxy_cidrs: str = "",
) -> PreviewServer:
    root = Path(site_root).resolve()
    if not (root / "index.html").is_file() or (root / "index.html").is_symlink():
        raise FileNotFoundError(f"Missing regular built site: {root / 'index.html'}")
    try:
        trusted_proxy_networks = tuple(
            ipaddress.ip_network(value.strip(), strict=True)
            for value in trusted_proxy_cidrs.split(",")
            if value.strip()
        )
    except ValueError as error:
        raise ValueError("TRUSTED_PROXY_CIDRS must contain exact IP networks") from error
    if mode in {"authenticated-preview", "core-backed"} and not trusted_proxy_networks:
        raise ValueError("authenticated modes require at least one trusted proxy host")
    if any(network.prefixlen != network.max_prefixlen for network in trusted_proxy_networks):
        raise ValueError("TRUSTED_PROXY_CIDRS must contain only IPv4 /32 or IPv6 /128 hosts")
    return PreviewServer(
        (host, port),
        root,
        state,
        auth_manager=auth_manager,
        mode=mode,
        trusted_proxy_networks=trusted_proxy_networks,
    )


def run() -> None:
    mode = os.environ.get("LEDGERBRIDGE_MODE", "synthetic-preview")
    if mode not in {"synthetic-preview", "authenticated-preview", "core-backed"}:
        raise SystemExit("Refusing to start: unsupported LedgerBridge mode")
    site_root = os.environ.get("SITE_ROOT", "/site")
    bind_address = os.environ.get("BIND_ADDRESS", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    cookie_secure = os.environ.get("SESSION_COOKIE_SECURE", "1") not in {"0", "false", "False"}
    auth_manager: AuthManager | None = None
    trusted_proxy_cidrs = ""
    persistence: SQLitePersistence | None = None
    actor = "prototype-single-user"
    state: SyntheticState | CoreBackedState
    if mode in {"authenticated-preview", "core-backed"}:
        if not cookie_secure:
            raise SystemExit("Refusing to start authenticated-preview without Secure cookies")
        trusted_proxy_cidrs = os.environ.get("TRUSTED_PROXY_CIDRS", "").strip()
        if not trusted_proxy_cidrs:
            raise SystemExit("TRUSTED_PROXY_CIDRS is required in authenticated-preview mode")
        data_path = os.environ.get("DATA_PATH")
        if not data_path:
            raise SystemExit("DATA_PATH is required in authenticated-preview mode")
        marker_value = os.environ.get("INSTALLATION_MARKER_PATH")
        if not marker_value:
            raise SystemExit("INSTALLATION_MARKER_PATH is required in authenticated-preview mode")
        marker_path = Path(marker_value)
        if marker_path.is_symlink():
            raise SystemExit("Refusing a symbolic-link installation marker")
        marker_exists = marker_path.is_file()
        if marker_exists and marker_path.read_text(encoding="utf-8") != "ledgerbridge-enrolled-v1\n":
            raise SystemExit("Refusing an invalid installation marker")
        database_exists = Path(data_path).is_file()
        allow_bootstrap = os.environ.get("ALLOW_INITIAL_BOOTSTRAP", "0") in {"1", "true", "True"}
        if marker_exists and not database_exists:
            raise SystemExit("Refusing to start: enrolled installation database is missing")
        if not marker_exists and not allow_bootstrap:
            raise SystemExit("Refusing bootstrap without ALLOW_INITIAL_BOOTSTRAP=1 and a missing enrollment marker")
        if mode == "authenticated-preview":
            persistence = SQLitePersistence(data_path)
        elif sqlite_contains_business_facts(data_path):
            raise SystemExit(
                "Refusing core-backed mode: Web SQLite contains preview business facts"
            )
        auth_store = AuthStore(data_path)
        if marker_exists and not auth_store.initialized():
            raise SystemExit("Refusing to start: enrollment marker exists but no Passkey is registered")
        if not marker_exists and auth_store.initialized():
            raise SystemExit("Refusing to start: registered Passkey exists without the installation marker")
        auth_manager = AuthManager(
            auth_store,
            rp_id=os.environ.get("WEBAUTHN_RP_ID", ""),
            expected_origin=os.environ.get("WEBAUTHN_EXPECTED_ORIGIN", ""),
            setup_code_sha256=os.environ.get("SETUP_CODE_SHA256", ""),
            setup_code_expires_at=int(os.environ.get("SETUP_CODE_EXPIRES_AT", "0")),
        )
        actor = "ledgerbridge-owner"
        try:
            Path(data_path).chmod(0o600)
        except OSError:
            pass
    if mode == "core-backed":
        required = {
            name: os.environ.get(name, "").strip()
            for name in (
                "CORE_BASE_URL",
                "CORE_CA_FILE",
                "CORE_CERT_FILE",
                "CORE_KEY_FILE",
                "CORE_USER_ASSERTION_KEY",
                "CORE_ASSERTION_ISSUER",
                "CORE_ASSERTION_AUDIENCE",
                "CORE_WORKLOAD_PRINCIPAL",
                "CORE_POLICY_GENERATION",
                "CORE_USER_SUBJECT",
                "CORE_AUTHENTICATION_GENERATION",
                "CORE_ENTITY_REF",
                "CORE_BUSINESS_UNIT_REF",
            )
        }
        missing = sorted(name for name, value in required.items() if not value)
        if missing:
            raise SystemExit(
                f"Refusing core-backed mode: required Core settings are missing: {', '.join(missing)}"
            )
        payroll_commands_enabled = os.environ.get("PAYROLL_COMMANDS_ENABLED", "0") in {
            "1",
            "true",
            "True",
        }
        payroll_test_workspace_enabled = os.environ.get(
            "PAYROLL_TEST_WORKSPACE_ENABLED", "0"
        ) in {"1", "true", "True"}
        payroll_test_batch_id = os.environ.get("PAYROLL_TEST_BATCH_ID", "").strip() or None
        payroll_test_workspace_autocreate = os.environ.get(
            "PAYROLL_TEST_WORKSPACE_AUTOCREATE", "0"
        ) in {"1", "true", "True"}
        payroll_role_bindings: dict[str, frozenset[str]] = {}
        raw_payroll_bindings = os.environ.get("PAYROLL_ROLE_BINDINGS_JSON", "").strip()
        if payroll_commands_enabled and not raw_payroll_bindings:
            raise SystemExit(
                "Refusing payroll commands without PAYROLL_ROLE_BINDINGS_JSON"
            )
        if raw_payroll_bindings:
            try:
                parsed_bindings = json.loads(raw_payroll_bindings)
                if not isinstance(parsed_bindings, dict) or any(
                    not isinstance(subject, str)
                    or not isinstance(roles, list)
                    or any(not isinstance(role, str) for role in roles)
                    for subject, roles in parsed_bindings.items()
                ):
                    raise ValueError("invalid payroll role bindings")
                payroll_role_bindings = {
                    subject: frozenset(roles)
                    for subject, roles in parsed_bindings.items()
                }
            except (json.JSONDecodeError, ValueError) as error:
                raise SystemExit("Refusing invalid PAYROLL_ROLE_BINDINGS_JSON") from error
        raw_personal_statement_refs = os.environ.get(
            "CORE_PERSONAL_STATEMENT_REFS", ""
        ).strip()
        personal_statement_refs = (
            tuple(part.strip() for part in raw_personal_statement_refs.split(","))
            if raw_personal_statement_refs
            else None
        )
        raw_candidate_business_units = os.environ.get(
            "CORE_CANDIDATE_BUSINESS_UNIT_REFS", ""
        ).strip()
        candidate_business_unit_refs = (
            tuple(part.strip() for part in raw_candidate_business_units.split(","))
            if raw_candidate_business_units
            else None
        )
        try:
            timeout_seconds = float(os.environ.get("CORE_TIMEOUT_SECONDS", "10"))
            client = CoreHttpClient(
                base_url=required["CORE_BASE_URL"],
                ca_file=required["CORE_CA_FILE"],
                certificate_file=required["CORE_CERT_FILE"],
                private_key_file=required["CORE_KEY_FILE"],
                timeout_seconds=timeout_seconds,
            )
            company_report_client = _build_company_report_client(
                default_base_url=required["CORE_BASE_URL"],
                default_ca_file=required["CORE_CA_FILE"],
                timeout_seconds=timeout_seconds,
            )
            company_bank_review_client = _build_company_bank_review_client(
                default_ca_file=required["CORE_CA_FILE"],
                timeout_seconds=timeout_seconds,
            )
            state = CoreBackedState(
                client,
                company_report_client=company_report_client,
                company_bank_review_client=company_bank_review_client,
                company_bank_statement_mappings=_company_bank_statement_mappings(),
                assertion_key=required["CORE_USER_ASSERTION_KEY"].encode("utf-8"),
                assertion_issuer=required["CORE_ASSERTION_ISSUER"],
                assertion_audience=required["CORE_ASSERTION_AUDIENCE"],
                workload_principal=required["CORE_WORKLOAD_PRINCIPAL"],
                policy_generation=int(required["CORE_POLICY_GENERATION"]),
                user_subject=required["CORE_USER_SUBJECT"],
                authentication_generation=int(required["CORE_AUTHENTICATION_GENERATION"]),
                entity_ref=required["CORE_ENTITY_REF"],
                business_unit_ref=required["CORE_BUSINESS_UNIT_REF"],
                candidate_business_unit_refs=candidate_business_unit_refs,
                personal_finance_entity_ref=os.environ.get(
                    "CORE_PERSONAL_ENTITY_REF", ""
                ).strip()
                or None,
                personal_finance_statement_ref=os.environ.get(
                    "CORE_PERSONAL_STATEMENT_REF", ""
                ).strip()
                or None,
                personal_finance_statement_refs=personal_statement_refs,
                evidence_unlock_path=os.environ.get("CORE_EVIDENCE_UNLOCK_PATH", "").strip() or None,
                payroll_commands_enabled=payroll_commands_enabled,
                payroll_role_bindings=payroll_role_bindings,
                payroll_test_workspace_enabled=payroll_test_workspace_enabled,
                payroll_test_batch_id=payroll_test_batch_id,
                payroll_test_workspace_autocreate=payroll_test_workspace_autocreate,
                payroll_test_workspace_expected_store_revision=int(
                    os.environ.get("PAYROLL_TEST_WORKSPACE_EXPECTED_STORE_REVISION", "0")
                ),
            )
        except (OSError, ValueError) as error:
            raise SystemExit("Refusing core-backed mode: invalid Core settings") from error
    else:
        state = SyntheticState(
            cookie_secure=cookie_secure,
            persistence=persistence,
            actor=actor,
        )
    with create_server(
        host=bind_address,
        port=port,
        site_root=site_root,
        state=state,
        auth_manager=auth_manager,
        mode=mode,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
    ) as server:
        print(f"Serving {Path(site_root).resolve()} in {mode} mode on {bind_address}:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
