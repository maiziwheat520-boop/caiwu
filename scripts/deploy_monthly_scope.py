"""Atomic, rollback-managed policy/Web activation under the external release lock.

Core code/schema and the financial database are not modified. The Web release
uses the existing reviewed helper; this wrapper also preserves both generation
consumers. Run only on the authorized host after encrypted backup/restore gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
import time
from contextlib import suppress
from pathlib import Path

CORE = Path("/srv/ai-center/ledgerbridge")
WEB = Path("/home/aiadmin/services/ledgerbridge-web-auth-preview")
POLICY = Path("/srv/ledgerbridge-secure/core-review/policy.json")
CORE_REVISION = "e6ec44c7cf2ff8784009551762d9390c7d2acf65"
OLD_WEB = "5ce6a074ac56715b6997ed17392108f82428fb53"


def command(args: list[str], *, cwd: Path = CORE, data: bytes | None = None) -> bytes:
    result = subprocess.run(args, cwd=cwd, input=data, capture_output=True, check=False)  # nosec B603
    if result.returncode:
        raise RuntimeError("ACTIVATION_COMMAND_FAILED")
    return result.stdout


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replace(path: Path, content: bytes) -> None:
    target = path.with_name(path.name + ".monthly-staged")
    descriptor = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(path, target)
        owner = path.stat()
        chown = getattr(os, "chown", None)
        if not callable(chown):
            raise RuntimeError("POSIX_HOST_REQUIRED")
        chown(target, owner.st_uid, owner.st_gid)
        os.replace(target, path)
    finally:
        if target.exists():
            target.unlink()


def health(name: str) -> None:
    for _ in range(45):
        state = json.loads(command(["docker", "inspect", name]))[0]["State"]
        if state.get("Health", {}).get("Status") == "healthy":
            return
        time.sleep(1)
    raise RuntimeError("ACTIVATION_HEALTH_FAILED")


def recreate_core(compose: list[str]) -> None:
    command(
        [
            "docker",
            "compose",
            *compose,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "internal-reader",
        ]
    )
    health("ledgerbridge-internal-reader-1")


def activate(args: argparse.Namespace) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", args.web_revision):
        raise ValueError("REVISION_INVALID")
    if (CORE / "DEPLOYED_REVISION").read_text().strip() != CORE_REVISION:
        raise ValueError("CORE_BASELINE_CHANGED")
    if (WEB / "REVISION").read_text().strip() != OLD_WEB:
        raise ValueError("WEB_BASELINE_CHANGED")
    stage = args.stage.resolve(strict=True)
    acceptance = stage / "acceptance.json"
    if digest(acceptance) != args.acceptance_sha256:
        raise ValueError("ACCEPTANCE_HASH_CHANGED")
    expected = json.loads(acceptance.read_bytes())
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", expected["month"]):
        raise ValueError("ACCEPTANCE_MONTH_INVALID")
    candidate = stage / "policy-generation-12.json"
    if digest(candidate) != args.policy_sha256:
        raise ValueError("POLICY_HASH_CHANGED")
    if json.loads(POLICY.read_bytes())["policy_generation"] != 11:
        raise ValueError("POLICY_BASELINE_CHANGED")
    if json.loads(candidate.read_bytes())["policy_generation"] != 12:
        raise ValueError("POLICY_TARGET_INVALID")
    files = (POLICY, CORE / ".env", WEB / ".env")
    if any(p.is_symlink() for p in files):
        raise ValueError("CONFIG_PATH_INVALID")
    replacements = [candidate.read_bytes()]
    for path, key in (
        (CORE / ".env", "LEDGERBRIDGE_INTERNAL_READ_POLICY_GENERATION"),
        (WEB / ".env", "CORE_POLICY_GENERATION"),
    ):
        raw = path.read_bytes()
        old, new = (key + "=11").encode(), (key + "=12").encode()
        if len(re.findall(rb"(?m)^" + old + rb"\r?$", raw)) != 1:
            raise ValueError("GENERATION_CONFIG_DRIFT")
        replacements.append(re.sub(rb"(?m)^" + old + rb"(?=\r?$)", new, raw))
    # Read the running container's actual compose files, not an assumed list.
    info = json.loads(command(["docker", "inspect", "ledgerbridge-internal-reader-1"]))[0]
    compose = [
        value
        for path in info["Config"]["Labels"]["com.docker.compose.project.config_files"].split(",")
        for value in ("-f", path)
    ]
    backup = POLICY.parent / "monthly-before-generation-12"
    backup.mkdir(mode=0o700)
    for index, path in enumerate(files):
        saved = backup / str(index)
        shutil.copy2(path, saved)
        os.chmod(saved, 0o600)
        if digest(saved) != digest(path):
            raise ValueError("BACKUP_MISMATCH")
    changed: list[tuple[int, Path]] = []
    web_done = False
    try:
        for index, (path, content) in enumerate(zip(files, replacements, strict=True)):
            replace(path, content)
            changed.append((index, path))
        recreate_core(compose)
        receipt = command(
            [
                "python3",
                str(stage / "release_web_reference.py"),
                "--expected",
                OLD_WEB,
                "--revision",
                args.web_revision,
                "--archive",
                str(stage / "web.tar.gz"),
                "--archive-sha256",
                args.archive_sha256,
                "--report",
                str(WEB / "config/monthly-reconciliation-review.json"),
                "--report-sha256",
                args.report_sha256,
            ],
            cwd=WEB,
        )
        web_done = True
        probe = """
