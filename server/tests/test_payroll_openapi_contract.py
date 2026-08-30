from __future__ import annotations

import json
import re
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import UUID

from server.tests.test_payroll_bff import FakePayrollCoreClient


OPENAPI_PATH = Path(__file__).resolve().parents[2] / "contracts" / "openapi.yaml"


def _scalar(value: str) -> object:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_scalar(item) for item in inner.split(",")]
    if value.startswith('"') and value.endswith('"'):
        return json.loads(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value == "true":
        return True
    if value == "false":
        return False
    if value == "null":
        return None
    if re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _next_content(lines: list[str], start: int) -> int:
    index = start
    while index < len(lines) and not lines[index].strip():
        index += 1
    return index


def _mapping(lines: list[str], start: int, indent: int) -> tuple[dict[str, object], int]:
    result: dict[str, object] = {}
    index = start
    while index < len(lines):
        index = _next_content(lines, index)
        if index >= len(lines) or _indent(lines[index]) < indent:
            break
        if _indent(lines[index]) != indent:
            raise AssertionError(f"unexpected OpenAPI indentation: {lines[index]}")
        content = lines[index].strip()
        key, separator, raw = content.partition(":")
        if not separator:
            raise AssertionError(f"invalid OpenAPI mapping line: {lines[index]}")
        if raw.strip():
            result[key] = _scalar(raw)
            index += 1
            continue

        child = _next_content(lines, index + 1)
        if child >= len(lines) or _indent(lines[child]) <= indent:
            result[key] = {}
            index = child
            continue
        if key == "allOf":
            result[key] = []
            index = child
            while index < len(lines) and (
                not lines[index].strip() or _indent(lines[index]) > indent
            ):
                index += 1
            continue
        child_indent = _indent(lines[child])
        if lines[child].strip().startswith("- "):
            values: list[object] = []
            index = child
            while index < len(lines) and _indent(lines[index]) == child_indent:
                item = lines[index].strip()
                if not item.startswith("- "):
                    break
                values.append(_scalar(item[2:]))
                index += 1
            result[key] = values
            continue
        result[key], index = _mapping(lines, child, child_indent)
    return result, index


def _component(document: str, name: str) -> dict[str, object]:
    lines = document.splitlines()
    marker = f"    {name}:"
    try:
        start = lines.index(marker)
    except ValueError as exc:
        raise AssertionError(f"missing OpenAPI component {name}") from exc
    value, _ = _mapping(lines, start + 1, 6)
    return value


def _matches_type(value: object, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "null": value is None,
    }[expected]


def _validate(document: str, schema: dict[str, object], value: object, path: str = "$") -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        prefix = "#/components/schemas/"
        if not reference.startswith(prefix):
            raise AssertionError(f"unsupported schema reference at {path}: {reference}")
        _validate(document, _component(document, reference.removeprefix(prefix)), value, path)
        return

    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _matches_type(value, expected_type):
        raise AssertionError(f"{path} is not {expected_type}")
    if isinstance(expected_type, list) and not any(
        isinstance(item, str) and _matches_type(value, item) for item in expected_type
    ):
        raise AssertionError(f"{path} has an unsupported type")
    if "const" in schema and value != schema["const"]:
        raise AssertionError(f"{path} does not match const {schema['const']!r}")
    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        raise AssertionError(f"{path} is outside the enum")

    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = set(cast(list[str], required)) - set(value)
            if missing:
                raise AssertionError(f"{path} is missing {sorted(missing)}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise AssertionError(f"{path} has invalid schema properties")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise AssertionError(f"{path} contains unknown fields {sorted(unknown)}")
        for key, item in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                _validate(document, child_schema, item, f"{path}.{key}")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise AssertionError(f"{path} has too few items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise AssertionError(f"{path} has too many items")
        if schema.get("uniqueItems") is True:
            encoded = [json.dumps(item, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                raise AssertionError(f"{path} contains duplicate items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate(document, item_schema, item, f"{path}[{index}]")

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise AssertionError(f"{path} does not match {pattern}")
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise AssertionError(f"{path} is too short")
        if isinstance(maximum, int) and len(value) > maximum:
            raise AssertionError(f"{path} is too long")
        if schema.get("format") == "uuid":
            UUID(value)
        if schema.get("format") == "date-time":
            datetime.fromisoformat(value.replace("Z", "+00:00"))

    minimum = schema.get("minimum")
    if type(value) is int and isinstance(minimum, int) and value < minimum:
        raise AssertionError(f"{path} is below its minimum")


class PayrollOpenApiContractTests(unittest.TestCase):
    def test_openapi_schemas_validate_the_actual_core_backed_responses(self) -> None:
        document = OPENAPI_PATH.read_text(encoding="utf-8")
        client = FakePayrollCoreClient()
        cases = [
            (
                "/api/v1/payroll/status",
                "PayrollStatusEnvelope",
                client.json("GET", "/internal/v1/payroll/status"),
            ),
            (
                "/api/v1/payroll/dashboard",
                "PayrollDashboardEnvelope",
                client.json("GET", "/internal/v1/payroll/dashboard"),
            ),
            (
                "/api/v1/payroll/materials",
                "PayrollMaterialsEnvelope",
                client.json("GET", "/internal/v1/payroll/materials"),
            ),
            (
                "/api/v1/payroll/batches",
                "PayrollBatchesEnvelope",
                client.json("GET", "/internal/v1/payroll/batches"),
            ),
            (
                "/api/v1/payroll/verification",
                "PayrollVerificationEnvelope",
                client.json("GET", "/internal/v1/payroll/verification"),
            ),
        ]
        for path, component, response in cases:
            with self.subTest(path=path):
                self.assertIn(f'$ref: "#/components/schemas/{component}"', document)
                _validate(document, _component(document, component), response)

        operation_id = "30000000-0000-4000-8000-000000000001"
        command = client.json(
            "POST",
            "/internal/v1/payroll/batches/batch_live_2026_08/verify-receipts",
            body=b"{}",
            headers={"Idempotency-Key": operation_id},
        )
        _validate(document, _component(document, "PayrollCommandResult"), command)

    def test_openapi_rejects_the_three_retired_response_shapes(self) -> None:
        document = OPENAPI_PATH.read_text(encoding="utf-8")
        client = FakePayrollCoreClient()

        status = client.json("GET", "/internal/v1/payroll/status")
        cast(dict[str, object], status["data"])["provider"] = {"status": "ready"}
        with self.assertRaises(AssertionError):
            _validate(document, _component(document, "PayrollStatusEnvelope"), status)

        dashboard = client.json("GET", "/internal/v1/payroll/dashboard")
        cast(dict[str, object], dashboard["data"])["schema_version"] = (
            "payroll-ledgerbridge-live-projection/v1"
        )
        with self.assertRaises(AssertionError):
            _validate(document, _component(document, "PayrollDashboardEnvelope"), dashboard)

        command = client.json(
            "POST",
            "/internal/v1/payroll/batches/batch_live_2026_08/verify-receipts",
            body=b"{}",
            headers={"Idempotency-Key": "30000000-0000-4000-8000-000000000002"},
        )
        cast(dict[str, object], command["data"])["schema_version"] = (
            "payroll-ledgerbridge-live-projection/v1"
        )
        with self.assertRaises(AssertionError):
            _validate(document, _component(document, "PayrollCommandResult"), command)


if __name__ == "__main__":
    unittest.main()
