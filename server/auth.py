from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


AUTH_SCHEMA_VERSION = 2
SESSION_LIFETIME = timedelta(hours=12)
CEREMONY_LIFETIME_SECONDS = 300
MAX_ACTIVE_CEREMONIES = 128
MAX_ACTIVE_PUBLIC_CEREMONIES = 120
MAX_ACTIVE_CEREMONIES_PER_CALLER = 5
MAX_FAILURE_BUCKETS = 4096
MAX_PASSKEY_CREDENTIALS = 10
RECOVERY_CODE_COUNT = 10
RECOVERY_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
RECOVERY_CODE_LENGTH = 32
RP_LABEL_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _utc_iso(timestamp: float | None = None) -> str:
    instant = datetime.fromtimestamp(timestamp, timezone.utc) if timestamp is not None else datetime.now(timezone.utc)
    return instant.isoformat(timespec="seconds")


def _token_hash(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _normalise_code(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


class AuthError(RuntimeError):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class StoredCredential:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    device_type: str
    backed_up: bool


@dataclass(frozen=True)
class AuthSession:
    token: str
    csrf_token: str
    expires_at: str


@dataclass(frozen=True)
class Ceremony:
    kind: str
    challenge: bytes
    user_id: bytes
    expires_at: float
    caller_key: str
    initial_registration: bool = False
    recovery_registration: bool = False
    authorization_session_hash: bytes | None = None
    auth_epoch: int | None = None


class AuthStore:
    """Persistent single-user credentials, recovery codes, and hashed sessions."""

    def __init__(self, database_path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        raw_path = Path(database_path)
        if raw_path.is_symlink():
            raise ValueError("database_path must not be a symbolic link")
        self.path = raw_path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1_000, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS auth_schema (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL) STRICT"
            )
            versions = [int(row[0]) for row in connection.execute("SELECT version FROM auth_schema")]
            if any(version != AUTH_SCHEMA_VERSION for version in versions):
                raise RuntimeError("unsupported authentication schema version")
            if not versions and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'auth_user'"
            ).fetchone():
                raise RuntimeError("authentication schema metadata is missing")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_user (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    user_id BLOB NOT NULL UNIQUE,
                    user_name TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS passkey_credentials (
                    credential_id BLOB PRIMARY KEY,
                    user_id BLOB NOT NULL REFERENCES auth_user(user_id),
                    public_key BLOB NOT NULL,
                    sign_count INTEGER NOT NULL CHECK (sign_count >= 0),
                    device_type TEXT NOT NULL,
                    backed_up INTEGER NOT NULL CHECK (backed_up IN (0, 1)),
                    created_at TEXT NOT NULL,
                    last_used_at TEXT
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recovery_codes (
                    code_hash BLOB PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash BLOB PRIMARY KEY,
                    csrf_hash BLOB NOT NULL,
                    previous_csrf_hash BLOB,
                    created_at TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    authenticated_with TEXT NOT NULL
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    auth_epoch INTEGER NOT NULL CHECK (auth_epoch >= 0),
                    recovery_pending INTEGER NOT NULL CHECK (recovery_pending IN (0, 1))
                ) STRICT
                """
            )
            if not versions:
                connection.execute(
                    "INSERT INTO auth_state(singleton, auth_epoch, recovery_pending) VALUES (1, 0, 0)"
                )
                connection.execute(
                    "INSERT INTO auth_schema(version, applied_at) VALUES (?, ?)",
                    (AUTH_SCHEMA_VERSION, _utc_iso()),
                )
            elif connection.execute("SELECT 1 FROM auth_state WHERE singleton = 1").fetchone() is None:
                raise RuntimeError("authentication state row is missing")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialized(self) -> bool:
        with self.connection() as connection:
            return connection.execute("SELECT 1 FROM auth_user WHERE singleton = 1").fetchone() is not None

    def user_id(self) -> bytes | None:
        with self.connection() as connection:
            row = connection.execute("SELECT user_id FROM auth_user WHERE singleton = 1").fetchone()
            return bytes(row[0]) if row else None

    def credentials(self) -> list[StoredCredential]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT credential_id, public_key, sign_count, device_type, backed_up FROM passkey_credentials ORDER BY created_at"
            ).fetchall()
        return [
            StoredCredential(bytes(row[0]), bytes(row[1]), int(row[2]), str(row[3]), bool(row[4]))
            for row in rows
        ]

    def credential(self, credential_id: bytes) -> StoredCredential | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT credential_id, public_key, sign_count, device_type, backed_up FROM passkey_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
        return StoredCredential(bytes(row[0]), bytes(row[1]), int(row[2]), str(row[3]), bool(row[4])) if row else None

    def login_epoch(self) -> int:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT auth_epoch, recovery_pending FROM auth_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise AuthError(500, "AUTH_STATE_INVALID", "认证状态不完整")
        if bool(row[1]):
            raise AuthError(409, "RECOVERY_IN_PROGRESS", "账户恢复尚未完成，请使用恢复码继续")
        return int(row[0])

    def recovery_pending(self) -> bool:
        with self.connection() as connection:
            row = connection.execute("SELECT recovery_pending FROM auth_state WHERE singleton = 1").fetchone()
        return bool(row and row[0])

    def register_credential(
        self,
        *,
        user_id: bytes,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        device_type: str,
        backed_up: bool,
        initial: bool,
    ) -> list[str]:
        now = _utc_iso()
        recovery_codes = _generate_recovery_codes() if initial else []
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT user_id FROM auth_user WHERE singleton = 1").fetchone()
            if existing is None:
                if not initial:
                    raise AuthError(409, "AUTH_NOT_INITIALIZED", "尚未完成首次通行密钥登记")
                connection.execute(
                    "INSERT INTO auth_user(singleton, user_id, user_name, display_name, created_at) VALUES (1, ?, ?, ?, ?)",
                    (user_id, "ledgerbridge-owner", "LedgerBridge Owner", now),
                )
            elif not hmac.compare_digest(bytes(existing[0]), user_id):
                raise AuthError(409, "USER_ID_MISMATCH", "通行密钥用户标识不匹配")
            connection.execute(
                """
                INSERT INTO passkey_credentials(
                    credential_id, user_id, public_key, sign_count, device_type, backed_up, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (credential_id, user_id, public_key, sign_count, device_type, int(backed_up), now),
            )
            if initial:
                connection.execute("DELETE FROM recovery_codes")
                connection.executemany(
                    "INSERT INTO recovery_codes(code_hash, created_at) VALUES (?, ?)",
                    [(_token_hash(_normalise_code(code)), now) for code in recovery_codes],
                )
            connection.commit()
            return recovery_codes
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise AuthError(409, "PASSKEY_ALREADY_REGISTERED", "该通行密钥已经登记") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def add_credential_after_step_up(
        self,
        *,
        user_id: bytes,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        device_type: str,
        backed_up: bool,
        authorization_session_token: str,
        expected_auth_epoch: int,
    ) -> int:
        now = _utc_iso()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            authorization = connection.execute(
                """
                SELECT 1 FROM auth_sessions
                WHERE token_hash = ? AND expires_at > ?
                  AND authenticated_with IN ('passkey', 'passkey-registration')
                """,
                (_token_hash(authorization_session_token), int(time.time())),
            ).fetchone()
            state = connection.execute(
                "SELECT auth_epoch, recovery_pending FROM auth_state WHERE singleton = 1"
            ).fetchone()
            existing = connection.execute("SELECT user_id FROM auth_user WHERE singleton = 1").fetchone()
            if authorization is None:
                raise AuthError(401, "AUTH_CEREMONY_REVOKED", "授权此次登记的登录会话已失效")
            if state is None or bool(state[1]) or int(state[0]) != expected_auth_epoch:
                raise AuthError(401, "AUTH_CEREMONY_REVOKED", "认证状态已经变化，请重新开始")
            if existing is None or not hmac.compare_digest(bytes(existing[0]), user_id):
                raise AuthError(409, "USER_ID_MISMATCH", "通行密钥用户标识不匹配")
            credential_count = int(connection.execute("SELECT count(*) FROM passkey_credentials").fetchone()[0])
            if credential_count >= MAX_PASSKEY_CREDENTIALS:
                raise AuthError(409, "PASSKEY_LIMIT_REACHED", "已达到通行密钥数量上限")
            connection.execute(
                """
                INSERT INTO passkey_credentials(
                    credential_id, user_id, public_key, sign_count, device_type, backed_up, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (credential_id, user_id, public_key, sign_count, device_type, int(backed_up), now),
            )
            connection.commit()
            return credential_count + 1
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise AuthError(409, "PASSKEY_ALREADY_REGISTERED", "该通行密钥已经登记") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def replace_credentials_after_recovery(
        self,
        *,
        user_id: bytes,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        device_type: str,
        backed_up: bool,
        authorization_session_token: str,
    ) -> tuple[list[str], AuthSession]:
        recovery_codes = _generate_recovery_codes()
        now = _utc_iso()
        session, session_values = self._new_session("passkey-registration")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            authorization = connection.execute(
                """
                SELECT 1 FROM auth_sessions
                WHERE token_hash = ? AND expires_at > ? AND authenticated_with = 'recovery-code'
                """,
                (_token_hash(authorization_session_token), int(time.time())),
            ).fetchone()
            if authorization is None:
                raise AuthError(401, "AUTH_CEREMONY_REVOKED", "授权此次登记的恢复会话已失效")
            existing = connection.execute("SELECT user_id FROM auth_user WHERE singleton = 1").fetchone()
            if existing is None or not hmac.compare_digest(bytes(existing[0]), user_id):
                raise AuthError(409, "USER_ID_MISMATCH", "通行密钥用户标识不匹配")
            connection.execute("DELETE FROM passkey_credentials")
            connection.execute(
                """
                INSERT INTO passkey_credentials(
                    credential_id, user_id, public_key, sign_count, device_type, backed_up, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (credential_id, user_id, public_key, sign_count, device_type, int(backed_up), now),
            )
            connection.execute("DELETE FROM recovery_codes")
            connection.executemany(
                "INSERT INTO recovery_codes(code_hash, created_at) VALUES (?, ?)",
                [(_token_hash(_normalise_code(code)), now) for code in recovery_codes],
            )
            connection.execute("DELETE FROM auth_sessions")
            connection.execute(
                "UPDATE auth_state SET auth_epoch = auth_epoch + 1, recovery_pending = 0 WHERE singleton = 1"
            )
            connection.execute(
                "INSERT INTO auth_sessions(token_hash, csrf_hash, created_at, expires_at, authenticated_with) VALUES (?, ?, ?, ?, ?)",
                session_values,
            )
            connection.commit()
            return recovery_codes, session
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise AuthError(409, "PASSKEY_ALREADY_REGISTERED", "该通行密钥已经登记") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def consume_recovery_code(self, code: str) -> bool:
        digest = _token_hash(_normalise_code(code))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE recovery_codes SET consumed_at = ? WHERE code_hash = ? AND consumed_at IS NULL",
                (_utc_iso(), digest),
            )
            connection.commit()
            return cursor.rowcount == 1
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def create_session(self, authenticated_with: str) -> AuthSession:
        session, values = self._new_session(authenticated_with)
        with self.connection() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (int(time.time()),))
            connection.execute(
                "INSERT INTO auth_sessions(token_hash, csrf_hash, created_at, expires_at, authenticated_with) VALUES (?, ?, ?, ?, ?)",
                values,
            )
        return session

    def recover_with_code(self, code: str) -> AuthSession | None:
        session, values = self._new_session("recovery-code")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE recovery_codes SET consumed_at = ? WHERE code_hash = ? AND consumed_at IS NULL",
                (_utc_iso(), _token_hash(_normalise_code(code))),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.execute("DELETE FROM auth_sessions")
            connection.execute(
                "UPDATE auth_state SET auth_epoch = auth_epoch + 1, recovery_pending = 1 WHERE singleton = 1"
            )
            connection.execute(
                "INSERT INTO auth_sessions(token_hash, csrf_hash, created_at, expires_at, authenticated_with) VALUES (?, ?, ?, ?, ?)",
                values,
            )
            connection.commit()
            return session
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_login(
        self,
        credential_id: bytes,
        *,
        expected_sign_count: int,
        new_sign_count: int,
        device_type: str,
        backed_up: bool,
        expected_auth_epoch: int,
    ) -> AuthSession:
        session, values = self._new_session("passkey")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT auth_epoch, recovery_pending FROM auth_state WHERE singleton = 1"
            ).fetchone()
            if (
                state is None
                or bool(state[1])
                or int(state[0]) != expected_auth_epoch
            ):
                raise AuthError(401, "AUTH_CEREMONY_REVOKED", "认证状态已经变化，请重新开始")
            cursor = connection.execute(
                """
                UPDATE passkey_credentials
                SET sign_count = ?, device_type = ?, backed_up = ?, last_used_at = ?
                WHERE credential_id = ? AND sign_count = ?
                """,
                (new_sign_count, device_type, int(backed_up), _utc_iso(), credential_id, expected_sign_count),
            )
            if cursor.rowcount != 1:
                raise AuthError(409, "PASSKEY_STATE_CHANGED", "通行密钥状态已经变化，请重新验证")
            connection.execute(
                "INSERT INTO auth_sessions(token_hash, csrf_hash, created_at, expires_at, authenticated_with) VALUES (?, ?, ?, ?, ?)",
                values,
            )
            connection.commit()
            return session
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_step_up(
        self,
        credential_id: bytes,
        *,
        expected_sign_count: int,
        new_sign_count: int,
        device_type: str,
        backed_up: bool,
        expected_auth_epoch: int,
        authorization_session_token: str,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            authorization = connection.execute(
                """
                SELECT 1 FROM auth_sessions
                WHERE token_hash = ? AND expires_at > ?
                  AND authenticated_with IN ('passkey', 'passkey-registration')
                """,
                (_token_hash(authorization_session_token), int(time.time())),
            ).fetchone()
            state = connection.execute(
                "SELECT auth_epoch, recovery_pending FROM auth_state WHERE singleton = 1"
            ).fetchone()
            if authorization is None:
                raise AuthError(401, "AUTH_CEREMONY_REVOKED", "授权此次登记的登录会话已失效")
            if state is None or bool(state[1]) or int(state[0]) != expected_auth_epoch:
                raise AuthError(401, "AUTH_CEREMONY_REVOKED", "认证状态已经变化，请重新开始")
            cursor = connection.execute(
                """
                UPDATE passkey_credentials
                SET sign_count = ?, device_type = ?, backed_up = ?, last_used_at = ?
                WHERE credential_id = ? AND sign_count = ?
                """,
                (new_sign_count, device_type, int(backed_up), _utc_iso(), credential_id, expected_sign_count),
            )
            if cursor.rowcount != 1:
                raise AuthError(409, "PASSKEY_STATE_CHANGED", "通行密钥状态已经变化，请重新验证")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _new_session(authenticated_with: str) -> tuple[AuthSession, tuple[bytes, bytes, str, int, str]]:
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + SESSION_LIFETIME
        session = AuthSession(token, csrf_token, expires.isoformat(timespec="seconds"))
        values = (
            _token_hash(token),
            _token_hash(csrf_token),
            _utc_iso(),
            int(expires.timestamp()),
            authenticated_with,
        )
        return session, values

    def session_payload(self, token: str, *, rotate_csrf: bool = True) -> dict[str, str] | None:
        digest = _token_hash(token)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT expires_at FROM auth_sessions WHERE token_hash = ? AND expires_at > ?",
                (digest, int(time.time())),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            csrf_token = secrets.token_urlsafe(32)
            if rotate_csrf:
                connection.execute(
                    "UPDATE auth_sessions SET previous_csrf_hash = csrf_hash, csrf_hash = ? WHERE token_hash = ?",
                    (_token_hash(csrf_token), digest),
                )
            connection.commit()
            return {
                "principal": "ledgerbridge-owner",
                "csrf_token": csrf_token,
                "expires_at": _utc_iso(float(row[0])),
            }
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def validate_csrf(self, token: str, csrf_token: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT csrf_hash, previous_csrf_hash FROM auth_sessions WHERE token_hash = ? AND expires_at > ?",
                (_token_hash(token), int(time.time())),
            ).fetchone()
        digest = _token_hash(csrf_token)
        return bool(
            row
            and (
                hmac.compare_digest(bytes(row[0]), digest)
                or (row[1] is not None and hmac.compare_digest(bytes(row[1]), digest))
            )
        )

    def session_method(self, token: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT authenticated_with FROM auth_sessions WHERE token_hash = ? AND expires_at > ?",
                (_token_hash(token), int(time.time())),
            ).fetchone()
        return str(row[0]) if row else None

    def logout(self, token: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (_token_hash(token),))

    def revoke_all_sessions(self) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM auth_sessions")


class FailureGate:
    """Small in-process throttle; recovery codes remain high-entropy and one-use."""

    def __init__(self, *, attempts: int = 5, window_seconds: int = 900) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def begin_attempt(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            recent = self._failures.get(key, [])
            if len(recent) >= self.attempts:
                raise AuthError(429, "AUTH_RATE_LIMITED", "认证尝试过多，请稍后重试")
            if key not in self._failures and len(self._failures) >= MAX_FAILURE_BUCKETS:
                oldest = min(self._failures, key=lambda item: self._failures[item][-1])
                self._failures.pop(oldest, None)
            self._failures.setdefault(key, []).append(now)

    def success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def _prune(self, now: float) -> None:
        for key in list(self._failures):
            recent = [stamp for stamp in self._failures[key] if now - stamp < self.window_seconds]
            if recent:
                self._failures[key] = recent
            else:
                self._failures.pop(key, None)


class AuthManager:
    def __init__(
        self,
        store: AuthStore,
        *,
        rp_id: str,
        expected_origin: str,
        setup_code_sha256: str,
        setup_code_expires_at: int,
    ) -> None:
        rp_id = rp_id.lower().rstrip(".")
        parsed_origin = urlsplit(expected_origin)
        try:
            origin_port = parsed_origin.port
        except ValueError as error:
            raise ValueError("Passkey origin contains an invalid port") from error
        if (
            parsed_origin.scheme != "https"
            or not parsed_origin.hostname
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.query
            or parsed_origin.fragment
            or parsed_origin.hostname.lower().rstrip(".") != rp_id
        ):
            raise ValueError("Passkey mode requires an exact HTTPS origin whose hostname equals the RP ID")
        try:
            ipaddress.ip_address(rp_id)
        except ValueError:
            pass
        else:
            raise ValueError("Passkey RP ID must be a DNS hostname, not an IP address")
        labels = rp_id.split(".")
        if len(labels) < 2 or any(not RP_LABEL_PATTERN.fullmatch(label) for label in labels):
            raise ValueError("Passkey RP ID must be a valid DNS hostname")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", setup_code_sha256):
            raise ValueError("SETUP_CODE_SHA256 must contain one SHA-256 hex digest")
        if not store.initialized() and setup_code_expires_at > int(time.time()) + 600:
            raise ValueError("Initial setup code lifetime must not exceed ten minutes")
        self.store = store
        self.rp_id = rp_id
        self.expected_origin = f"https://{rp_id}" + (f":{origin_port}" if origin_port not in {None, 443} else "")
        self.setup_code_sha256 = setup_code_sha256.lower()
        self.setup_code_expires_at = setup_code_expires_at
        self._ceremonies: dict[bytes, Ceremony] = {}
        self._ceremony_lock = threading.Lock()
        self.failures = FailureGate()

    def status(self, session_token: str | None) -> dict[str, bool]:
        method = self.store.session_method(session_token) if session_token else None
        authenticated = bool(method and method != "recovery-code")
        initialized = self.store.initialized()
        return {
            "authenticated": authenticated,
            "setup_required": not initialized,
            "passkey_registered": bool(self.store.credentials()),
            "recovery_setup_required": method == "recovery-code",
            "recovery_pending": self.store.recovery_pending(),
        }

    def session_payload(self, session_token: str) -> dict[str, str] | None:
        if self.store.session_method(session_token) == "recovery-code":
            return None
        return self.store.session_payload(session_token)

    def recovery_session_payload(self, session_token: str) -> dict[str, str] | None:
        if self.store.session_method(session_token) != "recovery-code":
            return None
        return self.store.session_payload(session_token)

    def payroll_session_subject(self, session_token: str | None) -> str | None:
        if not session_token or self.store.session_method(session_token) in {None, "recovery-code"}:
            return None
        payload = self.store.session_payload(session_token, rotate_csrf=False)
        if payload is None:
            return None
        principal = payload.get("principal")
        return principal if isinstance(principal, str) and principal else None

    def _require_full_session(self, session_token: str | None) -> str:
        if not session_token:
            raise AuthError(401, "AUTHENTICATION_REQUIRED", "需要先登录才能添加通行密钥")
        method = self.store.session_method(session_token)
        if method is None:
            raise AuthError(401, "AUTHENTICATION_REQUIRED", "登录会话已经失效")
        if method == "recovery-code":
            raise AuthError(403, "PASSKEY_REAUTH_REQUIRED", "账户恢复期间不能添加其他通行密钥")
        return session_token

    def _registration_options(self, user_id: bytes) -> tuple[dict[str, object], bytes]:
        credentials = self.store.credentials()
        if len(credentials) >= MAX_PASSKEY_CREDENTIALS:
            raise AuthError(409, "PASSKEY_LIMIT_REACHED", "已达到通行密钥数量上限")
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name="LedgerBridge",
            user_id=user_id,
            user_name="ledgerbridge-owner",
            user_display_name="LedgerBridge Owner",
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                require_resident_key=True,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[PublicKeyCredentialDescriptor(id=item.credential_id) for item in credentials],
        )
        return json.loads(options_to_json(options)), options.challenge

    def start_registration(
        self,
        setup_code: str,
        session_token: str | None,
        *,
        caller_key: str,
        previous_flow_token: str | None = None,
    ) -> tuple[dict[str, object], str]:
        initialized = self.store.initialized()
        recovery_registration = False
        if initialized:
            method = self.store.session_method(session_token) if session_token else None
            if method is None:
                raise AuthError(401, "AUTHENTICATION_REQUIRED", "需要先登录才能添加通行密钥")
            if method != "recovery-code":
                raise AuthError(403, "PASSKEY_REAUTH_REQUIRED", "添加通行密钥需要新的通行密钥验证流程")
            recovery_registration = method == "recovery-code"
            user_id = self.store.user_id()
            if user_id is None:
                raise AuthError(500, "AUTH_STATE_INVALID", "认证状态不完整")
        else:
            failure_key = f"setup:{caller_key}"
            if int(time.time()) >= self.setup_code_expires_at:
                raise AuthError(401, "SETUP_CODE_EXPIRED", "初始设置码已过期")
            self.failures.begin_attempt(failure_key)
            supplied = hashlib.sha256(_normalise_code(setup_code).encode("utf-8")).hexdigest()
            if not hmac.compare_digest(supplied, self.setup_code_sha256):
                raise AuthError(401, "SETUP_CODE_INVALID", "初始设置码无效")
            self.failures.success(failure_key)
            user_id = secrets.token_bytes(32)
        registration_options, registration_challenge = self._registration_options(user_id)
        flow_token = secrets.token_urlsafe(32)
        self._remember_ceremony(
            flow_token,
            Ceremony(
                "register",
                registration_challenge,
                user_id,
                min(time.time() + CEREMONY_LIFETIME_SECONDS, float(self.setup_code_expires_at))
                if not initialized
                else time.time() + CEREMONY_LIFETIME_SECONDS,
                caller_key,
                not initialized,
                recovery_registration,
                _token_hash(session_token) if recovery_registration and session_token else None,
                None,
            ),
            previous_flow_token=previous_flow_token,
        )
        return registration_options, flow_token

    def finish_registration(
        self,
        flow_token: str,
        credential: dict[str, Any],
        *,
        setup_code: str,
        session_token: str | None,
    ) -> tuple[AuthSession, list[str]]:
        ceremony = self._take_ceremony(flow_token, "register")
        if ceremony.initial_registration:
            supplied = hashlib.sha256(_normalise_code(setup_code).encode("utf-8")).hexdigest()
            if int(time.time()) >= self.setup_code_expires_at or not hmac.compare_digest(
                supplied, self.setup_code_sha256
            ):
                raise AuthError(401, "SETUP_CODE_EXPIRED", "初始设置码已失效，请重新开始")
        if ceremony.recovery_registration:
            if (
                not session_token
                or ceremony.authorization_session_hash is None
                or not hmac.compare_digest(_token_hash(session_token), ceremony.authorization_session_hash)
                or self.store.session_method(session_token) != "recovery-code"
            ):
                raise AuthError(401, "AUTH_CEREMONY_REVOKED", "授权此次登记的恢复会话已失效")
        _reject_cross_origin_client_data(credential)
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.expected_origin,
                require_user_verification=True,
            )
        except Exception as error:
            raise AuthError(401, "PASSKEY_VERIFICATION_FAILED", "无法验证通行密钥登记结果") from error
        if ceremony.recovery_registration:
            recovery_codes, session = self.store.replace_credentials_after_recovery(
                user_id=ceremony.user_id,
                credential_id=verified.credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count,
                device_type=verified.credential_device_type.value,
                backed_up=verified.credential_backed_up,
                authorization_session_token=session_token or "",
            )
        else:
            if int(time.time()) >= self.setup_code_expires_at:
                raise AuthError(401, "SETUP_CODE_EXPIRED", "初始设置码已失效，请重新开始")
            recovery_codes = self.store.register_credential(
                user_id=ceremony.user_id,
                credential_id=verified.credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count,
                device_type=verified.credential_device_type.value,
                backed_up=verified.credential_backed_up,
                initial=ceremony.initial_registration,
            )
            session = self.store.create_session("passkey-registration")
        return session, recovery_codes

    def start_login(
        self,
        *,
        caller_key: str,
        previous_flow_token: str | None = None,
    ) -> tuple[dict[str, object], str]:
        auth_epoch = self.store.login_epoch()
        credentials = self.store.credentials()
        if not credentials:
            raise AuthError(409, "PASSKEY_NOT_CONFIGURED", "尚未登记通行密钥")
        options = generate_authentication_options(
            rp_id=self.rp_id,
            allow_credentials=[PublicKeyCredentialDescriptor(id=item.credential_id) for item in credentials],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        flow_token = secrets.token_urlsafe(32)
        user_id = self.store.user_id() or b""
        self._remember_ceremony(
            flow_token,
            Ceremony(
                "login",
                options.challenge,
                user_id,
                time.time() + CEREMONY_LIFETIME_SECONDS,
                caller_key,
                auth_epoch=auth_epoch,
            ),
            previous_flow_token=previous_flow_token,
        )
        return json.loads(options_to_json(options)), flow_token

    def _verify_authentication_credential(
        self,
        ceremony: Ceremony,
        credential: dict[str, Any],
    ) -> tuple[StoredCredential, Any]:
        raw_id = credential.get("rawId") or credential.get("id")
        if not isinstance(raw_id, str) or len(raw_id) > 2048:
            raise AuthError(400, "PASSKEY_RESPONSE_INVALID", "通行密钥响应缺少凭据标识")
        try:
            credential_id = base64url_to_bytes(raw_id)
        except Exception as error:
            raise AuthError(400, "PASSKEY_RESPONSE_INVALID", "通行密钥凭据标识无效") from error
        stored = self.store.credential(credential_id)
        if stored is None:
            raise AuthError(401, "PASSKEY_NOT_RECOGNIZED", "无法验证通行密钥")
        response = credential.get("response")
        if isinstance(response, dict) and response.get("userHandle") is not None:
            user_handle = response.get("userHandle")
            try:
                decoded_handle = base64url_to_bytes(user_handle) if isinstance(user_handle, str) else b""
            except Exception as error:
                raise AuthError(400, "PASSKEY_RESPONSE_INVALID", "通行密钥用户标识无效") from error
            if not hmac.compare_digest(decoded_handle, ceremony.user_id):
                raise AuthError(401, "PASSKEY_USER_MISMATCH", "通行密钥用户标识不匹配")
        _reject_cross_origin_client_data(credential)
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.expected_origin,
                credential_public_key=stored.public_key,
                credential_current_sign_count=stored.sign_count,
                require_user_verification=True,
            )
        except Exception as error:
            raise AuthError(401, "PASSKEY_VERIFICATION_FAILED", "无法验证通行密钥") from error
        return stored, verified

    def finish_login(self, flow_token: str, credential: dict[str, Any]) -> AuthSession:
        ceremony = self._take_ceremony(flow_token, "login")
        stored, verified = self._verify_authentication_credential(ceremony, credential)
        if ceremony.auth_epoch is None:
            raise AuthError(400, "AUTH_CEREMONY_EXPIRED", "认证请求已过期，请重新开始")
        return self.store.complete_login(
            verified.credential_id,
            expected_sign_count=stored.sign_count,
            new_sign_count=verified.new_sign_count,
            device_type=verified.credential_device_type.value,
            backed_up=verified.credential_backed_up,
            expected_auth_epoch=ceremony.auth_epoch,
        )

    def start_passkey_addition_authorization(
        self,
        session_token: str | None,
        *,
        caller_key: str,
        previous_flow_token: str | None = None,
    ) -> tuple[dict[str, object], str]:
        authorization_session_token = self._require_full_session(session_token)
        auth_epoch = self.store.login_epoch()
        credentials = self.store.credentials()
        if not credentials:
            raise AuthError(409, "PASSKEY_NOT_CONFIGURED", "尚未登记通行密钥")
        if len(credentials) >= MAX_PASSKEY_CREDENTIALS:
            raise AuthError(409, "PASSKEY_LIMIT_REACHED", "已达到通行密钥数量上限")
        options = generate_authentication_options(
            rp_id=self.rp_id,
            allow_credentials=[PublicKeyCredentialDescriptor(id=item.credential_id) for item in credentials],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        flow_token = secrets.token_urlsafe(32)
        user_id = self.store.user_id() or b""
        self._remember_ceremony(
            flow_token,
            Ceremony(
                "add-authorize",
                options.challenge,
                user_id,
                time.time() + CEREMONY_LIFETIME_SECONDS,
                caller_key,
                authorization_session_hash=_token_hash(authorization_session_token),
                auth_epoch=auth_epoch,
            ),
            previous_flow_token=previous_flow_token,
        )
        return json.loads(options_to_json(options)), flow_token

    def finish_passkey_addition_authorization(
        self,
        flow_token: str,
        credential: dict[str, Any],
        *,
        session_token: str | None,
    ) -> tuple[dict[str, object], str]:
        authorization_session_token = self._require_full_session(session_token)
        ceremony = self._take_ceremony(flow_token, "add-authorize")
        if (
            ceremony.authorization_session_hash is None
            or not hmac.compare_digest(
                _token_hash(authorization_session_token), ceremony.authorization_session_hash
            )
            or ceremony.auth_epoch is None
        ):
            raise AuthError(401, "AUTH_CEREMONY_REVOKED", "授权此次登记的登录会话已失效")
        stored, verified = self._verify_authentication_credential(ceremony, credential)
        self.store.complete_step_up(
            verified.credential_id,
            expected_sign_count=stored.sign_count,
            new_sign_count=verified.new_sign_count,
            device_type=verified.credential_device_type.value,
            backed_up=verified.credential_backed_up,
            expected_auth_epoch=ceremony.auth_epoch,
            authorization_session_token=authorization_session_token,
        )
        registration_options, registration_challenge = self._registration_options(ceremony.user_id)
        registration_flow_token = secrets.token_urlsafe(32)
        self._remember_ceremony(
            registration_flow_token,
            Ceremony(
                "add-register",
                registration_challenge,
                ceremony.user_id,
                time.time() + CEREMONY_LIFETIME_SECONDS,
                ceremony.caller_key,
                authorization_session_hash=ceremony.authorization_session_hash,
                auth_epoch=ceremony.auth_epoch,
            ),
        )
        return registration_options, registration_flow_token

    def finish_passkey_addition(
        self,
        flow_token: str,
        credential: dict[str, Any],
        *,
        session_token: str | None,
    ) -> int:
        authorization_session_token = self._require_full_session(session_token)
        ceremony = self._take_ceremony(flow_token, "add-register")
        if (
            ceremony.authorization_session_hash is None
            or not hmac.compare_digest(
                _token_hash(authorization_session_token), ceremony.authorization_session_hash
            )
            or ceremony.auth_epoch is None
        ):
            raise AuthError(401, "AUTH_CEREMONY_REVOKED", "授权此次登记的登录会话已失效")
        _reject_cross_origin_client_data(credential)
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.expected_origin,
                require_user_verification=True,
            )
        except Exception as error:
            raise AuthError(401, "PASSKEY_VERIFICATION_FAILED", "无法验证通行密钥登记结果") from error
        return self.store.add_credential_after_step_up(
            user_id=ceremony.user_id,
            credential_id=verified.credential_id,
            public_key=verified.credential_public_key,
            sign_count=verified.sign_count,
            device_type=verified.credential_device_type.value,
            backed_up=verified.credential_backed_up,
            authorization_session_token=authorization_session_token,
            expected_auth_epoch=ceremony.auth_epoch,
        )

    def recover(self, recovery_code: str, *, caller_key: str) -> AuthSession:
        failure_key = f"recovery:{caller_key}"
        self.failures.begin_attempt(failure_key)
        session = (
            self.store.recover_with_code(recovery_code)
            if len(_normalise_code(recovery_code)) == RECOVERY_CODE_LENGTH
            else None
        )
        if session is None:
            raise AuthError(401, "RECOVERY_CODE_INVALID", "恢复码无效或已经使用")
        self.failures.success(failure_key)
        return session

    def _remember_ceremony(
        self,
        flow_token: str,
        ceremony: Ceremony,
        *,
        previous_flow_token: str | None = None,
    ) -> None:
        now = time.time()
        with self._ceremony_lock:
            self._ceremonies = {key: value for key, value in self._ceremonies.items() if value.expires_at > now}
            if previous_flow_token:
                previous_key = _token_hash(previous_flow_token)
                previous = self._ceremonies.get(previous_key)
                if previous is not None and previous.caller_key == ceremony.caller_key and previous.kind == ceremony.kind:
                    self._ceremonies.pop(previous_key, None)
            caller_count = sum(
                value.caller_key == ceremony.caller_key and value.kind == ceremony.kind
                for value in self._ceremonies.values()
            )
            if caller_count >= MAX_ACTIVE_CEREMONIES_PER_CALLER:
                raise AuthError(429, "AUTH_RATE_LIMITED", "认证尝试过多，请稍后重试")
            public_ceremonies = [
                (key, value) for key, value in self._ceremonies.items() if value.kind == "login"
            ]
            if ceremony.kind == "login" and len(public_ceremonies) >= MAX_ACTIVE_PUBLIC_CEREMONIES:
                caller_counts: dict[str, int] = {}
                for _, value in public_ceremonies:
                    caller_counts[value.caller_key] = caller_counts.get(value.caller_key, 0) + 1
                busiest_count = max(caller_counts.values())
                eviction_key, _ = min(
                    ((key, value) for key, value in public_ceremonies if caller_counts[value.caller_key] == busiest_count),
                    key=lambda item: item[1].expires_at,
                )
                self._ceremonies.pop(eviction_key, None)
            if len(self._ceremonies) >= MAX_ACTIVE_CEREMONIES:
                public_ceremonies = [
                    (key, value) for key, value in self._ceremonies.items() if value.kind == "login"
                ]
                if public_ceremonies:
                    caller_counts: dict[str, int] = {}
                    for _, value in public_ceremonies:
                        caller_counts[value.caller_key] = caller_counts.get(value.caller_key, 0) + 1
                    busiest_count = max(caller_counts.values())
                    eviction_key, _ = min(
                        ((key, value) for key, value in public_ceremonies if caller_counts[value.caller_key] == busiest_count),
                        key=lambda item: item[1].expires_at,
                    )
                    self._ceremonies.pop(eviction_key, None)
                elif ceremony.kind == "register":
                    eviction_key = min(self._ceremonies, key=lambda key: self._ceremonies[key].expires_at)
                    self._ceremonies.pop(eviction_key, None)
            if len(self._ceremonies) >= MAX_ACTIVE_CEREMONIES:
                raise AuthError(429, "AUTH_CEREMONY_CAPACITY", "认证请求过多，请稍后重试")
            self._ceremonies[_token_hash(flow_token)] = ceremony

    def _take_ceremony(self, flow_token: str, kind: str) -> Ceremony:
        with self._ceremony_lock:
            ceremony = self._ceremonies.pop(_token_hash(flow_token), None)
        if ceremony is None or ceremony.kind != kind or ceremony.expires_at <= time.time():
            raise AuthError(400, "AUTH_CEREMONY_EXPIRED", "认证请求已过期，请重新开始")
        return ceremony


def _generate_recovery_codes() -> list[str]:
    codes: list[str] = []
    while len(codes) < RECOVERY_CODE_COUNT:
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
        formatted = "-".join(raw[index : index + 4] for index in range(0, RECOVERY_CODE_LENGTH, 4))
        if formatted not in codes:
            codes.append(formatted)
    return codes


def _reject_cross_origin_client_data(credential: dict[str, Any]) -> None:
    response = credential.get("response")
    encoded = response.get("clientDataJSON") if isinstance(response, dict) else None
    if not isinstance(encoded, str):
        return
    try:
        client_data = json.loads(base64url_to_bytes(encoded))
    except Exception:
        return
    if isinstance(client_data, dict) and (
        client_data.get("crossOrigin") is True or client_data.get("topOrigin") is not None
    ):
        raise AuthError(403, "CROSS_ORIGIN_WEBAUTHN_REJECTED", "拒绝跨来源通行密钥响应")
