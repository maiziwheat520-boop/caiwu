from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.safe_load((ROOT / "docker-compose.core-review.yml").read_text(encoding="utf-8")),
    )


def test_evidence_unlocker_is_an_explicit_no_network_no_database_profile() -> None:
    unlocker = cast(dict[str, Any], _compose()["services"]["evidence-unlocker"])
    environment = cast(dict[str, str], unlocker["environment"])

    assert unlocker["profiles"] == ["evidence-unlock"]
    assert unlocker["network_mode"] == "none"
    assert unlocker["read_only"] is True
    assert unlocker["cap_drop"] == ["ALL"]
    assert unlocker["user"] == "10001:10001"
    assert not any("DATABASE" in name for name in environment)
    assert "artifacts:/var/lib/ledgerbridge/artifacts" in unlocker["volumes"]
    assert "evidence-unlock-socket:/run/ledgerbridge-unlocker" in unlocker["volumes"]


def test_core_api_keeps_artifacts_and_unlock_socket_read_only_and_route_off() -> None:
    reader = cast(dict[str, Any], _compose()["services"]["internal-reader"])
    environment = cast(dict[str, str], reader["environment"])

    assert "artifacts:/var/lib/ledgerbridge/artifacts:ro" in reader["volumes"]
    assert "evidence-unlock-socket:/run/ledgerbridge-unlocker:ro" in reader["volumes"]
    assert environment["LEDGERBRIDGE_ENABLE_INTERNAL_EVIDENCE_UNLOCK"].endswith(":-false}")
    assert environment["LEDGERBRIDGE_INTERNAL_EVIDENCE_UNLOCK_OPERATIONAL_GATE"].endswith(
        ":-closed}"
    )


def test_unlocker_receives_no_password_configuration_channel() -> None:
    compose = _compose()
    serialized = yaml.safe_dump(compose["services"]["evidence-unlocker"])

    assert "password" not in serialized.lower()
