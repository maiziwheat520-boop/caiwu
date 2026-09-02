# Core-backed web review

The existing LedgerBridge review page is the human review Interface. In
`core-backed` mode its business backend is an HTTP/mTLS Adapter to LedgerBridge
Core; Core remains the only business-fact Module.

## Data boundary

- The browser talks only to the same-origin Web BFF.
- The BFF keeps Passkeys, recovery-code hashes, and sessions in Web SQLite.
- Candidates, evidence, review events, conflicts, and reconciliation projections
  are read from Core on demand.
- Company reports are fetched from Core as three separate authorization-scoped
  pages (`CONFIRMED_CANDIDATE`, `ACCOUNT_STATEMENT`, and `POSTED_LEDGER`). The
  browser cannot supply company or date scope, and the BFF never adds the three
  layers together. Only `POSTED_LEDGER` is presented as formal revenue, expense,
  and profit.
- All three report layers and both category-composition pages use a dedicated
  read-only mTLS client. Candidate review, evidence, payroll, and every command
  continue to use the primary Web workload client. The authorized company list
  remains a dynamic Core projection and is never hard-coded in Web.
- The BFF treats every Core report page as an exact contract, not as a permissive
  object to trim. It rejects missing or extra fields at the layer, company, month,
  business-unit, metrics, and balance boundaries; unsafe integers; invalid metric
  equations or count relationships; unpaired material/taxonomy fields; different
  company sets or identities across layers; duplicate or non-ascending company,
  month, or business-unit keys; and the 50-company, 24-month, or 50-business-unit
  limits.
- Company and business-unit identity is copied only from Core's structured
  projection. The Web layer does not infer ownership from candidate summaries,
  counterparties, bank names, or other display text.
- Personal bank facts use a separate read-only route. The browser cannot submit
  an entity or statement reference; the BFF binds both from protected runtime
  configuration, asks Core for one audit-horizon snapshot, and rejects extra,
  unmasked, reordered, truncated, or arithmetically inconsistent transaction
  data. These rows are labeled as bank cash flow, never as posted revenue or expense.
- Each month explicitly declares its business-unit breakdown state. Empty means a
  completed empty list. Account-statement attribution gaps and missing historical
  posted-ledger snapshots carry distinct unavailable states with `null` lists.
  The Web UI preserves company-level facts in those cases and never fills a
  historical label from the current dimension catalogue or another report layer.
- Every company aggregate carries the same status as a strict roll-up of its
  months. No months is `EMPTY`; account-statement attribution gaps take priority
  over missing snapshots, posted-ledger missing snapshots take priority over
  available months, and candidate companies may only be `AVAILABLE` or `EMPTY`.
  The BFF rejects a company status that does not match this Core-defined roll-up.
- Until an authoritative balance projection exists, every aggregate carries an
  explicit unavailable-balance marker. Web displays that gap and never substitutes
  zero or derives balances from cash flow.
- This slice fails the complete company-report request with 503 if any one of the
  three Core layers is unavailable or violates the contract. The UI calls out that
  the layer is unavailable and renders no fallback zero. Zero is shown only after
  all three layer calls succeed and Core explicitly returns a complete zero fact.
- Human decisions are sent to Core with a UUID idempotency key, expected revision,
  and a request-bound user assertion. The request body cannot set the actor.
- Similar transactions are shown as deterministic groups with the complete Core
  matching scope and an exact member preview. Single-candidate review remains the
  default; the reviewer must explicitly opt into the group action and acknowledge
  every risk code before Web submits one batch request.
- Group classification changes only stable business-unit and category identifiers.
  Core rechecks the versioned group key, risk signature, member set, status, and
  every expected revision inside the same database transaction. A stale or changed
  member rejects the whole batch, so Web never loops over single-candidate writes.
