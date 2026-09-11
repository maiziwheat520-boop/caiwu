"""Run Core on one machine, for one person, without the production gates.

The deployed Core reaches its reader through an mTLS proxy, a policy
generation, a workload principal and a dedicated reader role. On a single
laptop none of that has anything to authenticate: there is one person, one
process and one loopback socket. Local mode therefore installs a fixed local
identity, the same way ``scripts/r1_synthetic_demo.py`` already does, except
pointed at a real local database instead of the packaged fixture.

What local mode is *not* is a second fact layer. It reads the same schema
through the same routes and services; the only things it changes are who is let
in and over which socket. That distinction is the whole reason this lives here
rather than in a program of its own.

Because "fewer gates" is the exact shape of a mistake that would be
catastrophic in production, the profile refuses to start on anything that looks
production-shaped. Several of those refusals duplicate a check ``Settings``
already performs; the duplication is deliberate. A second net that is never
reached costs nothing, and if the upstream validator is ever relaxed, this one
still fails loudly instead of silently downgrading a production deployment to
an unverified transport.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit
from uuid import UUID

from ledgerbridge.config import Settings
from ledgerbridge.internal_read_auth import VerifiedMtlsPrincipal
from ledgerbridge.internal_read_contract import (
    READ_CAPABILITIES,
    EntityGrant,
    WorkloadPrincipal,
)

#: Local mode binds loopback only. This is a hard condition rather than a
#: default: local mode performs no transport verification, so any other bind
#: address turns every host on the network into "the local user".
LOCAL_HOST: Final = "127.0.0.1"
LOCAL_PORT: Final = 8661

#: Database hosts a local profile may name. The compose service name resolves
#: only inside the local network, so it is admitted; everything else is
#: refused, production included.
LOCAL_DATABASE_HOSTS: Final = frozenset({"127.0.0.1", "::1", "localhost", "postgres"})

#: The contract requires a policy generation of at least one, so local mode
#: pins the lowest and leaves it there. It asserts nothing about freshness, and
#: is never compared against a production generation: the two never speak.
LOCAL_POLICY_GENERATION: Final = 1

LOCAL_PRINCIPAL_REF: Final = "workload:local-single-user"
LOCAL_SAN_URI: Final = "spiffe://ledgerbridge.local/single-user"

_LOOPBACK_BINDS: Final = frozenset({LOCAL_HOST, "::1", "localhost"})


class LocalModeRefused(RuntimeError):
    """Local mode declined to start, naming the condition that failed."""


@dataclass(frozen=True, slots=True)
class LocalProfile:
    """Everything one local start needs."""

    database_url: str
    artifact_root: Path
    entity_refs: tuple[UUID, ...]
    host: str = LOCAL_HOST
    port: int = LOCAL_PORT


def _database_host(database_url: str) -> str:
    """Return the host named by a driver-qualified database URL.

    ``postgresql+psycopg://user:pw@host:5432/db`` does not survive a plain URL
    parse: the ``+psycopg`` suffix makes some parsers read the scheme wrong and
    hand back an empty host. An empty host is not on the allow-list, so every
    valid local profile would be refused - safe, but still broken. Splitting the
    authority off by hand avoids pulling in SQLAlchemy's URL type for one field.
    """
    _, _, rest = database_url.partition("://")
    return (urlsplit(f"//{rest}").hostname or "").lower()


def guard(settings: Settings, *, host: str = LOCAL_HOST) -> None:
    """Refuse anything production-shaped, one condition at a time.

    Each check corresponds to something that must hold in production and that
    local mode deliberately does without. They refuse rather than correct: when
    a production environment is handed to the local launcher, the right response
    is to stop and say so, not to quietly rewrite ``env`` and carry on.
    """
    if settings.env == "production":
        raise LocalModeRefused("local mode does not accept env=production")
    if host not in _LOOPBACK_BINDS:
        raise LocalModeRefused(
            f"local mode binds loopback only, got {host!r}; it verifies no transport"
        )
    if settings.internal_read_transport != "disabled":
        raise LocalModeRefused(
            "local mode requires internal_read_transport=disabled, got "
            f"{settings.internal_read_transport!r}"
        )
    if settings.internal_read_operational_gate != "closed":
        raise LocalModeRefused(
            "local mode requires internal_read_operational_gate=closed, got "
            f"{settings.internal_read_operational_gate!r}"
        )
    if settings.enable_real_ingest:
        raise LocalModeRefused(
            "local mode does not enable real ingest; statements are imported explicitly"
        )
    database_url = settings.reader_database_url or settings.database_url
    if not database_url:
        raise LocalModeRefused("local mode needs a local database URL")
    database_host = _database_host(database_url)
    if database_host not in LOCAL_DATABASE_HOSTS:
        raise LocalModeRefused(
            f"local mode connects to a local database only, got host {database_host!r}; "
            "a remote database would mean reading production under an unverified identity"
        )


def local_settings(profile: LocalProfile) -> Settings:
    """Settings for one local start.

    ``internal_read_backend`` is ``database``: local mode reads the real books,
    not the packaged fixture. What differs from production is who is admitted
    and over which socket, not what a financial fact is.
    """
    settings = Settings(
        env="development",
        runtime_role="api",
        database_url=profile.database_url,
        # The api role requires an explicit api_database_url. Locally all three
        # roles are the same database, because there is no reader role to
        # separate; naming three URLs would only pretend an isolation exists.
        api_database_url=profile.database_url,
        reader_database_url=profile.database_url,
        artifact_root=profile.artifact_root,
        enable_internal_read_api=True,
        internal_read_backend="database",
        internal_read_transport="disabled",
        internal_read_operational_gate="closed",
        internal_read_policy_generation=LOCAL_POLICY_GENERATION,
        # Keyset cursors are signed, so the database backend requires a key.
        # Local mode mints one per start and never stores it: a cursor is bound
        # to one read's horizon anyway, so expiring on restart is correct, and a
        # long-lived key on disk would be one more secret to look after.
        internal_read_cursor_key=secrets.token_urlsafe(48),
    )
    guard(settings, host=profile.host)
    return settings


def local_principal(entity_refs: Iterable[UUID]) -> WorkloadPrincipal:
    """The one identity this machine has.

    Every entity in the local database is granted: the only person here owns the
    books, and a per-entity list would be one more thing to maintain that
    protects nobody. Business units are left empty with unassigned facts allowed,
    which matches the fact that most personal rows carry no business unit at all.
    """
    refs = tuple(entity_refs)
    if not refs:
        raise LocalModeRefused(
            "the local database holds no entity; import a statement before starting local mode"
        )
    return WorkloadPrincipal(
        principal_ref=LOCAL_PRINCIPAL_REF,
        san_uri=LOCAL_SAN_URI,
        policy_generation=LOCAL_POLICY_GENERATION,
        capabilities=READ_CAPABILITIES,
        grants=tuple(
            EntityGrant(
                entity_ref=ref,
                business_unit_refs=frozenset(),
                allow_unassigned_candidates=True,
            )
            for ref in refs
        ),
    )


def local_verifier(
    principal: WorkloadPrincipal,
) -> Callable[[Mapping[str, object]], VerifiedMtlsPrincipal]:
    """Install a fixed identity without reading anything the client sent.

    Reading client headers is the characteristic local-mode mistake: it would let
    anything that can reach the port name the identity it wants. This identity
    comes from the process arguments and does not vary per request.
    """

    def verify(scope: Mapping[str, object]) -> VerifiedMtlsPrincipal:
        _ = scope
        now = datetime.now(UTC)
        return VerifiedMtlsPrincipal(
            principal=principal,
            issued_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=5),
            policy_generation=LOCAL_POLICY_GENERATION,
        )

    return verify


__all__ = [
    "LOCAL_DATABASE_HOSTS",
    "LOCAL_HOST",
    "LOCAL_POLICY_GENERATION",
    "LOCAL_PORT",
    "LocalModeRefused",
    "LocalProfile",
    "guard",
    "local_principal",
    "local_settings",
    "local_verifier",
]
