"""Start LedgerBridge on one machine, for one person.

Four steps, each separately runnable so that a failure says which one failed:

    python scripts/local_mode.py init      # write .env.local (not committed)
    docker compose --env-file .env.local -f docker-compose.local.yml up -d
    python scripts/local_mode.py migrate   # alembic upgrade head
    python scripts/local_mode.py serve     # loopback API

Only PostgreSQL runs in Docker. The Python side runs on the host under uv,
because the only thing the container buys locally is the database, and Core's
invariants are the database's.

`init` generates the local database passwords itself and writes them to
`.env.local`, which `.gitignore` already excludes. They are never printed, never
passed as command arguments, and never read back into a message: a password that
is echoed once is a password that lives in a scrollback buffer forever.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from ledgerbridge.local_mode import (  # noqa: E402
    LOCAL_HOST,
    LOCAL_PORT,
    LocalModeRefused,
    LocalProfile,
    local_principal,
    local_settings,
)

ENV_FILE = REPO_ROOT / ".env.local"
LOCAL_STATE = Path.home() / ".ledgerbridge-local"
DATABASE = "ledgerbridge"
HOST_PORT = 5433

#: Written to .env.local by `init`. Two of these five matter to the launcher:
#: the owner role runs migrations, and the reader role is what the served API
#: reads through. The reader can execute the internal_read definer functions and
#: can read no base table at all, which is the boundary the whole read design
#: rests on; local mode keeps it rather than logging in as something roomier.
_ROLES = ("owner", "app", "api", "worker", "reader")


def _database_url(role: str, password: str) -> str:
    return (
        f"postgresql+psycopg://ledgerbridge_{role}:{password}@{LOCAL_HOST}:{HOST_PORT}/{DATABASE}"
    )


def command_init(_args: argparse.Namespace) -> int:
    """Write .env.local with freshly generated passwords, if it is absent.

    Refusing to overwrite is the point: regenerating passwords against a volume
    that already has the roles in it produces a database nobody can log into,
    and the failure would arrive one step later, looking like a connection bug.
    """
    if ENV_FILE.exists():
        print(f"{ENV_FILE.name} already exists; leaving it alone")
        return 0
    passwords = {role: secrets.token_urlsafe(24) for role in _ROLES}
    lines = [
        "# Local single-user mode. Generated; not committed; not shared.",
        f"POSTGRES_DB={DATABASE}",
        "POSTGRES_USER=ledgerbridge_owner",
        f"POSTGRES_PASSWORD={passwords['owner']}",
    ]
    lines += [f"LEDGERBRIDGE_{role.upper()}_DB_PASSWORD={passwords[role]}" for role in _ROLES[1:]]
    lines += [
        f"LEDGERBRIDGE_MIGRATION_DATABASE_URL={_database_url('owner', passwords['owner'])}",
        f"LEDGERBRIDGE_LOCAL_READER_DATABASE_URL={_database_url('reader', passwords['reader'])}",
        "",
    ]
    ENV_FILE.write_text("\n".join(lines), encoding="utf-8")
    # Windows ignores POSIX modes; the file is still inside the repo, which
    # .gitignore excludes. Say nothing rather than claim a permission was set.
    with suppress(OSError):
        ENV_FILE.chmod(0o600)
    print(f"wrote {ENV_FILE.name}")
    return 0


def _env_value(name: str) -> str:
    """Read one value out of .env.local without importing a dotenv library."""
    if not ENV_FILE.exists():
        raise LocalModeRefused(f"{ENV_FILE.name} is missing; run `local_mode.py init` first")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            return value.strip()
    raise LocalModeRefused(f"{name} is not set in {ENV_FILE.name}")


def command_migrate(_args: argparse.Namespace) -> int:
    """Run alembic against the local database as the owner role."""
    environment = dict(os.environ)
    environment["LEDGERBRIDGE_RUNTIME_ROLE"] = "migrate"
    environment["LEDGERBRIDGE_ENV"] = "development"
    environment["LEDGERBRIDGE_DATABASE_URL"] = _env_value("LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    environment["LEDGERBRIDGE_ARTIFACT_ROOT"] = str(LOCAL_STATE / "artifacts")
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
    )
    return completed.returncode


def _entity_refs(database_url: str) -> tuple[UUID, ...]:
    """Every entity in the local database, in a stable order.

    This is a bootstrap question, not a read-path query, and it deliberately
    uses a different connection from the one that serves requests. The reader
    role cannot select from `entity` at all - it reaches facts only through the
    internal_read definer functions - and that restriction is the point of the
    read design, so local mode does not widen it to answer "which books live on
    this machine". It asks the owner role once, before the first request, and
    then serves everything through the reader.

    Reading the list here rather than taking it as an argument means a freshly
    imported company appears without anyone remembering to widen a list.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError

    from ledgerbridge.db import get_session_factory
    from ledgerbridge.models.ledger import Entity

    # A short connect timeout, because the default waits about two minutes and
    # the answer to "is the database up" should not take two minutes. The URL
    # carries a password, so failures name only the host and port.
    probe_url = f"{database_url}{'&' if '?' in database_url else '?'}connect_timeout=3"
    try:
        with get_session_factory(probe_url)() as session:
            return tuple(session.scalars(select(Entity.id).order_by(Entity.name)).all())
    except OperationalError as failure:
        raise LocalModeRefused(
            f"the local database at {LOCAL_HOST}:{HOST_PORT} did not answer; "
            "start it with `docker compose --env-file .env.local "
            "-f docker-compose.local.yml up -d`"
        ) from failure


