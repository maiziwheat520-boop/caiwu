# Financial Web integration

The isolated review-events branch retains `73f6030` and incorporates only:

- Operating fee details: `3f14d2a`, cherry-picked as `9e15e16`.
- Ingested payroll reads: `26ede00`, cherry-picked as `6d5ea4a`.
- Payroll scheduled refresh: `98e3393`, cherry-picked as `b3220d1`.

`git log` and `git cherry` identified exactly the latter two exclusive payroll
commits. All three applied without conflicts; no old branch tree was copied.

Payroll review distinguishes unlinked source transactions from stored per-person
verification. Source read failures display an unavailable notice. A new UI test
checks that missing source mapping/read failure leaves actual payments as
unknown, shows evidence missing and never invokes a write command. Existing
stored verification remains authoritative for individual actual payment values;
unmatched ingested transactions do not manufacture these values.

Validation:

- Related App, company reporting/classification and payroll UI tests: 111 passed.
- Full frontend suite: 129 passed across 9 files.
- Full backend discovery: 184 run, 183 passed, one platform symlink skip.
- ESLint, typography, TypeScript and production Vite build passed.
- Vite's existing >500 kB main-chunk advisory remains (546.57 kB / 161.01 kB gzip).
- The audit event regression and structured invalid-event guard remain included.

Deployment prerequisites belong to the release owner: matching Core migrations,
read identity/policy and source mappings. This tree does not edit Core, real
financial data, production refs or deployment state. An unavailable source is
not represented as zero salary or completed reconciliation.
