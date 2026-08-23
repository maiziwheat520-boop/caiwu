# Security Hardening Review: authentication and Connector admission

## Evidence Basis

This is a source-backed design review of revision `48c6957` on
`ai/chatgpt/phase-3-connector-runner`, not a claim that either control is
implemented. The twelve-file evidence collection is bound by SHA-256
`79175486a1716efbf17828e49efa809caeae5b74ba0b48d2ca0f2f80e6ee0149`. We
inspected the internal upload route, the Connector contract and importer,
runner/client boundary, worker composition root, Docker topology, and the
approved Slice C contract. The route is default-disabled; production remains
untouched.

## Constraints

We have no selected identity provider, signing-key service, real Connector,
provider token, remote-exposure requirement, or measured performance budget.
The API currently does not mount the runner socket, while the worker does; a
synchronous production route therefore needs an explicit composition decision.
The runner is intentionally network-disabled, read-only, low-privilege and
credential-free. No proposal may enable the flag, register a real Connector,
ingest evidence, or deploy production.

## Opportunity Portfolio

| Opportunity | Evidence | Options | Recommendation | Proposal |
| --- | --- | --- | --- | --- |
| Trusted principal admission | Route principal seam and approved upload contract (`A01`/`A02`/`A11`) | 1. Trusted internal gateway/middleware; 2. OIDC/JWT verifier | Option 1 for the current internal loopback profile; Option 2 before remote or multi-client exposure | [trusted principal admission](proposals/trusted-principal-admission.md) |
| Connector manifest governance | Empty route/runner registries, object validators, importer and Compose boundary (`A01`/`A03`–`A08`/`A10`/`A11`) | 1. Static built-in manifest; 2. Signed declarative manifest plus runner | Option 2 for any real Connector; Option 1 only for synthetic/test use | [Connector manifest governance](proposals/connector-manifest-governance.md) |

The two opportunities are linked by one admission invariant: an upload must
arrive with a verifiable actor and a server-owned set of executable connector
capabilities before we consume or persist evidence. They should be implemented
as separate reviewable changes, because authentication provider failure and
connector code isolation have different owners and rollback paths.

The user selected the current internal middleware option and the signed runner
manifest option for planning. The resulting handoffs are
[trusted-internal-middleware.md](implementation/trusted-internal-middleware.md)
and
[signed-declarative-runner-manifest.md](implementation/signed-declarative-runner-manifest.md);
they remain plans, not source changes.

## Recommendation Summary

For today’s internal/test-only route, we can preserve the fast path with a
loopback or mTLS gateway that creates a typed principal, while retaining the
existing production-forced-off flag. We should shape that principal and policy
interface so an OIDC/JWT verifier can replace only the provider when the
exposure changes.

For Connector enablement, a static code-owned manifest is a reasonable bridge
for synthetic fixtures, but we should not turn it into an untracked production
registry. A signed declarative manifest with an allowlisted factory map, source
registry checks, generation/digest agreement and runner-only production entries
gives reviewers a durable deployment fact. What gives me pause is the API-to-
runner socket decision: a valid manifest cannot make a missing or overprivileged
composition safe. That boundary must be decided before a real Connector is
registered.

## Next Decisions

- Choose the Hermes internal identity boundary: loopback/mTLS gateway now, or
  an approved OIDC issuer for a future remote profile.
- Define the required capability and tenant claims; do not use a generic
  authenticated flag as write authorization.
- Select the manifest schema/signature owner and key-management location outside
  the repository.
- Decide whether synchronous API execution may mount `connector-socket`, or
  whether Connector work moves to the worker asynchronously.
- Before implementation: resolve the API socket versus worker async composition,
  key custody, gateway/provider ownership and source-system owner. No production
  enablement follows automatically from the selected plans.
