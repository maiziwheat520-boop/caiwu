# LedgerBridge release-audit final remediation

Date: 2026-08-24  
Branch: `ai/chatgpt/phase-3-connector-runner`  
Final implementation head: `e2c31be18ce77cbcecc2dec7be3aea2f195367b8`  
PR: #18 (review-only)

## Verdict

The release-readiness findings supplied by the independent audit are closed in
the review branch. The changes are verified locally, in disposable Hermes
Linux/PostgreSQL infrastructure, and by the hosted `secrets`, `quality`, and
`compose` jobs. This is a code/readiness closure, not authorization to merge,
migrate the existing database, enable a real Connector, or import financial
evidence.

## Finding-by-finding closure

| Finding | Remediation and proof |
| --- | --- |
| Role-membership HIGH | `20260823_0006` enumerates `pg_auth_members`, revokes every direct API/worker membership, and reasserts `NOINHERIT`/non-privileged attributes. The regression grants `ledgerbridge_owner` before `0006`, proves two memberships exist, upgrades, proves zero remain, and rejects `SET ROLE`. Hermes evidence: `role_drift_replay=PASS`. |
| Runner connection/spool MEDIUM | `connector_runner.py` admits connections before control reads and reserves declared aggregate spool bytes before creating a spool. Reservations survive cancelled waits until the underlying executor task finishes; all failure paths release exactly once. Capacity loss is `RUNNER_UNAVAILABLE`, importer retry classification preserves the job as `RUNNING`, and protocol corruption remains terminal. Runner `/tmp` is 96 MiB for a 64 MiB hard spool ceiling. |
| Heartbeat symlink LOW | `worker.write_heartbeat()` uses an exclusive random `mkstemp()` inode, flushes and fsyncs it, then atomically replaces the target. The regression preloads a predictable symlink and proves the sensitive target is unchanged. |
| P2-B1 historical `search_path` blocker | Forward migration `20260824_0009` fixes all fourteen Phase 1/2 security functions. Six table-reading functions are recreated with `SET search_path = pg_catalog` and `public.*` references; the other eight are altered and all fourteen are asserted after DDL. A separate `0008` control database demonstrates the historical `pg_temp` unbalanced-posting bypass; a second database upgrades to `0009` and rejects the full Claude five-attack sequence. Downgrading to `0008` intentionally preserves the hardened definitions. |
| Artifact descriptor TOCTOU HIGH | The existing single-descriptor verification path remains covered: digest, size, and inode identity are checked on the same open handle before the connector reads bytes. |

## Verification evidence

Windows local gates:

- `244 passed, 147 skipped, 1 warning` (PostgreSQL/POSIX-only cases are
  intentionally skipped on Windows).
- `uv lock --offline --check`, Ruff format/check, strict mypy, Bandit,
  `scripts/check_sensitive_paths.py`, and `git diff --check` passed.

Hermes disposable gates:

- Full Linux/PostgreSQL suite: `391 passed`, coverage `95.23%` (95% threshold).
- Migrations: `upgrade head → downgrade base → upgrade head`, including
  `20260824_0009` and its safe downgrade behavior.
- Ruff, strict mypy, Bandit, `pip-audit --strict`, and sensitive-path scanning
  passed; pip-audit reported no known vulnerabilities.
- Runner Linux regressions cover early connection rejection, stable
  `RUNNER_UNAVAILABLE` mapping, execution-pool saturation, spool reservation
  release, heartbeat safety, and the full declared artifact boundary.
- Every temporary container, network, volume, and `/tmp/ledgerbridge-full-audit-*`
  directory was removed in the cleanup trap.

Hosted GitHub CI for `e2c31be` passed both push run `32679541438` and pull-request
run `32679543455`, with all `secrets`, `quality`, and `compose` jobs successful.

Production Hermes was read-only verified after the run: API and worker images
remain revision `e426b488b2abb02f10ef02a61aae7ebe24c3283f`, PostgreSQL remains at
Alembic `20260822_0004`, the API ready probe is healthy, and no async dispatch,
Connector, or evidence row was created.

## Remaining enablement gates

These are intentionally not guessed or enabled by this remediation:

- killable process isolation for hostile Connectors (the current runner bounds
  threads and resources but cannot terminate arbitrary blocking code);
- trusted authentication and capability policy for the internal routes;
- signed declarative manifest verification and key custody/rotation;
- production role-password rollout, migration, feature flags, and protected PR
  merge/deployment;
- real Alipay/WeChat/Bank-of-China parser contracts, sanitized samples, and
  external OAuth/Graph credentials.

Until those gates receive their own reviewed implementation and authorization,
the branch remains review-only and real financial evidence remains disabled.
