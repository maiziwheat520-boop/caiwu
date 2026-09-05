"""The deploy script's refusals, which are the part that protects production.

A release tool is judged by what it declines to do. These pin the preconditions
against a real temporary repository and a stub host, so nothing here touches a
network or a container.
"""

from __future__ import annotations

import subprocess  # nosec B404
from pathlib import Path, PurePosixPath

import pytest

from scripts.deploy_core import (
    DeploymentFailed,
    DeploymentRefused,
    HostFacts,
    deploy,
    read_host_facts,
)

COMPOSE_LABEL = (
    "/srv/ai-center/ledgerbridge/docker-compose.yml,"
    "/srv/ai-center/ledgerbridge/docker-compose.secure-storage.yml,"
    "/srv/ai-center/ledgerbridge/docker-compose.core-review.yml"
)
ROOT = PurePosixPath("/srv/ai-center/ledgerbridge")
BACKUPS = PurePosixPath("/srv/ai-center/backups/ledgerbridge")


class _StubHost:
    """Answers the two questions the script asks before it changes anything."""

    def __init__(self, *, revision: str, compose: str = COMPOSE_LABEL) -> None:
        self.revision = revision
        self.compose = compose
        self.commands: list[tuple[str, ...]] = []

    def run(self, *arguments: str, sudo: bool = True) -> str:
        self.commands.append(arguments)
        if arguments[0] == "cat" and arguments[1].endswith("DEPLOYED_REVISION"):
            return self.revision
        if arguments[0] == "docker":
            return self.compose
        return ""

    def shell(self, script: str) -> str:
        self.commands.append(("shell", script))
        return ""

    def upload(self, local: Path, remote: PurePosixPath, *, identity: Path | None = None) -> None:
        self.commands.append(("upload", str(local), str(remote)))


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(  # nosec B603 B607
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A two-commit repository standing in for the release worktree."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "test")
    (root / "first.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "first")
    (root / "second.txt").write_text("two\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "second")
    return root


def _revisions(repository: Path) -> tuple[str, str]:
    listing = _git(repository, "log", "--format=%H").splitlines()
    return listing[1], listing[0]


def _deploy(repository: Path, host: _StubHost, **overrides: object) -> int:
    arguments: dict[str, object] = {
        "repository": repository,
        "host": host,
        "root": ROOT,
        "backup_root": BACKUPS,
        "services": ("api",),
        "identity": None,
        "dry_run": True,
        "skip_gates": True,
    }
    arguments.update(overrides)
    return deploy(**arguments)  # type: ignore[arg-type]


def test_a_release_that_would_overwrite_newer_production_is_refused(repository: Path) -> None:
    # The host is ahead of this tree, which is exactly the case where deploying
    # would silently revert someone else's live work.
    older, newer = _revisions(repository)
    _git(repository, "checkout", "--quiet", older)
    host = _StubHost(revision=newer)

    with pytest.raises(DeploymentRefused, match="not an ancestor"):
        _deploy(repository, host)


def test_a_revision_already_deployed_is_refused(repository: Path) -> None:
    _, head = _revisions(repository)
    host = _StubHost(revision=head)

    with pytest.raises(DeploymentRefused, match="already deployed"):
        _deploy(repository, host)


def test_uncommitted_changes_are_refused(repository: Path) -> None:
    older, _ = _revisions(repository)
    (repository / "second.txt").write_text("edited\n", encoding="utf-8")
    host = _StubHost(revision=older)

    with pytest.raises(DeploymentRefused, match="uncommitted changes"):
        _deploy(repository, host)


def test_a_release_that_deletes_a_file_is_refused(repository: Path) -> None:
    # An automatic rollback cannot be trusted to restore something it removed,
    # so a deletion is handed back to a person rather than attempted.
    older, _ = _revisions(repository)
    _git(repository, "rm", "--quiet", "first.txt")
    _git(repository, "commit", "--quiet", "-m", "remove")
    host = _StubHost(revision=older)

    with pytest.raises(DeploymentRefused, match="deletes files"):
        _deploy(repository, host)


def test_a_dry_run_reads_the_host_but_changes_nothing(repository: Path) -> None:
    older, _ = _revisions(repository)
    host = _StubHost(revision=older)

    assert _deploy(repository, host) == 0
    assert all(command[0] not in {"upload", "shell", "install"} for command in host.commands), (
        "a dry run must not write to the host"
    )


def test_the_compose_file_set_is_read_from_the_running_container(repository: Path) -> None:
    # The host layers several compose files; using only the base one would make
    # the other files' containers look like orphans.
    host = _StubHost(revision="a" * 40)
    facts = read_host_facts(host, root=ROOT, service="api")  # type: ignore[arg-type]

    assert facts.deployed_revision == "a" * 40
    assert len(facts.compose_files) == 3
    assert facts.compose_files[0].endswith("docker-compose.yml")


def test_a_host_without_a_usable_revision_is_refused() -> None:
    host = _StubHost(revision="not-a-revision")

    with pytest.raises(DeploymentRefused, match="unusable deployed revision"):
        read_host_facts(host, root=ROOT, service="api")  # type: ignore[arg-type]


def test_a_container_that_reports_no_compose_files_is_refused() -> None:
    host = _StubHost(revision="a" * 40, compose="")

    with pytest.raises(DeploymentRefused, match="compose files"):
        read_host_facts(host, root=ROOT, service="api")  # type: ignore[arg-type]


def test_an_identical_tree_is_refused(repository: Path) -> None:
    # An empty diff means the revision markers would move with no code behind
    # them, which is worse than doing nothing.
    _, head = _revisions(repository)
    _git(repository, "commit", "--quiet", "--allow-empty", "-m", "empty")
    host = _StubHost(revision=head)

    with pytest.raises(DeploymentRefused, match="no files differ"):
        _deploy(repository, host)


def test_a_failure_after_the_host_is_touched_raises_deployment_failed(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    older, _ = _revisions(repository)

    class _BreakingHost(_StubHost):
        def shell(self, script: str) -> str:
            self.commands.append(("shell", script))
            if "tar" in script:
                return ""
            raise subprocess.CalledProcessError(1, ("remote",), output="disk full")

    host = _BreakingHost(revision=older)
    with pytest.raises(DeploymentFailed):
        _deploy(repository, host, dry_run=False)

    assert any("tar" in str(command) for command in host.commands), "a backup must be taken first"


def test_host_facts_are_immutable() -> None:
    facts = HostFacts(deployed_revision="a" * 40, compose_files=("one.yml",))

    with pytest.raises(AttributeError):
        facts.deployed_revision = "b" * 40  # type: ignore[misc]