- The receipt repeats the acknowledged risk codes and every member result so an
  idempotent replay is self-contained. Learned-rule creation, mutation, and
  automatic application are not exposed in this slice; `active_rule` remains null
  and the projection's learning fields are explanatory only.
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
CORE_COMPANY_REPORT_BASE_URL=<report Core origin; defaults to CORE_BASE_URL>
CORE_COMPANY_REPORT_CA_FILE=<report CA bundle; defaults to CORE_CA_FILE>
CORE_COMPANY_REPORT_CERT_FILE=<report-only workload certificate>
CORE_COMPANY_REPORT_KEY_FILE=<report-only workload private key>
CORE_USER_ASSERTION_KEY=<runtime secret, 32-256 bytes>
CORE_ASSERTION_ISSUER=<fixed Web issuer>
CORE_ASSERTION_AUDIENCE=<fixed Core audience>
CORE_WORKLOAD_PRINCIPAL=<mTLS policy principal ref>
CORE_POLICY_GENERATION=<positive integer>
CORE_USER_SUBJECT=<fixed single-owner Passkey subject>
CORE_AUTHENTICATION_GENERATION=<positive integer>
CORE_ENTITY_REF=<authorized entity UUID>
CORE_BUSINESS_UNIT_REF=<authorized business-unit ref>
CORE_PERSONAL_ENTITY_REF=<authorized PERSON entity UUID; configure together with statement ref>
CORE_PERSONAL_STATEMENT_REF=<reviewed personal bank statement UUID; never browser supplied>
CORE_PERSONAL_STATEMENT_REFS=<comma-separated reviewed statement UUIDs; replaces the singular setting>
PAYROLL_COMMANDS_ENABLED=0
PAYROLL_ROLE_BINDINGS_JSON=<server-controlled subject to maker/checker/approver JSON mapping>
PAYROLL_TEST_WORKSPACE_ENABLED=0
PAYROLL_TEST_BATCH_ID=<server-controlled stable test batch id>
PAYROLL_TEST_WORKSPACE_AUTOCREATE=0
PAYROLL_TEST_WORKSPACE_EXPECTED_STORE_REVISION=0
```

The company-report certificate and key are intentionally not startup-required.
If either is absent, unreadable, or invalid, only `/api/v1/company-reports`
returns 503; Passkey login and the other Core-backed routes continue to start and
operate. The Compose template expects these files under the existing read-only
`config` mount at `company-report-core/client.crt` and
`company-report-core/client.key`, unless container paths are overridden.
The default report origin is `https://internal-ingress:8444`; Core binds that
port to the report-only proxy identity. The primary Web client remains on 8443,
and crossing either certificate with the other port fails authentication.

VM103 injects the non-secret identity context through those variables: entity
`a131ef1b-e250-5a6d-82ff-cab68f767997`, user subject `ledgerbridge-owner`,
authentication generation `2`, and workload principal
`workload:ledgerbridge-web`. These remain deployment values rather than code
defaults. The initial rollout keeps `PAYROLL_COMMANDS_ENABLED=0`; only the
receipt-verification action may be enabled after the reviewer binding and all
three service capability gates agree.

Payroll commands have independent Web and Core gates. Web defaults
`PAYROLL_COMMANDS_ENABLED` to `0`; enabling it without a server-controlled role
mapping refuses startup. Browser bodies never provide company, subject, actor,
role, payment flags, or provider authorization. The BFF derives the verified
session subject, requires the configured unique entity, and signs those facts
into the Core assertion. Production remains disabled until Core and the provider
advertise the trusted command capability for that exact session and entity.
The initial public command surface contains only receipt verification. Material
review and batch submit-review/review/approve are not Web routes until a later
version advertises those actions explicitly.

The removable payroll test workspace is a separate, default-off projection.
When enabled for product testing, the BFF fixes the batch id and company scope
from server configuration, routes material periods through 2026-08 to
`AUTO_TEST`, keeps 2026-09 onward in `REVIEW_REQUIRED`, and connects missing
dates as `DATE_UNKNOWN`. Optional autocreation is idempotent and never enables
payment or submission capability.

The Core origin must be HTTPS and origin-only. The client requires TLS 1.3,
validates the configured CA, presents the configured client certificate, rejects
redirects, bounds response sizes, and re-verifies evidence digests.

## Current gate

The adapter and synthetic end-to-end review loop are implemented and tested.
Core database command persistence, production mTLS policy, real Outlook/Hermes
ingest, and production deployment remain closed. Therefore this mode must not yet
be used to import or approve real financial evidence.
