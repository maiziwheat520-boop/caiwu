from __future__ import annotations

import pytest

from ledgerbridge.connector_registry import ConnectorRegistry, ConnectorRegistryError
from ledgerbridge.connectors import (
    ArtifactMetadata,
    DetectionResult,
    ParsedSourceRecord,
    ReadableBinary,
)


class SyntheticConnector:
    name = "synthetic"
    version = "1"
    source_system = "synthetic"

    def detect(self, metadata: ArtifactMetadata, bounded_prefix: bytes) -> DetectionResult:
        return DetectionResult.NO_MATCH

    def parse(self, stream: ReadableBinary) -> list[ParsedSourceRecord]:
        return []


class Factory:
    def __init__(
        self, factory_id: str = "test.factory", connector: object = SyntheticConnector()
    ) -> None:
        self.factory_id = factory_id
        self.connector = connector

    def build(self) -> SyntheticConnector:
        return self.connector  # type: ignore[return-value]


def test_empty_registry_is_fail_closed() -> None:
    registry = ConnectorRegistry()
    assert registry.is_empty
    assert registry.build_all() == ()


def test_explicit_factory_builds_and_validates_connector() -> None:
    registry = ConnectorRegistry([Factory()])
    assert registry.build_all()[0].name == "synthetic"


def test_duplicate_factory_or_connector_identity_is_rejected() -> None:
    with pytest.raises(ConnectorRegistryError, match="factory id"):
        ConnectorRegistry([Factory(), Factory()])

    class OtherFactory(Factory):
        factory_id = "other.factory"

    with pytest.raises(ConnectorRegistryError, match="connector identity"):
        ConnectorRegistry([Factory(), OtherFactory("other.factory")]).build_all()


def test_production_registry_rejects_in_process_connector() -> None:
    with pytest.raises(ConnectorRegistryError, match="invalid connector"):
        ConnectorRegistry([Factory()], production=True).build_all()


@pytest.mark.parametrize("factory_id", [" ", "x\n"])
def test_factory_id_is_bounded_and_storable(factory_id: str) -> None:
    with pytest.raises(ConnectorRegistryError, match="factory id"):
        ConnectorRegistry([Factory(factory_id)])
