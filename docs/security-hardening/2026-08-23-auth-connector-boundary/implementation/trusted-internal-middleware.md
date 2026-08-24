# Implementation Plan: trusted internal principal middleware

## Selected Design And Constraints

This plan implements authentication **Option 1** from the companion proposal:
a loopback or mTLS trusted gateway/middleware produces a typed principal before
the internal upload route reads the body. It does not select an identity vendor,
open a remote listener, enable production, or accept client actor headers.

The plan is anchored to source revision
`286bb626166e3e6a7222e9cadbe58c9ff191b59b` and the unchanged source-evidence
collection digest
`79175486a1716efbf17828e49efa809caeae5b74ba0b48d2ca0f2f80e6ee0149`.
The working tree was clean and the twelve source/config hashes matched the
design context; no relevant source drift was found.

Non-negotiable constraints:

- `LEDGERBRIDGE_ENABLE_INTERNAL_UPLOAD` remains false by default and production
  remains forced off.
- Authentication and `evidence:write` policy finish before body read, staging,
  artifact publication or database work.
- The route receives a verifier-owned `AuthenticatedPrincipal`, never a raw
  `X-Actor`, `X-Reason` or equivalent header.
- Principal text is bounded and storable; raw credentials, certificate chains
  and provider tokens never enter logs, audit payloads or responses.
- Test principal injection remains a dependency override/test provider only.

## Source Revision And Drift Check

At plan creation we refreshed `main.py`, `config.py`, `docker-compose.yml`, the
worker and runner composition roots, and the internal upload design/evidence
documents. The current code still has a state-only principal seam and no
provider settings. The API still publishes loopback port `8650`; no new ingress
or socket mount exists. This plan therefore describes future work only; it does
not silently adapt to a changed boundary.

## Affected Components

- `src/ledgerbridge/main.py`: typed principal dependency, capability policy and
  route ordering.
- `src/ledgerbridge/config.py`: provider mode, canonical principal limits,
  gateway policy generation and clock/skew settings (no secrets).
- A new internal auth module or middleware package: transport verification and
  principal mapping, with no artifact or database authority.
- `docker-compose.yml` and deployment manifest: loopback/mTLS topology and
  fail-closed health diagnostics.
- `tests/test_upload_route.py` plus focused auth tests: body-read, staging and
  header-negative cases.
- `docs/reviews/2026-08-23-internal-upload-route-implementation-codex.md` and
  `PROJECT_STATUS.md`: update only after the implementation evidence exists.

## Ordered Work Packages

1. **Lock the gateway contract.** Decide whether Hermes uses a loopback-only
   service identity or an mTLS reverse proxy. Specify the peer/certificate
   mapping, policy generation, capability name `evidence:write`, and canonical
   subject format. Do not proceed with an ambiguous header contract.
2. **Define typed identity objects.** Add an internal immutable
   `AuthenticatedPrincipal` carrying provider namespace, subject, capabilities,
   issued/expiry timestamps and policy generation. Add a single policy function
   that returns an authorization decision without exposing credentials.
3. **Implement the trusted middleware.** Authenticate the selected transport,
   reject direct or unverified identity headers, map the subject, validate
   storable bounds, and install the typed object in request state. Fail closed
   on missing gateway metadata, invalid certificate mapping or policy mismatch.
4. **Wire the route dependency.** Replace the string-only seam with a dependency
   that requires `evidence:write`, derives the audit actor from the canonical
   subject, and runs before `_read_bounded_request`. Keep the fixed server audit
   reason and existing bounded response.
5. **Add settings and composition checks.** Validate provider mode, policy
   generation, clock skew and principal length. Ensure production cannot enable
   the route and that compose diagnostics expose only a redacted policy hash.
6. **Add negative and integration tests.** Cover direct access, missing state,
   invalid transport identity, stale policy, malformed subject, missing scope,
   and client actor/reason headers. Assert the body stream was not consumed and
   ArtifactStore staging remains empty for every rejection.
