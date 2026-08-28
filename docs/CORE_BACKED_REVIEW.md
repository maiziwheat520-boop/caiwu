# Core-backed web review

The existing LedgerBridge review page is the human review Interface. In
`core-backed` mode its business backend is an HTTP/mTLS Adapter to LedgerBridge
Core; Core remains the only business-fact Module.

## Data boundary

- The browser talks only to the same-origin Web BFF.
- The BFF keeps Passkeys, recovery-code hashes, and sessions in Web SQLite.
- Candidates, evidence, review events, conflicts, and reconciliation projections
  are read from Core on demand.
- Human decisions are sent to Core with a UUID idempotency key, expected revision,
  and a request-bound user assertion. The request body cannot set the actor.
- A Web SQLite database containing preview candidates, review events, or workbook
  drafts makes `core-backed` startup fail closed.
- `synthetic-preview`, `authenticated-preview`, and `core-backed` are mutually
  exclusive runtime modes.

## Required runtime configuration

`core-backed` uses the same Passkey/bootstrap settings as
`authenticated-preview`, plus:

```text
LEDGERBRIDGE_MODE=core-backed
CORE_BASE_URL=https://<loopback-core-origin>
CORE_CA_FILE=<trusted CA bundle>
CORE_CERT_FILE=<Web workload certificate>
CORE_KEY_FILE=<Web workload private key>
CORE_USER_ASSERTION_KEY=<runtime secret, 32-256 bytes>
CORE_ASSERTION_ISSUER=<fixed Web issuer>
CORE_ASSERTION_AUDIENCE=<fixed Core audience>
CORE_WORKLOAD_PRINCIPAL=<mTLS policy principal ref>
CORE_POLICY_GENERATION=<positive integer>
CORE_USER_SUBJECT=<fixed single-owner Passkey subject>
CORE_AUTHENTICATION_GENERATION=<positive integer>
CORE_ENTITY_REF=<authorized entity UUID>
CORE_BUSINESS_UNIT_REF=<authorized business-unit ref>
```

The Core origin must be HTTPS and origin-only. The client requires TLS 1.3,
validates the configured CA, presents the configured client certificate, rejects
redirects, bounds response sizes, and re-verifies evidence digests.

## Current gate

The adapter and synthetic end-to-end review loop are implemented and tested.
Core database command persistence, production mTLS policy, real Outlook/Hermes
ingest, and production deployment remain closed. Therefore this mode must not yet
be used to import or approve real financial evidence.
