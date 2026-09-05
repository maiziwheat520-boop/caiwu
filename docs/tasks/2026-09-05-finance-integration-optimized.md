# Finance integration and review fixes

This integrates the pending operating-fee reporting detail and payroll disbursement
read model onto production Core c0469c6. The matching Web release includes full
decision-event responses and binds disbursement records to the displayed payroll period.

The production candidate reader migration remains 20260905_0046. New migrations
are linear: 20260905_0047 (fee details), then 20260905_0048 (payroll reader).
Restore inventory and privileges are checked at each boundary, including rollback.
The payroll SQL uses qualified joins, and the reader requests a 501st sentinel row
to reject oversized results instead of silently returning an incomplete 500-row report.

Payroll source-company mapping remains explicit configuration. Missing mapping fails
closed. These records are not employee-level payment proof: employee links remain
unmatched until separate evidence is available, and no payment submission is enabled.

Validation:

- Full local Core suite: 1441 passed, 214 environment/platform skips.
- Changed business modules, migrations, restore utility and regression tests: scoped
  mypy passed; Ruff check/format passed after integrating the earlier branch formatting.
- Full mypy still hits the same missing optional rapidocr import and duplicate test
  module error reproduced on production c0469c6; no full-mypy success is claimed.
- Bandit findings unchanged from production (no new finding, no HIGH); strict locked
  dependency audit reports no known vulnerabilities.
- A network-isolated PostgreSQL copy exercised upgrade 0046→0047→0048,
  downgrade to 0046, and upgrade to head again. Every stage passed the complete
  restore security validator. Financial fact counts were unchanged and registry
  seeds were not duplicated. Restricted reader-role model validation also passed.
- Independent integrated Core/Web review: no unresolved BLOCKER or HIGH.

Deploy Core and matching Web atomically with an encrypted backup, rollback copy,
fresh revision verification, and the shared release lock. Do not reuse the old
feature branches or mutate the already-published 0046 migration.
