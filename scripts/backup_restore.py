"""Create encrypted LedgerBridge backups and rehearse isolated restores on Hermes."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import quote, unquote, urlsplit, urlunsplit

BACKUP_FORMAT = "ledgerbridge-encrypted-backup-v1"
POSTGRES_IMAGE = (
    "postgres:15-alpine@sha256:fe0737ba566a2c5b2a28f34433c0a423261900ec17b9bf7ad115e1aae7e57f1b"
)
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
FINGERPRINT_PATTERN = re.compile(r"[0-9A-F]{40,64}")
SUFFIX_PATTERN = re.compile(r"[0-9a-f]{8}")
PAYLOAD_COMPONENTS = (
    "database.dump",
    "roles.sql",
    "artifacts.tar",
    "deployment-tree.tar",
    "metadata.json",
)
TAR_NORMALIZATION = (
    "--sort=name",
    "--format=posix",
    "--pax-option=delete=atime,delete=ctime",
    "--mtime=@0",
    "--owner=0",
    "--group=0",
    "--numeric-owner",
)
DATABASE_METADATA_SQL = """
SELECT json_build_object(
    'database_name', current_database(),
    'database_owner', (
        SELECT pg_get_userbyid(datdba)
        FROM pg_database
        WHERE datname = current_database()
    ),
    'alembic_version', (SELECT version_num FROM alembic_version),
    'data_checksums', current_setting('data_checksums'),
    'role_grant_count', (
        SELECT count(*)
        FROM information_schema.role_table_grants
        WHERE grantee = 'ledgerbridge_app'
    ),
    'runtime_role_valid', (
        SELECT rolcanlogin
            AND NOT rolsuper
            AND NOT rolcreatedb
            AND NOT rolcreaterole
            AND NOT rolreplication
            AND NOT rolbypassrls
            AND NOT EXISTS (
                SELECT 1 FROM pg_auth_members WHERE member = pg_roles.oid
            )
            AND NOT EXISTS (
                SELECT 1
                FROM pg_database
                WHERE datname = current_database() AND datdba = pg_roles.oid
            )
        FROM pg_roles
        WHERE rolname = 'ledgerbridge_app'
    ),
    'audit_select_only', (
        has_table_privilege('ledgerbridge_app', 'audit_event', 'SELECT')
        AND NOT has_table_privilege('ledgerbridge_app', 'audit_event', 'INSERT')
        AND NOT has_table_privilege('ledgerbridge_app', 'audit_event', 'UPDATE')
        AND NOT has_table_privilege('ledgerbridge_app', 'audit_event', 'DELETE')
    ),
    'schema_create_denied', NOT has_schema_privilege(
        'ledgerbridge_app', 'public', 'CREATE'
    ),
    'function_count', (
        SELECT count(*)
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname IN (
              'account_block_protected_dimension_change',
              'append_audit_event',
              'audit_event_block_mutation',
              'journal_entry_assert_posted_complete',
              'journal_entry_block_posted_mutation',
              'journal_entry_validate_relationships',
              'posting_assert_balanced',
              'posting_block_posted_mutation',
              'posting_enforce_entity'
          )
    ),
    'trigger_count', (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal),
    'row_counts', json_build_object(
        'entity', (SELECT count(*) FROM entity),
        'account', (SELECT count(*) FROM account),
        'journal_entry', (SELECT count(*) FROM journal_entry),
        'posting', (SELECT count(*) FROM posting),
        'audit_event', (SELECT count(*) FROM audit_event)
    )
)::text;
""".strip()
RUNTIME_IDENTITY_PROGRAM = (
    "import os; "
    "from sqlalchemy import create_engine, text; "
    "engine=create_engine(os.environ['LEDGERBRIDGE_DATABASE_URL']); "
    "connection=engine.connect(); "
    "row=connection.execute(text('SELECT session_user, current_user')).one(); "
    "print('|'.join(row)); "
    "connection.close(); engine.dispose()"
)


class BackupError(RuntimeError):
    """Raised when backup or restore safety validation fails."""


class Runner:
    """Subprocess adapter that never puts secret values in command arguments."""

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        with contextlib.ExitStack() as stack:
            stdin = stack.enter_context(stdin_path.open("rb")) if stdin_path else None
            stdout: int | Any
            if stdout_path is None:
                stdout = subprocess.PIPE
            else:
                stdout = stack.enter_context(stdout_path.open("wb"))
            result = subprocess.run(  # nosec B603
                args,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                check=False,
            )
        if check and result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-2000:]
            command = " ".join(args[:2])
            raise BackupError(f"command failed ({command}): {stderr.strip()}")
        return result

    def capture(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> str:
        result = self._run(args, cwd=cwd, env=env, check=check)
        return (result.stdout or b"").decode("utf-8", errors="strict").strip()

    def succeeds(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> bool:
        return self._run(args, cwd=cwd, env=env, check=False).returncode == 0

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        stdin_path: Path | None = None,
        stdout_path: Path | None = None,
        check: bool = True,
    ) -> None:
        self._run(
            args,
            cwd=cwd,
            env=env,
            stdin_path=stdin_path,
            stdout_path=stdout_path,
            check=check,
        )


@dataclass(frozen=True)
class CommonConfig:
    project_dir: Path
    backup_root: Path
    work_root: Path
    gpg_home: Path
    fingerprint: str
    postgres_image: str = POSTGRES_IMAGE


@dataclass(frozen=True)
class SourceState:
    revision: str
    postgres_container: str
    api_container: str
    worker_container: str
    api_image: str
    artifact_volume: str
    database: dict[str, Any]


@dataclass(frozen=True)
class RestoreResources:
    suffix: str
    container: str
    network: str
    database_volume: str
    artifact_volume: str

    @classmethod
    def create(cls, suffix: str) -> RestoreResources:
        if SUFFIX_PATTERN.fullmatch(suffix) is None:
            raise BackupError("restore suffix must be exactly eight lowercase hex characters")
        return cls(
            suffix=suffix,
            container=f"ledgerbridge-restore-postgres-{suffix}",
            network=f"ledgerbridge-restore-network-{suffix}",
            database_volume=f"ledgerbridge_restore_db_{suffix}",
            artifact_volume=f"ledgerbridge_restore_artifacts_{suffix}",
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(moment: datetime | None = None) -> str:
    return (moment or _now()).strftime("%Y%m%dT%H%M%SZ")


def _validate_absolute_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise BackupError(f"{label} must be an absolute path")
    if path.is_symlink():
        raise BackupError(f"{label} may not be a symlink")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or resolved == Path(resolved.anchor):
        raise BackupError(f"{label} must be a non-root directory")
    return resolved


def _validate_secure_directory(path: Path, label: str) -> Path:
    resolved = _validate_absolute_directory(path, label)
    mode = stat.S_IMODE(resolved.stat().st_mode)
    if mode & 0o077:
        raise BackupError(f"{label} must not be accessible by group or other users")
    return resolved


def _validate_work_root(path: Path) -> Path:
    resolved = _validate_absolute_directory(path, "plaintext work root")
    mode = resolved.stat().st_mode
    if mode & 0o022 and not mode & stat.S_ISVTX:
        raise BackupError("writable plaintext work root must have the sticky bit")
    return resolved


def _validate_private_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"{label} must be a regular non-symlink file")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise BackupError(f"{label} must not be accessible by group or other users")
    return path


def _normalize_fingerprint(value: str) -> str:
    normalized = value.replace(" ", "").upper()
    if FINGERPRINT_PATTERN.fullmatch(normalized) is None:
        raise BackupError("GPG fingerprint must be 40-64 hexadecimal characters")
    return normalized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(0o600)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BackupError(f"{label} must contain a JSON object")
    return cast(dict[str, Any], value)


def _parse_env(path: Path) -> dict[str, str]:
    _validate_private_file(path, "deployment .env")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator != "=" or not key or key in values:
            raise BackupError("deployment .env contains an invalid or duplicate entry")
        values[key] = value
    return values


def _replace_database_host(url: str, host: str) -> str:
    parsed = urlsplit(url)
    if not parsed.scheme or parsed.username is None or parsed.password is None or not parsed.path:
        raise BackupError("runtime database URL is incomplete")
    netloc = (
        f"{quote(unquote(parsed.username), safe='')}:"
        f"{quote(unquote(parsed.password), safe='')}@{host}"
    )
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _compose(runner: Runner, project_dir: Path, *args: str, check: bool = True) -> str:
    return runner.capture(["docker", "compose", *args], cwd=project_dir, check=check)


def _container_for(runner: Runner, project_dir: Path, service: str) -> str:
    container = _compose(runner, project_dir, "ps", "-q", service)
    if not container:
        raise BackupError(f"Compose service is missing: {service}")
    return container


def _container_health(runner: Runner, container: str) -> str:
    return runner.capture(["docker", "inspect", "--format", "{{.State.Health.Status}}", container])


def _database_metadata(
    runner: Runner, container: str, database: str | None = None
) -> dict[str, Any]:
    if database is None:
        command = (
            'exec psql --no-psqlrc --username "$POSTGRES_USER" '
            '--dbname "$POSTGRES_DB" -At -v ON_ERROR_STOP=1 -c "$1"'
        )
    else:
        command = (
            "exec psql --no-psqlrc --username postgres "
            f'--dbname "{database}" -At -v ON_ERROR_STOP=1 -c "$1"'
        )
    output = runner.capture(
        ["docker", "exec", container, "sh", "-c", command, "sh", DATABASE_METADATA_SQL]
    )
    value = json.loads(output)
    if not isinstance(value, dict):
        raise BackupError("database metadata query did not return a JSON object")
    return cast(dict[str, Any], value)


def _verify_gpg_key(runner: Runner, home: Path, fingerprint: str) -> None:
    output = runner.capture(
        [
            "gpg",
            "--homedir",
            str(home),
            "--batch",
            "--with-colons",
            "--list-secret-keys",
            fingerprint,
        ]
    )
    fingerprints = {
        fields[9].upper()
        for line in output.splitlines()
        if (fields := line.split(":"))[0] == "fpr" and len(fields) > 9
    }
    if fingerprint not in fingerprints:
        raise BackupError("the requested GPG secret key is not present")


def _verify_deployment_manifest(runner: Runner, project_dir: Path, revision: str) -> None:
    runner.run(
        [
            sys.executable,
            str(project_dir / "scripts" / "deployment_manifest.py"),
            "verify",
            "--root",
            str(project_dir),
            "--manifest",
            "MANIFEST.sha256",
            "--expected-revision",
            revision,
        ]
    )


def _collect_source_state(config: CommonConfig, runner: Runner) -> SourceState:
    project_dir = config.project_dir
    revision = (project_dir / "DEPLOYED_REVISION").read_text(encoding="utf-8").strip()
    if REVISION_PATTERN.fullmatch(revision) is None:
        raise BackupError("DEPLOYED_REVISION is not a full lowercase Git SHA")
    _validate_private_file(project_dir / ".env", "deployment .env")
    _verify_deployment_manifest(runner, project_dir, revision)
    postgres = _container_for(runner, project_dir, "postgres")
    api = _container_for(runner, project_dir, "api")
    worker = _container_for(runner, project_dir, "worker")
    for service, container in (("postgres", postgres), ("api", api), ("worker", worker)):
        if _container_health(runner, container) != "healthy":
            raise BackupError(f"production service is not healthy: {service}")
    image = runner.capture(["docker", "inspect", "--format", "{{.Config.Image}}", api])
    worker_image = runner.capture(["docker", "inspect", "--format", "{{.Config.Image}}", worker])
    image_revision = runner.capture(
        [
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            api,
        ]
    )
    if image != worker_image or not image.startswith("ledgerbridge-app:"):
        raise BackupError("API and worker do not share one revision-tagged image")
    if image_revision != revision:
        raise BackupError("production image revision label does not match DEPLOYED_REVISION")
    artifact_volume = runner.capture(
        [
            "docker",
            "inspect",
            "--format",
            (
                "{{range .Mounts}}{{if eq .Destination "
                '"/var/lib/ledgerbridge/artifacts"}}{{.Name}}{{end}}{{end}}'
            ),
            api,
        ]
    )
    if not artifact_volume:
        raise BackupError("artifact named volume was not found on the API container")
    return SourceState(
        revision=revision,
        postgres_container=postgres,
        api_container=api,
        worker_container=worker,
        api_image=image,
        artifact_volume=artifact_volume,
        database=_database_metadata(runner, postgres),
    )


def _write_payload_hashes(directory: Path) -> None:
    lines = [f"{_sha256(directory / name)}  {name}" for name in PAYLOAD_COMPONENTS]
    (directory / "PAYLOAD.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def _verify_payload_hashes(directory: Path) -> None:
    manifest = directory / "PAYLOAD.sha256"
    expected: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        pure = PurePosixPath(name)
        if (
            separator != "  "
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or pure.is_absolute()
            or ".." in pure.parts
            or name in expected
        ):
            raise BackupError("payload hash manifest is invalid")
        expected[name] = digest
    if set(expected) != set(PAYLOAD_COMPONENTS):
        raise BackupError("payload hash manifest has an unexpected file set")
    for name, digest in expected.items():
        if _sha256(directory / name) != digest:
            raise BackupError(f"payload component hash mismatch: {name}")


def _deterministic_artifact_tar(
    runner: Runner, *, image: str, volume: str, destination_dir: Path, output: str
) -> None:
    runner.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "DAC_READ_SEARCH",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{volume}:/source:ro",
            "-v",
            f"{destination_dir}:/backup:rw",
            image,
            "tar",
            *TAR_NORMALIZATION,
            "-C",
            "/source",
            "-cf",
            f"/backup/{output}",
            ".",
        ]
    )


def _create_plain_payload(
    config: CommonConfig, state: SourceState, work_dir: Path, runner: Runner
) -> Path:
    database_dump = work_dir / "database.dump"
    roles_dump = work_dir / "roles.sql"
    runner.run(
        [
            "docker",
            "exec",
            state.postgres_container,
            "sh",
            "-c",
            (
                'exec pg_dump --no-password --username "$POSTGRES_USER" '
                '--dbname "$POSTGRES_DB" --format=custom --create'
            ),
        ],
        stdout_path=database_dump,
    )
    runner.run(
        [
            "docker",
            "exec",
            state.postgres_container,
            "sh",
            "-c",
            ('exec pg_dumpall --no-password --username "$POSTGRES_USER" --roles-only'),
        ],
        stdout_path=roles_dump,
    )
    runner.run(
        ["docker", "exec", "-i", state.postgres_container, "pg_restore", "--list"],
        stdin_path=database_dump,
    )
    _deterministic_artifact_tar(
        runner,
        image=state.api_image,
        volume=state.artifact_volume,
        destination_dir=work_dir,
        output="artifacts.tar",
    )
    runner.run(
        [
            "tar",
            *TAR_NORMALIZATION,
            "-C",
            str(config.project_dir.parent),
            "-cf",
            str(work_dir / "deployment-tree.tar"),
            config.project_dir.name,
        ]
    )
    metadata = {
        "format": BACKUP_FORMAT,
        "created_at": _now().isoformat(),
        "revision": state.revision,
        "api_image": state.api_image,
        "artifact_volume": state.artifact_volume,
        "database": state.database,
        "artifact_archive_sha256": _sha256(work_dir / "artifacts.tar"),
        "deployment_tree_sha256": _sha256(work_dir / "deployment-tree.tar"),
    }
    _write_json(work_dir / "metadata.json", metadata)
    _write_payload_hashes(work_dir)
    payload = work_dir / "payload.tar"
    runner.run(
        [
            "tar",
            *TAR_NORMALIZATION,
            "-C",
            str(work_dir),
            "-cf",
            str(payload),
            *PAYLOAD_COMPONENTS,
            "PAYLOAD.sha256",
        ]
    )
    return payload


def _assert_tree_has_no_symlinks(root: Path) -> None:
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            relative = candidate.relative_to(root).as_posix()
            raise BackupError(f"deployment tree contains a symlink: {relative}")


def _container_status(runner: Runner, container: str) -> str:
    return runner.capture(["docker", "inspect", "--format", "{{.State.Status}}", container])


def _wait_for_health(
    runner: Runner, container: str, *, expected: str = "healthy", timeout: int = 90
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _container_health(runner, container) == expected:
            return
        time.sleep(2)
    raise BackupError(f"container did not become {expected}: {container}")


def _restart_application(runner: Runner, state: SourceState) -> None:
    runner.run(["docker", "start", state.api_container, state.worker_container])
    _wait_for_health(runner, state.api_container)
    _wait_for_health(runner, state.worker_container)


def _assert_source_unchanged(
    before: SourceState, after: SourceState, *, require_container_identity: bool = True
) -> None:
    checks: tuple[tuple[str, object, object], ...] = (
        ("revision", before.revision, after.revision),
        ("API image", before.api_image, after.api_image),
        ("artifact volume", before.artifact_volume, after.artifact_volume),
        ("database metadata", before.database, after.database),
    )
    if require_container_identity:
        checks += (
            ("Postgres container", before.postgres_container, after.postgres_container),
            ("API container", before.api_container, after.api_container),
            ("worker container", before.worker_container, after.worker_container),
        )
    changed = [label for label, old, new in checks if old != new]
    if changed:
        raise BackupError(f"production state changed unexpectedly: {', '.join(changed)}")


def _validated_config(config: CommonConfig, runner: Runner) -> CommonConfig:
    if config.postgres_image != POSTGRES_IMAGE:
        raise BackupError("restore rehearsal must use the repository-pinned PostgreSQL image")
    validated = CommonConfig(
        project_dir=_validate_absolute_directory(config.project_dir, "project directory"),
        backup_root=_validate_secure_directory(config.backup_root, "backup root"),
        work_root=_validate_work_root(config.work_root),
        gpg_home=_validate_secure_directory(config.gpg_home, "GPG home"),
        fingerprint=_normalize_fingerprint(config.fingerprint),
        postgres_image=config.postgres_image,
    )
    _verify_gpg_key(runner, validated.gpg_home, validated.fingerprint)
    return validated


def _safe_remove_partial(path: Path, backup_root: Path) -> None:
    if not path.exists():
        return
    resolved_root = backup_root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    if (
        resolved_path.parent != resolved_root
        or not resolved_path.name.startswith(".partial-")
        or resolved_path.is_symlink()
    ):
        raise BackupError("refusing to remove an unguarded partial backup path")
    shutil.rmtree(resolved_path)


def create_backup(config: CommonConfig, runner: Runner | None = None) -> Path:
    """Create an encrypted, self-verified backup with a bounded write quiesce."""
    runner = runner or Runner()
    config = _validated_config(config, runner)
    before = _collect_source_state(config, runner)
    stamp = _timestamp()
    partial = config.backup_root / f".partial-{stamp}-{secrets.token_hex(4)}"
    destination = config.backup_root / f"{stamp}-{before.revision[:12]}"
    if destination.exists():
        raise BackupError(f"backup destination already exists: {destination.name}")
    work_dir: Path | None = None
    stopped = False
    published = False
    try:
        partial.mkdir(mode=0o700)
        partial.chmod(0o700)
        work_dir = Path(tempfile.mkdtemp(prefix="ledgerbridge-backup-", dir=config.work_root))
        work_dir.chmod(0o700)
        _assert_tree_has_no_symlinks(config.project_dir)
        stopped = True
        runner.run(
            [
                "docker",
                "stop",
                "--time",
                "30",
                before.api_container,
                before.worker_container,
            ]
        )
        for service, container in (
            ("api", before.api_container),
            ("worker", before.worker_container),
        ):
            if _container_status(runner, container) != "exited":
                raise BackupError(f"production service did not stop cleanly: {service}")

        quiesced = replace(
            before,
            database=_database_metadata(runner, before.postgres_container),
        )
        payload = _create_plain_payload(config, quiesced, work_dir, runner)
        cipher = partial / "ledgerbridge-backup.tar.gpg"
        runner.run(
            [
                "gpg",
                "--homedir",
                str(config.gpg_home),
                "--batch",
                "--yes",
                "--trust-model",
                "always",
                "--recipient",
                config.fingerprint,
                "--output",
                str(cipher),
                "--encrypt",
                str(payload),
            ]
        )
        cipher.chmod(0o600)
        roundtrip = work_dir / "roundtrip.tar"
        runner.run(
            [
                "gpg",
                "--homedir",
                str(config.gpg_home),
                "--batch",
                "--yes",
                "--output",
                str(roundtrip),
                "--decrypt",
                str(cipher),
            ]
        )
        if not hmac.compare_digest(_sha256(payload), _sha256(roundtrip)):
            raise BackupError("encrypted backup failed its decrypt round-trip check")

        sidecar = {
            "format": BACKUP_FORMAT,
            "created_at": _now().isoformat(),
            "revision": before.revision,
            "gpg_fingerprint": config.fingerprint,
            "ciphertext": cipher.name,
            "ciphertext_sha256": _sha256(cipher),
            "postgres_image": config.postgres_image,
        }
        _write_json(partial / "backup.json", sidecar)
        checksum = partial / "SHA256SUMS"
        checksum.write_text(
            f"{sidecar['ciphertext_sha256']}  {cipher.name}\n",
            encoding="utf-8",
            newline="\n",
        )
        checksum.chmod(0o600)

        _restart_application(runner, before)
        stopped = False
        after = _collect_source_state(config, runner)
        _assert_source_unchanged(quiesced, after)
        partial.rename(destination)
        published = True
        return destination
    except BaseException as error:
        if stopped:
            try:
                _restart_application(runner, before)
            except BaseException as restart_error:
                raise BackupError(
                    f"backup failed and production restart also failed: {error}"
                ) from restart_error
        raise
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        if not published:
            _safe_remove_partial(partial, config.backup_root)


def _safe_extract_tar(
    archive: Path, destination: Path, *, expected_files: set[str] | None = None
) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    destination.chmod(0o700)
    with tarfile.open(archive, mode="r:") as bundle:
        members = bundle.getmembers()
        names: set[str] = set()
        for member in members:
            pure = PurePosixPath(member.name)
            normalized = pure.as_posix()
            if (
                not normalized
                or pure.is_absolute()
                or ".." in pure.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or normalized in names
            ):
                raise BackupError(f"unsafe or duplicate tar member: {member.name!r}")
            names.add(normalized)
        if expected_files is not None and names != expected_files:
            raise BackupError("backup payload contains an unexpected file set")
        bundle.extractall(destination, members=members, filter="data")  # nosec B202


def _validate_backup_directory(config: CommonConfig, backup: Path) -> Path:
    backup = _validate_secure_directory(backup, "backup directory")
    if backup.parent != config.backup_root:
        raise BackupError("backup directory must be a direct child of the configured root")
    return backup


def _validate_backup_sidecar(config: CommonConfig, backup: Path) -> tuple[dict[str, Any], Path]:
    sidecar = _load_json(backup / "backup.json", "backup sidecar")
    expected_keys = {
        "format",
        "created_at",
        "revision",
        "gpg_fingerprint",
        "ciphertext",
        "ciphertext_sha256",
        "postgres_image",
    }
    if set(sidecar) != expected_keys or sidecar.get("format") != BACKUP_FORMAT:
        raise BackupError("backup sidecar format or field set is invalid")
    revision = sidecar.get("revision")
    if not isinstance(revision, str) or REVISION_PATTERN.fullmatch(revision) is None:
        raise BackupError("backup sidecar revision is invalid")
    if sidecar.get("gpg_fingerprint") != config.fingerprint:
        raise BackupError("backup was not encrypted for the configured key")
    if sidecar.get("postgres_image") != config.postgres_image:
        raise BackupError("backup PostgreSQL image pin does not match this automation")
    if sidecar.get("ciphertext") != "ledgerbridge-backup.tar.gpg":
        raise BackupError("backup ciphertext filename is invalid")
    digest = sidecar.get("ciphertext_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise BackupError("backup ciphertext digest is invalid")
    cipher = _validate_private_file(backup / "ledgerbridge-backup.tar.gpg", "ciphertext")
    checksum = _validate_private_file(backup / "SHA256SUMS", "ciphertext checksum")
    expected_line = f"{digest}  {cipher.name}\n"
    if not hmac.compare_digest(checksum.read_text(encoding="utf-8"), expected_line):
        raise BackupError("SHA256SUMS does not match the backup sidecar")
    if not hmac.compare_digest(_sha256(cipher), digest):
        raise BackupError("encrypted backup checksum mismatch")
    return sidecar, cipher


def _validate_tar_archive(archive: Path) -> None:
    with tarfile.open(archive, mode="r:") as bundle:
        names: set[str] = set()
        for member in bundle.getmembers():
            pure = PurePosixPath(member.name)
            normalized = pure.as_posix()
            if (
                not normalized
                or pure.is_absolute()
                or ".." in pure.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or normalized in names
                or (normalized == "." and not member.isdir())
            ):
                raise BackupError(f"unsafe or duplicate tar member: {member.name!r}")
            names.add(normalized)


def _validate_backup_image(runner: Runner, image: object, revision: str) -> str:
    if (
        not isinstance(image, str)
        or re.fullmatch(r"ledgerbridge-app:[0-9a-f]{7,40}", image) is None
    ):
        raise BackupError("backup application image tag is invalid")
    label = runner.capture(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            image,
        ]
    )
    if not hmac.compare_digest(label, revision):
        raise BackupError("backup application image revision label is invalid")
    return image


def _wait_for_postgres(runner: Runner, container: str, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        output = runner.capture(
            ["docker", "exec", container, "pg_isready", "-U", "postgres"],
            check=False,
        )
        if "accepting connections" in output:
            return
        time.sleep(2)
    raise BackupError("isolated PostgreSQL did not become ready")


def _cleanup_restore_resources(runner: Runner, resources: RestoreResources) -> None:
    runner.run(["docker", "rm", "--force", resources.container], check=False)
    runner.run(["docker", "volume", "rm", "--force", resources.database_volume], check=False)
    runner.run(["docker", "volume", "rm", "--force", resources.artifact_volume], check=False)
    runner.run(["docker", "network", "rm", resources.network], check=False)
    probes = (
        ("container", ["docker", "inspect", resources.container]),
        ("database volume", ["docker", "volume", "inspect", resources.database_volume]),
        ("artifact volume", ["docker", "volume", "inspect", resources.artifact_volume]),
        ("network", ["docker", "network", "inspect", resources.network]),
    )
    remaining = [label for label, command in probes if runner.succeeds(command)]
    if remaining:
        raise BackupError(f"restore resources were not removed: {', '.join(remaining)}")


def _database_name(metadata: dict[str, Any]) -> str:
    value = metadata.get("database_name")
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) is None:
        raise BackupError("database name in backup metadata is invalid")
    return value


def _validate_restored_database(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    if actual != expected:
        differing = sorted(
            key for key in set(expected) | set(actual) if expected.get(key) != actual.get(key)
        )
        raise BackupError(f"restored database metadata differs: {', '.join(differing)}")
    if not isinstance(actual.get("role_grant_count"), int) or actual["role_grant_count"] <= 0:
        raise BackupError("ledgerbridge_app has no restored table grants")
    required_true = (
        "runtime_role_valid",
        "audit_select_only",
        "schema_create_denied",
    )
    failed = [name for name in required_true if actual.get(name) is not True]
    if failed:
        raise BackupError(f"restored privilege invariants failed: {', '.join(failed)}")
    if actual.get("data_checksums") != "on":
        raise BackupError("restored PostgreSQL cluster does not have data checksums enabled")
    missing_objects = [
        name
        for name in ("function_count", "trigger_count")
        if not isinstance(actual.get(name), int) or actual[name] <= 0
    ]
    if missing_objects:
        raise BackupError(f"restored database lacks required objects: {', '.join(missing_objects)}")


def _deployment_root(runner: Runner, archive: Path, destination: Path, revision: str) -> Path:
    _safe_extract_tar(archive, destination)
    children = list(destination.iterdir())
    if len(children) != 1 or not children[0].is_dir() or children[0].is_symlink():
        raise BackupError("deployment archive must contain exactly one top-level directory")
    root = children[0]
    archived_revision = (root / "DEPLOYED_REVISION").read_text(encoding="utf-8").strip()
    if not hmac.compare_digest(archived_revision, revision):
        raise BackupError("restored deployment revision differs from backup metadata")
    _validate_private_file(root / ".env", "restored deployment .env")
    _verify_deployment_manifest(runner, root, revision)
    return root


def _restore_artifacts(
    runner: Runner,
    *,
    image: str,
    volume: str,
    work_dir: Path,
    archive: Path,
) -> str:
    runner.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--cap-add",
            "DAC_READ_SEARCH",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{volume}:/target:rw",
            "-v",
            f"{work_dir}:/backup:ro",
            image,
            "tar",
            "-C",
            "/target",
            "-xf",
            f"/backup/{archive.name}",
        ]
    )
    _deterministic_artifact_tar(
        runner,
        image=image,
        volume=volume,
        destination_dir=work_dir,
        output="restored-artifacts.tar",
    )
    return _sha256(work_dir / "restored-artifacts.tar")


def _runtime_identity(
    runner: Runner,
    *,
    image: str,
    network: str,
    database_url: str,
) -> str:
    process_env = os.environ.copy()
    process_env["LEDGERBRIDGE_DATABASE_URL"] = database_url
    return runner.capture(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-e",
            "LEDGERBRIDGE_DATABASE_URL",
            image,
            "python",
            "-c",
            RUNTIME_IDENTITY_PROGRAM,
        ],
        env=process_env,
    )


def rehearse_restore(config: CommonConfig, backup: Path, runner: Runner | None = None) -> Path:
    """Restore a backup into fresh isolated resources and prove all invariants."""
    runner = runner or Runner()
    config = _validated_config(config, runner)
    backup = _validate_backup_directory(config, backup)
    sidecar, cipher = _validate_backup_sidecar(config, backup)
    revision = cast(str, sidecar["revision"])
    before = _collect_source_state(config, runner)
    resources = RestoreResources.create(secrets.token_hex(4))
    work_dir = Path(tempfile.mkdtemp(prefix="ledgerbridge-restore-", dir=config.work_root))
    started_at = _now()
    try:
        work_dir.chmod(0o700)
        payload = work_dir / "payload.tar"
        runner.run(
            [
                "gpg",
                "--homedir",
                str(config.gpg_home),
                "--batch",
                "--yes",
                "--output",
                str(payload),
                "--decrypt",
                str(cipher),
            ]
        )
        extracted = work_dir / "payload"
        _safe_extract_tar(
            payload,
            extracted,
            expected_files={*PAYLOAD_COMPONENTS, "PAYLOAD.sha256"},
        )
        _verify_payload_hashes(extracted)
        metadata = _load_json(extracted / "metadata.json", "encrypted backup metadata")
        expected_metadata_keys = {
            "format",
            "created_at",
            "revision",
            "api_image",
            "artifact_volume",
            "database",
            "artifact_archive_sha256",
            "deployment_tree_sha256",
        }
        if (
            set(metadata) != expected_metadata_keys
            or metadata.get("format") != BACKUP_FORMAT
            or metadata.get("revision") != revision
        ):
            raise BackupError("encrypted metadata does not match the backup sidecar")
        backup_image = _validate_backup_image(runner, metadata.get("api_image"), revision)
        expected_database = metadata.get("database")
        if not isinstance(expected_database, dict):
            raise BackupError("encrypted database metadata is invalid")
        expected_database = cast(dict[str, Any], expected_database)
        database_name = _database_name(expected_database)
        archive_digests = (
            ("artifacts.tar", "artifact_archive_sha256"),
            ("deployment-tree.tar", "deployment_tree_sha256"),
        )
        for filename, field in archive_digests:
            expected_digest = metadata.get(field)
            if (
                not isinstance(expected_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
                or not hmac.compare_digest(_sha256(extracted / filename), expected_digest)
            ):
                raise BackupError(f"encrypted metadata digest differs: {field}")
        _validate_tar_archive(extracted / "artifacts.tar")

        runner.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                config.postgres_image,
                "pg_restore",
                "--list",
            ],
            stdin_path=extracted / "database.dump",
        )
        deployment = _deployment_root(
            runner,
            extracted / "deployment-tree.tar",
            work_dir / "deployment",
            revision,
        )

        try:
            runner.run(["docker", "network", "create", "--internal", resources.network])
            runner.run(["docker", "volume", "create", resources.database_volume])
            runner.run(["docker", "volume", "create", resources.artifact_volume])
            postgres_password = secrets.token_urlsafe(32)
            process_env = os.environ.copy()
            process_env["POSTGRES_PASSWORD"] = postgres_password
            runner.capture(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    resources.container,
                    "--network",
                    resources.network,
                    "--security-opt",
                    "no-new-privileges",
                    "--pids-limit",
                    "128",
                    "--memory",
                    "512m",
                    "-e",
                    "POSTGRES_PASSWORD",
                    "-e",
                    "POSTGRES_INITDB_ARGS=--data-checksums",
                    "-v",
                    f"{resources.database_volume}:/var/lib/postgresql/data",
                    config.postgres_image,
                ],
                env=process_env,
            )
            _wait_for_postgres(runner, resources.container)
            runner.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    resources.container,
                    "psql",
                    "--no-psqlrc",
                    "--username",
                    "postgres",
                    "--dbname",
                    "postgres",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "--file",
                    "-",
                ],
                stdin_path=extracted / "roles.sql",
            )
            runner.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    resources.container,
                    "pg_restore",
                    "--username",
                    "postgres",
                    "--dbname",
                    "postgres",
                    "--create",
                    "--exit-on-error",
                ],
                stdin_path=extracted / "database.dump",
            )
            actual_database = _database_metadata(runner, resources.container, database_name)
            _validate_restored_database(expected_database, actual_database)

            artifact_digest = _restore_artifacts(
                runner,
                image=backup_image,
                volume=resources.artifact_volume,
                work_dir=extracted,
                archive=extracted / "artifacts.tar",
            )
            expected_artifact_digest = metadata.get("artifact_archive_sha256")
            if not isinstance(expected_artifact_digest, str) or not hmac.compare_digest(
                artifact_digest, expected_artifact_digest
            ):
                raise BackupError("restored artifact volume digest differs from backup")

            environment = _parse_env(deployment / ".env")
            source_url = environment.get("LEDGERBRIDGE_DATABASE_URL")
            if source_url is None:
                raise BackupError("deployment .env lacks LEDGERBRIDGE_DATABASE_URL")
            restored_url = _replace_database_host(source_url, resources.container)
            identity = _runtime_identity(
                runner,
                image=backup_image,
                network=resources.network,
                database_url=restored_url,
            )
            if not hmac.compare_digest(identity, "ledgerbridge_app|ledgerbridge_app"):
                raise BackupError("application image did not connect as ledgerbridge_app")
        finally:
            _cleanup_restore_resources(runner, resources)
            after = _collect_source_state(config, runner)
            _assert_source_unchanged(before, after)

        report = backup / f"restore-rehearsal-{_timestamp()}.json"
        _write_json(
            report,
            {
                "format": "ledgerbridge-restore-rehearsal-v1",
                "status": "passed",
                "started_at": started_at.isoformat(),
                "completed_at": _now().isoformat(),
                "backup": backup.name,
                "revision": revision,
                "database": database_name,
                "database_metadata": expected_database,
                "artifact_archive_sha256": metadata["artifact_archive_sha256"],
                "deployment_tree_sha256": metadata["deployment_tree_sha256"],
                "production_unchanged": True,
                "isolated_resources_removed": True,
            },
        )
        return report
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _common_config(args: argparse.Namespace) -> CommonConfig:
    return CommonConfig(
        project_dir=args.project_dir,
        backup_root=args.backup_root,
        work_root=args.work_root,
        gpg_home=args.gpg_home,
        fingerprint=args.fingerprint,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path("/srv/ai-center/ledgerbridge"))
    parser.add_argument(
        "--backup-root", type=Path, default=Path("/srv/ai-center/backups/ledgerbridge")
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=Path("/dev/shm"),  # nosec B108
    )
    parser.add_argument(
        "--gpg-home",
        type=Path,
        default=Path("/srv/ai-center/ledgerbridge-secrets/backup-gnupg"),
    )
    parser.add_argument("--fingerprint", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup")
    rehearse = commands.add_parser("rehearse")
    rehearse.add_argument("--backup", required=True, type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        config = _common_config(args)
        if args.command == "backup":
            result = create_backup(config)
            print(f"encrypted backup created: {result}")
            return
        report = rehearse_restore(config, args.backup)
        print(f"isolated restore rehearsal passed: {report}")
    except (BackupError, OSError, json.JSONDecodeError, tarfile.TarError) as error:
        print(f"backup_restore: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
