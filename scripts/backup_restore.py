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

BACKUP_FORMAT_V1 = "ledgerbridge-encrypted-backup-v1"
BACKUP_FORMAT_V2 = "ledgerbridge-encrypted-backup-v2"
BACKUP_FORMAT = BACKUP_FORMAT_V2
SUPPORTED_BACKUP_FORMATS = frozenset({BACKUP_FORMAT_V1, BACKUP_FORMAT_V2})
RESTORE_REPORT_FORMAT = "ledgerbridge-restore-rehearsal-v2"
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
DATABASE_SECURITY_SQL = """
SELECT json_build_object(
    'database_temp_denied', NOT has_database_privilege(
        'ledgerbridge_app', current_database(), 'TEMPORARY'
    ),
    'security_functions', COALESCE((
        SELECT json_agg(
            json_build_object(
                'name', p.proname,
                'identity_arguments', pg_get_function_identity_arguments(p.oid),
                'proconfig', COALESCE(to_json(p.proconfig), '[]'::json)
            ) ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)
        )
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND NOT EXISTS (
              SELECT 1
              FROM pg_depend AS dependency
              WHERE dependency.classid = 'pg_proc'::regclass
                AND dependency.objid = p.oid
                AND dependency.deptype = 'e'
          )
    ), '[]'::json),
    'public_triggers', COALESCE((
        SELECT json_agg(
            json_build_object(
                'table', table_class.relname,
                'name', trigger.tgname,
                'enabled', trigger.tgenabled
            ) ORDER BY table_class.relname, trigger.tgname
        )
        FROM pg_trigger AS trigger
        JOIN pg_class AS table_class ON table_class.oid = trigger.tgrelid
        JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
        WHERE namespace.nspname = 'public' AND NOT trigger.tgisinternal
    ), '[]'::json),
    'table_grants', COALESCE((
        SELECT json_agg(
            json_build_object(
                'table', table_name,
                'privilege', privilege_type,
                'grantable', is_grantable
            ) ORDER BY table_name, privilege_type
        )
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public' AND grantee = 'ledgerbridge_app'
    ), '[]'::json),
    'sequence_grants', COALESCE((
        SELECT json_agg(
            json_build_object(
                'sequence', object_name,
                'privilege', privilege_type,
                'grantable', is_grantable
            ) ORDER BY object_name, privilege_type
        )
        FROM information_schema.role_usage_grants
        WHERE object_schema = 'public'
          AND object_type = 'SEQUENCE'
          AND grantee = 'ledgerbridge_app'
    ), '[]'::json),
    'function_grants', COALESCE((
        SELECT json_agg(
            json_build_object(
                'function', routine_name,
                'grantee', grantee,
                'privilege', privilege_type,
                'grantable', is_grantable
            ) ORDER BY routine_name, grantee, privilege_type
        )
        FROM information_schema.routine_privileges
        WHERE specific_schema = 'public'
          AND grantee IN ('ledgerbridge_app', 'PUBLIC')
    ), '[]'::json)
)::text;
""".strip()

