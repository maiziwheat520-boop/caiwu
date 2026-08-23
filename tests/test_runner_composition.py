from __future__ import annotations

import hashlib

import pytest

from ledgerbridge.connectors import ConnectorExecutionMode
from ledgerbridge.runner_composition import (
    RUNNER_FACTORY_ID,
    RunnerCompositionError,
    RunnerConnectorSpec,
    VerifiedRunnerManifest,
    build_worker_runner_connectors,
)


def _spec(
    *,
    name: str = "synthetic.csv",
    version: str = "1",
    source_system: str = "synthetic",
) -> RunnerConnectorSpec:
    return RunnerConnectorSpec(
        factory_id=RUNNER_FACTORY_ID,
        name=name,
        version=version,
        source_system=source_system,
    )


def test_verified_manifest_has_stable_identity_and_builds_worker_facade() -> None:
    manifest = VerifiedRunnerManifest.from_connectors("test-generation", (_spec(),))

    assert manifest.digest == hashlib.sha256(manifest.canonical_bytes()).digest()
    assert manifest.identity == ("test-generation", manifest.digest)

    connectors = build_worker_runner_connectors(
        manifest,
        socket_path="/run/ledgerbridge-connector/runner.sock",
    )
    assert len(connectors) == 1
    assert connectors[0].name == "synthetic.csv"
    assert connectors[0].version == "1"
    assert connectors[0].source_system == "synthetic"
    assert connectors[0].execution_mode == ConnectorExecutionMode.RUNNER.value


def test_empty_manifest_is_safe_default() -> None:
    assert (
        build_worker_runner_connectors(
            None,
            socket_path="/run/ledgerbridge-connector/runner.sock",
        )
        == ()
    )


def test_manifest_rejects_digest_drift_and_duplicate_identity() -> None:
    spec = _spec()
    with pytest.raises(RunnerCompositionError, match="digest"):
        VerifiedRunnerManifest("generation", b"d" * 32, (spec,))

    with pytest.raises(RunnerCompositionError, match="duplicate"):
        VerifiedRunnerManifest.from_connectors("generation", (spec, spec))


def test_manifest_rejects_non_runner_factory_and_mode() -> None:
    with pytest.raises(RunnerCompositionError, match="allowlisted"):
        RunnerConnectorSpec(
            factory_id="dynamic.import.path",
            name="synthetic.csv",
            version="1",
            source_system="synthetic",
        )

    with pytest.raises(RunnerCompositionError, match="execution_mode"):
        RunnerConnectorSpec(
            factory_id=RUNNER_FACTORY_ID,
            name="synthetic.csv",
            version="1",
            source_system="synthetic",
            execution_mode=ConnectorExecutionMode.IN_PROCESS,
        )
