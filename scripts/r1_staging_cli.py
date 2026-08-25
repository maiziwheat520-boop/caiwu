"""Small loopback CLI for the isolated staging gateway."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_URL = "http://127.0.0.1:8653"


def _request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    content_type: str = "application/json",
) -> object:
    _require_loopback(url)
    request = Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": content_type},
        method=method,
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise RuntimeError("gateway response exceeds CLI limit")
            return json.loads(payload)
    except HTTPError as exc:
        detail = exc.read(MAX_RESPONSE_BYTES)
        try:
            parsed = json.loads(detail)
        except (ValueError, json.JSONDecodeError):
            parsed = "gateway request failed"
        raise RuntimeError(f"gateway returned HTTP {exc.code}: {parsed}") from exc
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError("gateway is unavailable") from exc


def _read(path: str) -> bytes:
    if path == "-":
        import sys

        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        data = Path(path).read_bytes()
    if len(data) > MAX_INPUT_BYTES:
        raise RuntimeError("input exceeds CLI limit")
    return data


def _require_loopback(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("CLI only permits an HTTP loopback gateway URL")
    if parsed.username or parsed.password:
        raise RuntimeError("gateway URL must not contain credentials")


def _command(args: argparse.Namespace) -> object:
    patch = json.loads(args.patch_json) if args.patch_json else None
    if patch is not None and not isinstance(patch, dict):
        raise RuntimeError("--patch-json must be a JSON object")
    command: dict[str, object] = {
        "operation_id": args.operation_id or str(uuid4()),
        "action": args.action,
        "expected_revision": args.expected_revision,
        "reason": args.reason,
        "decided_at": args.decided_at or datetime.now(UTC).isoformat(),
    }
    if patch is not None:
        command["patch"] = patch
    if args.conflict_resolutions_json:
        resolutions = json.loads(args.conflict_resolutions_json)
        if not isinstance(resolutions, dict):
            raise RuntimeError("--conflict-resolutions-json must be a JSON object")
        command["conflict_resolutions"] = resolutions
    if args.derived_candidate_ref:
        command["derived_candidate_ref"] = str(UUID(args.derived_candidate_ref))
    if args.derived_short_id:
        command["derived_short_id"] = args.derived_short_id
    payload = json.dumps({"actor_ref": args.actor_ref, "command": command}).encode()
    return _request(
        f"{args.url}/v1/candidates/{args.candidate_ref}/command",
        method="POST",
        body=payload,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help="loopback gateway base URL")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    intake_json = subparsers.add_parser("intake-json", help="submit a JSON intake file or stdin")
    intake_json.add_argument("file", help="JSON file path or -")

    intake_eml = subparsers.add_parser("intake-eml", help="submit an RFC5322 EML file")
    intake_eml.add_argument("file", help="EML file path or -")
    intake_eml.add_argument("--entity-ref", required=True, type=UUID)

    subparsers.add_parser("list", help="list persisted/in-memory candidates")

    command = subparsers.add_parser("command", help="apply one CandidateCommand")
    command.add_argument("candidate_ref", type=UUID)
    command.add_argument(
        "--action",
        required=True,
        choices=("COMPLETE_FIELDS", "RESOLVE_CONFLICT", "CONFIRM", "IGNORE", "SUPERSEDE"),
    )
    command.add_argument("--expected-revision", required=True, type=int)
    command.add_argument("--reason", required=True)
    command.add_argument("--actor-ref", required=True)
    command.add_argument("--operation-id")
    command.add_argument("--decided-at")
    command.add_argument("--patch-json")
    command.add_argument("--conflict-resolutions-json")
    command.add_argument("--derived-candidate-ref")
    command.add_argument("--derived-short-id")

    args = parser.parse_args()
    if args.operation == "intake-json":
        result = _request(
            f"{args.url}/v1/intake",
            method="POST",
            body=_read(args.file),
        )
    elif args.operation == "intake-eml":
        result = _request(
            f"{args.url}/v1/intake/eml",
            method="POST",
            body=_read(args.file),
            content_type="message/rfc822",
        )
        if not isinstance(result, dict):
            raise RuntimeError("gateway response is invalid")
    elif args.operation == "list":
        result = _request(f"{args.url}/v1/candidates")
    else:
        result = _command(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
