# Implementation Plan: signed declarative Connector manifest plus runner

## Selected Design And Constraints

This plan implements Connector **Option 2** from the companion proposal. A
signed, immutable manifest selects only allowlisted factory IDs, pins Connector
identity/source-system/protocol/code digest, and is loaded consistently by API,
worker and runner. Production entries are runner-only. This plan does not add a
real Connector, sign a production manifest, mount a socket, or enable ingestion.

The plan is anchored to source revision
`286bb626166e3e6a7222e9cadbe58c9ff191b59b` and evidence collection digest
`79175486a1716efbf17828e49efa809caeae5b74ba0b48d2ca0f2f80e6ee0149`. The
source refresh found no drift: `get_internal_connectors()` and
`ConnectorSupervisor()` are still empty, `validate_connector()` remains the
second defense, and API still lacks the runner socket mount. The companion
[execution composition proposal](../proposals/connector-execution-composition.md)
now recommends worker-owned asynchronous execution for the production runner
profile; this plan records that recommendation but does not claim it is
implemented or user-approved.

Non-negotiable constraints:

- Request values never choose Connector name, version, factory or source system.
- Production manifest entries must use `execution_mode=runner` and the existing
  network-disabled, read-only, low-privilege runner boundary.
- Signature and code/image digest verification do not replace object-level
  contract validation or database source-system registry checks.
- API, worker and runner must agree on one immutable generation/digest or fail
  closed; no hot reload during an import.
- Signing and verification keys stay outside the repository and runner image.

## Source Revision And Drift Check

We refreshed `connectors.py`, `imports.py`, `connector_runner.py`,
`runner_client.py`, `worker.py`, `main.py`, both Dockerfiles and
`docker-compose.yml`. The collection digest is unchanged from the design
context. No current loader, manifest file, signing config, API socket mount or
worker Connector registry exists. The plan therefore keeps the API/runner
composition as an explicit decision rather than assuming a mount is safe.

## Affected Components

- New internal manifest schema/loader module: canonical serialization,
  signature/schema verification, generation and digest.
- New allowlisted factory registry shared by API, worker and runner; preserve
  `validate_connector()` after construction.
- `src/ledgerbridge/main.py`: replace empty dependency with a registry-backed
  sequence only after generation validation.
- `src/ledgerbridge/worker.py`: load the same generation and construct the
  importer/RunnerConnector composition.
- `src/ledgerbridge/connector_runner.py` and `runner_client.py`: load the same
  verified registry and report generation in health/diagnostic output.
- `src/ledgerbridge/config.py` and `docker-compose.yml`: manifest path,
  verification-key reference, required generation, socket/async mode and
  fail-closed defaults; no key material.
- `tests/test_connectors.py`, `tests/test_connector_runner.py`,
  `tests/test_upload_route.py`, plus new manifest tests and Linux Compose tests.

## Ordered Work Packages

1. **Accept the composition decision first.** The proposed production choice is
   worker-owned asynchronous execution: API publishes/binds and durably queues,
   then returns `202`; worker claims the dispatch and owns the runner socket.
   Keep the current synchronous path only for an explicit internal/test profile.
   Record acceptance of the dispatch schema, status contract, lease/retry model
   and rollback before writing a loader. The signed manifest cannot decide this.
2. **Freeze the manifest schema.** Define canonical JSON fields: schema version,
   generation, factory ID, connector name/version, source system, execution mode,
   runner protocol version, code/image digest, allowed media types, byte limit,
   and declarative capabilities. Define canonical key ordering, UTF-8 rules and
   digest input; reject unknown fields unless a future schema explicitly allows
   them.
3. **Implement the factory allowlist.** Map stable factory IDs to built-in
   constructors in code. Reject dynamic import paths, arbitrary callables and
   factories not compiled into the image. Keep synthetic factories explicitly
   test-only.
4. **Implement signature/schema verification.** Verify the detached or
   envelope signature with a key reference supplied by deployment, validate
   generation/digest, and fail closed on unknown key, invalid signature,
   canonicalization mismatch, unsupported schema or missing file. Do not log
   manifest contents or key material.
5. **Cross-check deployment and registries.** For each entry call
   `validate_connector(production=...)`, require unique `(name, version)`, verify
   source-system row exists, verify runner protocol and code/image digest, and
   compare declared capabilities with actual Compose mounts/networks/UID.
6. **Distribute one generation.** Load the same verified generation in API,
   worker and runner. Health/readiness diagnostics expose only generation and
   digest. A mismatch, partial file or stale generation prevents route enablement
   and runner readiness.
7. **Add test fixtures and rollback.** Use unsigned synthetic fixtures only in
   tests; test signed test keys outside the repository. Add an empty signed
   generation and a previous-generation rollback path before any real entry.
