"""Back up the local database and rehearse restoring it, on this machine.

Importing a bank statement writes transaction facts, and Core refuses to do
that until it has been shown two things: an encrypted backup taken before the
write, and a restore of that backup into an isolated database whose inventory
matches the live one exactly. ``verify_mybank_cutover_safety_proof`` checks the
artifacts; it does not care who produced them.

Production produces them with ``scripts/backup_restore.py``, which is a Hermes
script - it defaults to ``/srv/ai-center``, reaches for ``/dev/shm`` and GNU
``tar``, and validates a whole deployment that does not exist on a laptop. The
conclusion drawn from that used to be "statements cannot be imported locally".
That conclusion was wrong. The gate asks for a proof, and the proof is
producible here: this machine has Docker, ``pg_dump``, ``pg_restore``, ``gpg``
and a database small enough to restore in seconds.

So this module does the work rather than dodging it. It takes a real dump,
encrypts it under a real key, restores it into a throwaway database in the same
container, compares the inventories, drops the throwaway and records what
happened. Nothing about the gate is relaxed, nothing is hand-written, and the
local ledger ends up with the backups a single machine needs anyway.

Two things are deliberately narrower than production:

- The backup key is generated without a passphrase, in a keyring of its own
  under the local state directory. A passphrase on a single-user machine is a
  secret with nowhere to live; the evidence key beside it is stored the same
  way. The backup never leaves the machine.
- The inventory counts every base table in ``public`` rather than a fixed
  allowlist. That is stronger evidence than production's list, not weaker, and
  it cannot fall out of date when a migration adds a table.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess  # nosec B404 - fixed argument vectors, no shell, no user strings.
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

#: The formats ``verify_mybank_cutover_safety_proof`` accepts. They are spelled
#: here rather than imported because this module produces the artifacts and the
#: cutover consumes them; a rename should break loudly at the seam.
BACKUP_FORMAT: Final = "ledgerbridge-encrypted-backup-v3"
REHEARSAL_FORMAT: Final = "ledgerbridge-restore-rehearsal-v3"

CIPHERTEXT_NAME: Final = "ledgerbridge-backup.tar.gpg"
_SIDECAR_NAME: Final = "backup.json"
_CHECKSUM_NAME: Final = "SHA256SUMS"
_DUMP_MEMBER: Final = "database.dump"
_INVENTORY_MEMBER: Final = "inventory.json"
_ARTIFACT_MEMBER: Final = "artifacts"

_TABLE: Final = re.compile(r"^[a-z_][a-z0-9_]*$")
_REVISION: Final = re.compile(r"^[0-9a-f]{40}$")
_FINGERPRINT: Final = re.compile(r"^[0-9A-F]{40}$")

#: Anything larger than this is not a laptop ledger, and streaming a dump
#: through a pipe into memory stops being reasonable.
_MAX_DUMP_BYTES: Final = 2 * 1024 * 1024 * 1024


class LocalBackupError(RuntimeError):
    """A local backup or restore rehearsal could not be completed honestly."""


@dataclass(frozen=True, slots=True)
class LocalDatabase:
    """The one PostgreSQL this machine runs, and how to reach it."""

    container: str
    database: str
    owner_role: str
    image: str


@dataclass(frozen=True, slots=True)
class LocalBackupResult:
    """Where the proof landed, and what it says.

    ``directory`` and ``restore_report`` are exactly the two paths
    ``MyBankCutoverSafetyProof`` wants.
    """

    directory: Path
    restore_report: Path
    revision: str
    inventory: dict[str, Any]
    isolated_database: str


def run_local_backup(
    database: LocalDatabase,
    *,
    revision: str,
    artifact_root: Path,
    backup_root: Path,
    gnupg_home: Path,
    now: datetime | None = None,
) -> LocalBackupResult:
    """Take a backup, rehearse restoring it, and leave a verifiable proof.

    The order matters and is the same order production uses: read the live
    inventory first, dump from that same state, then restore into a database
    that has never held anything and compare. A rehearsal that ran before the
    dump would prove something about a different database.
    """

    if _REVISION.fullmatch(revision) is None:
        raise LocalBackupError("backup revision must be a 40-character commit id")
    moment = now or datetime.now(UTC)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    directory = backup_root / f"local-backup-{stamp}"
    if directory.exists():
        raise LocalBackupError(f"backup directory already exists: {directory}")

    inventory = read_inventory(database, database.database)
    dump = _dump_database(database)

    directory.mkdir(parents=True)
    directory.chmod(0o700)
    try:
        archive = _build_archive(
            dump=dump,
            inventory=inventory,
            artifact_root=artifact_root,
        )
        fingerprint = _ensure_backup_key(gnupg_home)
        ciphertext = _encrypt(archive, gnupg_home=gnupg_home, fingerprint=fingerprint)
        ciphertext_path = directory / CIPHERTEXT_NAME
        ciphertext_path.write_bytes(ciphertext)
        ciphertext_digest = hashlib.sha256(ciphertext).hexdigest()

        # Exactly these seven fields; the verifier compares the key set, so an
        # extra "note" here would fail the proof rather than annotate it.
        _write_json(
            directory / _SIDECAR_NAME,
            {
                "format": BACKUP_FORMAT,
                "created_at": moment.isoformat().replace("+00:00", "Z"),
                "revision": revision,
                "gpg_fingerprint": fingerprint,
                "ciphertext": CIPHERTEXT_NAME,
                "ciphertext_sha256": ciphertext_digest,
                "postgres_image": database.image,
            },
        )
        (directory / _CHECKSUM_NAME).write_text(
            f"{ciphertext_digest}  {CIPHERTEXT_NAME}\n",
            encoding="utf-8",
        )

        report_path, isolated = _rehearse_restore(
            database,
            directory=directory,
            revision=revision,
            source_inventory=inventory,
            ciphertext=ciphertext,
            gnupg_home=gnupg_home,
            moment=moment,
        )
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise

    return LocalBackupResult(
        directory=directory.resolve(),
        restore_report=report_path.resolve(),
        revision=revision,
        inventory=inventory,
        isolated_database=isolated,
    )


def read_inventory(database: LocalDatabase, name: str) -> dict[str, Any]:
    """Count what one database holds, in the shape the cutover gate reads.

    The three scalars use production's own SQL verbatim. The row counts cover
    every base table in ``public`` instead of a fixed list: the gate reads the
    sixteen it needs and ignores the rest, and a migration that adds a table
    then widens the evidence automatically instead of quietly escaping it.
    """

    tables = _base_tables(database, name)
    # A VALUES list rather than json_build_object(...): that function takes at
    # most 100 arguments, and this schema already has fifty-nine tables.
    counts = ", ".join(
        f"('{table}', (SELECT count(*) FROM public.{table}))"  # nosec B608 - validated.
        for table in tables
    )
    sql = (
        f"WITH counts(name, n) AS (VALUES {counts}) "  # nosec B608 - validated identifiers.
        "SELECT json_build_object("
        "'schema_revision', (SELECT version_num FROM public.alembic_version), "
        "'candidate_total', (SELECT count(*) FROM public.candidate), "
        "'latest_pending_candidates', (SELECT count(*) FROM public.candidate c "
        "JOIN LATERAL (SELECT status FROM public.candidate_revision cr "
        "WHERE cr.candidate_id=c.id ORDER BY cr.revision DESC LIMIT 1) latest ON true "
        "WHERE latest.status='PENDING'), "
        "'audit_events', (SELECT count(*) FROM public.audit_event), "
        "'row_counts', (SELECT json_object_agg(name, n) FROM counts)"
        ")::text"
    )
    payload = json.loads(_psql(database, name, sql))
    if not isinstance(payload, dict) or set(payload) != {
        "schema_revision",
        "candidate_total",
        "latest_pending_candidates",
        "audit_events",
        "row_counts",
    }:
        raise LocalBackupError("local inventory query returned an unexpected shape")
    return payload


def existing_backups(backup_root: Path) -> tuple[LocalBackupResult, ...]:
    """Every backup already taken here, newest first.

    Re-running an import needs the backup that was taken *before* it, not a new
    one: the cutover proof asserts that the inventory it carries is the state
    the ledger was in, and the batch gate reads the same numbers to tell a first
    import from a replay. A backup taken now would describe the state after the
    import and satisfy neither honestly.

    Only directories carrying both a sidecar and a passed rehearsal are
    returned; anything half-written is left out rather than repaired.
    """

    if not backup_root.is_dir():
        return ()
    found: list[LocalBackupResult] = []
    for directory in sorted(backup_root.iterdir(), reverse=True):
        if not directory.is_dir() or directory.is_symlink():
            continue
        reports = sorted(directory.glob("restore-rehearsal-*.json"))
        if not reports or not (directory / _SIDECAR_NAME).is_file():
            continue
        try:
            report = json.loads(reports[-1].read_text(encoding="utf-8"))
            sidecar = json.loads((directory / _SIDECAR_NAME).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict) or report.get("status") != "passed":
            continue
        inventory = report.get("source_database_metadata", {}).get("cutover_inventory")
        if not isinstance(inventory, dict):
            continue
        found.append(
            LocalBackupResult(
                directory=directory.resolve(),
                restore_report=reports[-1].resolve(),
                revision=str(sidecar.get("revision", "")),
                inventory=inventory,
                isolated_database=str(report.get("isolated_database", "")),
            )
        )
    return tuple(found)


def _base_tables(database: LocalDatabase, name: str) -> tuple[str, ...]:
    rows = _psql(
        database,
        name,
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name",
    )
    tables = tuple(line.strip() for line in rows.splitlines() if line.strip())
    if not tables or any(_TABLE.fullmatch(table) is None for table in tables):
        raise LocalBackupError("local database reported an unusable table name")
    return tables


def _rehearse_restore(
    database: LocalDatabase,
    *,
    directory: Path,
    revision: str,
    source_inventory: dict[str, Any],
    ciphertext: bytes,
    gnupg_home: Path,
    moment: datetime,
) -> tuple[Path, str]:
    """Restore the backup into a database that has never held anything.

    The isolated database is created here and dropped here. Its name carries
    random bytes so a leftover from a crashed run can never be mistaken for
    this run's, and dropping it is checked rather than assumed - a rehearsal
    that left a copy of the ledger behind has not demonstrated what it claims.
    """

    isolated = f"ledgerbridge_rehearsal_{secrets.token_hex(8)}"
    archive = _decrypt(ciphertext, gnupg_home=gnupg_home)
    dump = _member(archive, _DUMP_MEMBER)
    if json.loads(_member(archive, _INVENTORY_MEMBER).decode("utf-8")) != source_inventory:
        raise LocalBackupError("the archived inventory does not match the live one")

    _psql(database, "postgres", f'CREATE DATABASE "{isolated}"')
    try:
        _docker(
            database,
            (
                "pg_restore",
                "-U",
                database.owner_role,
                "-d",
                isolated,
                "--no-owner",
                "--exit-on-error",
            ),
            stdin=dump,
        )
        restored_inventory = read_inventory(database, isolated)
    finally:
        _psql(database, "postgres", f'DROP DATABASE IF EXISTS "{isolated}"')

    remaining = _psql(
        database,
        "postgres",
        f"SELECT count(*) FROM pg_database WHERE datname='{isolated}'",  # nosec B608 - hex name.
    ).strip()
    removed = remaining == "0"
    unchanged = read_inventory(database, database.database) == source_inventory

    if restored_inventory != source_inventory:
        raise LocalBackupError("the restored database does not match the live inventory")
    if not removed:
        raise LocalBackupError("the isolated rehearsal database could not be dropped")
    if not unchanged:
        raise LocalBackupError("the live database changed during the rehearsal")

    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    report_path = directory / f"restore-rehearsal-{stamp}.json"
    _write_json(
        report_path,
        {
            "format": REHEARSAL_FORMAT,
            "status": "passed",
            "backup": directory.name,
            "revision": revision,
            "source_format": "v3",
            "production_unchanged": unchanged,
            "isolated_resources_removed": removed,
            "isolated_database": isolated,
            "rehearsed_at": moment.isoformat().replace("+00:00", "Z"),
            "database_compared_fields": ["cutover_inventory"],
            "source_database_metadata": {"cutover_inventory": source_inventory},
            "post_restore_database_observations": {"cutover_inventory": restored_inventory},
        },
    )
    return report_path, isolated


def _build_archive(
    *,
    dump: bytes,
    inventory: dict[str, Any],
    artifact_root: Path,
) -> bytes:
    """Pack the dump, its inventory and the encrypted artifacts, reproducibly.

    Timestamps and ownership are pinned so that backing up an unchanged
    database twice produces the same bytes. That is not cosmetic: it is what
    makes "the ciphertext digest changed" mean "the ledger changed".
    """

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        _add_bytes(archive, _DUMP_MEMBER, dump)
        _add_bytes(
            archive,
            _INVENTORY_MEMBER,
            json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        )
        if artifact_root.is_dir():
            for path in sorted(p for p in artifact_root.rglob("*") if p.is_file()):
                if path.is_symlink():
                    raise LocalBackupError(f"artifact store holds a symlink: {path}")
                member = f"{_ARTIFACT_MEMBER}/{path.relative_to(artifact_root).as_posix()}"
                _add_bytes(archive, member, path.read_bytes())
    return buffer.getvalue()


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mtime = 0
    info.mode = 0o600
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(payload))


def _member(archive: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as opened:
        try:
            extracted = opened.extractfile(name)
        except KeyError:
            # A member that is absent and a member that is not a regular file
            # are the same problem to a caller reading a restored backup, and
            # tarfile reports them by two different mechanisms.
            raise LocalBackupError(f"backup archive is missing {name}") from None
        if extracted is None:
            raise LocalBackupError(f"backup archive is missing {name}")
        return extracted.read()


def _dump_database(database: LocalDatabase) -> bytes:
    dump = _docker(
        database,
        (
            "pg_dump",
            "-U",
            database.owner_role,
            "-d",
            database.database,
            "--format=custom",
        ),
    )
    if not dump or len(dump) > _MAX_DUMP_BYTES:
        raise LocalBackupError("the database dump is empty or implausibly large")
    return dump


def _psql(database: LocalDatabase, name: str, sql: str) -> str:
    output = _docker(
        database,
        (
            "psql",
            "-U",
            database.owner_role,
            "-d",
            name,
            "--no-align",
            "--tuples-only",
            "--quiet",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ),
    )
    return output.decode("utf-8")


def _docker(
    database: LocalDatabase,
    command: tuple[str, ...],
    *,
    stdin: bytes | None = None,
) -> bytes:
    argv = ["docker", "exec"]
    if stdin is not None:
        argv.append("-i")
    argv.extend([database.container, *command])
    return _run(argv, stdin=stdin, label=command[0])


def _run(
    argv: list[str],
    *,
    stdin: bytes | None = None,
    label: str,
    env: dict[str, str] | None = None,
) -> bytes:
    try:
        completed = subprocess.run(  # nosec B603 - fixed vectors, no shell.
            argv,
            input=stdin,
            capture_output=True,
            check=False,
            env=env,
        )
    except OSError as error:
        raise LocalBackupError(f"{label} could not be started: {error}") from None
    if completed.returncode != 0:
        raise LocalBackupError(f"{label} failed: {_diagnosis(completed.stderr)}")
    return completed.stdout


def _diagnosis(stderr: bytes) -> str:
    """The line that says what went wrong, not the last line printed.

    psql ends its complaints with a caret pointing at the offending character,
    so reporting the final line reports a lone "^" and nothing else.
    """
    lines = [line.strip() for line in stderr.decode("utf-8", "replace").splitlines()]
    meaningful = [line for line in lines if line and set(line) != {"^"}]
    errors = [line for line in meaningful if "ERROR" in line or "error" in line]
    chosen = errors or meaningful
    return " | ".join(chosen[:3]) if chosen else "no output"


def _ensure_backup_key(gnupg_home: Path) -> str:
    """Return the fingerprint of this machine's backup key, creating it once.

    The key lives in a keyring of its own so that a local backup never touches
    the user's own GnuPG configuration, and so that deleting the local state
    directory takes the key with it.
    """

    gnupg_home.mkdir(parents=True, exist_ok=True)
    gnupg_home.chmod(0o700)
    existing = _gpg(gnupg_home, ("--list-secret-keys", "--with-colons"))
    fingerprint = _first_fingerprint(existing)
    if fingerprint is not None:
        return fingerprint

    parameters = (
        "%no-protection\n"
        "Key-Type: RSA\n"
        "Key-Length: 3072\n"
        "Name-Real: LedgerBridge local backup\n"
        "Name-Comment: single-user mode\n"
        "Expire-Date: 0\n"
        "%commit\n"
    )
    _gpg(gnupg_home, ("--batch", "--gen-key"), stdin=parameters.encode("utf-8"))
    fingerprint = _first_fingerprint(_gpg(gnupg_home, ("--list-secret-keys", "--with-colons")))
    if fingerprint is None:
        raise LocalBackupError("the local backup key was not created")
    return fingerprint


def _first_fingerprint(listing: str) -> str | None:
    for line in listing.splitlines():
        fields = line.split(":")
        if fields and fields[0] == "fpr" and len(fields) > 9:
            candidate = fields[9]
            if _FINGERPRINT.fullmatch(candidate) is not None:
                return candidate
    return None


def _encrypt(payload: bytes, *, gnupg_home: Path, fingerprint: str) -> bytes:
    return _gpg_bytes(
        gnupg_home,
        (
            "--batch",
            "--yes",
            "--trust-model",
            "always",
            "--recipient",
            fingerprint,
            "--encrypt",
        ),
        stdin=payload,
    )


def _decrypt(payload: bytes, *, gnupg_home: Path) -> bytes:
    return _gpg_bytes(gnupg_home, ("--batch", "--yes", "--decrypt"), stdin=payload)


def _gpg(gnupg_home: Path, arguments: tuple[str, ...], *, stdin: bytes | None = None) -> str:
    return _gpg_bytes(gnupg_home, arguments, stdin=stdin).decode("utf-8", "replace")


def _gpg_bytes(
    gnupg_home: Path,
    arguments: tuple[str, ...],
    *,
    stdin: bytes | None = None,
) -> bytes:
    environment = dict(os.environ)
    environment["GNUPGHOME"] = _gnupg_home_argument(gnupg_home)
    return _run(["gpg", *arguments], stdin=stdin, label="gpg", env=environment)


def _gnupg_home_argument(gnupg_home: Path) -> str:
    """Spell the keyring path the way this particular gpg spells paths.

    Two different gpg builds are common on Windows: Gpg4win, which takes a
    drive-letter path, and the one Git for Windows ships, which is an MSYS
    build wanting ``/c/Users/...`` - hand that one a drive letter and it reads
    the colon as part of a relative directory, then looks for the keyring under
    the current working directory. Rather than guess, ask gpg how it spells its
    own default home and match that convention.
    """
    if os.name != "nt":
        return str(gnupg_home)
    reported = _run(["gpg", "--version"], label="gpg").decode("utf-8", "replace")
    posix_style = any(
        line.startswith("Home:") and line.split(":", 1)[1].strip().startswith("/")
        for line in reported.splitlines()
    )
    if not posix_style:
        return str(gnupg_home)
    resolved = gnupg_home.resolve()
    drive = resolved.drive.rstrip(":").lower()
    tail = resolved.as_posix()[len(resolved.drive) :].lstrip("/")
    return f"/{drive}/{tail}" if drive else resolved.as_posix()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a report about a backup the way such a report should be written.

    Into a sibling temporary file, then renamed over the target, so that an
    interrupted run leaves either the old report or the new one - never a
    truncated file that a later verification would read as a malformed proof.
    """

    descriptor, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


__all__ = [
    "BACKUP_FORMAT",
    "CIPHERTEXT_NAME",
    "REHEARSAL_FORMAT",
    "LocalBackupError",
    "LocalBackupResult",
    "LocalDatabase",
    "read_inventory",
    "run_local_backup",
]
