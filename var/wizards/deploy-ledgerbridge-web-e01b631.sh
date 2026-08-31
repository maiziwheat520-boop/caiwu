#!/usr/bin/env bash
set -Eeuo pipefail

# Fixed-revision LedgerBridge Web release gate. It is deliberately plan-only
# unless --execute is supplied together with the exact deployed Core revision.

EXPECTED_REVISION=e01b6316e8697b28b933feb0a6a158ab917b4142
EXPECTED_ARCHIVE_SHA256=912477124c79be7f7da99926d2dad73f2d9f24db3339fbd6d3d9737f23c6903a
EXPECTED_CORE_MIGRATION=20260831_0026
ARCHIVE=/home/aiadmin/private-releases/ledgerbridge-web/e01b6316e869/release.tar.gz
CURRENT=/home/aiadmin/services/ledgerbridge-web-auth-preview
BACKUP_ROOT=/home/aiadmin/backups/ledgerbridge-web
CORE_CURRENT=/srv/ai-center/ledgerbridge
PROJECT=ledgerbridge-web-core
COMPOSE=compose.core-backed.yaml
MODE=plan
EXPECTED_CORE_REVISION=
stop_intended=0
execute_requested=0
self_test_requested=0

container_health() {
  local name=$1 state
  state=$(docker inspect -f \
    '{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.RestartCount}}' \
    "$name" 2>/dev/null) || return 1
  [[ "$state" == 'true|healthy|0' ]]
}

stop_web_for_release() {
  stop_intended=1
  (
    cd "$CURRENT" || exit 1
    docker compose --project-name "$PROJECT" -f "$COMPOSE" stop web >/dev/null
  )
}

restore_web_service_from_tree() {
  local tree=$1
  if ! container_health ledgerbridge-web-core; then
    restart_web_from_tree "$tree" || return 1
  fi
  wait_for_web_health || return 1
  sqlite_quick_check "$tree/state/ledgerbridge-preview.sqlite3"
}

run_payroll_probe_container() {
  local tree=$1 project=$2
  (
    cd "$tree" || exit 1
    docker compose --project-name "$project" -f "$COMPOSE" \
      run --rm --no-deps -T --entrypoint python web -
  )
}

cleanup_payroll_probe_project() {
  local tree=$1 project=$2
  (
    cd "$tree" || exit 1
    docker compose --project-name "$project" -f "$COMPOSE" \
      down --remove-orphans >/dev/null
  )
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute)
      MODE=execute
      execute_requested=1
      shift
      ;;
    --core-revision)
      [[ $# -ge 2 ]]
      EXPECTED_CORE_REVISION=$2
      shift 2
      ;;
    --self-test)
      MODE=self-test
      self_test_requested=1
      shift
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

if [[ "$execute_requested" -eq 1 && "$self_test_requested" -eq 1 ]]; then
  printf '%s\n' 'refusing to combine --execute and --self-test' >&2
  exit 2
fi

if [[ "$MODE" == self-test ]]; then
  TEST_CURRENT=$(mktemp -d /dev/shm/ledgerbridge-web-runner-test.XXXXXXXX)
  trap 'rmdir -- "$TEST_CURRENT"' EXIT
  CURRENT=$TEST_CURRENT
  TEST_DOCKER_MODE=stop-fails
  docker() {
    case "$TEST_DOCKER_MODE" in
      stop-fails)
        return 47
        ;;
      healthy)
        printf 'true|healthy|0\n'
        ;;
      stopped)
        printf 'false|healthy|0\n'
        ;;
      missing)
        return 1
        ;;
      probe)
        [[ "$*" == \
          'compose --project-name ledgerbridge-web-release-probe-test -f compose.core-backed.yaml run --rm --no-deps -T --entrypoint python web -' ]]
        ;;
      probe-cleanup)
        [[ "$*" == \
          'compose --project-name ledgerbridge-web-release-probe-test -f compose.core-backed.yaml down --remove-orphans' ]]
        ;;
      *)
        return 48
        ;;
    esac
  }
  stop_status=0
  stop_web_for_release || stop_status=$?
  [[ "$stop_status" -eq 47 && "$stop_intended" -eq 1 ]]
  TEST_DOCKER_MODE=healthy
  container_health ledgerbridge-web-core
  TEST_RESTART_COUNT=0
  restart_web_from_tree() {
    TEST_RESTART_COUNT=$((TEST_RESTART_COUNT + 1))
    TEST_DOCKER_MODE=healthy
  }
  wait_for_web_health() {
    container_health ledgerbridge-web-core
  }
  sqlite_quick_check() {
    return 0
  }
  restore_web_service_from_tree "$CURRENT"
  [[ "$TEST_RESTART_COUNT" -eq 0 ]]
  TEST_DOCKER_MODE=stopped
  ! container_health ledgerbridge-web-core
  restore_web_service_from_tree "$CURRENT"
  [[ "$TEST_RESTART_COUNT" -eq 1 ]]
  TEST_DOCKER_MODE=missing
  ! container_health ledgerbridge-web-core
  restore_web_service_from_tree "$CURRENT"
  [[ "$TEST_RESTART_COUNT" -eq 2 ]]
  TEST_DOCKER_MODE=probe
  run_payroll_probe_container \
    "$CURRENT" ledgerbridge-web-release-probe-test </dev/null
  TEST_DOCKER_MODE=probe-cleanup
  cleanup_payroll_probe_project \
    "$CURRENT" ledgerbridge-web-release-probe-test
  printf 'WEB_RELEASE_SELF_TEST_OK stop_failure_recoverable=1 runtime_recovery_checked=1 isolated_probe_checked=1\n'
  exit 0
