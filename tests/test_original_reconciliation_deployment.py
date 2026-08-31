from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_internal_reader_mounts_hash_pinned_private_original_layout() -> None:
    compose = cast(
        dict[str, Any],
        yaml.safe_load((ROOT / "docker-compose.core-review.yml").read_text(encoding="utf-8")),
    )
    reader = cast(dict[str, Any], compose["services"]["internal-reader"])
    environment = cast(dict[str, str], reader["environment"])

    assert environment["LEDGERBRIDGE_ORIGINAL_RECONCILIATION_LAYOUT_FILE"] == (
        "/run/ledgerbridge-private/original-reconciliation-layout.json"
    )
    assert environment["LEDGERBRIDGE_ORIGINAL_RECONCILIATION_LAYOUT_SHA256"].startswith("${")
    assert (
        "${LEDGERBRIDGE_ORIGINAL_RECONCILIATION_LAYOUT_HOST_FILE:"
        "?reviewed original-reconciliation layout host file is required}:"
        "/run/ledgerbridge-private/original-reconciliation-layout.json:ro"
    ) in reader["volumes"]
