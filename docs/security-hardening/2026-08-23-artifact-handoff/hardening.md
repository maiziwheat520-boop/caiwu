# Security Hardening Review: bounded artifact handoff

## Evidence Basis

This review is derived from the current source and five directly inspected
repository files at revision `862056816126428c1694985da7b7b3272bc749bc`. The
collection is bound by SHA-256
`443933d9019784b0927617d985c3b1348bc9ae86f8b32e92ee95c67e246f490b`; the full
inventory is in [`context.md`](context.md). No upload route exists in this
revision, so the early-publication path is an inferred integration risk, not a
claim about a currently exposed production endpoint.

## Constraints

We must keep `ArtifactStore` as the only durable publication authority, retain
the 50 MiB file and 512 MiB staging limits, and ensure malformed, cancelled,
over-limit, or incomplete requests create no durable artifact or import state.
The first route remains synchronous and internal/test-only. No authentication
provider, Connector, production route, or deployment is part of this design.

## Opportunity Portfolio

| Opportunity | Evidence | Options | Recommendation | Proposal |
| --- | --- | --- | --- | --- |
| Make multipart completion and artifact publication one explicit boundary | `ArtifactStore` stream/EOF publication and staging behavior (H001); parser completion state (H002); approved no-orphan Slice C requirement (H005) | 1. Bounded request spool; 2. ArtifactStore-owned transactional session; 3. Completion-aware stream | Option 2 under the current quota and internal-only constraints; Option 1 is a time-boxed bridge, Option 3 only if lock contention is measured acceptable | [artifact-handoff-publication-boundary.md](proposals/artifact-handoff-publication-boundary.md) |

The common structural issue is not that either existing component lacks useful
controls. The parser is careful about malformed input, and the store already
cleans private staging on stream failure. The gap is the contract between them:
`MultipartFileEnd` means file bytes are finished, while a valid request still
requires the closing boundary and parser final state. A route that maps the
former to stream EOF can therefore ask the store to commit before the latter is
known. We can close that gap locally, but we should make completion a typed
state rather than relying on every future route author to remember it.

## Recommendation Summary

I recommend Option 2, the ArtifactStore-owned transactional handoff. It keeps
quota accounting, digest verification, permissions, fsync, deduplication and
publication in one owner while allowing the request parser to release the
global lock between bounded writes. The attractive part is that one staged
inode can be sealed and adopted without a second request-spool read. What
gives me pause is the new lifecycle API: because it sits at the publication
choke point, we need state-machine, crash, concurrency and platform tests
before enabling even an internal route.

Option 1 is preferable when delivery time is the dominant constraint, but only
if request staging and ArtifactStore staging share one aggregate budget and a
reaper is observable. Option 3 is the smallest code change and may be useful
for a short experiment, but it should not be the default: the existing quota
lock would be held while a client delivers the body, making slow-client
availability risk part of the storage authority.

## Next Decisions

1. Select an option, or confirm that the transactional session should be
   refined into an implementation plan.
2. Set the aggregate staging interpretation and request deadline/concurrency
   budget.
3. Define the crash/recovery contract for sealed handoffs on Windows and Linux.
4. Only after those decisions, write `implementation/<option-id>.md` and ask
   for explicit implementation authorization. This design itself changes no
   source behavior.

Option 2 has now been selected and implemented at `6300bf5`. The implementation
handoff remains the contract and evidence index:
[`implementation/transactional-handoff-session.md`](implementation/transactional-handoff-session.md).
The implementation report is
[`../../reviews/2026-08-23-artifact-handoff-implementation-codex.md`](../../reviews/2026-08-23-artifact-handoff-implementation-codex.md).
The HTTP route, authentication, importer wiring and production composition remain
outside this implementation.
