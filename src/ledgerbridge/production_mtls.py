"""Production mTLS identity handoff for the dedicated internal-read ingress.

The TLS proxy is the only process allowed to connect to the Core Unix socket.
It removes client-supplied identity headers, verifies the client certificate,
and writes the three fixed headers consumed here.  Direct TCP requests and any
ambiguous header shape fail closed.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from ledgerbridge.config import Settings
from ledgerbridge.internal_read_auth import VerifiedMtlsPrincipal
from ledgerbridge.internal_read_contract import WorkloadPrincipal

MAX_MTLS_POLICY_BYTES: Final = 64 * 1024
MTLS_ASSERTION_LIFETIME: Final = timedelta(minutes=5)
_VERIFIED_HEADER: Final = b"x-ledgerbridge-mtls-verified"
_SAN_HEADER: Final = b"x-ledgerbridge-client-san"
_SERIAL_HEADER: Final = b"x-ledgerbridge-client-serial"
_FORBIDDEN_IDENTITY_HEADERS: Final = frozenset(
    {
        b"x-forwarded-client-cert",
        b"x-client-cert",
        b"x-ssl-client-cert",
        b"x-principal",
    }
)


class MtlsPolicyError(RuntimeError):
    """The pinned workload policy cannot be trusted or parsed."""


class MtlsWorkloadPolicy(BaseModel):
    """One exact client-certificate identity mapped to one Core principal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["ledgerbridge.mtls-workload-policy.v1"] = (
        "ledgerbridge.mtls-workload-policy.v1"
    )
    certificate_serial: str = Field(pattern=r"^[0-9A-F]{2,40}$")
    policy_generation: int = Field(ge=1)
    principal: WorkloadPrincipal

    @model_validator(mode="after")
    def generation_is_bound_to_principal(self) -> MtlsWorkloadPolicy:
        if self.policy_generation != self.principal.policy_generation:
            raise ValueError("mTLS policy generation does not match principal generation")
        return self


class MtlsWorkloadIdentity(BaseModel):
    """One exact certificate serial and proxy-attested SAN principal binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    certificate_serial: str = Field(pattern=r"^[0-9A-F]{2,40}$")
    principal: WorkloadPrincipal


class MtlsWorkloadPolicyV2(BaseModel):
    """A bounded set of independently authorized mTLS workload identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["ledgerbridge.mtls-workload-policy.v2"] = (
        "ledgerbridge.mtls-workload-policy.v2"
    )
    policy_generation: int = Field(ge=1)
    identities: tuple[MtlsWorkloadIdentity, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def identities_are_unique_and_generation_bound(self) -> MtlsWorkloadPolicyV2:
        serials = [identity.certificate_serial for identity in self.identities]
        principal_refs = [identity.principal.principal_ref for identity in self.identities]
        san_uris = [identity.principal.san_uri for identity in self.identities]
        if len(serials) != len(set(serials)):
            raise ValueError("mTLS policy certificate serials must be unique")
        if len(principal_refs) != len(set(principal_refs)):
            raise ValueError("mTLS policy principal refs must be unique")
        if len(san_uris) != len(set(san_uris)):
            raise ValueError("mTLS policy SAN URIs must be unique")
        if any(
            identity.principal.policy_generation != self.policy_generation
            for identity in self.identities
        ):
            raise ValueError("mTLS policy generation does not match principal generation")
        return self


type LoadedMtlsWorkloadPolicy = MtlsWorkloadPolicy | MtlsWorkloadPolicyV2
_MTLS_POLICY_ADAPTER: TypeAdapter[LoadedMtlsWorkloadPolicy] = TypeAdapter(
    Annotated[LoadedMtlsWorkloadPolicy, Field(discriminator="version")]
)


class UnixSocketMtlsVerifier:
    """Accept the proxy assertion only over the protected Unix-socket transport."""

    def __init__(
        self,
        policy: LoadedMtlsWorkloadPolicy,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))

    def __call__(self, scope: Mapping[str, object]) -> VerifiedMtlsPrincipal | None:
        if scope.get("type") != "http" or scope.get("client") is not None:
            return None
        raw_headers = scope.get("headers")
        if not isinstance(raw_headers, (list, tuple)):
            return None

        values: dict[bytes, list[bytes]] = {}
        for item in raw_headers:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], bytes)
                or not isinstance(item[1], bytes)
            ):
                return None
            name, value = item
            if name != name.lower() or name in _FORBIDDEN_IDENTITY_HEADERS:
                return None
            values.setdefault(name, []).append(value)

        verified = _single(values, _VERIFIED_HEADER)
        san = _single(values, _SAN_HEADER)
        serial = _single(values, _SERIAL_HEADER)
        if verified != b"SUCCESS":
            return None
        identity = _resolve_identity(self._policy, san=san, serial=serial)
        if identity is None:
            return None

        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            return None
        issued_at = now.astimezone(UTC)
        return VerifiedMtlsPrincipal(
            principal=identity.principal,
            issued_at=issued_at,
            expires_at=issued_at + MTLS_ASSERTION_LIFETIME,
            policy_generation=self._policy.policy_generation,
        )