PHASE_1_TABLES = ("entity", "account", "journal_entry", "posting", "audit_event")
PHASE_2_TABLES = ("raw_artifact", "import_job", "source_record")
PHASE_3_TABLES = ("ingest_channel", "source_system")
ROW_COUNT_SQL = {
    PHASE_1_TABLES: """
        SELECT json_build_object(
            'entity', (SELECT count(*) FROM public.entity),
            'account', (SELECT count(*) FROM public.account),
            'journal_entry', (SELECT count(*) FROM public.journal_entry),
            'posting', (SELECT count(*) FROM public.posting),
            'audit_event', (SELECT count(*) FROM public.audit_event)
        )::text;
    """.strip(),
    PHASE_1_TABLES + PHASE_2_TABLES: """
        SELECT json_build_object(
            'entity', (SELECT count(*) FROM public.entity),
            'account', (SELECT count(*) FROM public.account),
            'journal_entry', (SELECT count(*) FROM public.journal_entry),
            'posting', (SELECT count(*) FROM public.posting),
            'audit_event', (SELECT count(*) FROM public.audit_event),
            'raw_artifact', (SELECT count(*) FROM public.raw_artifact),
            'import_job', (SELECT count(*) FROM public.import_job),
            'source_record', (SELECT count(*) FROM public.source_record)
        )::text;
    """.strip(),
    PHASE_1_TABLES + PHASE_2_TABLES + PHASE_3_TABLES: """
        SELECT json_build_object(
            'entity', (SELECT count(*) FROM public.entity),
            'account', (SELECT count(*) FROM public.account),
            'journal_entry', (SELECT count(*) FROM public.journal_entry),
            'posting', (SELECT count(*) FROM public.posting),
            'audit_event', (SELECT count(*) FROM public.audit_event),
            'raw_artifact', (SELECT count(*) FROM public.raw_artifact),
            'import_job', (SELECT count(*) FROM public.import_job),
            'source_record', (SELECT count(*) FROM public.source_record),
            'ingest_channel', (SELECT count(*) FROM public.ingest_channel),
            'source_system', (SELECT count(*) FROM public.source_system)
        )::text;
    """.strip(),
}
PHASE_1_FUNCTIONS = frozenset(
    {
        "account_block_protected_dimension_change",
        "append_audit_event",
        "audit_event_block_mutation",
        "journal_entry_assert_posted_complete",
        "journal_entry_block_posted_mutation",
        "journal_entry_validate_relationships",
        "posting_assert_balanced",
        "posting_block_posted_mutation",
        "posting_enforce_entity",
    }
)
PHASE_2_FUNCTIONS = frozenset(
    {
        "import_job_enforce_transition",
        "journal_entry_validate_post_audit",
        "raw_artifact_block_mutation",
        "raw_artifact_validate_audit",
        "source_record_block_mutation",
    }
)
PHASE_3_FUNCTIONS = frozenset({"registry_block_mutation"})
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

    def query(sql: str) -> object:
        output = runner.capture(["docker", "exec", container, "sh", "-c", command, "sh", sql])
        return json.loads(output)

    value = query(DATABASE_METADATA_SQL)
    if not isinstance(value, dict):
        raise BackupError("database metadata query did not return a JSON object")
    metadata = cast(dict[str, Any], value)
    revision = metadata.get("alembic_version")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9]{8}_[0-9]{4}", revision) is None:
        raise BackupError("database metadata has an invalid Alembic revision")
    tables = list(PHASE_1_TABLES)
    if revision >= "20260821_0003":
        tables.extend(PHASE_2_TABLES)
    if revision >= "20260822_0004":
        tables.extend(PHASE_3_TABLES)
    table_key = tuple(tables)
    row_counts = query(ROW_COUNT_SQL[table_key])
    if not isinstance(row_counts, dict) or set(row_counts) != set(tables):
        raise BackupError("database row-count query returned an invalid object")
    security = query(DATABASE_SECURITY_SQL)
    if not isinstance(security, dict):
        raise BackupError("database security query returned an invalid object")
    metadata["metadata_version"] = 2
    metadata["row_counts"] = row_counts
    metadata.update(cast(dict[str, Any], security))
    return metadata


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


