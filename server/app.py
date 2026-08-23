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

from .synthetic_data import (
    SYNTHETIC_CONNECTIONS,
    SYNTHETIC_EVIDENCE_CONTENT,
    SYNTHETIC_RECONCILIATION,
    initial_candidates,
    initial_review_events,
)


COOKIE_NAME = "__Host-ledgerbridge_session"
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
    """Process-local synthetic state; a restart discards every review decision."""

    def __init__(self, *, cookie_secure: bool = True) -> None:
        self.lock = threading.RLock()
        self.candidates = {str(item["id"]): item for item in initial_candidates()}
        self.review_events = initial_review_events()
        self.idempotency: dict[str, tuple[str, dict[str, object]]] = {}
        self.draft_idempotency: dict[str, tuple[str, dict[str, object], str]] = {}
        self.drafts: dict[str, dict[str, object]] = {}
        self.session_id = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.expires_at = datetime.now(timezone.utc) + timedelta(hours=12)
        self.cookie_secure = cookie_secure
        self.cookie_name = COOKIE_NAME if cookie_secure else "ledgerbridge_preview_session"
        self.reconciliation_revision = int(SYNTHETIC_RECONCILIATION["revision"])

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
            candidate = self.candidates.get(candidate_id)
            if candidate is None:
                return None
            detail = deepcopy(candidate)
            detail["review_events"] = deepcopy(self.review_events.get(candidate_id, []))
            return detail

    def list_candidates(self, *, status: str | None, month: str | None, cursor: str | None) -> dict[str, object]:
        offset = int(cursor) if cursor is not None else 0
        with self.lock:
            items = [deepcopy(item) for item in self.candidates.values()]
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
            if any(item["status"] == "CONFLICTED" for item in self.candidates.values()):
                blockers.append({"code": "BUSINESS_KEY_CONFLICT", "message": "城南店银行收款候选存在金额冲突。"})
            if any(item["status"] == "INCOMPLETE" for item in self.candidates.values()):
                blockers.append({"code": "MISSING_ACCOUNTING_MONTH", "message": "机场店水费候选尚未确认归属月份。"})
            payload["revision"] = self.reconciliation_revision
            payload["blockers"] = blockers
            payload["ready"] = not blockers
            return payload

    def append_decision(
        self, candidate_id: str, idempotency_key: str, request: dict[str, object]
    ) -> tuple[int, dict[str, object]]:
        fingerprint = hashlib.sha256(
            (candidate_id + "\n" + json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))).encode()
        ).hexdigest()
        with self.lock:
            replay = self.idempotency.get(idempotency_key)
            if replay is not None:
                old_fingerprint, old_response = replay
                if not hmac.compare_digest(old_fingerprint, fingerprint):
                    return 409, _problem(409, "IDEMPOTENCY_KEY_REUSED", "幂等键已用于其他请求")
                return 200, deepcopy(old_response)

            candidate = self.candidates.get(candidate_id)
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
            events = self.review_events.setdefault(candidate_id, [])
            event: dict[str, object] = {
                "id": str(uuid.uuid4()),
                "candidate_id": candidate_id,
                "sequence": len(events) + 1,
                "from_revision": from_revision,
                "to_revision": to_revision,
                "decision": decision,
                "actor": "prototype-single-user",
                "reason": request["reason"],
                "changes": changes,
                "conflict_resolution": request.get("conflict_resolution"),
                "created_at": _utc_timestamp(),
            }
            events.append(deepcopy(event))
            self.candidates[candidate_id] = updated
            self.reconciliation_revision += 1
            response = {"candidate": deepcopy(updated), "event": deepcopy(event)}
            self.idempotency[idempotency_key] = (fingerprint, deepcopy(response))
            return 200, response

    def create_draft(
        self, month: str, idempotency_key: str, expected_revision: int
    ) -> tuple[int, dict[str, object], str | None]:
        fingerprint = hashlib.sha256(f"{month}\n{expected_revision}".encode()).hexdigest()
        with self.lock:
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
            self.drafts[draft_id] = deepcopy(draft)
            self.draft_idempotency[idempotency_key] = (fingerprint, deepcopy(draft), location)
            return 202, draft, location

    def get_draft(self, draft_id: str) -> dict[str, object] | None:
        with self.lock:
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
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        content_type = "application/problem+json" if status >= 400 else "application/json"
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if cookie:
            secure = "; Secure" if self.preview_server.state.cookie_secure else ""
            self.send_header("Set-Cookie", f"{self.preview_server.state.cookie_name}={self.preview_server.state.session_id}; Path=/; HttpOnly; SameSite=Strict{secure}")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(encoded)

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

    def _authenticated(self) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        morsel = cookie.get(self.preview_server.state.cookie_name)
        return bool(
            morsel
            and self.preview_server.state.session_active()
            and hmac.compare_digest(morsel.value, self.preview_server.state.session_id)
        )

    def _require_session(self) -> bool:
        if self._authenticated():
            return True
        self._send_json(401, _problem(401, "AUTHENTICATION_REQUIRED", "需要先建立合成预览会话"))
        return False

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
        if path == "/api/v1/session":
            self._send_json(200, state.session_payload(), cookie=True)
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

    def do_POST(self) -> None:
        # A rejected POST can leave an unread body. Close the connection so those
        # bytes can never be interpreted as a second HTTP request.
        self.close_connection = True
        path = urlsplit(self.path).path
        decision_match = DECISION_PATH.fullmatch(path)
        draft_match = DRAFT_CREATE_PATH.fullmatch(path)
        if decision_match is None and draft_match is None:
            self._send_json(404 if path.startswith("/api/") else 405, _problem(404 if path.startswith("/api/") else 405, "API_ROUTE_NOT_FOUND" if path.startswith("/api/") else "METHOD_NOT_ALLOWED", "路径不接受该请求"))
            return
        if not self._require_session():
            return
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.preview_server.state.csrf_token):
            self._send_json(403, _problem(403, "CSRF_VALIDATION_FAILED", "CSRF 令牌缺失或无效"))
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
        self.send_header("X-LedgerBridge-Mode", "synthetic-preview")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Permitted-Cross-Domain-Policies", "none")
        self.send_header("Cache-Control", "public, max-age=31536000, immutable" if urlsplit(self.path).path.startswith("/assets/") else "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.client_address[0]} {format % args}", flush=True)


class PreviewServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], site_root: Path, state: SyntheticState | None = None) -> None:
        self.site_root = site_root.resolve()
        self.state = state or SyntheticState()
        super().__init__(server_address, PreviewHandler)

    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def create_server(host: str = "0.0.0.0", port: int = 8080, site_root: str | Path = "/site", *, state: SyntheticState | None = None) -> PreviewServer:
    root = Path(site_root).resolve()
    if not (root / "index.html").is_file() or (root / "index.html").is_symlink():
        raise FileNotFoundError(f"Missing regular built site: {root / 'index.html'}")
    return PreviewServer((host, port), root, state)


def run() -> None:
    mode = os.environ.get("LEDGERBRIDGE_MODE", "synthetic-preview")
    if mode != "synthetic-preview":
        raise SystemExit("Refusing to start: this server supports synthetic-preview mode only")
    site_root = os.environ.get("SITE_ROOT", "/site")
    bind_address = os.environ.get("BIND_ADDRESS", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    cookie_secure = os.environ.get("SESSION_COOKIE_SECURE", "1") not in {"0", "false", "False"}
    with create_server(host=bind_address, port=port, site_root=site_root, state=SyntheticState(cookie_secure=cookie_secure)) as server:
        print(f"Serving {Path(site_root).resolve()} in synthetic-preview mode on {bind_address}:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