def verify_configured_mtls_principal(
    scope: Mapping[str, object],
    settings: Settings,
) -> VerifiedMtlsPrincipal | None:
    """Resolve the current policy on every request so replacement is fail-closed."""

    if (
        not settings.enable_internal_read_api
        or settings.internal_read_transport != "unix-mtls-proxy"
        or settings.internal_read_mtls_policy_path is None
        or settings.internal_read_policy_generation is None
    ):
        return None
    try:
        policy = load_mtls_workload_policy(
            settings.internal_read_mtls_policy_path,
            expected_policy_generation=settings.internal_read_policy_generation,
            require_root_owner=settings.env == "production",
        )
    except MtlsPolicyError:
        return None
    return UnixSocketMtlsVerifier(policy)(scope)


def load_mtls_workload_policy(
    path: Path,
    *,
    expected_policy_generation: int,
    require_root_owner: bool = True,
) -> LoadedMtlsWorkloadPolicy:
    """Read one small, stable, non-symlink policy file without following links."""

    if not path.is_absolute():
        raise MtlsPolicyError("mTLS policy path must be absolute")
    try:
        before = path.lstat()
    except OSError as exc:
        raise MtlsPolicyError("mTLS policy file is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise MtlsPolicyError("mTLS policy must be a regular file, not a symlink")
    if before.st_size < 2 or before.st_size > MAX_MTLS_POLICY_BYTES:
        raise MtlsPolicyError("mTLS policy file size is invalid")
    if os.name == "posix" and before.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise MtlsPolicyError("mTLS policy file is writable by group or others")
    if require_root_owner and os.name == "posix" and before.st_uid != 0:
        raise MtlsPolicyError("mTLS policy file must be owned by root")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if _identity(opened) != _identity(before):
                raise MtlsPolicyError("mTLS policy changed while opening")
            content = os.read(descriptor, MAX_MTLS_POLICY_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    except MtlsPolicyError:
        raise
    except OSError as exc:
        raise MtlsPolicyError("mTLS policy file cannot be read safely") from exc
    if _identity(after) != _identity(before) or len(content) != before.st_size:
        raise MtlsPolicyError("mTLS policy changed while reading")

    try:
        payload = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
        policy = _MTLS_POLICY_ADAPTER.validate_python(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise MtlsPolicyError("mTLS policy content is invalid") from exc
    if policy.policy_generation != expected_policy_generation:
        raise MtlsPolicyError("mTLS policy generation is stale")
    return policy


def _resolve_identity(
    policy: LoadedMtlsWorkloadPolicy,
    *,
    san: bytes | None,
    serial: bytes | None,
) -> MtlsWorkloadIdentity | None:
    identities = (
        (
            MtlsWorkloadIdentity(
                certificate_serial=policy.certificate_serial,
                principal=policy.principal,
            ),
        )
        if isinstance(policy, MtlsWorkloadPolicy)
        else policy.identities
    )
    matches = [
        identity
        for identity in identities
        if san == identity.principal.san_uri.encode("ascii")
        and serial == identity.certificate_serial.encode("ascii")
    ]
    return matches[0] if len(matches) == 1 else None


def _single(values: Mapping[bytes, list[bytes]], name: bytes) -> bytes | None:
    matches = values.get(name, [])
    return matches[0] if len(matches) == 1 else None


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("mTLS policy contains a duplicate JSON key")
        result[key] = value
    return result