8. **Prepare a narrow audit.** Run Linux/Windows quality, runner protocol,
   source-registry and Compose isolation tests; replay the selected composition
   on Hermes as a disposable project only. Do not register a real Connector or
   write evidence.

## Compatibility And Migration

The existing `Connector` protocol and importer selection API remain unchanged.
The loader returns the same `Sequence[Connector]` shape, but only after manifest
validation. Existing test fixtures can use a static unsigned test manifest via
dependency override; production code must require a signed generation.

No database migration is included in this plan. A real source-system row must be
added through the owner-only registry path before a signed manifest references
it. Existing `ingest_channel` rows remain independent; request fields still do
not choose source systems.

The first manifest deployment should contain zero real entries. Adding the first
entry is a separate reviewable generation and should include its parser/code
digest, runner image digest and owner approval.

## Tactical Protections During Migration

- Keep `get_internal_connectors()` empty and the route flag closed until all
  consumers agree on a verified generation.
- Retain `validate_connector`, importer detection ambiguity handling, source
  registry foreign keys and runner record/provenance validation.
- Reject `execution_mode=in_process` whenever `env=production`, even if a
  signature is valid.
- Do not let a valid signature grant network, database, artifact, OAuth or
  filesystem authority; cross-check the actual container boundary.
- Do not hot reload while an artifact is being detected or parsed. Restart or
  drain processes for a generation change.
- Keep manifest and digest diagnostics bounded and redacted; no raw signature,
  key, credentials or parser payload in logs.

## Tests And Security Validation

- Property-test canonical serialization, signature verification, unknown fields,
  duplicate identities, invalid factory IDs, source-system format, protocol and
  digest mismatches.
- Test an unsigned, tampered, stale, partially written and mixed-generation
  manifest; every case must leave route staging and database state untouched.
- Test API/worker/runner digest mismatch and readiness behavior. A runner with an
  empty or invalid registry must never report ready for a declared connector.
- Test request `ingest_channel` cannot select connector identity, version or
  source system; ambiguous detection remains `NEEDS_REVIEW`.
- Test production rejects in-process connectors and capability claims that do
  not match Compose network, mount, UID and credential boundaries.
- Replay runner protocol, hostile records, limits, stale responses and source
  registry constraints on Linux/PostgreSQL CI.

## Performance And Resource Benchmarks

No measurements are claimed. Before approving a real manifest, measure:

- startup verification latency and peak RSS for 0, 1, 10 and 100 entries;
- detection latency and memory with the same artifact across registry sizes;
- runner socket throughput/backpressure for the chosen synchronous or async
  composition;
- generation rollout and rollback time while draining in-flight imports;
- API/worker/runner health convergence after a manifest mismatch.

The benchmark result must be attached to the selected composition decision. A
valid signature is not a performance exemption, and a faster static loader is
not evidence that it is safe for production.

## Rollout And Rollback

1. Ship schema, verifier and empty-generation tests while the route remains off.
2. Run a test-only unsigned/static fixture through dependency overrides.
3. Run a signed empty generation in a disposable Linux/Compose profile; verify
   all consumers report the same digest and runner remains no-network.
4. Implement and test the accepted worker-async composition separately from the
   manifest loader. Do not add an API socket mount to production as a fallback.
5. Add only a synthetic signed entry after review; request Claude narrow audit.
6. A real source-system/Connector generation requires a separate user gate and
   production deployment authorization.

Rollback selects the previous signed generation or empty generation, restarts or
drains all consumers, and keeps the route disabled until digest agreement is
restored. Do not roll back by constructing an arbitrary in-process Connector.

## Acceptance Criteria

- No request field selects Connector name, version, factory or source system.
- Invalid, unsigned, stale, mixed or capability-inconsistent manifests fail
  closed before body read, staging, Connector construction or runner readiness.
- API, worker and runner expose the same generation/digest or refuse service.
- Production rejects every in-process entry and the runner has no network,
  database, artifact or OAuth authority beyond the reviewed boundary.
- Existing object-level Connector and record validation remains active.
- A generation change is immutable, reviewable, observable and rollbackable.
- Windows/Linux CI and disposable Hermes Compose replay pass without a real
  Connector, evidence row, audit event or production change.

## Open Decisions

- Which signing scheme and key-management service are approved, and who rotates
  or revokes verification keys?
- Where does the manifest live at runtime: image layer, read-only secret mount,
  or a deployment artifact with an integrity-checked path?
- Has the user accepted worker-owned asynchronous execution, including the
  durable dispatch migration and `202`/status contract? Until then the plan is
  design-only and the current route remains production-disabled.
- Which owner approves source-system registration, factory IDs, parser versions
  and image/code digests?
- What generation mismatch should readiness report, and how is an in-flight
  import drained during rollback?
