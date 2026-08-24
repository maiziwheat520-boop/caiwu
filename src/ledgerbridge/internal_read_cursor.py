"""Signed, scope-bound keyset cursors for the database Core reader."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import zlib
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from ledgerbridge.internal_read_contract import READ_CONTRACT_VERSION, WorkloadPrincipal

_VERSION = 1
_MAX_CURSOR_BYTES = 512
_MAX_BODY_BYTES = 2048


class CursorInvalid(ValueError):
    """A cursor is malformed, stale, or not bound to the current read request."""


class ReadCursorSigner:
    """HMAC signer with canonical JSON and strict length/claim validation."""

    def __init__(self, key: str) -> None:
        if not isinstance(key, str) or not 32 <= len(key.encode("utf-8")) <= 256:
            raise ValueError("internal read cursor key must be 32 to 256 UTF-8 bytes")
        self._key = key.encode("utf-8")

    def issue(
        self,
        principal: WorkloadPrincipal,
        *,
        month: str | None,
        status: str | None,
        business_unit: str | None,
        horizon_sequence: int,
        horizon_hash: bytes,
        last_created_at: datetime,
        last_candidate_id: UUID,
    ) -> str:
        if horizon_sequence <= 0 or len(horizon_hash) != 32:
            raise CursorInvalid("cursor horizon is malformed")
        if last_created_at.tzinfo is None:
            raise CursorInvalid("cursor timestamp must be timezone-aware")
        payload: dict[str, Any] = {
            "v": _VERSION,
            "contract": READ_CONTRACT_VERSION,
            "principal_digest": _digest_text(principal.principal_ref),
            "policy_generation": principal.policy_generation,
            "grant_digest": grant_digest(principal),
            "filters": [month, status, business_unit],
            "horizon": [horizon_sequence, horizon_hash.hex()],
            "last": [last_created_at.isoformat(), str(last_candidate_id)],
        }
        body = zlib.compress(_canonical_json(payload), level=9)
        token = f"{_encode(body)}.{_encode(self._mac(body))}"
        if len(token) > _MAX_CURSOR_BYTES:
            raise CursorInvalid("cursor exceeds the maximum length")
        return token

    def verify(
        self,
        token: str,
        principal: WorkloadPrincipal,
        *,
        month: str | None,
        status: str | None,
        business_unit: str | None,
    ) -> dict[str, Any]:
        if not isinstance(token, str) or not 1 <= len(token) <= _MAX_CURSOR_BYTES:
            raise CursorInvalid("cursor length is invalid")
        try:
            encoded_body, encoded_mac = token.split(".", 1)
            signed_body = _decode(encoded_body)
            supplied_mac = _decode(encoded_mac)
        except (ValueError, TypeError) as exc:
            raise CursorInvalid("cursor encoding is invalid") from exc
        if not hmac.compare_digest(supplied_mac, self._mac(signed_body)):
            raise CursorInvalid("cursor signature is invalid")
        try:
            body = zlib.decompress(signed_body)
            if len(body) > _MAX_BODY_BYTES:
                raise ValueError
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise TypeError
            self._validate_claims(
                payload,
                principal,
                month=month,
                status=status,
                business_unit=business_unit,
            )
            horizon = payload["horizon"]
            last = payload["last"]
            if not isinstance(horizon, list) or not isinstance(last, list):
                raise TypeError
            created_at = datetime.fromisoformat(last[0])
            candidate_ref = UUID(last[1])
            if created_at.tzinfo is None:
                raise ValueError
            if not isinstance(horizon[0], int) or not isinstance(horizon[1], str):
                raise TypeError
            horizon_hash = bytes.fromhex(horizon[1])
            if len(horizon_hash) != 32:
                raise ValueError
            return {
                "horizon_sequence": horizon[0],
                "horizon_hash": horizon_hash,
                "last_created_at": created_at,
                "last_candidate_id": candidate_ref,
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CursorInvalid("cursor claims are invalid") from exc

    def _validate_claims(
        self,
        payload: dict[str, Any],
        principal: WorkloadPrincipal,
        *,
        month: str | None,
        status: str | None,
        business_unit: str | None,
    ) -> None:
        if payload.get("v") != _VERSION or payload.get("contract") != READ_CONTRACT_VERSION:
            raise CursorInvalid("cursor contract version is invalid")
        if payload.get("principal_digest") != _digest_text(principal.principal_ref):
            raise CursorInvalid("cursor principal binding is stale")
        if payload.get("policy_generation") != principal.policy_generation:
            raise CursorInvalid("cursor policy generation is stale")
        if payload.get("grant_digest") != grant_digest(principal):
            raise CursorInvalid("cursor grant binding is stale")
        filters = payload.get("filters")
        if filters != [month, status, business_unit]:
            raise CursorInvalid("cursor filters do not match")

    def _mac(self, body: bytes) -> bytes:
        return hmac.new(self._key, body, hashlib.sha256).digest()


def grant_digest(principal: WorkloadPrincipal) -> str:
    grants = [
        {
            "entity_ref": str(grant.entity_ref),
            "business_unit_refs": sorted(grant.business_unit_refs),
            "business_unit_ids": sorted(str(value) for value in grant.business_unit_ids),
            "allow_unassigned_candidates": grant.allow_unassigned_candidates,
        }
        for grant in principal.grants
    ]
    grants.sort(key=lambda value: cast(str, value["entity_ref"]))
    return hashlib.sha256(_canonical_json(grants)).hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value:
        raise ValueError("empty base64 value")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