import os,json
from server.app import _build_cash_reconciliation_client
c=_build_cash_reconciliation_client(default_ca_file=os.environ['CORE_CA_FILE'],timeout_seconds=10)
expected=json.loads(EXPECTED_JSON)
p=c.json('GET','/internal/v1/cash-reconciliations/'+expected['month'])
assert p['totals']==expected['totals']
assert p['eligible_fact_count']==expected['eligible_fact_count']
assert p['matched_fact_count']==expected['matched_fact_count']
assert os.environ['CORE_POLICY_GENERATION']=='12'
print('CASH_SCOPE_VERIFIED')
"""
        command(
            ["docker", "exec", "-i", "-w", "/app", "ledgerbridge-web-core", "python", "-"],
            data=probe.replace("EXPECTED_JSON", repr(json.dumps(expected))).encode(),
        )
        print(receipt.decode().strip())
        print("MONTHLY_SCOPE_AND_WEB_VERIFIED")
    except BaseException:
        rollback_errors = []
        if web_done:
            try:
                saved = WEB / (".monthly-before-" + args.web_revision[:12])
                failed = WEB / (".monthly-failed-" + args.web_revision[:12])
                failed.mkdir(mode=0o700)
                with suppress(RuntimeError):
                    command(["docker", "stop", "ledgerbridge-web-core"], cwd=WEB)
                for name in ("server", "dist"):
                    (WEB / name).rename(failed / name)
                    shutil.copytree(saved / name, WEB / name)
                shutil.copy2(saved / "REVISION", WEB / "REVISION")
            except (OSError, RuntimeError):
                rollback_errors.append("WEB_FILES")
        for index, path in reversed(changed):
            try:
                replace(path, (backup / str(index)).read_bytes())
            except (OSError, RuntimeError):
                rollback_errors.append("CONFIG_" + str(index))
        try:
            recreate_core(compose)
        except (OSError, RuntimeError):
            rollback_errors.append("CORE_HEALTH")
        try:
            command(
                [
                    "docker",
                    "compose",
                    "-f",
                    "compose.core-backed.yaml",
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    "web",
                ],
                cwd=WEB,
            )
            health("ledgerbridge-web-core")
        except (OSError, RuntimeError):
            rollback_errors.append("WEB_HEALTH")
        if rollback_errors:
            print("MONTHLY_ROLLBACK_FAILED " + ",".join(rollback_errors))
            raise RuntimeError("MONTHLY_ROLLBACK_FAILED") from None
        print("MONTHLY_ACTIVATION_ROLLED_BACK")
        raise RuntimeError("MONTHLY_ACTIVATION_FAILED") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=Path)
    for name in (
        "web-revision",
        "policy-sha256",
        "archive-sha256",
        "report-sha256",
        "acceptance-sha256",
    ):
        parser.add_argument("--" + name, required=True)
    try:
        activate(parser.parse_args())
    except (ValueError, RuntimeError, OSError):
        print("MONTHLY_ACTIVATION_REFUSED_OR_FAILED")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
