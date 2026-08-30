---
status: accepted
---

# Independently maintained finance modules share one database foundation

LedgerBridge will be a modular monolith with independently maintained Personal Finance, Hotel
Reconciliation, and Payroll modules on one PostgreSQL database. The modules share a controlled
financial foundation for entities, Managed Accounts, immutable evidence, audit, stable identity,
and integer-minor-unit money; each business module owns its private schema/tables and exposes
versioned services or read projections instead of writing another module's tables. This keeps one
authoritative data foundation and permits cross-module transactions where explicitly designed,
while avoiding three separate databases or a tightly coupled all-purpose module.

## Consequences

- The current PayrollVerification HTTP adapter is a transitional integration seam. When Payroll
  is moved into the shared deployment, its provider implementation may change to an in-process or
  database-backed adapter without changing the publication contract.
- Existing `public` tables remain operational during incremental migration; new private tables
  must declare one of the three module owners and move toward the corresponding PostgreSQL schema.
- No module may infer authorization from a shared database connection or perform cross-module
  direct writes. Authentication, company scope, audit, and explicit service contracts still
  apply inside the modular monolith.
