"""The committed internal contract must match the models actually served.

The Web BFF re-validates every Core response by hand and cannot import these
models. Committing the generated schema is what makes a wire-shape change
visible in review instead of surfacing as a production 503.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from scripts.internal_contract_schema import (
    CONTRACT_VERSION,
    DEFAULT_OUTPUT,
    MODELS,
    build_contract,
    render,
)

ARTIFACT = Path(__file__).resolve().parents[1] / DEFAULT_OUTPUT


def test_committed_contract_matches_the_served_models() -> None:
    assert ARTIFACT.exists(), "internal contract artifact is missing"
    assert ARTIFACT.read_text(encoding="utf-8") == render(build_contract()), (
        "contracts/internal-contract.json is stale; regenerate with "
        "`python scripts/internal_contract_schema.py`"
    )


def test_contract_covers_every_internal_response_model() -> None:
    contract = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert contract["contract_version"] == CONTRACT_VERSION
    assert set(contract["models"]) == {model.__name__ for model in MODELS}


@pytest.mark.parametrize("model", MODELS, ids=lambda model: model.__name__)
def test_every_contract_model_forbids_unknown_fields(model: type[BaseModel]) -> None:
    # A response model that silently accepts extra fields would let Core widen
    # the wire shape without the artifact showing a diff.
    assert model.model_config.get("extra") == "forbid", (
        f"{model.__name__} must forbid extra fields to keep the contract honest"
    )