7. **Prepare the rollout evidence.** Run Linux/Windows CI and a disposable
   Hermes profile using a synthetic gateway identity. Do not use production
   certificates or enable the production flag; request a separate narrow audit.

## Compatibility And Migration

Existing tests that override `get_authenticated_principal` should move to a
typed test principal override. The route path, importer API, audit actor string,
and response schema remain compatible. Development profiles may use an explicit
test provider, but the production composition must not load that provider.

No database migration is required. Existing audit actors remain strings derived
from the canonical subject. If a future provider changes subject format, add a
versioned mapping rather than rewriting historical audit events.

## Tactical Protections During Migration

- Keep the feature flag and production guard before all auth/body dependencies.
- Keep `request.state` private to the middleware and dependency; reject all
  client-supplied actor/reason headers even while the provider is shadowed.
- Do not log authorization headers, certificate details, raw claims or subject
  values that fail validation. Log only bounded error code and policy generation.
- Keep empty Connector manifest behavior and ArtifactStore handoff unchanged.
- If the gateway contract is incomplete, fail with `AUTH_REQUIRED` and keep
  staging at zero; never fall back to the current string seam in a deployable
  profile.

## Tests And Security Validation

- Unit-test principal canonicalization, capability matching, policy generation,
  staleness and storable-text boundaries.
- Test the route with a body stream that raises if read; every auth failure must
  return before the first chunk and before `begin_handoff()`.
- Test forged/direct identity headers, invalid certificate/SAN mapping, missing
  gateway metadata, wrong capability, expired policy and overlong subjects.
- Verify audit actor equals the canonical subject and never the client header.
- Run sensitive-header/log scanning and assert no raw token/certificate text in
  response, audit payload or structured log fields.
- On Linux, exercise loopback and mTLS topology in a disposable Compose profile;
  verify direct API bypass fails closed.

## Performance And Resource Benchmarks

No measurements are claimed. Before choosing the boundary for a persistent
internal workload, measure:

- warm and cold authentication p50/p95 latency for 1, 10 and 100 concurrent
  rejected/accepted requests;
- API RSS and allocation count with the typed principal and policy cache;
- gateway/middleware CPU while a bounded multipart body is not yet read;
- failure recovery time after gateway restart or certificate rotation.

The decision threshold is operational rather than a fabricated number: the
added local verification must not consume the upload body budget or make the
health/readiness contract ambiguous. Record measurements in a later evidence
report, not in this plan.

## Rollout And Rollback

1. Ship types, tests and diagnostics with the route still disabled.
2. Enable the test provider only in the test profile and run CI.
3. Run a disposable Hermes internal profile with loopback/mTLS and synthetic
   identity; compare policy generation and rejection telemetry.
4. Request independent security review and explicit user authorization before
   enabling any non-test profile.
5. Keep production flag false until a separate deployment decision.

Rollback is to the disabled route and previous image. Remove the provider
   binding or gateway profile; do not re-enable arbitrary state strings or client
   headers. A provider outage is a fail-closed admission event, not a reason to
   bypass the dependency.

## Acceptance Criteria

- Every request without a verifier-owned principal or `evidence:write` fails
  before body read and staging with a stable bounded error.
- Direct API access that bypasses the trusted gateway fails closed.
- Client actor/reason headers have no effect on the audit actor or reason.
- Valid test identities produce the same bounded route response and importer
  ordering as the current dependency override tests.
- Production settings reject the route even when the feature flag is true.
- No raw credentials, certificate material or unbounded claim text appears in
  logs, audit events or responses.
- Windows and Linux CI pass; a disposable Hermes topology replay is recorded;
  no production service or evidence changes.

## Open Decisions

- Is the trusted gateway loopback-only, or is mTLS required for a non-loopback
  hop?
- What exact subject namespace and capability policy does the operator approve?
- Which component owns certificate rotation and policy-generation rollout?
- Is this option sufficient for the next authorized client, or must we move
  directly to the OIDC/JWT option before implementation?
