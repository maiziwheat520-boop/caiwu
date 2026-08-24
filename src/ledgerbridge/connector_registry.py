"""Explicit, fail-closed Connector factory registry.

The registry accepts only factories supplied by reviewed composition code. It
does not import modules, read a manifest, or discover entry points. A future
signature/manifest gate can construct the tuple of factories and then reuse
this contract without changing the application-facing API.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ledgerbridge.connectors import Connector, ConnectorContractError, validate_connector
from ledgerbridge.text import contains_unstorable_text

MAX_FACTORY_ID = 100


class ConnectorRegistryError(ValueError):
    """Raised when explicit Connector composition is unsafe or ambiguous."""


class ConnectorFactory(Protocol):
    """Reviewed factory seam; implementations must not perform discovery."""

    @property
    def factory_id(self) -> str: ...

    def build(self) -> Connector: ...


class ConnectorRegistry:
    """Build a deterministic Connector tuple from an explicit factory list."""

    def __init__(
        self,
        factories: Iterable[ConnectorFactory] = (),
        *,
        production: bool = False,
    ) -> None:
        self._production = production
        validated: list[ConnectorFactory] = []
        identities: set[str] = set()
        for factory in factories:
            factory_id = getattr(factory, "factory_id", None)
            if (
                not isinstance(factory_id, str)
                or not factory_id.strip()
                or not factory_id.isprintable()
                or len(factory_id) > MAX_FACTORY_ID
                or contains_unstorable_text(factory_id)
            ):
                raise ConnectorRegistryError("connector factory id is invalid")
            if factory_id in identities:
                raise ConnectorRegistryError("connector factory id is duplicated")
            if not callable(getattr(factory, "build", None)):
                raise ConnectorRegistryError("connector factory is invalid")
            identities.add(factory_id)
            validated.append(factory)
        self._factories = tuple(validated)

    @property
    def is_empty(self) -> bool:
        return not self._factories

    def build_all(self) -> tuple[Connector, ...]:
        connectors: list[Connector] = []
        identities: set[tuple[str, str, str]] = set()
        for factory in self._factories:
            try:
                connector = factory.build()
            except Exception as exc:
                raise ConnectorRegistryError("connector factory failed") from exc
            try:
                identity = validate_connector(connector, production=self._production)
            except ConnectorContractError as exc:
                raise ConnectorRegistryError(
                    "connector factory returned an invalid connector"
                ) from exc
            if identity in identities:
                raise ConnectorRegistryError("connector identity is duplicated")
            identities.add(identity)
            connectors.append(connector)
        return tuple(connectors)
