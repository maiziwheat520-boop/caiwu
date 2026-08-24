"""Fail-closed composition for worker-owned runner connectors.

The signed manifest verifier is intentionally a separate gate.  This module
accepts only an already verified, immutable manifest object and turns its
allowlisted runner entries into ``RunnerConnector`` facades.  No file, key, or
dynamic import is read here, so the default application composition remains
empty until a separately reviewed verifier supplies the object.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final

from ledgerbridge.connectors import CANONICAL_SOURCE_PATTERN, ConnectorExecutionMode
from ledgerbridge.runner_client import ConnectorRunnerClient, RunnerConnector
from ledgerbridge.text import contains_unstorable_text

RUNNER_FACTORY_ID: Final = "ledgerbridge.runner_connector"
_GENERATION_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$")


class RunnerCompositionError(ValueError):
    """Raised when an injected verified manifest cannot form a safe registry."""


@dataclass(frozen=True, slots=True)
class RunnerConnectorSpec:
    """One allowlisted, runner-only Connector declaration."""

    factory_id: str
    name: str
    version: str
    source_system: str
    execution_mode: ConnectorExecutionMode = ConnectorExecutionMode.RUNNER

    def __post_init__(self) -> None:
        if self.factory_id != RUNNER_FACTORY_ID:
            raise RunnerCompositionError("runner factory is not allowlisted")
        if self.execution_mode is not ConnectorExecutionMode.RUNNER:
            raise RunnerCompositionError("runner composition requires execution_mode=runner")
        _require_manifest_text("connector.name", self.name, 100)
        if self.name.startswith("ledgerbridge."):
            raise RunnerCompositionError("connector.name uses the reserved internal namespace")
        _require_manifest_text("connector.version", self.version, 100)
        if CANONICAL_SOURCE_PATTERN.fullmatch(self.source_system) is None:
            raise RunnerCompositionError("connector.source_system is not canonical")


@dataclass(frozen=True, slots=True)
class VerifiedRunnerManifest:
    """Immutable manifest identity produced by a future signature verifier.

    ``digest`` is checked against canonical entry bytes here.  Signature and
    key verification happen outside this module; callers must not construct
    this value from untrusted request data.
    """

    generation: str
    digest: bytes
    connectors: tuple[RunnerConnectorSpec, ...]

    @classmethod
    def from_connectors(
        cls,
        generation: str,
        connectors: tuple[RunnerConnectorSpec, ...],
    ) -> VerifiedRunnerManifest:
        payload = cls._canonical_payload(generation, connectors)
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).digest()
        return cls(generation, digest, connectors)

    def __post_init__(self) -> None:
        if type(self.digest) is not bytes:
            raise RunnerCompositionError("manifest digest must be immutable bytes")
        if type(self.connectors) is not tuple:
            raise RunnerCompositionError("manifest connectors must be an immutable tuple")
        if _GENERATION_PATTERN.fullmatch(self.generation) is None:
            raise RunnerCompositionError("manifest generation is invalid")
        if len(self.digest) != 32:
            raise RunnerCompositionError("manifest digest must contain 32 bytes")
        identities: set[tuple[str, str]] = set()
        for connector in self.connectors:
            if not isinstance(connector, RunnerConnectorSpec):
                raise RunnerCompositionError("manifest contains an invalid connector spec")
            identity = (connector.name, connector.version)
            if identity in identities:
                raise RunnerCompositionError("manifest contains duplicate connector identity")
            identities.add(identity)
        if self.digest != hashlib.sha256(self.canonical_bytes()).digest():
            raise RunnerCompositionError("manifest digest does not match canonical contents")

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self._canonical_payload(self.generation, self.connectors),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _canonical_payload(
        generation: str,
        connectors: tuple[RunnerConnectorSpec, ...],
    ) -> dict[str, object]:
        return {
            "connectors": [
                {
                    "factory_id": connector.factory_id,
                    "name": connector.name,
                    "source_system": connector.source_system,
                    "version": connector.version,
                    "execution_mode": connector.execution_mode.value,
                }
                for connector in connectors
            ],
            "generation": generation,
            "schema_version": 1,
        }

    @property
    def identity(self) -> tuple[str, bytes]:
        return self.generation, self.digest


def build_worker_runner_connectors(
    manifest: VerifiedRunnerManifest | None,
    *,
    socket_path: str,
) -> tuple[RunnerConnector, ...]:
    """Build the worker-owned runner facade from an injected verified manifest."""

    if manifest is None:
        return ()
    client = ConnectorRunnerClient(socket_path)
    return tuple(
        RunnerConnector(
            connector.name,
            connector.version,
            connector.source_system,
            client,
        )
        for connector in manifest.connectors
    )


def _require_manifest_text(field: str, value: object, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or contains_unstorable_text(value)
    ):
        raise RunnerCompositionError(f"{field} must be non-blank and at most {maximum} characters")
