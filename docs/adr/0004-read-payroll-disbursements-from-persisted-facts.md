---
status: accepted
---

# Read payroll disbursements from persisted classified facts

LedgerBridge will expose a versioned Payroll Disbursement Record read model over already-ingested
bank transactions with a current confirmed `PAYROLL` classification. Source files are parsed once
by ingestion. Payroll reads the normalized facts through a bounded, source-scoped internal
interface and assigns the regular pay period with a versioned next-month rule.

The read model preserves source artifact, statement, row, account, occurrence time, ingestion time,
and classification revision. Records remain `UNMATCHED` until a separate, evidence-backed employee
link exists. Batch payments and amount similarities are never used to invent employee matches.

We reject reparsing source files from the payroll page, copying bank facts into a second payroll
fact store, and broadening the payroll Web principal to every source company. The payroll company
instead has an explicit configured source-entity set, while the authoritative facts remain in the
bank and classification model.

## Consequences

- Payroll review is a direct database read and does not repeat parsing on each page visit.
- Corrections remain auditable through the existing append-only classification history.
- Regular payroll appears under the preceding payroll period; exceptional timing requires review.
- Employee-level actual amounts remain unavailable until reliable linkage evidence is implemented.
- The projection is read-only and cannot enable payment submission or posting.
