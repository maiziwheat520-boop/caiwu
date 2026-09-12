"""Start LedgerBridge on one machine, for one person.

Five steps, each separately runnable so that a failure says which one failed:

    python scripts/local_mode.py init      # write .env.local (not committed)
    docker compose --env-file .env.local -f docker-compose.local.yml up -d
    python scripts/local_mode.py migrate   # alembic upgrade head
    python scripts/local_mode.py import --source-manifest <path>
    python scripts/local_mode.py serve     # loopback API

`serve` refuses on an empty database, so the import is not optional: an
identity holding no grants would render a clean empty page that looks exactly
like a working install. See docs/operations/LOCAL_IMPORT.md for what can be
imported on one machine and what cannot.

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
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import UUID

from fastapi import APIRouter, FastAPI

if TYPE_CHECKING:  # pragma: no cover - imports for annotations only
    from sqlalchemy import Engine

    from ledgerbridge.bank_statement_cutover_plan import BankStatementExistingAccountPlan

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from ledgerbridge.local_backup import (  # noqa: E402
    LocalBackupError,
    LocalBackupResult,
    LocalDatabase,
    existing_backups,
    run_local_backup,
)
from ledgerbridge.local_mode import (  # noqa: E402
    LOCAL_HOST,
    LOCAL_PORT,
    LocalBook,
    LocalModeRefused,
    LocalProfile,
    local_principal,
    local_settings,
)
from ledgerbridge.local_statements import LocalStatementError  # noqa: E402

ENV_FILE = REPO_ROOT / ".env.local"
LOCAL_STATE = Path.home() / ".ledgerbridge-local"
#: Evidence is encrypted at rest locally too, with a key this machine keeps and
#: never rotates. It lives outside the repository for the same reason .env.local
#: does: nothing under the working tree should hold a key by accident.
EVIDENCE_KEY_FILE = LOCAL_STATE / "keys" / "evidence-key.json"
#: Backups of the local database, each one a directory holding an encrypted
#: archive and the report of the restore that was rehearsed from it. The
#: statement import will not run without a fresh one.
BACKUP_ROOT = LOCAL_STATE / "backups"
#: A keyring of its own, so that backing up a laptop ledger never touches the
#: user's own GnuPG configuration.
GNUPG_HOME = LOCAL_STATE / "gnupg"
DATABASE = "ledgerbridge"
CONTAINER = "ledgerbridge-local-postgres"
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
        f"LEDGERBRIDGE_LOCAL_API_DATABASE_URL={_database_url('api', passwords['api'])}",
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


#: Every entity on this machine with the business units it owns. Grants need
#: both keys the read design keeps for a unit - the ref the HTTP layer
#: authorizes on and the UUID the scoped SQL functions query - so the bootstrap
#: reads them together rather than resolving one into the other later.
_BOOKS_SQL = """
SELECT e.id AS entity_id, u.ref AS unit_ref, u.id AS unit_id
FROM public.entity AS e
LEFT JOIN public.business_unit AS u ON u.entity_id = e.id AND u.retired_at IS NULL
ORDER BY e.name, u.ref
"""


def _books(database_url: str) -> tuple[LocalBook, ...]:
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
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from ledgerbridge.db import get_session_factory

    # A short connect timeout, because the default waits about two minutes and
    # the answer to "is the database up" should not take two minutes. The URL
    # carries a password, so failures name only the host and port.
    probe_url = f"{database_url}{'&' if '?' in database_url else '?'}connect_timeout=3"
    units: dict[UUID, list[tuple[str, UUID]]] = {}
    try:
        with get_session_factory(probe_url)() as session:
            for row in session.execute(text(_BOOKS_SQL)).mappings():
                owned = units.setdefault(row["entity_id"], [])
                if row["unit_id"] is not None:
                    owned.append((row["unit_ref"], row["unit_id"]))
    except OperationalError as failure:
        raise LocalModeRefused(
            f"the local database at {LOCAL_HOST}:{HOST_PORT} did not answer; "
            "start it with `docker compose --env-file .env.local "
            "-f docker-compose.local.yml up -d`"
        ) from failure
    return tuple(
        LocalBook(entity_ref=entity, business_units=tuple(owned)) for entity, owned in units.items()
    )


_READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


def _read_routers() -> tuple[APIRouter, ...]:
    """The routers `main.py` mounts that answer questions rather than change facts.

    Local mode is read-only over facts, so the command routers - candidate
    decisions, statement reviews, evidence unlock, payroll - are left off. That
    is also why the list is written out here instead of importing `main.app`:
    reusing the deployed app would mount every writer by default, and the next
    writer added there would arrive locally without anyone choosing it.
    """
    from ledgerbridge.cash_reconciliation_routes import router as cash_reconciliation
    from ledgerbridge.company_bank_statement_routes import router as company_bank_statement
    from ledgerbridge.company_reporting_routes import router as company_reporting
    from ledgerbridge.company_transaction_classification_routes import router as classification
    from ledgerbridge.internal_candidate_command_routes import router as candidate_command
    from ledgerbridge.internal_read_routes import router as internal_read
    from ledgerbridge.original_reconciliation_routes import router as original_reconciliation
    from ledgerbridge.personal_finance_routes import router as personal_finance

    return (
        internal_read,
        company_reporting,
        company_bank_statement,
        personal_finance,
        original_reconciliation,
        cash_reconciliation,
        # These two are command routers that also answer questions: the review
        # screen's event history and its classification groups live beside the
        # decisions that write. Dropping the whole router would cost the local
        # UI two views it only reads, so the reads are taken and the commands
        # left behind.
        _reads_of(candidate_command),
        _reads_of(classification),
    )


def _reads_of(router: APIRouter) -> APIRouter:
    """One router holding only the GET routes of a router that also commands.

    The route objects are moved across as they are, not re-declared, so each one
    keeps the dependencies and route class its own module gave it - including
    the internal-read-API gate the source router applies to everything it
    carries. Re-declaring them here would be a second, quietly diverging copy of
    somebody else's authorization.
    """
    from fastapi.routing import APIRoute

    taken = APIRouter()
    taken.routes = [
        route
        for route in router.routes
        if isinstance(route, APIRoute) and route.methods and route.methods <= _READ_METHODS
    ]
    return taken


def _refuse_non_read_routes(routers: Iterable[APIRouter]) -> None:
    """Make "local mode is read-only" true of the assembly, not just the list.

    Choosing read-only routers is a judgement that has to be re-made every time
    one is added, and a router that grows a POST later would bring it here
    silently. This turns the property into something the process asserts about
    itself before it binds a socket.

    The check reads each router's own routes rather than the assembled app's,
    because a mounted router is no longer a list of routes: FastAPI wraps it,
    and walking the app would quietly inspect nothing at all.
    """
    from fastapi.routing import APIRoute

    for router in routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            writes = (route.methods or set()) - _READ_METHODS
            if writes:
                raise LocalModeRefused(
                    f"local mode serves reads only; {route.path} accepts {sorted(writes)}"
                )


def _build_app(profile: LocalProfile) -> FastAPI:
    from ledgerbridge.config import get_settings
    from ledgerbridge.internal_read_auth import VerifiedInternalReadPrincipalMiddleware
    from ledgerbridge.internal_read_routes import InternalReadNoStoreMiddleware
    from ledgerbridge.local_mode import local_verifier

    settings = local_settings(profile)
    principal = local_principal(profile.books)
    app = FastAPI(title="LedgerBridge Local", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(InternalReadNoStoreMiddleware)
    app.add_middleware(VerifiedInternalReadPrincipalMiddleware, verifier=local_verifier(principal))
    routers = _read_routers()
    _refuse_non_read_routes(routers)
    for router in routers:
        app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def _profile(host: str, port: int) -> LocalProfile:
    reader_url = _env_value("LEDGERBRIDGE_LOCAL_READER_DATABASE_URL")
    artifact_root = LOCAL_STATE / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    return LocalProfile(
        database_url=reader_url,
        api_database_url=_env_value("LEDGERBRIDGE_LOCAL_API_DATABASE_URL"),
        artifact_root=artifact_root,
        books=_books(_env_value("LEDGERBRIDGE_MIGRATION_DATABASE_URL")),
        host=host,
        port=port,
        evidence_key_file=EVIDENCE_KEY_FILE if EVIDENCE_KEY_FILE.is_file() else None,
    )


def _revision() -> str:
    """The commit this backup was taken at, recorded in the proof.

    The cutover gate requires the backup and its rehearsal report to name the
    same 40-character revision. Production takes it from the deployed release;
    here it is the working tree's HEAD, which is the honest answer to "what
    code produced this".
    """
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    revision = completed.stdout.strip()
    if completed.returncode != 0 or len(revision) != 40:
        raise LocalModeRefused("could not read the working tree's HEAD revision")
    return revision


def _postgres_image() -> str:
    """The exact image the local database runs, digest and all."""
    completed = subprocess.run(
        ["docker", "inspect", "--format", "{{index .Config.Image}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    image = completed.stdout.strip()
    if completed.returncode != 0 or not image:
        raise LocalModeRefused(
            f"the local database container {CONTAINER} is not running; "
            "start it with `docker compose --env-file .env.local "
            "-f docker-compose.local.yml up -d`"
        )
    return image


def _local_database() -> LocalDatabase:
    return LocalDatabase(
        container=CONTAINER,
        database=DATABASE,
        owner_role=f"{DATABASE}_owner",
        image=_postgres_image(),
    )


def command_backup(_args: argparse.Namespace) -> int:
    """Back up the local database and rehearse restoring it.

    This is the same proof production requires before a statement import: an
    encrypted backup, and a restore of it into an isolated database whose
    inventory matches the live one exactly. It was once assumed unobtainable
    here because the production tooling is a Linux deployment script. The gate
    asks for a proof rather than for that script, and this machine can produce
    one: Docker, pg_dump, pg_restore and gpg are all present, and the ledger
    restores in seconds.

    The backup is also worth having on its own. A local workbench holding the
    only copy of a year of statements is an accident waiting to happen.
    """
    result = run_local_backup(
        _local_database(),
        revision=_revision(),
        artifact_root=LOCAL_STATE / "artifacts",
        backup_root=BACKUP_ROOT,
        gnupg_home=GNUPG_HOME,
    )
    inventory = result.inventory
    counts = inventory["row_counts"]
    print(
        "LOCAL_BACKUP_OK "
        f"backup={result.directory.name} "
        f"schema={inventory['schema_revision']} "
        f"statements={counts.get('bank_statement', 0)} "
        f"transactions={counts.get('bank_statement_transaction', 0)} "
        f"candidates={inventory['candidate_total']} "
        f"audit={inventory['audit_events']}"
    )
    print(f"  directory: {result.directory}")
    print(f"  rehearsal: {result.restore_report.name}")
    return 0


def command_import(args: argparse.Namespace) -> int:
    """Import one controlled-review batch into the local database.

    Two steps that production also runs separately, joined here because locally
    they are one intention: encrypt each evidence file under the machine's key,
    then write the batch inside one transaction as the owner role.

    The database URL is read out of `.env.local` rather than taken from the
    environment, so importing never involves pasting a password into a shell.
    The batch is idempotent by `batch_ref`: running it again re-reads its own
    receipt and writes nothing, which is how you check a partial run rather
    than by counting rows.

    Nothing here confirms or posts anything. Candidates arrive PENDING and stay
    there until a person decides, which is the whole point of importing them.
    """
    from sqlalchemy import create_engine

    from ledgerbridge.controlled_import import import_prepared_manifest, prepare_source_manifest
    from ledgerbridge.file_key_provider import bootstrap_file_key

    source = args.source_manifest.resolve()
    prepared_path = (
        args.prepared_manifest.resolve()
        if args.prepared_manifest is not None
        else source.with_suffix(".prepared.json")
    )
    artifact_root = LOCAL_STATE / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    if not EVIDENCE_KEY_FILE.exists():
        EVIDENCE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_file_key(EVIDENCE_KEY_FILE.resolve(), generation="local-single-user-1")
    prepared = prepare_source_manifest(
        source,
        key_file=EVIDENCE_KEY_FILE.resolve(),
        artifact_root=artifact_root,
        prepared_manifest_path=prepared_path,
    )
    engine = create_engine(_env_value("LEDGERBRIDGE_MIGRATION_DATABASE_URL"), pool_pre_ping=True)
    try:
        # Auto-confirmation left at its disabled default: a local import is
        # still an import, and nothing may reach POSTED without a person.
        result = import_prepared_manifest(engine, prepared_path)
    finally:
        engine.dispose()
    print(
        f"LOCAL_IMPORT_OK replayed={str(result.replayed).lower()} "
        f"evidence={result.evidence_count} candidates={result.candidate_count} "
        f"batch={prepared.batch_ref} horizon={result.audit_horizon_sequence}"
    )
    return 0


def command_candidates(args: argparse.Namespace) -> int:
    """Import a payment platform's exports into one book as review candidates.

    The difference from `statements` is what the document is. A bank statement
    is an account's own record and carries the balance that proves it is whole;
    a payment bill records what was paid to whom and carries no balance at all.
    Forcing the second into the statement cutover would mean inventing the
    balance it is missing, so it enters here instead - as candidates, which are
    facts nobody has classified yet.

    That is also why this needs no backup and no restore rehearsal. Candidates
    are not confirmed facts, and the controlled import already carries the
    receipted, idempotent replay that makes re-running a batch a no-op rather
    than a second import.

    Nothing here confirms or posts anything.
    """
    from sqlalchemy import create_engine

    from ledgerbridge.file_key_provider import bootstrap_file_key
    from ledgerbridge.local_candidates import import_local_candidates, load_candidate_batch

    batch = load_candidate_batch(args.manifest.resolve())
    print(f"reading {len(batch.sources)} export(s)")
    artifact_root = LOCAL_STATE / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    if not EVIDENCE_KEY_FILE.exists():
        EVIDENCE_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        bootstrap_file_key(EVIDENCE_KEY_FILE.resolve(), generation="local-single-user-1")
    engine = create_engine(_env_value("LEDGERBRIDGE_MIGRATION_DATABASE_URL"), pool_pre_ping=True)
    try:
        result = import_local_candidates(
            engine,
            batch,
            key_file=EVIDENCE_KEY_FILE.resolve(),
            artifact_root=artifact_root,
        )
    finally:
        engine.dispose()
    print(
        f"LOCAL_CANDIDATES_OK replayed={str(result.replayed).lower()} "
        f"evidence={result.evidence_count} candidates={result.candidate_count} "
        f"batch={result.batch_ref} review=PENDING"
    )
    return 0


def command_book(args: argparse.Namespace) -> int:
    """Admit one book: its entity, business unit, first evidence and account.

    Core already has exactly this operation - `account_registry_intake` creates
    the entity, the business unit, the admission evidence, the managed account,
    its aliases and the business-unit assignment inside one transaction, then
    replays itself and refuses if the replay is not exact. Only the *command
    wrapper* around it insists on a production environment; the operation
    itself asks nothing about where it runs. Local mode calls the operation.

    Without a business-unit assignment covering the statement period the
    statement import refuses with "managed account requires continuous
    business-unit assignment coverage", so the plan should carry one that
    starts before the oldest statement.
    """
    from sqlalchemy import create_engine

    from ledgerbridge.account_registry_intake import (
        load_private_account_registry_intake,
        run_transactional_account_registry_intake,
    )

    loaded = load_private_account_registry_intake(args.plan.resolve())
    engine = create_engine(_env_value("LEDGERBRIDGE_MIGRATION_DATABASE_URL"))
    receipt = run_transactional_account_registry_intake(engine, loaded, commit=True)
    print(
        "LOCAL_BOOK_OK "
        f"entity={loaded.plan.entity.name} "
        f"account={loaded.plan.account.account_key} "
        f"created={receipt.created} "
        f"revision={receipt.registry_revision}"
    )
    return 0


def command_statements(args: argparse.Namespace) -> int:
    """Import one book's statements, behind a backup taken for the occasion.

    The backup is not a formality bolted on to satisfy a check. Core refuses a
    statement import without an encrypted backup and a passed isolated restore
    whose inventory equals the live counts, and it is right to: this is the
    only import path that writes transaction facts. Local mode meets that
    requirement by actually taking the backup and actually rehearsing the
    restore, immediately before the import, which is also the moment at which a
    backup is worth the most.

    The backup is taken here rather than left to the operator because the
    inventory must match the database as it is at import time. Any write in
    between - registering another account, importing another batch - invalidates
    it, and the failure would otherwise arrive as an opaque "proof is invalid".

    Re-running a manifest is the exception, and it has to be: the proof asserts
    what the ledger held *before* the batch, and after the batch has landed a
    fresh backup can no longer say that truthfully. So a replay presents the
    backup that was actually taken before it, found by its inventory.

    That works for the most recently imported batch and not for an older one -
    the gate wants one inventory to equal both the backup and the live counts
    minus this batch, and a book imported after it makes those two different
    numbers. An older manifest is therefore refused rather than re-imported.
    """
    from sqlalchemy import create_engine

    from ledgerbridge.local_statements import (
        batch_is_already_imported,
        build_statement_plans,
        import_local_statements,
        load_statement_batch,
    )
    from ledgerbridge.mybank_statement_cutover import (
        MyBankCutoverSafetyProof,
        MyBankStatementCutoverGates,
        production_counts_from_cutover_inventory,
    )

    batch = load_statement_batch(args.manifest.resolve())
    print(f"reading {len(batch.statements)} statements for {len(batch.accounts)} account(s)")
    # Every file is parsed before the database is touched at all. This is the
    # bounded preview: an unreadable file stops the run here, not halfway in.
    plans = build_statement_plans(batch)

    engine = create_engine(_env_value("LEDGERBRIDGE_MIGRATION_DATABASE_URL"))
    if batch_is_already_imported(engine, plans):
        backup = _backup_taken_before(engine, plans)
        print(f"replaying against backup: {backup.directory.name}")
    else:
        backup = run_local_backup(
            _local_database(),
            revision=_revision(),
            artifact_root=LOCAL_STATE / "artifacts",
            backup_root=BACKUP_ROOT,
            gnupg_home=GNUPG_HOME,
        )
        print(f"backup: {backup.directory.name}")

    schema_revision = str(backup.inventory["schema_revision"])
    gates = MyBankStatementCutoverGates(
        schema_revision=schema_revision,
        backup_verified=True,
        isolated_restore_verified=True,
        rollback_ready=True,
        expected_before=production_counts_from_cutover_inventory(
            backup.inventory,
            expected_schema_revision=schema_revision,
        ),
    )
    receipts = import_local_statements(
        engine,
        plans,
        proof=MyBankCutoverSafetyProof(
            backup_directory=backup.directory,
            restore_report=backup.restore_report,
        ),
        gates=gates,
        key_file=EVIDENCE_KEY_FILE,
        artifact_root=LOCAL_STATE / "artifacts",
    )
    created = sum(1 for receipt in receipts if receipt.created)
    transactions = sum(receipt.transaction_count for receipt in receipts)
    print(
        "LOCAL_STATEMENTS_OK "
        f"statements={len(receipts)} "
        f"created={created} "
        f"replayed={len(receipts) - created} "
        f"transactions={transactions} "
        f"review=PENDING"
    )
    return 0


def _backup_taken_before(
    engine: Engine,
    plans: tuple[BankStatementExistingAccountPlan, ...],
) -> LocalBackupResult:
    """Find the backup this batch was imported behind, by its inventory.

    Not by filename and not by time. The batch's own count arithmetic, run
    backwards from what the ledger holds now, says exactly what the pre-import
    inventory was; the right backup is the one carrying it. If none does, say so
    rather than taking a new one - a new one would describe the state after the
    import, and would be a proof of the wrong thing.
    """
    from ledgerbridge.local_statements import LocalStatementError, counts_before_batch
    from ledgerbridge.mybank_statement_cutover import (
        MyBankStatementCutoverError,
        _read_production_counts,
        production_counts_from_cutover_inventory,
    )

    with engine.connect() as connection:
        completed = _read_production_counts(connection)
    wanted = counts_before_batch(completed, plans)

    for backup in existing_backups(BACKUP_ROOT):
        revision = str(backup.inventory.get("schema_revision", ""))
        try:
            counts = production_counts_from_cutover_inventory(
                backup.inventory,
                expected_schema_revision=revision,
            )
        except MyBankStatementCutoverError:
            continue
        if counts == wanted:
            return backup
    raise LocalStatementError(
        "these statements are already imported, and no backup here describes "
        "the ledger as it was before them. Replay works for the most recently "
        "imported batch and stops working once anything is written after it: "
        "the proof must state what the ledger held beforehand, and the gate "
        "also compares that to the live counts minus this batch. Once another "
        "book lands, no single inventory can be both. This is the concrete "
        "shape of the D-028 gap recorded in the task document; nothing was "
        "double-imported."
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
                "entities": len(profile.books),
                "business_units": sum(len(book.business_units) for book in profile.books),
                "evidence_key": profile.evidence_key_file is not None,
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
    importer = subparsers.add_parser("import", help="import one controlled-review batch")
    importer.add_argument("--source-manifest", type=Path, required=True)
    importer.add_argument("--prepared-manifest", type=Path, default=None)
    importer.set_defaults(func=command_import)
    subparsers.add_parser(
        "backup",
        help="back up the local database and rehearse restoring it",
    ).set_defaults(func=command_backup)
    book = subparsers.add_parser("book", help="admit one entity, unit and account")
    book.add_argument("--plan", type=Path, required=True)
    book.set_defaults(func=command_book)
    statements = subparsers.add_parser(
        "statements",
        help="back up, then import one book's bank statements",
    )
    statements.add_argument("--manifest", type=Path, required=True)
    statements.set_defaults(func=command_statements)
    candidates = subparsers.add_parser(
        "candidates",
        help="import a payment platform's exports as review candidates",
    )
    candidates.add_argument("--manifest", type=Path, required=True)
    candidates.set_defaults(func=command_candidates)
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
    except (LocalModeRefused, LocalBackupError, LocalStatementError) as refusal:
        print(f"local mode refused: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