def _artifact_quota_config(project_dir: Path) -> dict[str, int]:
    environment = _parse_env(project_dir / ".env")
    defaults = {
        "per_artifact_max_bytes": 50 * 1024 * 1024,
        "published_max_bytes": 10 * 1024 * 1024 * 1024,
        "staging_max_bytes": 512 * 1024 * 1024,
        "staging_ttl_seconds": 60 * 60,
    }
    variables = {
        "per_artifact_max_bytes": "LEDGERBRIDGE_ARTIFACT_MAX_BYTES",
        "published_max_bytes": "LEDGERBRIDGE_ARTIFACT_TOTAL_MAX_BYTES",
        "staging_max_bytes": "LEDGERBRIDGE_ARTIFACT_STAGING_MAX_BYTES",
        "staging_ttl_seconds": "LEDGERBRIDGE_ARTIFACT_STAGING_TTL_SECONDS",
    }
    result: dict[str, int] = {}
    for field, variable in variables.items():
        raw = environment.get(variable, str(defaults[field]))
        try:
            value = int(raw)
        except ValueError as exc:
            raise BackupError(f"deployment quota setting is not an integer: {variable}") from exc
        if value <= 0 or value > 2**63 - 1:
            raise BackupError(f"deployment quota setting is outside its safe range: {variable}")
        result[field] = value
    return result


def _artifact_archive_metadata(archive: Path, quota: dict[str, int]) -> dict[str, object]:
    published_bytes = 0
    staging_bytes = 0
    with tarfile.open(archive, mode="r:") as bundle:
        names: set[str] = set()
        for member in bundle.getmembers():
            pure = PurePosixPath(member.name)
            parts = tuple(part for part in pure.parts if part != ".")
            normalized = pure.as_posix()
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or normalized in names
            ):
                raise BackupError("artifact archive contains an unsafe entry")
            names.add(normalized)
            if member.isdir():
                if not _valid_artifact_archive_directory(parts):
                    raise BackupError("artifact archive contains an unexpected directory")
                continue
            if not member.isfile():
                raise BackupError("artifact archive contains an unexpected entry type")
            if parts == (".quota.lock",):
                continue
            if len(parts) == 2 and parts[0] == ".staging" and parts[1].startswith("artifact-"):
                staging_bytes += member.size
                continue
            if _valid_artifact_blob_parts(parts):
                published_bytes += member.size
                continue
            raise BackupError("artifact archive contains an unexpected file")
    if published_bytes > quota["published_max_bytes"]:
        raise BackupError("artifact archive exceeds the configured published quota")
    if staging_bytes > quota["staging_max_bytes"]:
        raise BackupError("artifact archive exceeds the configured staging quota")
    return {
        "published_bytes": published_bytes,
        "staging_bytes": staging_bytes,
        "unsafe_entries": 0,
        "quota": quota,
    }


def _valid_artifact_archive_directory(parts: tuple[str, ...]) -> bool:
    if not parts or parts in {(".staging",), ("sha256",)}:
        return True
    if parts[0] != "sha256" or len(parts) not in {2, 3}:
        return False
    return all(re.fullmatch(r"[0-9a-f]{2}", part) is not None for part in parts[1:])


