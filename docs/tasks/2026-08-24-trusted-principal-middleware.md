# Trusted principal middleware

Status: implemented on the Codex branch; route and production profile remain
disabled by default.

## Contract

`AuthenticatedPrincipal` is an immutable, bounded identity containing provider,
subject, capability set, issued/expiry timestamps, and policy generation. The
policy function requires `evidence:write`, rejects stale generations and
expired/future identities, and allows only a bounded clock skew.

`TrustedPrincipalMiddleware` is an ASGI seam for a loopback/mTLS gateway. Its
resolver receives the gateway-owned scope and may install a typed principal in
ASGI state. It never parses `X-Actor`, `X-Reason`, Authorization headers, or
certificate/token material. Resolver errors and missing principals leave the
request unauthenticated. The upload and async dispatch dependencies accept the
typed principal before body reads; audit actors use `provider/subject`.

Settings add `auth_provider`, `auth_policy_generation`, and bounded clock skew.
Enabling a provider requires an explicit policy generation. Existing test
dependency overrides remain compatible, while a configured `trusted_gateway`
profile rejects raw string state.

## Verification

- `tests/test_auth.py`: principal shape/lifetime/capability/policy tests,
  resolver-error and client-header-negative middleware tests, plus route
  dependency integration.
- Existing upload route tests remain green; no production route or gateway
  listener was enabled.

## Non-goals

This slice does not implement certificate validation, token/JWKS retrieval,
gateway deployment, key rotation, or production enablement. Those remain
deployment-owned gates and require a separate audit.
