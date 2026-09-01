from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from ledgerbridge.bank_statement_plan_batch import (
    BANK_STATEMENT_PLAN_BATCH_INDEX_SCHEMA,
    BANK_STATEMENT_PLAN_BATCH_SCHEMA,
    BankStatementPlanBatchError,
    materialize_private_bank_statement_plan_batch,
    run_bank_statement_plan_batch_builder,
)


def _private_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _manifest(path: Path, drafts: list[Path]) -> Path:
    return _private_json(
        path,
        {
            "schema_version": BANK_STATEMENT_PLAN_BATCH_SCHEMA,
            "expected_item_count": len(drafts),
            "items": [{"draft_path": str(draft)} for draft in drafts],
        },
    )


def _drafts(tmp_path: Path, count: int = 2) -> list[Path]:
    return [
        _private_json((tmp_path / f"draft-{ordinal}.json").resolve(), {"ordinal": ordinal})
        for ordinal in range(1, count + 1)
    ]


def _fake_plan_builder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_ordinal: int | None = None,
    duplicate_source: bool = False,
) -> list[Path]:
    built: list[Path] = []
    draft_by_plan: dict[Path, Path] = {}

    def finalize(draft_path: Path, output_path: Path) -> Path:
        ordinal = len(built) + 1
        if ordinal == fail_ordinal:
            raise RuntimeError("synthetic private value that must not escape")
        output_path.write_text(
            json.dumps(
                {
                    "private_marker": "full-account-and-transaction-detail",
                    "ordinal": ordinal,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(output_path, 0o600)
        built.append(draft_path)
        draft_by_plan[output_path] = draft_path
        return output_path

    def load(output_path: Path) -> SimpleNamespace:
        ordinal = int(output_path.stem.split(".")[0])
        plan_sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
        source_ordinal = 1 if duplicate_source else ordinal
        return SimpleNamespace(
            plan_sha256=plan_sha256,
            target_revision="a" * 40,
            cutover=SimpleNamespace(
                expected_sha256=hashlib.sha256(f"source-{source_ordinal}".encode()).hexdigest(),
                evidence_ref=UUID(int=ordinal),
            ),
        )

    monkeypatch.setattr(
        "ledgerbridge.bank_statement_plan_batch.finalize_private_bank_statement_plan",
        finalize,
    )
    monkeypatch.setattr(
        "ledgerbridge.bank_statement_plan_batch.load_private_bank_statement_plan",
        load,
    )
    return built


def test_batch_materializes_independent_plans_and_a_non_sensitive_index_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drafts = _drafts(tmp_path)
    manifest = _manifest((tmp_path / "batch.json").resolve(), drafts)
    output = (tmp_path / "plans").resolve()
    built = _fake_plan_builder(monkeypatch)

    result = materialize_private_bank_statement_plan_batch(manifest, output)

    assert result.output_directory == output
    assert result.plan_count == 2
    assert len(result.index_sha256) == 64
    assert built == drafts
    assert sorted(path.name for path in output.iterdir()) == [
        "000001.plan.json",
        "000002.plan.json",
        "index.json",
    ]
    index_text = (output / "index.json").read_text(encoding="ascii")
    index = json.loads(index_text)
    assert index["schema_version"] == BANK_STATEMENT_PLAN_BATCH_INDEX_SCHEMA
    assert index["plan_count"] == 2
    assert [item["ordinal"] for item in index["plans"]] == [1, 2]
    assert hashlib.sha256(index_text.encode("ascii")).hexdigest() == result.index_sha256
    assert str(tmp_path) not in index_text
    assert "full-account" not in index_text
    assert "transaction" not in index_text
    assert "password" not in index_text.casefold()
    assert "00000000-0000-0000-0000" not in index_text
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o700
        assert stat.S_IMODE((output / "index.json").stat().st_mode) == 0o600


def test_batch_failure_leaves_no_final_or_partial_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest((tmp_path / "batch.json").resolve(), _drafts(tmp_path, 3))
    output = (tmp_path / "plans").resolve()
    _fake_plan_builder(monkeypatch, fail_ordinal=2)

    with pytest.raises(BankStatementPlanBatchError, match="could not be materialized") as error:
        materialize_private_bank_statement_plan_batch(manifest, output)

    assert "synthetic private value" not in str(error.value)
    assert not output.exists()
    assert not list(tmp_path.glob(".bank-statement-plan-batch-*"))


def test_batch_never_overwrites_an_existing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest((tmp_path / "batch.json").resolve(), _drafts(tmp_path, 1))
    output = (tmp_path / "plans").resolve()
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    _fake_plan_builder(monkeypatch)

    with pytest.raises(BankStatementPlanBatchError):
        materialize_private_bank_statement_plan_batch(manifest, output)

    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload.update({"expected_item_count": 2}),
        lambda payload: payload["items"][0].update({"password": "forbidden"}),
        lambda payload: payload["items"].append(dict(payload["items"][0])),
        lambda payload: payload["items"][0].update({"draft_path": "relative.json"}),
    ],
)
def test_batch_manifest_is_strict_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
) -> None:
    drafts = _drafts(tmp_path, 1)
    payload: dict[str, object] = {
        "schema_version": BANK_STATEMENT_PLAN_BATCH_SCHEMA,
        "expected_item_count": 1,
        "items": [{"draft_path": str(drafts[0])}],
    }
    assert callable(mutate)
    mutate(payload)
    manifest = _private_json((tmp_path / "batch.json").resolve(), payload)
    _fake_plan_builder(monkeypatch)

    with pytest.raises(BankStatementPlanBatchError):
        materialize_private_bank_statement_plan_batch(
            manifest,
            (tmp_path / "plans").resolve(),
        )


