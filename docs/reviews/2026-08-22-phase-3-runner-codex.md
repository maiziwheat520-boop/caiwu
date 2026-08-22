# LedgerBridge Phase 3 Slice B Connector Runner Implementation

Date: 2026-08-22

## Scope and authorization

This report records the independent Slice B implementation requested after the
Phase 3 Slice A deployment. It is an implementation and acceptance report only:
the branch has been pushed for review, no protected PR was created, Slice B was
not deployed to production, and no real Connector, OAuth flow, mailbox collector,
or financial evidence was enabled. The production revision remains the Slice A merge
`e426b488b2abb02f10ef02a61aae7ebe24c3283f`.

Implementation branch: `ai/chatgpt/phase-3-connector-runner`.

Implementation commits:

- `23412d2` — isolate Connector execution over a Unix socket;
- `3f468ec` — harden runner protocol boundaries and failure tests;
- `cb8f6d2` — reject duplicate JSON control keys and add regression coverage.
- `ebf5a42` — make the runner socket typing cross-platform for Linux CI;
- `6c1b6c4` — cover Linux runner failure boundaries;
- `ebc2974` — close protocol/client coverage gaps and normalize empty pre-request IDs;
- `991e617` — close the remaining Linux runner coverage threshold and response cases.

## Implemented boundary

- `runner_protocol.py` defines protocol version 1 with length-delimited frames,
  separate control/binary/record/terminal limits, request IDs, operation and
  provenance binding, verified byte count/SHA-256, duplicate-key rejection, and
  bounded typed JSON records.
- `connector_runner.py` receives one request per Unix connection, spools only
  the declared artifact bytes, recomputes the digest, executes a registered
  Connector in a bounded worker thread, validates identity/provenance/locators,
  and emits either a complete result or one sanitized terminal error.
- `runner_client.py` validates response IDs, operation, digest, byte count,
  record count, and total response bytes. It discards all partial records when a
  terminal error arrives. `RunnerConnector` provides the importer facade while
  retaining in-process connectors only for tests/synthetic fixtures.
- Import routing maps runner failures to bounded `ImportJob` terminal codes and
  keeps the existing exactly-one-match and Phase 2 output validation rules.
- `execution_mode=runner` is now an explicit Connector contract; production
  validation rejects in-process execution.
- Compose adds a separate `connector-runner` image/service. It uses
  `network_mode: none`, only the Unix socket volume, no app/database/artifact/
  OAuth environment or mounts, UID 10001, read-only root, no-new-privileges,
  dropped capabilities, 32 MiB tmpfs, 128 MiB memory, 64 PIDs, and 0.50 CPU.

## Local verification

The final local run on Windows completed:

- `uv lock --offline`;
- Ruff format/check;
- strict mypy for `src`, `alembic`, `tests`, and `scripts`;
- Bandit (warnings only for existing intentional `nosec` sites);
- full pytest: **99 passed, 103 skipped, 1 warning**. Skips are explicit
  Windows/POSIX or missing local PostgreSQL integration conditions.

The protocol/runner/compose and importer-focused subset also passed. The new
duplicate-key test proves a control message cannot smuggle two values for one
field and rely on parser last-write-wins behavior.

## Linux full-suite closure

The disposable Hermes PostgreSQL 15 replay of the exact CI pytest command passed
after the final runner test additions:

- **204 passed, 1 warning**;
- coverage **95.01%** with `--cov-fail-under=95`;
- the run covered POSIX Unix-socket IPC, runner error mapping, protocol limits,
  response validation, and PostgreSQL-backed integration tests.

The earlier Linux replay exposed a test fixture that made both byte count and
digest invalid, so the client correctly reported the first applicable
`ARTIFACT_SIZE_MISMATCH`. The fixture was corrected to isolate the digest case;
the final replay is green. The repository GitHub Actions run for the final
documentation head `bc833b200c94cefb7d930d1ab3bce22d58901331` (`32575131259`)
passed all three required jobs: `secrets`, `quality`, and `compose`.

## Hermes disposable runner acceptance

The current branch was archived and built as the disposable image
`ledgerbridge-connector-runner:cb8f6d2`; it was never attached to the production
Compose project. A synthetic IPC smoke passed inside a container with the
runner supervisor and client. A hostile probe passed with:

- no network route or DNS reachability;
- `/app` not writable;
- no `/var/lib/ledgerbridge/artifacts` path;
- read-only root, 128 MiB memory, 64 PIDs, 0.50 CPU, all capabilities dropped,
  and `no-new-privileges:true`;
- image environment limited to base Python/runtime values, with no database URL,
  artifact root, OAuth, mail, or provider credentials.

The production Compose project was not used for this test; no `down --volumes`
or production volume operation is part of the acceptance procedure.

## Production recovery and fresh restore evidence

During an earlier temporary-image cleanup, a test command accidentally used the
default Compose project name and `down --volumes`, which collided with the
production `ledgerbridge` project. It stopped the API/worker/PostgreSQL
containers and removed the production-named volumes. This was an operational
incident caused by the test cleanup, not a Slice B deployment.

Recovery was performed immediately from the unchanged `/srv/ai-center/ledgerbridge`
tree: PostgreSQL was recreated, the runtime-role bootstrap and Alembic migration
were run, and API/worker were restarted. The recovered production state now
passes live/ready health checks, OpenAPI 404, migration `20260822_0004 (head)`,
the 35-file manifest, image label `e426b488b2abb02f10ef02a61aae7ebe24c3283f`,
`data_checksums=on`, `TEMP=false`, public `CREATE=false`, 52 functions with 16
pinned `search_path` configurations, 16 enabled triggers, read-only registry
grants, seeded `ingest_channel=2`/`source_system=1`, zero business/evidence
rows, and an empty `0700` artifact root owned by UID/GID 10001.

Because the earlier final backup predated the incident, a fresh encrypted backup
and isolated restore rehearsal were created after recovery:

- backup: `/srv/ai-center/backups/ledgerbridge/20260822T121526Z-e426b488b2ab`;
- rehearsal: `restore-rehearsal-20260822T121556Z.json`;
- result: isolated restore rehearsal passed.

No real financial evidence was present before or after recovery. The incident
and recovery are recorded here and in `work.md`; future disposable Compose tests
must use a unique project name and must never target `/srv/ai-center/ledgerbridge`.

## Remaining gates

Slice B is ready for a narrow independent audit, not for production release.
The narrow Claude audit, protected PR review, merge authorization, and any
future production deployment remain separate gates. The production runner is
not enabled, and real Connector registration remains out of scope.