fi

[[ "$EXPECTED_REVISION" =~ ^[0-9a-f]{40}$ ]]
[[ "$EXPECTED_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]]
[[ -f "$ARCHIVE" && ! -L "$ARCHIVE" ]]
[[ "$(stat -c '%a' "$ARCHIVE")" == 600 ]]
[[ "$(sha256sum "$ARCHIVE" | awk '{print $1}')" == "$EXPECTED_ARCHIVE_SHA256" ]]
[[ -d "$CURRENT" && ! -L "$CURRENT" ]]
[[ -f "$CURRENT/.env" && ! -L "$CURRENT/.env" ]]
[[ "$(stat -c '%a' "$CURRENT/.env")" == 600 ]]
[[ -d "$CURRENT/state" && ! -L "$CURRENT/state" ]]
[[ "$(realpath -e "$CURRENT/state")" == "$CURRENT/state" ]]
[[ -f "$CURRENT/state/ledgerbridge-preview.sqlite3" \
    && ! -L "$CURRENT/state/ledgerbridge-preview.sqlite3" ]]
[[ "$(realpath -e "$CURRENT/state/ledgerbridge-preview.sqlite3")" \
    == "$CURRENT/state/ledgerbridge-preview.sqlite3" ]]
[[ "$(stat -c '%a' "$CURRENT/state/ledgerbridge-preview.sqlite3")" == 600 ]]
[[ -d "$CURRENT/vendor" && ! -L "$CURRENT/vendor" ]]
[[ "$(realpath -e "$CURRENT/vendor")" == "$CURRENT/vendor" ]]
[[ -d "$CURRENT/config" && ! -L "$CURRENT/config" ]]
[[ "$(realpath -e "$CURRENT/config")" == "$CURRENT/config" ]]
[[ -f "$CURRENT/config/enrolled-v1" && ! -L "$CURRENT/config/enrolled-v1" ]]
[[ "$(stat -c '%a' "$CURRENT/config/enrolled-v1")" == 444 ]]
[[ -d "$BACKUP_ROOT" && ! -L "$BACKUP_ROOT" ]]
[[ "$(realpath -e "$BACKUP_ROOT")" == "$BACKUP_ROOT" ]]
[[ "$(stat -c '%a' "$BACKUP_ROOT")" == 700 ]]

web_http_health() {
  [[ "$(curl -fsS http://127.0.0.1:8781/healthz)" == ok ]]
  curl -fsSI http://127.0.0.1:8781/healthz \
    | tr -d '\r' \
    | grep -Fqx 'X-LedgerBridge-Mode: core-backed'
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8781/)" == 200 ]]
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8781/overview)" == 200 ]]
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8781/review)" == 200 ]]
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8781/payroll)" == 200 ]]
  [[ "$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8781/api/v1/session)" == 401 ]]
}

sqlite_quick_check() {
  local database=$1
  python3 - "$database" <<'PY'
import sqlite3
import sys

database = sys.argv[1]
connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
try:
    result = connection.execute("PRAGMA quick_check").fetchone()
finally:
    connection.close()
if result != ("ok",):
    raise SystemExit("SQLite quick_check failed")
PY
}