def _valid_artifact_blob_parts(parts: tuple[str, ...]) -> bool:
    if len(parts) != 4 or parts[0] != "sha256":
        return False
    first, second, digest = parts[1:]
    return (
        re.fullmatch(r"[0-9a-f]{2}", first) is not None
        and re.fullmatch(r"[0-9a-f]{2}", second) is not None
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and digest.startswith(first + second)
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
    artifact_control = _artifact_archive_metadata(
        work_dir / "artifacts.tar",
        _artifact_quota_config(config.project_dir),
    )
    metadata = {
        "format": BACKUP_FORMAT,
        "created_at": _now().isoformat(),
        "revision": state.revision,
        "api_image": state.api_image,
        "artifact_volume": state.artifact_volume,
        "database": state.database,
        "artifact_control": artifact_control,
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
    if set(sidecar) != expected_keys or sidecar.get("format") not in SUPPORTED_BACKUP_FORMATS:
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


def _validate_restored_database(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    is_v2 = expected.get("metadata_version") == 2
    compared_fields = sorted(expected)
    comparison_actual = actual if is_v2 else {key: actual.get(key) for key in expected}
    if comparison_actual != expected:
        differing = sorted(
            key
            for key in set(expected) | set(comparison_actual)
            if expected.get(key) != comparison_actual.get(key)
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
    if is_v2:
        _validate_rich_database_security(actual)
    return compared_fields


def _validate_rich_database_security(metadata: dict[str, Any]) -> None:
    if metadata.get("metadata_version") != 2:
        raise BackupError("restored database lacks v2 metadata observations")
    revision = metadata.get("alembic_version")
    if not isinstance(revision, str):
        raise BackupError("restored database revision is invalid")
    if metadata.get("database_temp_denied") is not True:
        raise BackupError("restored database TEMP privilege invariant failed")
    functions = metadata.get("security_functions")
    if not isinstance(functions, list):
        raise BackupError("restored function metadata is invalid")
    function_names: set[str] = set()
    for value in functions:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise BackupError("restored function metadata is invalid")
        function_names.add(value["name"])
        proconfig = value.get("proconfig")
        if not isinstance(proconfig, list) or "search_path=pg_catalog" not in proconfig:
            raise BackupError("restored security function search path is not pinned")
    required_functions = set(PHASE_1_FUNCTIONS)
    if revision >= "20260821_0003":
        required_functions.update(PHASE_2_FUNCTIONS)
    if revision >= "20260822_0004":
        required_functions.update(PHASE_3_FUNCTIONS)
    missing_functions = sorted(required_functions - function_names)
    if missing_functions:
        raise BackupError(
            f"restored database lacks required security functions: {', '.join(missing_functions)}"
        )
    triggers = metadata.get("public_triggers")
    if not isinstance(triggers, list) or not triggers:
        raise BackupError("restored trigger metadata is invalid")
    disabled = [
        value.get("name", "invalid")
        for value in triggers
        if not isinstance(value, dict) or value.get("enabled") != "O"
    ]
    if disabled:
        raise BackupError(f"restored database has disabled triggers: {', '.join(disabled)}")
    for field in ("table_grants", "sequence_grants", "function_grants"):
        if not isinstance(metadata.get(field), list):
            raise BackupError(f"restored grant metadata is invalid: {field}")


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
        source_format = cast(str, sidecar["format"])
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
        if source_format == BACKUP_FORMAT_V2:
            expected_metadata_keys.add("artifact_control")
        if (
            set(metadata) != expected_metadata_keys
            or metadata.get("format") != source_format
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
        deployment_quota = _artifact_quota_config(deployment)
        artifact_observation = _artifact_archive_metadata(
            extracted / "artifacts.tar", deployment_quota
        )
        source_artifact_control = metadata.get("artifact_control")
        if source_format == BACKUP_FORMAT_V2:
            if not isinstance(source_artifact_control, dict):
                raise BackupError("encrypted artifact-control metadata is invalid")
            if source_artifact_control != artifact_observation:
                raise BackupError("restored artifact-control metadata differs from backup")

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
            compared_database_fields = _validate_restored_database(
                expected_database, actual_database
            )

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
                "format": RESTORE_REPORT_FORMAT,
                "status": "passed",
                "started_at": started_at.isoformat(),
                "completed_at": _now().isoformat(),
                "backup": backup.name,
                "revision": revision,
                "source_format": "v2" if source_format == BACKUP_FORMAT_V2 else "v1",
                "database": database_name,
                "database_compared_fields": compared_database_fields,
                "source_database_metadata": expected_database,
                "post_restore_database_observations": actual_database,
                "unpaired_database_observation_fields": (
                    []
                    if source_format == BACKUP_FORMAT_V2
                    else sorted(set(actual_database) - set(expected_database))
                ),
                "source_artifact_control": source_artifact_control,
                "post_restore_artifact_observations": artifact_observation,
                "artifact_archive_sha256": metadata["artifact_archive_sha256"],
                "deployment_tree_sha256": metadata["deployment_tree_sha256"],
                "connector_runner_boundary_present": (
                    "connector-runner:"
                    in (deployment / "docker-compose.yml").read_text(encoding="utf-8")
                ),
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
