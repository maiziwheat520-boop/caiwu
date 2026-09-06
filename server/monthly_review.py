"""Validate private, read-only reconciliation review evidence before serving it.

This package never changes cash totals, posting state, or the underlying ledger.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import unicodedata
from datetime import date


MAX_PACKAGE_BYTES = 1024 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
_MONTH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z")
_PATH = re.compile(
    r"[a-z]:[\\/]|\\\\|[a-z][a-z0-9+.-]*://|file:|\.\.[\\/]|(?:^|[\s\"'(（])/(?:[a-z0-9_.-]+/)+",
    re.IGNORECASE,
)
_POLICY = {
    "expense_basis": "ACTUAL_PAYMENT_MONTH",
    "income_basis": "SCREENSHOT_SETTLEMENT_PERIOD",
    "legacy_payroll_through": "2026-07",
    "workbench_payroll_from": "2026-08",
    "workbench_status": "NOT_CONNECTED",
}


class MonthlyReviewUnavailable(Exception):
    """The private review is absent or cannot safely be served."""


def _reject() -> None:
    raise MonthlyReviewUnavailable("Monthly review evidence is unavailable")


def _keys(value: object, expected: set[str]) -> dict:
    if type(value) is not dict or set(value) != expected:
        _reject()
    return value


def _text(value: object, maximum: int = 200, *, empty: bool = False) -> str:
    if (
        type(value) is not str
        or len(value) > maximum
        or (not empty and not value.strip())
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)
        or _PATH.search(value)
    ):
        _reject()
    return value


def _month(value: object) -> str:
    if type(value) is not str or not _MONTH.fullmatch(value) or value.startswith("0000"):
        _reject()
    return value


def _integer(value: object, *, signed: bool = False, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or abs(value) > MAX_SAFE_INTEGER or (not signed and value < 0):
        _reject()
    return value


def _list(value: object, maximum: int = 1000) -> list:
    if type(value) is not list or len(value) > maximum:
        _reject()
    return value


def _unique(value: object, seen: set) -> None:
    if value in seen:
        _reject()
    seen.add(value)


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _reject()
        result[key] = value
    return result


def _read_package(path: Path) -> dict:
    # Check each path component: the private mount must not resolve through a link.
    absolute = Path(os.path.abspath(path))
    for component in (absolute, *absolute.parents):
        metadata = component.lstat()
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            _reject()
    before = absolute.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_PACKAGE_BYTES:
        _reject()
    descriptor = os.open(absolute, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or not stat.S_ISREG(opened.st_mode):
            _reject()
        content = stream.read(MAX_PACKAGE_BYTES + 1)
    if len(content) > MAX_PACKAGE_BYTES:
        _reject()
    return json.loads(content.decode("utf-8"), object_pairs_hook=_json_object)


def _validate(package: object) -> dict:
    package = _keys(package, {
        "schema_version", "revision", "confirmed_on", "policy", "adjustments", "bridges",
        "pending", "historical_payroll", "backtest", "accepted_exceptions",
    })
    if package["schema_version"] != "ledgerbridge.monthly-review-package.v1":
        _reject()
    _text(package["revision"], 100)
    confirmed = _text(package["confirmed_on"], 10)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", confirmed) or date.fromisoformat(confirmed).isoformat() != confirmed:
        _reject()
    if _keys(package["policy"], set(_POLICY)) != _POLICY:
        _reject()
    ids: set[str] = set()
    for collection, status in (("adjustments", "PENDING_ENTRY"), ("bridges", "PENDING_BRIDGE"), ("pending", None)):
        monthly_totals: dict[str, int] = {}
        for entry in _list(package[collection]):
            keys = {"id", "label", "month", "amount_minor", "status", "note"}
            if collection != "pending":
                keys |= {"balance_effect_minor", "cash_effect_minor"}
            entry = _keys(entry, keys)
            _unique(_text(entry["id"], 100), ids)
            _text(entry["label"])
            _text(entry["note"], 2000, empty=True)
            month = _month(entry["month"])
            amount = _integer(entry["amount_minor"], signed=collection == "adjustments")
            # A safe individual integer does not guarantee a safe JS reduce.
            # Validate each prefix too: signed later reversals cannot repair
            # precision already lost while the client computes an earlier sum.
            monthly_totals[month] = _integer(
                monthly_totals.get(month, 0) + amount, signed=True,
            )
            if collection == "pending":
                if entry["status"] not in ("AWAITING_PAYMENT", "PENDING_ENTRY"):
                    _reject()
            else:
                if entry["status"] != status or _integer(entry["cash_effect_minor"]) != 0:
                    _reject()
                expected = -amount if collection == "adjustments" else amount
                if _integer(entry["balance_effect_minor"], signed=True) != expected:
                    _reject()

    periods: set[str] = set()
    for entry in _list(package["historical_payroll"], 7):
        entry = _keys(entry, {"period", "total_minor", "stores"})
        _unique(_month(entry["period"]), periods)
        total = _integer(entry["total_minor"])
        labels: set[str] = set()
        amounts = []
        for store in _list(entry["stores"], 100):
            store = _keys(store, {"label", "amount_minor"})
            _unique(_text(store["label"]), labels)
            amounts.append(_integer(store["amount_minor"], nullable=True))
        if not labels or sum(amount for amount in amounts if amount is not None) != total:
            _reject()
    if periods != {f"2026-{month:02}" for month in range(1, 8)}:
        _reject()

    backtest_keys: set[tuple[str, str]] = set()
    for entry in _list(package["backtest"]):
        entry = _keys(entry, {"month", "label", "original_minor", "matched_minor", "difference_minor", "status", "note"})
        _unique((_month(entry["month"]), _text(entry["label"])), backtest_keys)
        _text(entry["status"], 100)
        _text(entry["note"], 2000, empty=True)
        original = _integer(entry["original_minor"], signed=True, nullable=True)
        matched = _integer(entry["matched_minor"], signed=True, nullable=True)
        difference = _integer(entry["difference_minor"], signed=True, nullable=True)
        if original is not None and matched is not None and difference is not None and matched - original != difference:
            _reject()
    for entry in _list(package["accepted_exceptions"]):
        entry = _keys(entry, {"id", "label", "note", "status"})
        _unique(_text(entry["id"], 100), ids)
        _text(entry["label"])
        _text(entry["note"], 2000, empty=True)
        if entry["status"] != "ACCEPTED_OPEN":
            _reject()
    return package


def load_monthly_review(path: Path, month: str) -> dict:
    """Load validated evidence; validation errors never expose private source data."""
    try:
        _month(month)
        package = _validate(_read_package(path))
        return {
            "contract_version": "ledgerbridge.monthly-review.v1",
            "authority": "NON_AUTHORITATIVE_REFERENCE",
            "accounting_month": month,
            "revision": package["revision"],
            "confirmed_on": package["confirmed_on"],
            "policy": package["policy"],
            **{
                key: [entry for entry in package[key] if entry["month"] == month]
                for key in ("adjustments", "bridges", "pending", "backtest")
            },
            "historical_payroll": package["historical_payroll"],
            "accepted_exceptions": package["accepted_exceptions"],
            "production_posted": False,
        }
    except MonthlyReviewUnavailable:
        raise
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        raise MonthlyReviewUnavailable("Monthly review evidence is unavailable") from None
