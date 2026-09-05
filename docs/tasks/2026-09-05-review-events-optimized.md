# Complete review decision events

Base: production/web `5bd37488c1b5bf43311fa883a19211d4809fb383`.
Branch: `ai/chatgpt/review-events-optimized-20260905`.

Claude's four-file uncommitted decision-events patch was preserved externally
and applied to this isolated tree. The original Claude tree is untouched.

A correction or conflict resolution followed by confirmation returns two Core
events. The BFF now forwards both while retaining the old single `event` field.
The App immediately renders them newest first, replacing already-loaded copies
by event ID. A malformed event projection returns a structured
`503 CORE_CONTRACT_INVALID` instead of an unhandled conversion exception.

Verification at the existing App/network and BFF/Core boundaries:

- New App regression failed against the production implementation: two audit
  rows appeared where three were required. Applying the preserved patch made it
  pass. It checks ordering, duplicate removal, immediate conflict reasoning and
  the changed amount without reloading the history endpoint.
- New malformed-first-event test raised ValueError before the guard, then
  returned the expected contract problem after it.
- Frontend: 126 tests passed across 9 files.
- Backend discovery: 181 tests run, 180 passed and one platform symlink test
  skipped. The existing Core Python environment was reused.
- Backend module entry: 72 tests passed, including the new class now defined
  before unittest.main().
- ESLint, typography gate, TypeScript and production Vite build passed.
  Vite reports the existing advisory for a main chunk above 500 kB
  (this build: 545.24 kB, 160.65 kB gzip); it is not a build failure.
- git diff --check passed.

No database, Core service, production reference, original Claude worktree or
deployment was changed. node_modules is a junction to the canonical Web repo.
The response remains compatible with single-event synthetic/older BFF clients.
Full production rollout and verification remain with the release owner.
