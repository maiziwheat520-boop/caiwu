# Independent security audit — Phase 3 follow-up

Date: 2026-08-24  
Branch: `ai/chatgpt/phase-3-connector-runner`  
Audit range: `801a2f2bd0f76ca6a91c42047e1c2943f23fe373..1d816a2`

## Verdict

No current reportable BLOCKER, HIGH, or MEDIUM finding survived the audit after
the remediation commits in this branch. The production topology remains
fail-closed: async dispatch is disabled, the default manifest is empty, and no
real Connector is registered.

The Codex Security workbench scan could not start because its Windows Git
metadata helper attempted to decode repository output as GBK. The terminal
contract workflow was completed instead: a 22-file security-sensitive
inventory, threat model, canonical `scan-manifest.json`, `findings.json`, and
`coverage.json` were finalized successfully.

## Fixes applied during this audit

- Migration `20260823_0008` now binds `raw_artifact.source` to
  `NEW.ingest_channel` in the acceptance trigger. A compatibility-role direct
  insert with mismatched channels is rejected before persistence.
- The Compose `migrate` service now requires an explicit `LEDGERBRIDGE_ENV`;
  a missing environment cannot silently select the non-production compatibility
  path.
- Connector runner record materialization now accounts for encoded response
  frames before appending records, reserving terminal-frame budget and stopping
  at `MAX_RESPONSE_BYTES`.

## Verification

- Windows: `232 passed, 139 skipped, 1 warning`; Ruff format/check, strict mypy,
  offline lock, and diff checks pass.
- Hermes disposable PostgreSQL 15: migration chain `0001→0008` replayed with
  the current code. The new trigger rejected the compatibility-role
  cross-channel direct insert; all temporary containers and networks were
  cleaned up. Production Hermes was not touched.
- Hosted Linux/POSIX runner behavior remains covered by the existing CI suite;
  Windows skips are expected for Unix-socket and POSIX filesystem tests.

## Deferred follow-up gates

The four foundation controls listed in the original audit were implemented in
`bb3eee4` and are recorded in
`docs/reviews/2026-08-24-deferred-boundary-remediation-codex.md`:

1. Connector execution now uses a dedicated hard-capped executor and retains
   occupied slots until cancelled synchronous work actually finishes; saturated
   requests fail closed. This bounds the prior `asyncio.to_thread()` growth
   risk, while true killable process isolation remains a requirement for a
   hostile real Connector.
2. `RunnerConnector` pending request state is context-local.
3. Upload request reads have a deadline and loop-independent admission limit.
4. Migration `20260823_0006` reasserts runtime role attributes and revokes
   compatibility-role inheritance.

No merge, production migration, password rollout, feature-flag enablement,
manifest/key registration, or real evidence import was performed.
