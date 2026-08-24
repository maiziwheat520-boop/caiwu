from __future__ import annotations

import base64
import hashlib
import hmac
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
RECONCILIATION_PATH = re.compile(r"^/api/v1/reconciliations/([^/]+)$")
DRAFT_CREATE_PATH = re.compile(r"^/api/v1/reconciliations/([^/]+)/drafts$")
DRAFT_PATH = re.compile(r"^/api/v1/workbook-drafts/([0-9a-f-]{36})$")
EVIDENCE_PATH = re.compile(r"^/api/v1/evidence/([0-9a-f-]{36})/content$")
MAX_REQUEST_BYTES = 64 * 1024
STATUSES = {"INCOMPLETE", "PENDING", "CONFLICTED", "CONFIRMED", "IGNORED", "SUPERSEDED"}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _problem(status: int, code: str, title: str, detail: str = "") -> dict[str, object]:
    payload: dict[str, object] = {"type": "about:blank", "title": title, "status": status, "code": code}
    if detail:
        payload["detail"] = detail
    return payload


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
        seeded_candidates = initial_candidates()
        seeded_events = initial_review_events()
        if persistence is not None:
            persistence.seed_if_empty(seeded_candidates, seeded_events, seed_version="synthetic-v1")
            seeded_candidates = persistence.list_candidates()
            seeded_events = {
                str(candidate["id"]): persistence.get_review_events(str(candidate["id"]))
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
        return self.persistence.list_candidates() if self.persistence is not None else list(self.candidates.values())

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

    def candidate_detail(self, candidate_id: str) -> dict[str, object] | None:
        with self.lock:
            candidate = self.persistence.get_candidate(candidate_id) if self.persistence is not None else self.candidates.get(candidate_id)
            if candidate is None:
                return None
            detail = deepcopy(candidate)
            events = self.persistence.get_review_events(candidate_id) if self.persistence is not None else self.review_events.get(candidate_id, [])
            detail["review_events"] = deepcopy(events)
            return detail

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
            if request["expected_revision"] != candidate["revision"]:
                return 409, _problem(409, "STALE_REVISION", "候选版本已变化，请刷新后重试")
            if candidate["status"] in {"CONFIRMED", "IGNORED", "SUPERSEDED"}:
                return 409, _problem(409, "TERMINAL_CANDIDATE", "候选已完成审核")

            decision = str(request["decision"])
            corrections = deepcopy(request.get("corrections") or {})
            updated = deepcopy(candidate)
            changes: list[dict[str, object]] = []
            for field in ("business_unit", "category", "amount_minor", "accounting_month"):
                if field in corrections and updated[field] != corrections[field]:
                    changes.append({"field": field, "previous_value": updated[field], "new_value": corrections[field]})
                    updated[field] = corrections[field]

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
                changes.append({"field": "status", "previous_value": candidate["status"], "new_value": updated["status"]})
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
        if not isinstance(corrections, dict) or not corrections or set(corrections) - {"business_unit", "category", "amount_minor", "accounting_month"}:
            self._invalid_decision("INVALID_CORRECTIONS", "corrections 必须包含至少一个受支持字段")
            return False
        for field in ("business_unit", "category"):
            if field in corrections and (not isinstance(corrections[field], str) or not 1 <= len(corrections[field]) <= 200):
                self._invalid_decision("INVALID_CORRECTIONS", f"{field} 无效")
                return False
        if "amount_minor" in corrections and (isinstance(corrections["amount_minor"], bool) or not isinstance(corrections["amount_minor"], int)):
            self._invalid_decision("INVALID_CORRECTIONS", "amount_minor 必须是整数")
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
                self._send_json(200, state.session_payload(), cookie=True)
                return
            token = self._session_token()
            payload = manager.session_payload(token) if token else None
            if payload is None:
                self._send_json(401, _problem(401, "AUTHENTICATION_REQUIRED", "需要先完成身份验证"))
            else:
                self._send_json(200, payload)
            return
        if not self._require_session():
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
            if cursor is not None and (not cursor.isdigit() or int(cursor) < 0):
                self._send_json(400, _problem(400, "INVALID_CURSOR", "分页游标无效"))
                return
            self._send_json(200, state.list_candidates(status=status, month=month, cursor=cursor))
            return
        match = CANDIDATE_PATH.fullmatch(path)
        if match:
            detail = state.candidate_detail(match.group(1))
            self._send_json(200, detail) if detail else self._send_json(404, _problem(404, "CANDIDATE_NOT_FOUND", "候选不存在"))
            return
        match = EVIDENCE_PATH.fullmatch(path)
        if match:
            evidence = SYNTHETIC_EVIDENCE_CONTENT.get(match.group(1))
            if evidence is None:
                self._send_json(404, _problem(404, "EVIDENCE_NOT_FOUND", "证据不存在"))
            else:
                self._send_evidence(evidence)
            return
        match = RECONCILIATION_PATH.fullmatch(path)
        if match:
            month = match.group(1)
            if not MONTH_PATTERN.fullmatch(month):
                self._send_json(400, _problem(400, "INVALID_ACCOUNTING_MONTH", "归属月份格式无效"))
            else:
                self._send_json(200, state.reconciliation(month))
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
            self._send_json(200, {"items": deepcopy(SYNTHETIC_CONNECTIONS)})
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
            self._api_get(split.path, split.query)
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
            if path == "/api/v1/auth/passkey/register/options":
                if set(request) != {"setup_code"} or not isinstance(request["setup_code"], str):
                    raise AuthError(422, "INVALID_AUTH_REQUEST", "setup_code 字段无效")
                if manager.store.initialized() and not self._require_csrf():
                    return True
                options, flow_token = manager.start_registration(request["setup_code"], self._session_token())
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
                options, flow_token = manager.start_login()
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
            session = manager.recover(request["recovery_code"])
            self._send_json(200, {"authenticated": False, "setup_required": False, "passkey_registered": True, "recovery_setup_required": True, "recovery_pending": True, "csrf_token": session.csrf_token, "expires_at": session.expires_at}, cookies=[self._session_cookie(session.token)])
            return True
        except AuthError as error:
            self._send_json(error.status, _problem(error.status, error.code, error.detail))
            return True

    def do_POST(self) -> None:
        # A rejected POST can leave an unread body. Close the connection so those
        # bytes can never be interpreted as a second HTTP request.
        self.close_connection = True
        path = urlsplit(self.path).path
        if self._auth_post(path):
            return
        decision_match = DECISION_PATH.fullmatch(path)
        draft_match = DRAFT_CREATE_PATH.fullmatch(path)
        if decision_match is None and draft_match is None:
            self._send_json(404 if path.startswith("/api/") else 405, _problem(404 if path.startswith("/api/") else 405, "API_ROUTE_NOT_FOUND" if path.startswith("/api/") else "METHOD_NOT_ALLOWED", "路径不接受该请求"))
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
        request = self._read_json_object()
        if request is None:
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
        print(f"{self.client_address[0]} {format % args}", flush=True)


class PreviewServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        site_root: Path,
        state: SyntheticState | None = None,
        *,
        auth_manager: AuthManager | None = None,
        mode: str = "synthetic-preview",
    ) -> None:
        self.site_root = site_root.resolve()
        self.state = state or SyntheticState()
        self.auth_manager = auth_manager
        self.mode = mode
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
    state: SyntheticState | None = None,
    auth_manager: AuthManager | None = None,
    mode: str = "synthetic-preview",
) -> PreviewServer:
    root = Path(site_root).resolve()
    if not (root / "index.html").is_file() or (root / "index.html").is_symlink():
        raise FileNotFoundError(f"Missing regular built site: {root / 'index.html'}")
    return PreviewServer((host, port), root, state, auth_manager=auth_manager, mode=mode)


def run() -> None:
    mode = os.environ.get("LEDGERBRIDGE_MODE", "synthetic-preview")
    if mode not in {"synthetic-preview", "authenticated-preview"}:
        raise SystemExit("Refusing to start: unsupported LedgerBridge mode")
    site_root = os.environ.get("SITE_ROOT", "/site")
    bind_address = os.environ.get("BIND_ADDRESS", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    cookie_secure = os.environ.get("SESSION_COOKIE_SECURE", "1") not in {"0", "false", "False"}
    auth_manager: AuthManager | None = None
    persistence: SQLitePersistence | None = None
    actor = "prototype-single-user"
    if mode == "authenticated-preview":
        if not cookie_secure:
            raise SystemExit("Refusing to start authenticated-preview without Secure cookies")
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
        persistence = SQLitePersistence(data_path)
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
    state = SyntheticState(cookie_secure=cookie_secure, persistence=persistence, actor=actor)
    with create_server(host=bind_address, port=port, site_root=site_root, state=state, auth_manager=auth_manager, mode=mode) as server:
        print(f"Serving {Path(site_root).resolve()} in {mode} mode on {bind_address}:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
