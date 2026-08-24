"""Shared text safety predicates for untrusted boundary values."""

from __future__ import annotations


def contains_unstorable_text(value: str) -> bool:
    """Return whether *value* cannot be safely persisted as UTF-8 text."""

    return any(codepoint == 0 or 0xD800 <= codepoint <= 0xDFFF for codepoint in map(ord, value))
