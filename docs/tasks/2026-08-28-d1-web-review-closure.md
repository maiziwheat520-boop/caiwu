# Task: D1 web review closure

- Status: review
- Implementation owner: Codex
- Review owner: unassigned
- Branch: `ai/chatgpt/d1-web-review-closure`
- Owned files: D1 command contract/routes/service/tests and the matching
  LedgerBridge-Web Core-backed adapter on `ai/chatgpt/core-backed-review`

## Goal

Let an authenticated human review a synthetic Core candidate in the existing
LedgerBridge web page and append exactly one auditable decision in Core.

## In scope

- A versioned, closed Core candidate-decision HTTP contract.
- Idempotency and optimistic revision checks at the Core boundary.
- A short-lived BFF user assertion bound to the exact command request.
- Synthetic Core command/event persistence for end-to-end contract proof.
- A LedgerBridge-Web candidate backend port with synthetic and Core HTTP adapters.
- A Core-backed Web mode that keeps only Passkey/session data in Web SQLite.
- Candidate list/detail, evidence download, review-event history, and decisions.

## Out of scope

- Production database mutation functions or runtime grants.
- Real Hermes or Outlook capture, historical mailbox sweep, or real evidence.
- Production certificates, production deployment, workbook publication, or auto-posting.
- A second candidate/event store in LedgerBridge-Web.

## Frozen invariants

- D-015 remains unchanged: LedgerBridge Core is the sole business fact source.
- The browser talks only to the Web BFF. Core has no CORS and receives no browser cookie.
- Synthetic and Core-backed modes are mutually exclusive.
- Web SQLite stores authentication state only in Core-backed mode.
- Request bodies never self-report an actor.
- The user assertion expires within 60 seconds and binds issuer, audience, subject,
  authentication generation, HTTP method, canonical path, body digest, candidate
  reference, expected revision, workload principal, policy generation, and unique JTI.
- Every decision uses a UUID idempotency key and an expected revision.
- Candidate events are append-only and replaying the same operation returns the
  original result without appending a second event.
- Corrections are allowlisted and all money remains integer minor units.
- Any missing capability, assertion, configuration, or Core response fails closed.

## Acceptance tests

- Core route tests cover confirm, ignore, correction, conflict resolution,
  idempotent replay, key reuse, stale revision, assertion expiry, body/path binding,
  and actor derivation.
- Web adapter tests map Core candidate/evidence/event contracts without persisting
  business facts locally.
- A Web BFF integration test proves list -> detail -> decision -> refreshed event
  history through an in-memory Core adapter.
- Existing Core and Web tests remain green; formatting, lint, typing, and security
  checks pass for changed files.
- No real financial values or evidence are added to either Git repository.

## Implementation evidence

- Core exposes a disabled-by-default synthetic candidate command/event surface
  with request-bound user assertions, idempotency, optimistic revision checks,
  actor derivation, and the frozen append-only candidate state graph.
- `docs/contracts/internal-command-v1.openapi.yaml` freezes the HTTP contract.
- LedgerBridge-Web `core-backed` mode uses an HTTPS/mTLS Core adapter, refuses
  Web SQLite business facts, and maps Outlook/Hermes/Core candidate projections
  into the existing review page.
- Core focused regression: 49 tests passed; D1/config suite: 13 tests passed;
  Ruff, strict mypy, and Bandit passed.
- Web server regression: 44 tests passed (1 Windows symlink skip); frontend:
  19 tests passed; TypeScript and ESLint passed.

## Review findings

- Database-backed command persistence, production mTLS policy, real ingest, and
  deployment remain explicitly out of scope and fail closed.