validate_archive() {
  python3 - "$ARCHIVE" <<'PY'
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
required = {
    "REVISION",
    "SHA256SUMS",
    "compose.core-backed.yaml",
    "server/app.py",
    "dist/index.html",
}
seen: set[str] = set()
with tarfile.open(archive, "r:gz") as bundle:
    members = bundle.getmembers()
    if not members or len(members) > 2000:
        raise SystemExit("release archive member count is invalid")
    for member in members:
        name = member.name
        while name.startswith("./"):
            name = name[2:]
        if not name or name == ".":
            continue
        path = pathlib.PurePosixPath(name)
        if not path.parts or path.is_absolute() or ".." in path.parts:
            raise SystemExit("release archive path is unsafe")
        if not (member.isfile() or member.isdir()):
            raise SystemExit("release archive contains a non-regular member")
        if path.parts[0] in {".git", "state", "vendor", "config"} or path.name == ".env":
            raise SystemExit("release archive contains runtime state or credentials")
        seen.add(name)
if not required.issubset(seen):
    raise SystemExit("release archive lacks required members")
PY
}

safe_remove_plan_tree() {
  local target resolved
  target=$1
  [[ -n "$target" && -d "$target" && ! -L "$target" ]] || return 0
  resolved=$(readlink -f -- "$target")
  case "$resolved" in
    /dev/shm/ledgerbridge-web-plan.*)
      find "$resolved" -mindepth 1 -delete
      rmdir -- "$resolved"
      ;;
    *)
      printf 'refusing to remove unexpected plan directory: %s\n' "$resolved" >&2
      return 1
      ;;
  esac
}

prepare_tree() {
  local destination=$1
  install -d -m 700 "$destination"
  tar -xzf "$ARCHIVE" --no-same-owner -C "$destination"
  (
    cd "$destination"
    sha256sum --strict -c SHA256SUMS >/dev/null
  )
  [[ "$(tr -d '[:space:]' < "$destination/REVISION")" == "$EXPECTED_REVISION" ]]
  install -m 600 "$CURRENT/.env" "$destination/.env"
  (
    cd "$destination"
    docker compose --project-name "$PROJECT" -f "$COMPOSE" config --quiet
    docker compose --project-name "$PROJECT" -f "$COMPOSE" config --format json
  ) | python3 -c '
import json, sys
c = json.load(sys.stdin)
assert set(c.get("services", {})) == {"web"}
web = c["services"]["web"]
assert web.get("container_name") == "ledgerbridge-web-core"
assert not web.get("depends_on")
e = web["environment"]
assert str(e.get("PAYROLL_COMMANDS_ENABLED")) == "0"
assert not e.get("PAYROLL_ROLE_BINDINGS_JSON")
ports = web.get("ports", [])
assert len(ports) == 1
assert ports[0].get("host_ip") == "127.0.0.1"
assert int(ports[0].get("target")) == 8080
'
}