def test_batch_rejects_duplicate_source_binding_without_partial_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest((tmp_path / "batch.json").resolve(), _drafts(tmp_path, 2))
    output = (tmp_path / "plans").resolve()
    _fake_plan_builder(monkeypatch, duplicate_source=True)

    with pytest.raises(BankStatementPlanBatchError):
        materialize_private_bank_statement_plan_batch(manifest, output)

    assert not output.exists()


def test_batch_rejects_a_symlink_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_manifest = _manifest((tmp_path / "real.json").resolve(), _drafts(tmp_path, 1))
    manifest = (tmp_path / "batch.json").resolve()
    try:
        manifest.symlink_to(real_manifest)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    _fake_plan_builder(monkeypatch)

    with pytest.raises(BankStatementPlanBatchError):
        materialize_private_bank_statement_plan_batch(
            manifest,
            (tmp_path / "plans").resolve(),
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX private-file modes only")
def test_batch_rejects_a_world_readable_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest((tmp_path / "batch.json").resolve(), _drafts(tmp_path, 1))
    os.chmod(manifest, 0o644)
    _fake_plan_builder(monkeypatch)

    with pytest.raises(BankStatementPlanBatchError):
        materialize_private_bank_statement_plan_batch(
            manifest,
            (tmp_path / "plans").resolve(),
        )


def test_environment_builder_reports_only_count_and_index_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest((tmp_path / "batch.json").resolve(), _drafts(tmp_path, 1))
    output = (tmp_path / "plans").resolve()
    _fake_plan_builder(monkeypatch)

    assert (
        run_bank_statement_plan_batch_builder(
            {
                "LEDGERBRIDGE_BANK_STATEMENT_PRIVATE_BATCH": str(manifest),
                "LEDGERBRIDGE_BANK_STATEMENT_PRIVATE_PLAN_DIRECTORY": str(output),
            }
        )
        == 0
    )

    stdout = capsys.readouterr().out
    assert stdout.startswith("BANK_STATEMENT_CUTOVER_PLAN_BATCH_READY count=1 index_sha256=")
    assert str(tmp_path) not in stdout
    assert "full-account" not in stdout
    assert "transaction" not in stdout
