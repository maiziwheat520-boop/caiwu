# Context Map

LedgerBridge is one product and one PostgreSQL database with three independently maintained
business modules. A shared financial foundation owns cross-module identity and evidence terms;
business modules reference those stable identifiers but do not write one another's private
tables.

## Contexts

- [Shared Financial Foundation](./CONTEXT.md): owns entities, Managed Accounts, immutable
  evidence, audit events, stable identifiers, and common money semantics.
- [Personal Finance](./docs/contexts/personal-finance/CONTEXT.md): reconciles an individual's
  bank, WeChat Pay, and Alipay activity into a complete personal view.
- [Hotel Reconciliation](./docs/contexts/hotel-reconciliation/CONTEXT.md): reconciles hotel
  platform orders and payouts against company statements and evidence.
- [Payroll](./docs/contexts/payroll/CONTEXT.md): prepares locked payroll batches and verifies
  disbursement results without executing payment.

## Relationships

- **All modules → Shared Financial Foundation**: use the same `Entity`, `Managed Account`,
  `Evidence Object`, `Audit Event`, stable identity, and integer-minor-unit money contracts.
- **Personal Finance ↔ Hotel Reconciliation**: exchange only explicit evidence/projections when
  a personal and company transaction are related; neither module updates the other's private
  records.
- **Payroll ↔ Hotel Reconciliation**: share company, employee/account mapping, statement evidence,
  and reconciliation projections through versioned service interfaces; neither module treats a
  payroll batch as a hotel payout.
- **Payroll ↔ Personal Finance**: a verified salary receipt may be projected into personal
  finance, but personal spending does not mutate the payroll batch or its approval chain.

## Database ownership

The target is one PostgreSQL database with schema ownership boundaries: shared foundation tables
remain common, while `personal_finance`, `hotel_reconciliation`, and `payroll` own their private
tables and migrations. Cross-module reads use stable interfaces or controlled read projections;
cross-module direct writes and ad-hoc joins are not module APIs.
