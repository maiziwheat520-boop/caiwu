# Monthly workspace reference slice

This Web-only release starts from the exact live `production/web` revision.
No Core schema, permissions, ledger entries, payments or payroll provider changes.

## Behavior

- UI month defaults use the previous calendar month in Asia/Shanghai. Selecting
  or defaulting a month never shifts source dates or accounting assignments.
- Live cash is still read from Core. Presentation groups company/category/flow
  while retaining each original source, rule and fact reference in secondary details.
- A private historical reference report is served only to the configured owner
  through a full authenticated session. It is **not** Core review state, a posting
  draft or a second ledger. No consumer may post from this JSON directly.
- The report includes its revision and confirmation date; release records retain
  its SHA256 and underlying analytical-source hashes outside Git.
- Historical corrections and transition bridges stay separate from cash totals.
- Missing periods, unconnected payroll data and accepted historical exceptions
  remain explicit. No fallback to another month or fabricated zero.

## Frozen inputs and cutover

After tests, build and independent review, acquire the workspace release lock.
Package only `dist/` and `server/` (exclude tests and bytecode). Do not package
`config/`, `state/`, `.env`, private reference JSON or user financial source files.
Transfer the private report separately to a protected staging directory.

Run `release_web_reference.py` on VM103 with exact `--expected` and `--revision`
Git hashes, `--archive`, `--archive-sha256`, `--report`, `--report-sha256`.
It validates input hashes/archive paths/report, backs up code/assets/revision,
stops Web, swaps frozen files, installs only its own private file in existing
read-only `/config`, and recreates Web with the existing compose configuration.
Health or access-guard failure restores the old files and recreates old Web.
Failure to stop an already-removed container must not prevent rollback.

Post-cutover verify exact revision, assets and private-file hashes, healthy Web,
anonymous API401/static404, and unchanged Core revision and migration head.
Record evidence/backups in workspace work.md, update production/web, release the
lock and remove the clean pushed short-lived worktree. Authentication and live
cash verification must use existing read-only backend facilities; never forge
user sessions or operate the user's desktop/browser to prove a deployment.

Rollback filesystem branches are covered by synthetic tests in
`server/tests/test_web_reference_release.py`; normal tests never contact VM103.
