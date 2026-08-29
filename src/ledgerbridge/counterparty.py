"""Registry-backed counterparty identity and classification.

Names are observations, not proof of account ownership.  Only explicit registry
mappings may assign managed, related-party, or known-business classifications.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

_REF = re.compile(r"^cp_[a-z0-9_]{1,96}$")


class CounterpartyClass(StrEnum):
    SELF_MANAGED = "self_managed"
    RELATED_PARTY = "related_party"
    KNOWN_BUSINESS = "known_business"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CounterpartyMapping:
    counterparty_ref: str
    counterparty_class: CounterpartyClass
    aliases: tuple[str, ...]
    account_identifiers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedCounterparty:
    counterparty_ref: str
    counterparty_class: CounterpartyClass
    canonical_label: str


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _unknown_ref(identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"cp_{digest}"


class CounterpartyRegistry:
    """Resolve explicit identities and fail safely to a stable unknown identity."""

    def __init__(self, mappings: tuple[CounterpartyMapping, ...]) -> None:
        self._by_alias: dict[str, ResolvedCounterparty] = {}
        self._by_account: dict[str, ResolvedCounterparty] = {}
        for mapping in mappings:
            if not _REF.fullmatch(mapping.counterparty_ref):
                raise ValueError("counterparty reference is invalid")
            if not mapping.aliases:
                raise ValueError("counterparty mapping requires an alias")
            resolved = ResolvedCounterparty(
                counterparty_ref=mapping.counterparty_ref,
                counterparty_class=mapping.counterparty_class,
                canonical_label=" ".join(unicodedata.normalize("NFKC", mapping.aliases[0]).split()),
            )
            for alias in mapping.aliases:
                self._register(self._by_alias, _normalize(alias), resolved)
            for identifier in mapping.account_identifiers:
                self._register(self._by_account, _normalize(identifier), resolved)

    @staticmethod
    def _register(
        index: dict[str, ResolvedCounterparty],
        key: str,
        resolved: ResolvedCounterparty,
    ) -> None:
        if not key:
            raise ValueError("counterparty registry key is empty")
        existing = index.get(key)
        if existing is not None and existing != resolved:
            raise ValueError("counterparty registry key is ambiguous")
        index[key] = resolved

    def resolve(
        self,
        *,
        label: str,
        account_identifier: str | None = None,
        platform_internal: bool = False,
    ) -> ResolvedCounterparty | None:
        if platform_internal:
            return None
        normalized_account = _normalize(account_identifier or "")
        normalized_label = _normalize(label)
        if normalized_account and (resolved := self._by_account.get(normalized_account)):
            return resolved
        if normalized_label and (resolved := self._by_alias.get(normalized_label)):
            return resolved
        fallback = normalized_account or normalized_label
        if not fallback:
            return None
        return ResolvedCounterparty(
            counterparty_ref=_unknown_ref(fallback),
            counterparty_class=CounterpartyClass.UNKNOWN,
            canonical_label=" ".join(unicodedata.normalize("NFKC", label).split()),
        )