core_payroll_read_state() {
  local probe_tree=$1 probe_project probe_status=0 cleanup_status=0 path index
  local -a created_paths=()
  if [[ ! -d "$probe_tree" || -L "$probe_tree" \
      || "$(realpath -e "$probe_tree")" != "$probe_tree" \
      || ! -f "$probe_tree/.env" || -L "$probe_tree/.env" \
      || ! -f "$probe_tree/$COMPOSE" || -L "$probe_tree/$COMPOSE" ]]; then
    printf '0\n'
    return 0
  fi
  if ! container_health ledgerbridge-internal-reader-1 \
      || ! container_health payroll-verification; then
    printf '0\n'
    return 0
  fi
  if ! docker inspect ledgerbridge-internal-reader-1 2>/dev/null | python3 -c '
import json, sys
document = json.load(sys.stdin)[0]
environment = {}
for item in document.get("Config", {}).get("Env", []):
    if "=" in item:
        key, value = item.split("=", 1)
        environment[key] = value
networks = set(document.get("NetworkSettings", {}).get("Networks", {}))
ready = (
    environment.get("LEDGERBRIDGE_ENABLE_PAYROLL_INTEGRATION", "").lower() == "true"
    and environment.get("LEDGERBRIDGE_PAYROLL_BASE_URL") == "http://payroll-verification:4318"
    and environment.get("LEDGERBRIDGE_ENABLE_PAYROLL_COMMANDS", "").lower() == "false"
    and environment.get("LEDGERBRIDGE_PAYROLL_PROVIDER_TRUSTED_COMMAND_CONTRACT") == "disabled"
    and environment.get("LEDGERBRIDGE_PAYROLL_COMMAND_ALLOWLIST") == "[]"
    and environment.get("LEDGERBRIDGE_PAYROLL_ROLE_BINDINGS") == "{}"
    and "ledgerbridge_backend" in networks
)
raise SystemExit(0 if ready else 1)
' >/dev/null 2>&1; then
    printf '0\n'
    return 0
  fi
  for path in "$probe_tree/state" "$probe_tree/vendor" "$probe_tree/config"; do
    if [[ -e "$path" || -L "$path" ]]; then
      if [[ ! -d "$path" || -L "$path" ]]; then
        printf '0\n'
        return 0
      fi
    else
      if ! install -d -m 700 "$path"; then
        printf '0\n'
        return 0
      fi
      created_paths+=("$path")
    fi
  done
  probe_project="ledgerbridge-web-release-probe-${EXPECTED_REVISION:0:12}-${BASHPID}"
  run_payroll_probe_container \
    "$probe_tree" "$probe_project" >/dev/null 2>&1 <<'PY' || probe_status=$?
import base64
import hashlib
import hmac
import json
import os
import ssl
import time
import uuid
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def base64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


path = "/internal/v1/payroll/status"
issued_at = int(time.time())
assertion_key = os.environ["CORE_USER_ASSERTION_KEY"].encode("utf-8")
session_ref = hmac.new(
    assertion_key,
    b"ledgerbridge.payroll-session.v1\x00ledgerbridge-web-release-readiness-v1",
    hashlib.sha256,
).hexdigest()
claims = {
    "version": "ledgerbridge.payroll-bff-user-assertion.v1",
    "issuer": os.environ["CORE_ASSERTION_ISSUER"],
    "audience": os.environ["CORE_ASSERTION_AUDIENCE"],
    "subject": os.environ["CORE_USER_SUBJECT"],
    "authentication_generation": int(os.environ["CORE_AUTHENTICATION_GENERATION"]),
    "session_ref": session_ref,
    "entity_ref": os.environ["CORE_ENTITY_REF"],
    "action": "payroll.status.read",
    "method": "GET",
    "canonical_path": path,
    "body_sha256": hashlib.sha256(b"").hexdigest(),
    "resource_ref": "payroll-status",
    "expected_revision": None,
    "operation_id": None,
    "workload_principal": os.environ["CORE_WORKLOAD_PRINCIPAL"],
    "policy_generation": int(os.environ["CORE_POLICY_GENERATION"]),
    "issued_at": issued_at,
    "expires_at": issued_at + 30,
    "jti": str(uuid.uuid4()),
}
encoded = base64url(
    json.dumps(claims, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
)
signed = f"v1.{encoded}".encode("ascii")
signature = base64url(hmac.new(assertion_key, signed, hashlib.sha256).digest())
assertion = f"v1.{encoded}.{signature}"

context = ssl.create_default_context(cafile=os.environ["CORE_CA_FILE"])
context.minimum_version = ssl.TLSVersion.TLSv1_3
context.load_cert_chain(
    os.environ["CORE_CERT_FILE"],
    os.environ["CORE_KEY_FILE"],
)
opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
request = Request(
    f"{os.environ['CORE_BASE_URL'].rstrip('/')}{path}",
    headers={
        "Accept": "application/json",
        "X-LedgerBridge-User-Assertion": assertion,
    },
    method="GET",
)
with opener.open(
    request,
    timeout=float(os.environ.get("CORE_TIMEOUT_SECONDS", "10")),
) as response:
    if response.status != 200:
        raise SystemExit(1)
    if response.headers.get("Content-Type", "").split(";", 1)[0].lower() != "application/json":
        raise SystemExit(1)
    content = response.read(2 * 1024 * 1024 + 1)
if len(content) > 2 * 1024 * 1024:
    raise SystemExit(1)
payload = json.loads(content)
if not isinstance(payload, dict):
    raise SystemExit(1)
data = payload.get("data")
setup = data.get("setup_summary") if isinstance(data, dict) else None
capabilities = data.get("capabilities") if isinstance(data, dict) else None
safety_flags = {
    "payable",
    "payment_execution_allowed",
    "payment_execution_supported",
    "payment_submission_allowed",
    "payment_submission_supported",
    "payment_operations_exposed",
    "submission_supported",
}
pending = [payload]
safe_values = True
while pending:
    value = pending.pop()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in safety_flags and nested is not False:
                safe_values = False
            pending.append(nested)
    elif isinstance(value, list):
        pending.extend(value)
safe = (
    payload.get("contract_version") == "ledgerbridge.payroll-read.v1"
    and payload.get("entity_ref") == os.environ["CORE_ENTITY_REF"]
    and isinstance(payload.get("company_id"), str)
    and isinstance(data, dict)
    and isinstance(data.get("live_data_ready"), bool)
    and data.get("payment_operations_exposed") is False
    and isinstance(setup, dict)
    and setup.get("provider_connected") is True
    and setup.get("runtime_mode") == "live-provider"
    and capabilities == {"commands_enabled": False, "allowed_actions": []}
    and safe_values
)
raise SystemExit(0 if safe else 1)
PY
  cleanup_payroll_probe_project \
    "$probe_tree" "$probe_project" >/dev/null 2>&1 || cleanup_status=$?
  for ((index=${#created_paths[@]} - 1; index >= 0; index--)); do
    rmdir -- "${created_paths[index]}" || cleanup_status=1
  done
  if [[ "$probe_status" -ne 0 || "$cleanup_status" -ne 0 ]]; then
    printf '0\n'
    return 0
  fi
  printf '1\n'
}

core_classification_groups_state() {
  local probe_tree=$1 probe_project probe_status=0 cleanup_status=0 path index
  local -a created_paths=()
  if [[ ! -d "$probe_tree" || -L "$probe_tree" \
      || "$(realpath -e "$probe_tree")" != "$probe_tree" \
      || ! -f "$probe_tree/.env" || -L "$probe_tree/.env" \
      || ! -f "$probe_tree/$COMPOSE" || -L "$probe_tree/$COMPOSE" ]]; then
    printf '0\n'
    return 0
  fi
  if ! container_health ledgerbridge-internal-reader-1; then
    printf '0\n'
    return 0
  fi
  for path in "$probe_tree/state" "$probe_tree/vendor" "$probe_tree/config"; do
    if [[ -e "$path" || -L "$path" ]]; then
      if [[ ! -d "$path" || -L "$path" ]]; then
        printf '0\n'
        return 0
      fi
    else
      if ! install -d -m 700 "$path"; then
        printf '0\n'
        return 0
      fi
      created_paths+=("$path")
    fi
  done
  probe_project="ledgerbridge-web-classification-probe-${EXPECTED_REVISION:0:12}-${BASHPID}"
  run_payroll_probe_container \
    "$probe_tree" "$probe_project" >/dev/null 2>&1 <<'PY' || probe_status=$?
import os
import importlib.util

spec = importlib.util.spec_from_file_location(
    "ledgerbridge_core_backend_probe",
    "/app/server/core_backend.py",
)
if spec is None or spec.loader is None:
    raise SystemExit(1)
core_backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core_backend)
CoreBackedState = core_backend.CoreBackedState
CoreHttpClient = core_backend.CoreHttpClient


if os.environ.get("LEDGERBRIDGE_MODE") != "core-backed":
    raise SystemExit(1)
client = CoreHttpClient(
    base_url=os.environ["CORE_BASE_URL"],
    ca_file=os.environ["CORE_CA_FILE"],
    certificate_file=os.environ["CORE_CERT_FILE"],
    private_key_file=os.environ["CORE_KEY_FILE"],
    timeout_seconds=float(os.environ.get("CORE_TIMEOUT_SECONDS", "10")),
)
state = CoreBackedState(
    client,
    assertion_key=os.environ["CORE_USER_ASSERTION_KEY"].encode("utf-8"),
    assertion_issuer=os.environ["CORE_ASSERTION_ISSUER"],
    assertion_audience=os.environ["CORE_ASSERTION_AUDIENCE"],
    workload_principal=os.environ["CORE_WORKLOAD_PRINCIPAL"],
    policy_generation=int(os.environ["CORE_POLICY_GENERATION"]),
    user_subject=os.environ["CORE_USER_SUBJECT"],
    authentication_generation=int(os.environ["CORE_AUTHENTICATION_GENERATION"]),
    entity_ref=os.environ["CORE_ENTITY_REF"],
    business_unit_ref=os.environ["CORE_BUSINESS_UNIT_REF"],
    evidence_unlock_path=os.environ.get("CORE_EVIDENCE_UNLOCK_PATH", "").strip() or None,
    payroll_commands_enabled=False,
    payroll_role_bindings={},
)
page = state.candidate_classification_groups()
items = page.get("items")
safe = (
    page.get("contract_version") == "ledgerbridge.classification-groups.v1"
    and page.get("next_cursor") is None
    and isinstance(items, list)
    and len(items) == 96
)
raise SystemExit(0 if safe else 1)
PY
  cleanup_payroll_probe_project \
    "$probe_tree" "$probe_project" >/dev/null 2>&1 || cleanup_status=$?
  for ((index=${#created_paths[@]} - 1; index >= 0; index--)); do
    rmdir -- "${created_paths[index]}" || cleanup_status=1
  done
  if [[ "$probe_status" -ne 0 || "$cleanup_status" -ne 0 ]]; then
    printf '0\n'
    return 0
  fi
  printf 'CLASSIFICATION_GROUPS_PROBE_OK version=v1 groups=96\n' >&2
  printf '1\n'
}

core_state() {
  local probe_tree=$1 revision migration payroll_read_ready classification_groups_ready
  revision=$(sudo -n cat "$CORE_CURRENT/DEPLOYED_REVISION" | tr -d '[:space:]')
  migration=$(docker exec ledgerbridge-postgres-1 sh -lc \
    'exec psql -qAt -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version"')
  payroll_read_ready=$(core_payroll_read_state "$probe_tree")
  classification_groups_ready=$(core_classification_groups_state "$probe_tree")
  printf '%s|%s|%s|%s\n' \
    "$revision" "$migration" "$payroll_read_ready" "$classification_groups_ready"
}

snapshot_non_web_containers() {
  local output=$1 name
  : > "$output"
  while IFS= read -r name; do
    [[ -n "$name" && "$name" != ledgerbridge-web-core ]] || continue
    docker inspect -f '{{.Name}}|{{.Id}}|{{.State.StartedAt}}|{{.RestartCount}}' "$name" >> "$output"
  done < <(docker ps --format '{{.Names}}' | sort)
  sort -o "$output" "$output"
}

container_health ledgerbridge-web-core
web_http_health
sqlite_quick_check "$CURRENT/state/ledgerbridge-preview.sqlite3"
validate_archive

PLAN_TREE=$(mktemp -d /dev/shm/ledgerbridge-web-plan.XXXXXXXX)
trap 'safe_remove_plan_tree "$PLAN_TREE"' EXIT
prepare_tree "$PLAN_TREE"

IFS='|' read -r ACTIVE_CORE_REVISION ACTIVE_CORE_MIGRATION ACTIVE_PAYROLL_READ_READY \
  ACTIVE_CLASSIFICATION_GROUPS_READY \
  < <(core_state "$PLAN_TREE")
CORE_READY=0
if [[ -n "$EXPECTED_CORE_REVISION" \
      && "$EXPECTED_CORE_REVISION" =~ ^[0-9a-f]{40}$ \
      && "$ACTIVE_CORE_REVISION" == "$EXPECTED_CORE_REVISION" \
      && "$ACTIVE_CORE_MIGRATION" == "$EXPECTED_CORE_MIGRATION" \
      && "$ACTIVE_PAYROLL_READ_READY" == 1 \
      && "$ACTIVE_CLASSIFICATION_GROUPS_READY" == 1 ]]; then
  CORE_READY=1
fi

if [[ "$MODE" == plan ]]; then
  printf 'WEB_RELEASE_PLAN_OK revision=%s archive_sha256=%s core_ready=%s payroll_read_ready=%s classification_groups_ready=%s core_migration=%s target=web-only\n' \
    "$EXPECTED_REVISION" "$EXPECTED_ARCHIVE_SHA256" "$CORE_READY" \
    "$ACTIVE_PAYROLL_READ_READY" "$ACTIVE_CLASSIFICATION_GROUPS_READY" \
    "$ACTIVE_CORE_MIGRATION"
  exit 0
fi

[[ -n "$EXPECTED_CORE_REVISION" && "$EXPECTED_CORE_REVISION" =~ ^[0-9a-f]{40}$ ]]
[[ "$CORE_READY" == 1 ]]

safe_remove_plan_tree "$PLAN_TREE"
trap - EXIT
PLAN_TREE=

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
SHORT_REVISION=${EXPECTED_REVISION:0:12}
STAGE=/home/aiadmin/services/ledgerbridge-web.release-${SHORT_REVISION}-${STAMP}
ROLLBACK_TREE=/home/aiadmin/services/ledgerbridge-web.before-${SHORT_REVISION}-${STAMP}
FAILED_TREE=/home/aiadmin/services/ledgerbridge-web.failed-${SHORT_REVISION}-${STAMP}
TRANSACTION=/home/aiadmin/services/.ledgerbridge-web-release-${SHORT_REVISION}-${STAMP}
STATE_BACKUP=$BACKUP_ROOT/state-before-${SHORT_REVISION}-${STAMP}.sqlite3
for path in "$STAGE" "$ROLLBACK_TREE" "$FAILED_TREE" "$TRANSACTION" "$STATE_BACKUP"; do
  [[ ! -e "$path" && ! -L "$path" ]]
done
install -d -m 700 "$BACKUP_ROOT" "$TRANSACTION"
prepare_tree "$STAGE"
cp -a --reflink=auto "$CURRENT/vendor" "$STAGE/vendor"
sudo cp -a "$CURRENT/config" "$STAGE/config"
sudo chown "$(id -u):$(id -g)" "$STAGE/config"

snapshot_non_web_containers "$TRANSACTION/non-web-before.tsv"
CURRENT_CONTAINER_ID=$(docker inspect -f '{{.Id}}' ledgerbridge-web-core)
CURRENT_REVISION=$(python3 - "$CURRENT" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
manifest = root / "REVISION.json"
if manifest.is_file():
    value = json.loads(manifest.read_text(encoding="utf-8")).get("revision", "")
else:
    revision = root / "REVISION"
    value = revision.read_text(encoding="utf-8").strip() if revision.is_file() else "unknown"
print(value)
PY
)

current_moved=0
swapped=0
completed=0

restart_web_from_tree() {
  local tree=$1
  (
    cd "$tree"
    docker compose --project-name "$PROJECT" -f "$COMPOSE" \
      up -d --no-deps --force-recreate web >/dev/null
  )
}

wait_for_web_health() {
  local attempt
  for attempt in $(seq 1 45); do
    if container_health ledgerbridge-web-core && web_http_health; then
      return 0
    fi
    sleep 1
  done
  return 1
}

recover_release() {
  local status=$1 reason=$2 recovery_failed=0
  trap - ERR INT TERM HUP EXIT
  if [[ "$completed" -eq 0 ]]; then
    if [[ "$current_moved" -eq 1 && -d "$ROLLBACK_TREE" ]]; then
      if docker inspect ledgerbridge-web-core >/dev/null 2>&1; then
        if ! docker stop --time 30 ledgerbridge-web-core >/dev/null; then
          recovery_failed=1
        fi
      fi
      if [[ -e "$CURRENT" ]]; then
        if [[ -e "$FAILED_TREE" ]] || ! mv -- "$CURRENT" "$FAILED_TREE"; then
          recovery_failed=1
        fi
      fi
      if [[ ! -e "$CURRENT" ]]; then
        if ! mv -- "$ROLLBACK_TREE" "$CURRENT"; then
          recovery_failed=1
        fi
      else
        recovery_failed=1
      fi
    fi
    if [[ "$stop_intended" -eq 1 || "$current_moved" -eq 1 ]]; then
      if [[ ! -d "$CURRENT" ]] \
          || ! restore_web_service_from_tree "$CURRENT"; then
        recovery_failed=1
      fi
    fi
    if [[ -f "$TRANSACTION/non-web-before.tsv" ]]; then
      if ! snapshot_non_web_containers "$TRANSACTION/non-web-after-recovery.tsv" \
          || ! cmp -s "$TRANSACTION/non-web-before.tsv" \
            "$TRANSACTION/non-web-after-recovery.tsv"; then
        recovery_failed=1
      fi
    fi
    if [[ -n "$EXPECTED_CORE_REVISION" ]]; then
      local recovered_core_revision recovered_core_migration recovered_payroll_read_ready
      local recovered_classification_groups_ready
      if ! IFS='|' read -r recovered_core_revision recovered_core_migration \
          recovered_payroll_read_ready recovered_classification_groups_ready \
          < <(core_state "$CURRENT") \
          || [[ "$recovered_core_revision" != "$EXPECTED_CORE_REVISION" ]] \
          || [[ "$recovered_core_migration" != "$EXPECTED_CORE_MIGRATION" ]] \
          || [[ "$recovered_payroll_read_ready" != 1 ]] \
          || [[ "$recovered_classification_groups_ready" != 1 ]]; then
        recovery_failed=1
      fi
    fi
  fi
  if [[ "$recovery_failed" -ne 0 ]]; then
    printf 'WEB_RELEASE_RECOVERY_FAILED reason=%s transaction=%s\n' "$reason" "$TRANSACTION" >&2
    exit 90
  fi
  printf 'WEB_RELEASE_ROLLBACK_OK reason=%s\n' "$reason" >&2
  exit "$status"
}

rollback_on_error() {
  local status=$?
  recover_release "$status" ERR
}

release_exit_trap() {
  local status=$?
  if [[ "$completed" -eq 0 ]]; then
    if [[ "$status" -eq 0 ]]; then
      status=91
    fi
    recover_release "$status" EXIT
  fi
}

trap rollback_on_error ERR
trap 'recover_release 130 INT' INT
trap 'recover_release 143 TERM' TERM
trap 'recover_release 129 HUP' HUP
trap release_exit_trap EXIT

stop_status=0
stop_web_for_release || stop_status=$?
if [[ "$stop_status" -ne 0 ]]; then
  recover_release "$stop_status" STOP
fi

python3 - "$CURRENT/state/ledgerbridge-preview.sqlite3" "$STATE_BACKUP" <<'PY'
import os
import sqlite3
import sys

source_path, destination_path = sys.argv[1:]
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
destination = sqlite3.connect(destination_path)
try:
    source.backup(destination)
    result = destination.execute("PRAGMA quick_check").fetchone()
finally:
    destination.close()
    source.close()
if result != ("ok",):
    raise SystemExit("SQLite backup quick_check failed")
os.chmod(destination_path, 0o600)
PY

cp -a --reflink=auto "$CURRENT/state" "$STAGE/state"
[[ -f "$STAGE/state/ledgerbridge-preview.sqlite3" \
    && ! -L "$STAGE/state/ledgerbridge-preview.sqlite3" ]]
sqlite_quick_check "$STAGE/state/ledgerbridge-preview.sqlite3"

current_moved=1
mv -- "$CURRENT" "$ROLLBACK_TREE"
mv -- "$STAGE" "$CURRENT"
swapped=1
restart_web_from_tree "$CURRENT"

wait_for_web_health
container_health ledgerbridge-web-core
web_http_health
sqlite_quick_check "$CURRENT/state/ledgerbridge-preview.sqlite3"
[[ "$(tr -d '[:space:]' < "$CURRENT/REVISION")" == "$EXPECTED_REVISION" ]]

NEW_CONTAINER_ID=$(docker inspect -f '{{.Id}}' ledgerbridge-web-core)
[[ "$NEW_CONTAINER_ID" != "$CURRENT_CONTAINER_ID" ]]
RUNTIME_IMAGE=$(docker inspect -f '{{.Image}}' ledgerbridge-web-core)
python3 - "$CURRENT" "$EXPECTED_REVISION" "$CURRENT_REVISION" "$EXPECTED_ARCHIVE_SHA256" \
  "$RUNTIME_IMAGE" "$NEW_CONTAINER_ID" "$STAMP" "$ROLLBACK_TREE" "$STATE_BACKUP" <<'PY'
import json
import pathlib
import sys

(
    root_raw,
    revision,
    previous_revision,
    archive_sha256,
    runtime_image,
    container_id,
    deployed_at,
    rollback_tree,
    state_backup,
) = sys.argv[1:]
root = pathlib.Path(root_raw)
revision_manifest = {
    "schema": "ledgerbridge.web.release.v1",
    "service": "ledgerbridge-web",
    "revision": revision,
    "short_revision": revision[:12],
    "previous_revision": previous_revision,
    "runtime_mode": "core-backed",
    "archive_sha256": archive_sha256,
}
deployment_manifest = {
    "schema": "ledgerbridge.web.deployment.v1",
    "revision": revision,
    "previous_revision": previous_revision,
    "deployed_at_utc": deployed_at,
    "runtime_mode": "core-backed",
    "archive_sha256": archive_sha256,
    "runtime_image": runtime_image,
    "container_id": container_id,
    "rollback_tree": rollback_tree,
    "state_backup": state_backup,
}
for name, payload in (
    ("REVISION.json", revision_manifest),
    ("DEPLOYMENT_REVISION.json", deployment_manifest),
):
    target = root / name
    temporary = root / f".{name}.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(target)
PY

snapshot_non_web_containers "$TRANSACTION/non-web-after.tsv"
cmp -s "$TRANSACTION/non-web-before.tsv" "$TRANSACTION/non-web-after.tsv"
IFS='|' read -r FINAL_CORE_REVISION FINAL_CORE_MIGRATION FINAL_PAYROLL_READ_READY \
  FINAL_CLASSIFICATION_GROUPS_READY \
  < <(core_state "$CURRENT")
[[ "$FINAL_CORE_REVISION" == "$EXPECTED_CORE_REVISION" ]]
[[ "$FINAL_CORE_MIGRATION" == "$EXPECTED_CORE_MIGRATION" ]]
[[ "$FINAL_PAYROLL_READ_READY" == 1 ]]
[[ "$FINAL_CLASSIFICATION_GROUPS_READY" == 1 ]]

completed=1
stop_intended=0
trap - ERR INT TERM HUP EXIT
printf 'WEB_RELEASE_OK revision=%s previous=%s rollback_tree=%s state_backup=%s target=web-only\n' \
  "$EXPECTED_REVISION" "$CURRENT_REVISION" "$ROLLBACK_TREE" "$STATE_BACKUP"
