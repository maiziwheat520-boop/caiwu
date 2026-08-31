"""Fail-closed production runner for the company-report read capability rollout.

The default CLI mode is plan-only. Execution requires an explicit generation
acknowledgement and updates the Core policy, Core generation and Web generation
as one rollback-managed transaction. Policy contents and environment values are
never written to stdout or stderr.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


LEDGER_READ_CAPABILITY = "ledger:read"
CORE_GENERATION_KEY = "LEDGERBRIDGE_INTERNAL_READ_POLICY_GENERATION"
WEB_GENERATION_KEY = "CORE_POLICY_GENERATION"
_POLICY_KEYS = {"version", "certificate_serial", "policy_generation", "principal"}
_PRINCIPAL_KEYS = {
    "principal_ref",
    "san_uri",
    "policy_generation",
    "capabilities",
    "grants",
}


class RolloutError(RuntimeError):
    """A bounded rollout gate failed without exposing private configuration."""


@dataclass(frozen=True)
class ContainerIdentity:
    container_id: str
    image_id: str
    started_at: str
    restart_count: int


@dataclass(frozen=True)
class RolloutConfig:
    policy_path: Path
    core_env_path: Path
    web_env_path: Path
    backup_root: Path
    expected_generation: int
    target_generation: int
    core_compose_paths: tuple[Path, ...]
    web_compose_paths: tuple[Path, ...]
    reader_service: str
    web_service: str
    reader_container: str
    web_container: str

    def __post_init__(self) -> None:
        paths = (
            self.policy_path,
            self.core_env_path,
            self.web_env_path,
            self.backup_root,
            *self.core_compose_paths,
            *self.web_compose_paths,
        )
        if any(not path.is_absolute() for path in paths):
            raise RolloutError("ABSOLUTE_PATH_REQUIRED")
        if not self.core_compose_paths or not self.web_compose_paths:
            raise RolloutError("COMPOSE_FILE_REQUIRED")
        if self.expected_generation < 1 or self.target_generation != self.expected_generation + 1:
            raise RolloutError("GENERATION_TRANSITION_INVALID")
        names = (
            self.reader_service,
            self.web_service,
            self.reader_container,
            self.web_container,
        )
        if any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) is None for name in names):
            raise RolloutError("TARGET_NAME_INVALID")
        if self.reader_container == self.web_container:
            raise RolloutError("TARGET_CONTAINERS_MUST_DIFFER")


class RolloutRuntime(Protocol):
    def snapshot_containers(self) -> dict[str, ContainerIdentity]: ...

    def recreate(self, role: str) -> None: ...

    def wait_healthy(self, role: str) -> None: ...

    def assert_generation(self, role: str, expected: int) -> None: ...


def rollout_plan(config: RolloutConfig) -> tuple[str, ...]:
    """Return a non-sensitive, non-mutating execution order."""

    return (
        f"1. PREFLIGHT generation {config.expected_generation} -> {config.target_generation}; "
        "validate exact policy/env transition and target identities",
        "2. BACKUP policy and Core/Web environment files to a private recovery directory",
        "3. SNAPSHOT all running container identities and freeze the non-target set",
        "4. ATOMIC_FILES replace policy plus Core/Web generation files",
        "5. RECREATE reader only; require healthy state and target generation",
        "6. RECREATE Web only; require healthy state and target generation",
        "7. VERIFY non-target container identities are unchanged",
        "8. COMMIT by retaining the private backup; on any failure, ROLLBACK all three files "
        "and recreate/health-check reader then Web at the prior generation",
    )


def prepare_files(config: RolloutConfig) -> dict[Path, str]:
    """Build the exact three replacement files without mutating the filesystem."""

    policy = _load_policy(config.policy_path)
    if policy.get("policy_generation") != config.expected_generation:
        raise RolloutError("POLICY_GENERATION_MISMATCH")
    principal = policy.get("principal")
    if not isinstance(principal, dict) or set(principal) != _PRINCIPAL_KEYS:
        raise RolloutError("POLICY_SHAPE_INVALID")
    if principal.get("policy_generation") != config.expected_generation:
        raise RolloutError("PRINCIPAL_GENERATION_MISMATCH")
    capabilities = principal.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or any(not isinstance(value, str) or not value for value in capabilities)
        or len(capabilities) != len(set(capabilities))
    ):
        raise RolloutError("POLICY_CAPABILITIES_INVALID")
    if LEDGER_READ_CAPABILITY in capabilities:
        raise RolloutError("LEDGER_READ_ALREADY_PRESENT")

    updated = copy.deepcopy(policy)
    updated["policy_generation"] = config.target_generation
    updated_principal = updated["principal"]
    if not isinstance(updated_principal, dict):
        raise RolloutError("POLICY_SHAPE_INVALID")
    updated_principal["policy_generation"] = config.target_generation
    updated_capabilities = updated_principal["capabilities"]
    if not isinstance(updated_capabilities, list):
        raise RolloutError("POLICY_CAPABILITIES_INVALID")
    updated_capabilities.append(LEDGER_READ_CAPABILITY)

    return {
        config.policy_path: json.dumps(
            updated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        config.core_env_path: _replace_env_generation(
            config.core_env_path,
            CORE_GENERATION_KEY,
            config.expected_generation,
            config.target_generation,
        ),
        config.web_env_path: _replace_env_generation(
            config.web_env_path,
            WEB_GENERATION_KEY,
            config.expected_generation,
            config.target_generation,
        ),
    }


def execute_rollout(
    config: RolloutConfig,
    *,
    runtime: RolloutRuntime,
    emit: Callable[[str], None],
) -> Path:
    """Execute the bounded transaction and automatically roll back on failure."""

    prepared = prepare_files(config)
    originals = {path: _read_regular_file(path) for path in prepared}
    before = runtime.snapshot_containers()
    _require_targets(before, config)
    backup_dir = _create_backup(config, originals)
    emit(f"BACKUP_READY generation {config.expected_generation}")

    try:
        for path, content in prepared.items():
            _atomic_replace(path, content.encode("utf-8"))
        emit(f"FILES_READY generation {config.target_generation}")

        runtime.recreate("reader")
        runtime.wait_healthy("reader")
        runtime.assert_generation("reader", config.target_generation)
        emit(f"READER_HEALTHY generation {config.target_generation}")

        runtime.recreate("web")
        runtime.wait_healthy("web")
        runtime.assert_generation("web", config.target_generation)
        emit(f"WEB_HEALTHY generation {config.target_generation}")

        after = runtime.snapshot_containers()
        _assert_target_recreated(before, after, config)
        _assert_non_target_unchanged(before, after, config)
        emit("NON_TARGET_IDENTITIES_STABLE")
        emit(f"ROLLOUT_COMMITTED generation {config.target_generation}")
        return backup_dir
    except Exception as error:
        original_error = (
            error if isinstance(error, RolloutError) else RolloutError("ROLLOUT_FAILED")
        )
        emit("ROLLBACK_STARTED")
        try:
            for path, content in originals.items():
                _atomic_replace(path, content)
            runtime.recreate("reader")
            runtime.wait_healthy("reader")
            runtime.assert_generation("reader", config.expected_generation)
            runtime.recreate("web")
            runtime.wait_healthy("web")
            runtime.assert_generation("web", config.expected_generation)
            rolled_back = runtime.snapshot_containers()
            _assert_non_target_unchanged(before, rolled_back, config)
            emit(f"ROLLBACK_HEALTHY generation {config.expected_generation}")
        except Exception as rollback_error:
            rollback_code = (
                str(rollback_error)
                if isinstance(rollback_error, RolloutError)
                else "ROLLBACK_FAILED"
            )
            raise RolloutError(f"{original_error};{rollback_code}") from error
        raise original_error from error


class DockerComposeRuntime:
    """Production adapter. Command output is captured and never forwarded."""

    def __init__(self, config: RolloutConfig, *, health_timeout_seconds: int = 60) -> None:
        self.config = config
        self.health_timeout_seconds = health_timeout_seconds

    def snapshot_containers(self) -> dict[str, ContainerIdentity]:
        output = self._run(
            (
                "docker",
                "inspect",
                "--format",
                "{{.Name}}|{{.Id}}|{{.Image}}|{{.State.StartedAt}}|{{.RestartCount}}",
                *self._running_container_ids(),
            ),
            "CONTAINER_SNAPSHOT_FAILED",
        )
        result: dict[str, ContainerIdentity] = {}
        for line in output.splitlines():
            parts = line.split("|")
            if len(parts) != 5:
                raise RolloutError("CONTAINER_SNAPSHOT_INVALID")
            name, container_id, image_id, started_at, restart_count = parts
            if not name.startswith("/") or not restart_count.isdigit():
                raise RolloutError("CONTAINER_SNAPSHOT_INVALID")
            result[name[1:]] = ContainerIdentity(
                container_id=container_id,
                image_id=image_id,
                started_at=started_at,
                restart_count=int(restart_count),
            )
        return result

    def recreate(self, role: str) -> None:
        self._run(
            compose_recreate_command(self.config, role),
            f"{role.upper()}_RECREATE_FAILED",
        )

    def wait_healthy(self, role: str) -> None:
        container = self._container_for(role)
        deadline = time.monotonic() + self.health_timeout_seconds
        while time.monotonic() < deadline:
            status = self._run(
                ("docker", "inspect", "--format", "{{.State.Health.Status}}", container),
                f"{role.upper()}_HEALTH_INSPECT_FAILED",
            ).strip()
            if status == "healthy":
                return
            if status not in {"starting", "unhealthy"}:
                raise RolloutError(f"{role.upper()}_HEALTH_INVALID")
            time.sleep(2)
        raise RolloutError(f"{role.upper()}_HEALTH_FAILED")

    def assert_generation(self, role: str, expected: int) -> None:
        container = self._container_for(role)
        key = CORE_GENERATION_KEY if role == "reader" else WEB_GENERATION_KEY
        output = self._run(
            (
                "docker",
                "inspect",
                "--format",
                "{{range .Config.Env}}{{println .}}{{end}}",
                container,
            ),
            f"{role.upper()}_GENERATION_INSPECT_FAILED",
        )
        matches = [line for line in output.splitlines() if line.startswith(f"{key}=")]
        if matches != [f"{key}={expected}"]:
            raise RolloutError(f"{role.upper()}_GENERATION_MISMATCH")

    def _container_for(self, role: str) -> str:
        if role == "reader":
            return self.config.reader_container
        if role == "web":
            return self.config.web_container
        raise RolloutError("TARGET_ROLE_INVALID")

    def _running_container_ids(self) -> tuple[str, ...]:
        output = self._run(
            ("docker", "ps", "--quiet", "--no-trunc"),
            "CONTAINER_LIST_FAILED",
        )
        values = tuple(line for line in output.splitlines() if line)
        if not values:
            raise RolloutError("NO_RUNNING_CONTAINERS")
        return values

    @staticmethod
    def _run(command: Sequence[str], error_code: str) -> str:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RolloutError(error_code) from error
        if completed.returncode != 0:
            raise RolloutError(error_code)
        return completed.stdout


def _load_policy(path: Path) -> dict[str, object]:
    content = _read_regular_file(path)
    if not 2 <= len(content) <= 64 * 1024:
        raise RolloutError("POLICY_SIZE_INVALID")
    try:
        payload = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RolloutError("POLICY_JSON_INVALID") from error
    if not isinstance(payload, dict) or set(payload) != _POLICY_KEYS:
        raise RolloutError("POLICY_SHAPE_INVALID")
    if payload.get("version") != "ledgerbridge.mtls-workload-policy.v1":
        raise RolloutError("POLICY_VERSION_INVALID")
    return payload


def compose_recreate_command(config: RolloutConfig, role: str) -> tuple[str, ...]:
    if role == "reader":
        compose_paths = config.core_compose_paths
        env_path = config.core_env_path
        service = config.reader_service
    elif role == "web":
        compose_paths = config.web_compose_paths
        env_path = config.web_env_path
        service = config.web_service
    else:
        raise RolloutError("TARGET_ROLE_INVALID")
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(compose_paths[0].parent),
        "--env-file",
        str(env_path),
    ]
    for path in compose_paths:
        command.extend(("-f", str(path)))
    command.extend(("up", "-d", "--no-deps", "--force-recreate", service))
    return tuple(command)


def _replace_env_generation(
    path: Path,
    key: str,
    expected: int,
    target: int,
) -> str:
    try:
        content = _read_regular_file(path).decode("utf-8")
    except UnicodeDecodeError as error:
        raise RolloutError("ENV_ENCODING_INVALID") from error
    pattern = re.compile(rf"(?m)^{re.escape(key)}=([^\r\n]*)(\r?)$")
    matches = [match.group(1) for match in pattern.finditer(content)]
    if matches != [str(expected)]:
        raise RolloutError("ENV_GENERATION_MISMATCH")
    return pattern.sub(
        lambda match: f"{key}={target}{match.group(2)}",
        content,
        count=1,
    )


def _read_regular_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RolloutError("INPUT_FILE_UNAVAILABLE") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise RolloutError("INPUT_FILE_UNSAFE")
    try:
        return path.read_bytes()
    except OSError as error:
        raise RolloutError("INPUT_FILE_UNAVAILABLE") from error


def _create_backup(config: RolloutConfig, originals: Mapping[Path, bytes]) -> Path:
    config.backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if config.backup_root.is_symlink() or not config.backup_root.is_dir():
        raise RolloutError("BACKUP_ROOT_UNSAFE")
    backup_root_metadata = config.backup_root.stat()
    if os.name == "posix" and (
        stat.S_IMODE(backup_root_metadata.st_mode) & 0o077
        or backup_root_metadata.st_uid != os.geteuid()
    ):
        raise RolloutError("BACKUP_ROOT_PERMISSIONS_INVALID")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = config.backup_root / f"company-report-policy-{stamp}-{secrets.token_hex(4)}"
    backup_dir.mkdir(mode=0o700)
    roles = {
        config.policy_path: "policy.json",
        config.core_env_path: "core.env",
        config.web_env_path: "web.env",
    }
    manifest: dict[str, object] = {
        "version": "ledgerbridge.company-report-policy-backup.v1",
        "expected_generation": config.expected_generation,
        "target_generation": config.target_generation,
        "files": [],
    }
    for source, content in originals.items():
        destination = backup_dir / roles[source]
        destination.write_bytes(content)
        destination.chmod(0o600)
        manifest["files"].append(  # type: ignore[union-attr]
            {
                "role": roles[source],
                "sha256": hashlib.sha256(content).hexdigest(),
                "byte_size": len(content),
            }
        )
    manifest_path = backup_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    return backup_dir


def _atomic_replace(path: Path, content: bytes) -> None:
    original = path.lstat()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(original.st_mode))
        if hasattr(os, "chown"):
            os.chown(temporary, original.st_uid, original.st_gid)
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _require_targets(
    identities: Mapping[str, ContainerIdentity],
    config: RolloutConfig,
) -> None:
    if config.reader_container not in identities or config.web_container not in identities:
        raise RolloutError("TARGET_CONTAINER_MISSING")


def _assert_target_recreated(
    before: Mapping[str, ContainerIdentity],
    after: Mapping[str, ContainerIdentity],
    config: RolloutConfig,
) -> None:
    _require_targets(after, config)
    for name in (config.reader_container, config.web_container):
        if before[name] == after[name]:
            raise RolloutError("TARGET_CONTAINER_NOT_RECREATED")


def _assert_non_target_unchanged(
    before: Mapping[str, ContainerIdentity],
    after: Mapping[str, ContainerIdentity],
    config: RolloutConfig,
) -> None:
    targets = {config.reader_container, config.web_container}
    before_non_target = {name: value for name, value in before.items() if name not in targets}
    after_non_target = {name: value for name, value in after.items() if name not in targets}
    if before_non_target != after_non_target:
        raise RolloutError("NON_TARGET_CONTAINER_DRIFT")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name != "posix":
            raise RolloutError("POSIX_EXECUTION_REQUIRED")
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RolloutError("ROLLOUT_ALREADY_RUNNING") from error
        yield
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan or execute the company-report policy rollout"
    )
    parser.add_argument("--mode", choices=("plan", "execute"), default="plan")
    parser.add_argument("--policy-path", type=Path, required=True)
    parser.add_argument("--core-env-path", type=Path, required=True)
    parser.add_argument("--web-env-path", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--expected-generation", type=int, required=True)
    parser.add_argument("--target-generation", type=int, required=True)
    parser.add_argument("--core-compose-path", type=Path, action="append", required=True)
    parser.add_argument("--web-compose-path", type=Path, action="append", required=True)
    parser.add_argument("--reader-service", required=True)
    parser.add_argument("--web-service", required=True)
    parser.add_argument("--reader-container", required=True)
    parser.add_argument("--web-container", required=True)
    parser.add_argument("--acknowledge-generation")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        config = RolloutConfig(
            policy_path=arguments.policy_path.resolve(),
            core_env_path=arguments.core_env_path.resolve(),
            web_env_path=arguments.web_env_path.resolve(),
            backup_root=arguments.backup_root.resolve(),
            expected_generation=arguments.expected_generation,
            target_generation=arguments.target_generation,
            core_compose_paths=tuple(path.resolve() for path in arguments.core_compose_path),
            web_compose_paths=tuple(path.resolve() for path in arguments.web_compose_path),
            reader_service=arguments.reader_service,
            web_service=arguments.web_service,
            reader_container=arguments.reader_container,
            web_container=arguments.web_container,
        )
        prepare_files(config)
        if arguments.mode == "plan":
            for step in rollout_plan(config):
                print(step)
            return 0
        acknowledgement = f"{config.expected_generation}-to-{config.target_generation}"
        if arguments.acknowledge_generation != acknowledgement:
            raise RolloutError("EXECUTION_ACKNOWLEDGEMENT_REQUIRED")
        if os.name == "posix" and os.geteuid() != 0:
            raise RolloutError("ROOT_REQUIRED")
        runtime = DockerComposeRuntime(config)
        with _exclusive_lock(config.backup_root / ".company-report-policy.lock"):
            execute_rollout(config, runtime=runtime, emit=print)
        return 0
    except RolloutError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
