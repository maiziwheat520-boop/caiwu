# LedgerBridge Phase 3 Slice B Claude audit remediation

Date: 2026-08-23

## Review evidence

Claude's independent report is preserved at
`G:\\我的云端硬盘\\AI\\LedgerBridge-Claude\\review-worktree-phase3-runner\\docs\\reviews\\2026-08-22-phase-3-connector-runner-claude.md`.

- audited code parent: `bc833b200c94cefb7d930d1ab3bce22d58901331`;
- Claude review commit: `feecfc991cba0a20b59e7388aed6e6a5825f37ca`;
- verdict: `CHANGES REQUIRED` — 1 HIGH, 4 MEDIUM, 5 LOW;
- Claude did not push, merge, create a PR, deploy, or access Hermes production.

The report independently reproduced the Linux/PostgreSQL suite and confirmed
the runner protocol and isolation paths. Its required pre-merge finding was
P3-H1: an untrusted runner could return an invalid or overlong `error_code`,
which was then written through the importer into the constrained `import_job`
column. The failed terminal could therefore leave a job `PENDING` without a
terminal audit event.

## Remediation

Codex fixed the shared trust boundary in `5dab33e`:

- `RunnerClientError` now accepts only `[A-Z][A-Z0-9_]{0,63}` error codes and
  maps every invalid value to `RUNNER_ERROR`;
- diagnostic summaries are trimmed to the database's 500-character bound and
  blank values map to a bounded fallback;
- the importer terminalizes the normalized error, so the job becomes `FAILED`,
  keeps zero partial `SourceRecord` rows, and writes exactly one
  `import.complete` audit event;
- the client now carries one monotonic deadline through every response read,
  closing the per-receive slowloris gap;
- tests invoke the real `serve()` entrypoint and assert the Unix socket mode is
  `0600`, rather than reimplementing startup in a test helper;
- `validate_connector(..., production=True)` is now wired through the
  `EvidenceImporter(production=True)` path, rejecting in-process connectors
  before production routing;
- new behavior tests cover invalid/overlong error codes, terminal audit state,
  slow responses, socket permissions, and production execution-mode enforcement.

The four reported LOW findings remain non-blocking design notes: the empty
pre-request terminal ID is client-rejected, direct prefix detection is a
synthetic helper while production uses `detect_verified`, the runner facade's
pending request is fail-closed on concurrent misuse, and socket peer loss is
bounded by the server request timeout. Container cgroup effectiveness remains
an environment-level deployment check; Slice B is still not deployed.

## Verification

- local Windows: Ruff, format, strict mypy, Bandit, sensitive-path scan, offline
  lock resolution, and full pytest passed (`99 passed`, `111 skipped`, one
  known Starlette warning);
- Hermes disposable Linux/PostgreSQL replay: `210 passed`, one warning,
  `95.85%` coverage with the unchanged `--cov-fail-under=95` gate;
- hosted GitHub Actions run `32586181832` for `5dab33e` passed `secrets`,
  `quality`, and `compose`;
- no production tree, image, database, artifact, credential, or real financial
  evidence was changed.

## Gate status

P3-H1 and the four medium findings are remediated and independently tested.
This branch is ready for a fresh narrow Claude recheck, not for merge or
production deployment. Merge, Slice B deployment, and real Connector
registration remain separate user-authorized gates.
