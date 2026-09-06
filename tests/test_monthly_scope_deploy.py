from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import pytest

from scripts import deploy_monthly_scope as release


def test_replacement_is_private_before_write_and_cleans_failed_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmp_path / ".env"
    original.write_bytes(b"SYNTHETIC=old")
    observed: list[int] = []
    real_open = os.open

    def record_open(path: object, flags: int, mode: int) -> int:
        observed.append(mode)
        return real_open(str(path), flags, mode)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "chown", lambda *args: None, raising=False)
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(OSError("failure")))
    with pytest.raises(OSError):
        release.replace(original, b"SYNTHETIC=new")
    assert observed == [0o600]
    assert original.read_bytes() == b"SYNTHETIC=old"
    assert not original.with_name(".env.monthly-staged").exists()


def test_failed_web_rollback_does_not_skip_any_generation_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    core, web, private, stage = [tmp_path / name for name in ("core", "web", "private", "stage")]
    for path in (core, web, private, stage):
        path.mkdir()
    monkeypatch.setattr(release, "CORE", core)
    monkeypatch.setattr(release, "WEB", web)
    monkeypatch.setattr(release, "POLICY", private / "policy.json")
    monkeypatch.setattr(os, "chown", lambda *args: None, raising=False)
    (core / "DEPLOYED_REVISION").write_text(release.CORE_REVISION)
    (web / "REVISION").write_text(release.OLD_WEB)
    (web / "server").mkdir()
    (web / "dist").mkdir()
    (core / ".env").write_text("LEDGERBRIDGE_INTERNAL_READ_POLICY_GENERATION=11\n")
    (web / ".env").write_text("CORE_POLICY_GENERATION=11\n")
    release.POLICY.write_text('{"policy_generation":11}')
    candidate = stage / "policy-generation-12.json"
    candidate.write_text('{"policy_generation":12}')
    acceptance = stage / "acceptance.json"
    acceptance.write_text('{"month":"2026-08"}')
    restored: list[bool] = []
    monkeypatch.setattr(release, "recreate_core", lambda files: restored.append(True))
    monkeypatch.setattr(release, "health", lambda name: None)

    def fake_command(args: list[str], **kwargs: object) -> bytes:
        if args[:2] == ["docker", "inspect"]:
            return json.dumps(
                [
                    {
                        "Config": {
                            "Labels": {"com.docker.compose.project.config_files": "synthetic.yml"}
                        }
                    }
                ]
            ).encode()
        if args[:2] in (["docker", "exec"], ["docker", "stop"]):
            raise RuntimeError("injected probe or missing-container failure")
        return b"{}"

    monkeypatch.setattr(release, "command", fake_command)
    monkeypatch.setattr(
        shutil,
        "copytree",
        lambda *args: (_ for _ in ()).throw(OSError("injected restore failure")),
    )
    args = argparse.Namespace(
        stage=stage,
        web_revision="1" * 40,
        policy_sha256=release.digest(candidate),
        archive_sha256="2" * 64,
        report_sha256="3" * 64,
        acceptance_sha256=release.digest(acceptance),
    )
    with pytest.raises(RuntimeError, match="MONTHLY_ROLLBACK_FAILED"):
        release.activate(args)
    assert json.loads(release.POLICY.read_text())["policy_generation"] == 11
    assert (core / ".env").read_text().strip().endswith("=11")
    assert (web / ".env").read_text().strip().endswith("=11")
    assert len(restored) == 2
    assert "MONTHLY_ROLLBACK_FAILED WEB_FILES" in capsys.readouterr().out