def _build_app(profile: LocalProfile) -> FastAPI:
    from ledgerbridge.config import get_settings
    from ledgerbridge.internal_read_auth import VerifiedInternalReadPrincipalMiddleware
    from ledgerbridge.internal_read_routes import InternalReadNoStoreMiddleware, router
    from ledgerbridge.local_mode import local_verifier

    settings = local_settings(profile)
    principal = local_principal(profile.entity_refs)
    app = FastAPI(title="LedgerBridge Local", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(InternalReadNoStoreMiddleware)
    app.add_middleware(VerifiedInternalReadPrincipalMiddleware, verifier=local_verifier(principal))
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def _profile(host: str, port: int) -> LocalProfile:
    reader_url = _env_value("LEDGERBRIDGE_LOCAL_READER_DATABASE_URL")
    artifact_root = LOCAL_STATE / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    return LocalProfile(
        database_url=reader_url,
        artifact_root=artifact_root,
        entity_refs=_entity_refs(_env_value("LEDGERBRIDGE_MIGRATION_DATABASE_URL")),
        host=host,
        port=port,
    )


def command_check(args: argparse.Namespace) -> int:
    """Prove the profile assembles and the database answers, then exit.

    This is the step that distinguishes "the launcher is wrong" from "the
    database is not up", which otherwise both surface as a page that will not
    load.
    """
    profile = _profile(args.host, args.port)
    settings = local_settings(profile)
    print(
        json.dumps(
            {
                "env": settings.env,
                "backend": settings.internal_read_backend,
                "transport": settings.internal_read_transport,
                "gate": settings.internal_read_operational_gate,
                "bind": f"{profile.host}:{profile.port}",
                "entities": len(profile.entity_refs),
            },
            sort_keys=True,
        )
    )
    return 0


def command_serve(args: argparse.Namespace) -> int:
    import uvicorn

    profile = _profile(args.host, args.port)
    uvicorn.run(_build_app(profile), host=profile.host, port=profile.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="write .env.local if absent").set_defaults(func=command_init)
    subparsers.add_parser("migrate", help="alembic upgrade head").set_defaults(func=command_migrate)
    for name, function, help_text in (
        ("check", command_check, "assemble the profile and count entities"),
        ("serve", command_serve, "serve the internal read API on loopback"),
    ):
        subparser = subparsers.add_parser(name, help=help_text)
        subparser.add_argument("--host", default=LOCAL_HOST)
        subparser.add_argument("--port", type=int, default=LOCAL_PORT)
        subparser.set_defaults(func=function)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LocalModeRefused as refusal:
        print(f"local mode refused: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
