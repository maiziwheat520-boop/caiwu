# LedgerBridge Phase 3 Slice B Claude audit remediation

Date: 2026-08-23

## Review evidence

Claude's independent report is preserved at
`G:\\我的云端硬盘\\AI\\LedgerBridge-Claude\\review-worktree-phase3-runner\\docs\\reviews\\2026-08-22-phase-3-connector-runner-claude.md`.

- initial audited code parent: `bc833b200c94cefb7d930d1ab3bce22d58901331`;
- initial Claude review commit: `feecfc991cba0a20b59e7388aed6e6a5825f37ca`;
- follow-up audited code parent: `2ff9812788e711aa191fdc09df403a8fbe15bd5c`;
- follow-up Claude review commit: `96d2287f1ec0c5459bd75462f286cb53f813d246`;
- follow-up verdict: `CHANGES REQUIRED` — 1 HIGH, 4 MEDIUM, 6 LOW;
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

The follow-up review found that the first normalization was incomplete and that
three regression tests were not defect-sensitive enough:

- P3-H2: NUL and lone-surrogate text could still enter terminal summaries or
  parsed-record fields, leaving jobs non-terminal or raising an encoding error;
- P3-M1R: the slow-response fixture wrote only one `0x00` byte, so the per-receive
  timeout fired before a complete frame could exercise the overall deadline;
- P3-M3R: the production flag had no `src/` composition-root call site;
- P3-M5: the frame kind byte made the default artifact payload one byte too
  large, and the chunk-count ceiling did not cover a full 50 MiB artifact;
- P3-M6: the stale-response handler could close before consuming the artifact
  stream, producing flaky `RUNNER_UNAVAILABLE` results.

The second remediation, committed as `b65581e7e76c79fce7f6c6996a6c68a3201d6483`,
closes those boundaries: protocol and Connector JSON validation reject NUL/U+D800–DFFF,
`RunnerClientError` sanitizes unsafe summaries, the worker composition root
constructs `EvidenceImporter(production=settings.env == "production")`, frame
payload and chunk-count limits account for the kind byte, and the regression
fixtures now send a valid slow terminal frame and consume `ARTIFACT_END` before
returning a stale response. Detection-time contract violations are terminalized
as `CONNECTOR_CONTRACT` with no partial records.

The six reported LOW findings were non-blocking design notes: the empty
pre-request terminal ID is client-rejected, direct prefix detection is a
synthetic helper while production uses `detect_verified`, the runner facade's
pending request is fail-closed on concurrent misuse, and socket peer loss is
bounded by the server request timeout. The production cgroup effectiveness
check and Windows/Linux coverage wording remain deployment/process notes. Slice
B is still not deployed.

## Verification

- local Windows: Ruff, format, strict mypy, Bandit, sensitive-path scan, offline
  lock resolution, and full pytest passed (`104 passed`, `114 skipped`, one
  known Starlette warning);
- Hermes disposable Linux/PostgreSQL 16 replay: `222 passed`, one warning,
  `95.47%` coverage with the unchanged `--cov-fail-under=95` gate; migration
  upgrade/downgrade/upgrade, Bandit, and strict pip-audit all passed;
- hosted GitHub Actions run `32593102155` for branch head `65b15df`
  passed `secrets`, `quality`, and `compose`; the code/test under review is the
  fixed ancestor `bd2ba4a` and the later commits are documentation-only;
- no production tree, image, database, artifact, credential, or real financial
  evidence was changed.

Before the final audit handoff, Codex added a bounded outbound-send deadline in
`5dfaca8bcc81641a6a4e106997a20cb7db0fc58c` and public-path integration tests in
`bd2ba4a` for hostile `record_locator`/`raw_fields` text. These are additional
hardening changes, not a change to the production deployment scope.

## Final Claude recheck

Claude's final narrow report is preserved at
`G:\\我的云端硬盘\\AI\\LedgerBridge-Claude\\review-worktree-phase3-runner\\docs\\reviews\\2026-08-22-phase-3-connector-runner-claude.md`,
commit `a19fa640247a98adacdb31741f6172b722f14f03`, with parent exactly
`bd2ba4a2513597e83764a56215c72b61c99a8c1e`.

**Verdict: APPROVED — 0 BLOCKER / 0 HIGH / 0 MEDIUM / 10 LOW.** Claude
replayed 222 tests with 95.47% coverage, confirmed all five follow-up findings
closed, verified bidirectional send/receive deadlines, full 50 MiB chunking,
stable stale-response rejection, and eight of nine defect injections sensitive.
The remaining LOWs are tracked design/deployment notes: harden the currently
unused upload-side `IngestMetadata` text validator before an upload endpoint,
deduplicate the text helper, add a worker composition-root call assertion,
leave one chunk-count headroom, correct two documentation phrasings, and retain
the previously declared container/peer-loss/concurrency notes.

After that report, Codex independently closed the text-helper, upload-metadata,
worker-call, and full-size chunk regression gaps in `e296b0d`; the associated
local suite now passes 109 tests with 118 Windows/POSIX skips. Hosted run
`32595863205` for head `8c43ffa` passed `secrets`, `quality`, and `compose`.
The one remaining chunk-count headroom item is a capacity note, not a
correctness failure; Claude's report does not claim to audit `e296b0d`.

## Gate status

P3-H1, P3-H2, P3-M1R, P3-M3R, P3-M5, and P3-M6 are remediated and independently
tested. Claude's final recheck is **APPROVED** with only non-blocking LOW notes.
The branch is not merged and Slice B is not deployed. Merge, Slice B deployment,
and real Connector registration remain separate user-authorized gates.
