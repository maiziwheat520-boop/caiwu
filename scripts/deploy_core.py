"""Deploy a reviewed Core revision to the LedgerBridge host in one bounded step.

Releases used to be assembled by hand: back up, copy the changed files, rewrite
the revision markers, rebuild the manifest, build the image, restart the right
services, check health. The steps are not hard, but they were rediscovered on
every release -- which compose files the host actually runs, which services live
in which of them, what the revision variables are called -- and the release lock
is a single global lock, so every minute spent rediscovering them is a minute
the next task waits.

Two facts are therefore read from the host rather than written down here: the
deployed revision, and the compose file set the running containers were created
from. Anything this script assumes about the host is something the host can
contradict.

The preflight is the point. A release that would overwrite a newer production
revision, or ship a dirty tree, or ship code whose gates have not run, is
refused before anything on the host is touched. Once the host is touched, every
failure rolls back to the revision that was live when the script started.

Usage:
    uv run --frozen --extra dev python scripts/deploy_core.py --host user@address
    uv run --frozen --extra dev python scripts/deploy_core.py --host ... --dry-run
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess  # nosec B404
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
DEFAULT_REMOTE_ROOT = PurePosixPath("/srv/ai-center/ledgerbridge")
DEFAULT_BACKUP_ROOT = PurePosixPath("/srv/ai-center/backups/ledgerbridge")
DEFAULT_SERVICES = ("api", "worker", "internal-reader")
HEALTH_TIMEOUT_SECONDS = 180
HEALTH_POLL_SECONDS = 5

# Everything CI runs that a developer machine can actually reproduce. Kept as
# one list so a release cannot quietly run a convenient subset: the reason to
# skip them is always "they already ran on this tree", never "these few will do".
#
# Three of CI's gates are deliberately absent because they cannot pass here, and
# a gate that always fails is a gate everyone learns to skip:
#
#   mypy src alembic tests scripts -- `rapidocr` lives in the `ocr` extra that
#     CI does not install, and those four paths make mypy see every test module
#     under two names. `uv run mypy` (the packages this project configures) does
#     complete, but reports four errors that predate any current work.
#   --cov-fail-under=90 -- coverage here is 79%. The missing eleven points are
#     the ~214 tests that skip without PostgreSQL.
#   alembic upgrade/downgrade/upgrade -- needs PostgreSQL for the same reason.
#
# So passing these three does not mean CI will pass; it means nothing this
# machine can check is broken. Closing the gap means giving the developer
# machine a PostgreSQL to test against and making one mypy invocation work in
# both places. Until then, saying so out loud beats a green light that means
# nothing.
GATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ruff check", ("ruff", "check", ".")),
    ("ruff format", ("ruff", "format", "--check", ".")),
    ("pytest", ("pytest", "-q")),
)


class DeploymentRefused(RuntimeError):
    """A precondition failed. Nothing on the host has been touched."""


class DeploymentFailed(RuntimeError):
    """A step failed after the host was touched. A rollback was attempted."""


@dataclass(frozen=True, slots=True)
class HostFacts:
    """What the host says about itself, rather than what we assume."""

    deployed_revision: str
    compose_files: tuple[str, ...]


def _run(command: tuple[str, ...], *, cwd: Path | None = None) -> str:
    # The encoding is pinned. Left to the platform default this decodes remote
    # output as GBK on this machine, and a single non-GBK byte in a container's
    # log raises inside subprocess's reader thread -- which would swallow the
    # error text of a failing step just when it is needed most.
    result = subprocess.run(  # nosec B603
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise subprocess.CalledProcessError(result.returncode, command, output=detail)
    return result.stdout.strip()


class Host:
    """One SSH endpoint, addressed only through whole commands.

    Remote commands are assembled as argument lists and quoted here, so a
    revision or path can never become shell syntax on the far side.
    """

    def __init__(self, target: str, *, identity: Path | None = None) -> None:
        self._prefix: tuple[str, ...] = ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
        if identity is not None:
            self._prefix += ("-i", str(identity))
        self._prefix += (target,)
        self._target = target

    def run(self, *arguments: str, sudo: bool = True) -> str:
        parts = ("sudo", *arguments) if sudo else arguments
        return _run((*self._prefix, shlex.join(parts)))

    def shell(self, script: str) -> str:
        """Run a prepared script. Callers must quote every value they interpolate."""
        return _run((*self._prefix, script))

    def upload(self, local: Path, remote: PurePosixPath, *, identity: Path | None = None) -> None:
        command: tuple[str, ...] = ("scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15")
        if identity is not None:
            command += ("-i", str(identity))
        command += (str(local), f"{self._target}:{remote}")
        _run(command)


def _local_revision(repository: Path) -> str:
    revision = _run(("git", "rev-parse", "HEAD"), cwd=repository)
    if REVISION_PATTERN.fullmatch(revision) is None:
        raise DeploymentRefused("HEAD is not a full 40-character Git SHA")
    return revision


def _require_clean_tree(repository: Path) -> None:
    if _run(("git", "status", "--porcelain"), cwd=repository):
        raise DeploymentRefused(
            "the working tree has uncommitted changes; commit or stash before deploying"
        )


def _changed_files(repository: Path, base: str, head: str) -> tuple[str, ...]:
    listing = _run(("git", "diff", "--name-only", f"{base}..{head}"), cwd=repository)
    return tuple(line for line in listing.splitlines() if line)


def read_host_facts(host: Host, *, root: PurePosixPath, service: str) -> HostFacts:
    """Ask the host what it is running, instead of assuming.

    The compose file set matters: the host layers several overrides, and a
    release that used only the base file would treat the others' containers as
    orphans.
    """
    revision = host.run("cat", str(root / "DEPLOYED_REVISION")).strip()
    if REVISION_PATTERN.fullmatch(revision) is None:
        raise DeploymentRefused(f"host reports an unusable deployed revision: {revision!r}")

    container = f"ledgerbridge-{service}-1"
    listed = host.run(
        "docker",
        "inspect",
        container,
        "--format",
        '{{index .Config.Labels "com.docker.compose.project.config_files"}}',
    ).strip()
    files = tuple(entry.strip() for entry in listed.split(",") if entry.strip())
    if not files:
        raise DeploymentRefused(
            f"{container} does not report the compose files it was created from"
        )
    return HostFacts(deployed_revision=revision, compose_files=files)


def _require_not_behind(repository: Path, deployed: str, head: str) -> None:
    """Refuse to let an older branch overwrite a newer production revision."""
    if deployed == head:
        raise DeploymentRefused("this revision is already deployed")
    result = subprocess.run(  # nosec B603 B607
        ["git", "merge-base", "--is-ancestor", deployed, head],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise DeploymentRefused(
            f"production is at {deployed[:7]}, which is not an ancestor of {head[:7]}; "
            "rebase onto the live revision and re-run the gates before deploying"
        )


def run_gates(repository: Path) -> None:
    """Run what CI runs, in the environment CI uses."""
    for name, command in GATES:
        print(f"  gate: {name} ... ", end="", flush=True)
        started = time.monotonic()
        try:
            _run(("uv", "run", "--frozen", "--extra", "dev", *command), cwd=repository)
        except subprocess.CalledProcessError as error:
            print("FAILED")
            raise DeploymentRefused(f"gate {name!r} failed:\n{error.output}") from error
        print(f"ok ({time.monotonic() - started:.1f}s)")


def _compose(host: Host, facts: HostFacts, root: PurePosixPath, *arguments: str) -> str:
    files: tuple[str, ...] = ()
    for path in facts.compose_files:
        files += ("-f", path)
    return host.shell(
        shlex.join(("cd", str(root)))
        + " && "
        + shlex.join(("sudo", "docker", "compose", *files, *arguments))
    )


def _health(host: Host, containers: tuple[str, ...]) -> dict[str, str]:
    states: dict[str, str] = {}
    for container in containers:
        states[container] = host.run(
            "docker",
            "inspect",
            container,
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
        ).strip()
    return states


def _await_health(host: Host, services: tuple[str, ...]) -> dict[str, str]:
    containers = tuple(f"ledgerbridge-{service}-1" for service in services)
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    states: dict[str, str] = {}
    while time.monotonic() < deadline:
        states = _health(host, containers)
        if all(state in {"healthy", "running"} for state in states.values()):
            return states
        if any(state in {"unhealthy", "exited", "dead"} for state in states.values()):
            break
        time.sleep(HEALTH_POLL_SECONDS)
    raise DeploymentFailed(f"services did not become healthy: {states}")


def _set_revision(host: Host, root: PurePosixPath, revision: str) -> None:
    env = shlex.quote(str(root / ".env"))
    for key in ("LEDGERBRIDGE_REVISION", "DEPLOYED_REVISION"):
        # The .env file holds credentials; it is edited in place and never read back.
        host.shell(f"sudo sed -i {shlex.quote(f's/^{key}=.*/{key}={revision}/')} {env}")
    # shlex.join quotes the format string, so the newline survives Python, this
    # script's own quoting, and the remote shell. Hand-escaping it does not:
    # the first release this script ran wrote a literal "n" onto the end of the
    # revision, and the check below is what caught it.
    host.shell(
        shlex.join(("printf", "%s\n", revision))
        + " | "
        + shlex.join(("sudo", "tee", str(root / "DEPLOYED_REVISION")))
        + " > /dev/null"
    )
    written = host.run("cat", str(root / "DEPLOYED_REVISION")).strip()
    if written != revision:
        raise DeploymentFailed(f"revision marker did not take: {written!r}")


def _restore_files(
    host: Host,
    repository: Path,
    root: PurePosixPath,
    revision: str,
    changed: tuple[str, ...],
    staging: Path,
) -> None:
    """Put back the file contents that were live at `revision`.

    Restarting the old image is not on its own a rollback: the host's source
    tree would keep the new files, so its manifest would no longer describe
    what is deployed, and the next release would compute its diff against a
    tree nobody chose.

    A file the release added does not exist at `revision`, so putting it back
    means removing it. That is safe in a way deleting during a release is not:
    the only thing removed is what this run just uploaded.
    """
    for name in changed:
        previous = subprocess.run(  # nosec B603 B607
            ["git", "show", f"{revision}:{name}"],
            cwd=repository,
            capture_output=True,
            check=False,
        )
        if previous.returncode != 0:
            host.run("rm", "-f", str(root / name))
            continue
        local = staging / Path(name).name
        local.write_bytes(previous.stdout)
        remote = PurePosixPath("/tmp") / f"lb-rollback-{Path(name).name}"  # nosec B108
        host.upload(local, remote)
        host.run("install", "-o", "root", "-g", "root", "-m", "644", str(remote), str(root / name))
        host.run("rm", "-f", str(remote))


def _rollback(
    host: Host,
    facts: HostFacts,
    root: PurePosixPath,
    services: tuple[str, ...],
    *,
    repository: Path,
    changed: tuple[str, ...],
    staging: Path,
) -> str:
    """Return the host to the revision that was live when this run started."""
    try:
        _restore_files(host, repository, root, facts.deployed_revision, changed, staging)
        _set_revision(host, root, facts.deployed_revision)
        _compose(host, facts, root, "up", "-d", *services)
        states = _await_health(host, services)
        return f"rolled back to {facts.deployed_revision[:7]}; services {states}"
    except (subprocess.CalledProcessError, DeploymentFailed, OSError) as error:
        return (
            f"ROLLBACK FAILED ({error}); the host is between revisions and needs a person: "
            f"restore {DEFAULT_BACKUP_ROOT} and restart from {facts.deployed_revision[:7]}"
        )


def deploy(
    *,
    repository: Path,
    host: Host,
    root: PurePosixPath,
    backup_root: PurePosixPath,
    services: tuple[str, ...],
    identity: Path | None,
    dry_run: bool,
    skip_gates: bool,
) -> int:
    head = _local_revision(repository)
    _require_clean_tree(repository)

    print(f"local revision  {head}")
    facts = read_host_facts(host, root=root, service=services[0])
    print(f"host revision   {facts.deployed_revision}")
    print(f"compose files   {len(facts.compose_files)} layered")

    _require_not_behind(repository, facts.deployed_revision, head)
    changed = _changed_files(repository, facts.deployed_revision, head)
    if not changed:
        raise DeploymentRefused("no files differ from the deployed revision")
    print(f"changed files   {len(changed)}")
    for name in changed:
        print(f"                {name}")

    missing = [name for name in changed if not (repository / name).is_file()]
    if missing:
        raise DeploymentRefused(
            "this release deletes files, which this script does not do; "
            f"deploy by hand: {', '.join(missing)}"
        )

    if skip_gates:
        print("gates           SKIPPED at the caller's request")
    else:
        print("gates")
        run_gates(repository)

    if dry_run:
        print("\ndry run: the host was read but not changed")
        return 0

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / f"predeploy-{stamp}-{facts.deployed_revision[:12]}.tar.gz"
    print(f"backup          {backup}")
    host.shell(
        shlex.join(
            (
                "sudo",
                "tar",
                "-czf",
                str(backup),
                "-C",
                str(root.parent),
                root.name,
            )
        )
    )

    try:
        print("upload          ", end="", flush=True)
        for name in changed:
            staged = PurePosixPath("/tmp") / f"lb-deploy-{stamp}-{Path(name).name}"  # nosec B108
            host.upload(repository / name, staged, identity=identity)
            host.run(
                "install", "-o", "root", "-g", "root", "-m", "644", str(staged), str(root / name)
            )
            host.run("rm", "-f", str(staged))
        print(f"{len(changed)} files")

        print(f"revision        {head[:12]}")
        _set_revision(host, root, head)

        print("manifest        ", end="", flush=True)
        host.shell(
            shlex.join(("cd", str(root)))
            + " && "
            + shlex.join(
                ("sudo", "python3", "scripts/deployment_manifest.py", "create", "--revision", head)
            )
        )
        verified = host.shell(
            shlex.join(("cd", str(root)))
            + " && "
            + shlex.join(("sudo", "python3", "scripts/deployment_manifest.py", "verify"))
        )
        print(verified.strip())

        print("build           ", end="", flush=True)
        _compose(host, facts, root, "build", services[0])
        print("done")

        print("switch          ", end="", flush=True)
        _compose(host, facts, root, "up", "-d", *services)
        print("done")

        print("health          ", end="", flush=True)
        states = _await_health(host, services)
        print(", ".join(f"{name.split('-')[1]}={state}" for name, state in states.items()))

    except (subprocess.CalledProcessError, DeploymentFailed, OSError) as error:
        detail = getattr(error, "output", None) or str(error)
        print(f"\nFAILED: {detail}", file=sys.stderr)
        with tempfile.TemporaryDirectory() as scratch:
            print(
                _rollback(
                    host,
                    facts,
                    root,
                    services,
                    repository=repository,
                    changed=changed,
                    staging=Path(scratch),
                ),
                file=sys.stderr,
            )
        print(f"pre-deploy backup: {backup}", file=sys.stderr)
        raise DeploymentFailed(str(detail)) from error

    print(f"\ndeployed {head}")
    print(f"rollback point: image ledgerbridge-app:{facts.deployed_revision} (still on the host)")
    print(f"pre-deploy backup: {backup}")
    print("remember: push production/core, record the release, then release the lock")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", required=True, help="SSH target, e.g. user@address")
    parser.add_argument("--identity", type=Path, default=None, help="SSH private key file")
    parser.add_argument("--repository", type=Path, default=Path.cwd(), help="repository to deploy")
    parser.add_argument("--root", type=PurePosixPath, default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--backup-root", type=PurePosixPath, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument(
        "--service",
        action="append",
        dest="services",
        default=None,
        help=f"service to restart; repeatable (default: {', '.join(DEFAULT_SERVICES)})",
    )
    parser.add_argument("--dry-run", action="store_true", help="read the host, change nothing")
    parser.add_argument(
        "--skip-gates",
        action="store_true",
        help="only when the gates already ran on this exact tree",
    )
    arguments = parser.parse_args(argv)

    try:
        return deploy(
            repository=arguments.repository.resolve(),
            host=Host(arguments.host, identity=arguments.identity),
            root=arguments.root,
            backup_root=arguments.backup_root,
            services=tuple(arguments.services or DEFAULT_SERVICES),
            identity=arguments.identity,
            dry_run=arguments.dry_run,
            skip_gates=arguments.skip_gates,
        )
    except DeploymentRefused as error:
        print(f"refused: {error}", file=sys.stderr)
        return 2
    except DeploymentFailed:
        return 1
    except subprocess.CalledProcessError as error:
        print(f"command failed: {error.output or error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
