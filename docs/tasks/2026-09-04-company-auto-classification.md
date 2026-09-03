# Company auto-classification after confirmed statements

## Status

Complete and production-verified on 2026-09-04.

## Rule contract

Only transactions observed by an official company bank statement are eligible,
and classification runs only when that statement receives a new `CONFIRMED`
review. Rules apply from the Asia/Shanghai transaction date 2026-09-04:

1. `AUTO-P03`: positive company transaction with counterparty name exactly
   `陈明哲` -> `RELATED_PARTY_CURRENT`.
2. `AUTO-P04`: combined counterparty and transaction name contains both
   `企业代发过渡户` and `批量代发` -> `PAYROLL`, regardless of sign so payroll
   refunds stay in the same category.
3. `AUTO-P06`: negative company transaction containing both `浙江网商银行` and
   `贷款还款` -> `FINANCING`.

Existing company classifications are immutable and are not replaced. More than
one rule matching the same transaction aborts confirmation. The deterministic
operation key makes review replay idempotent. The automation writes only a
confirmed classification fact and never creates a journal entry or posting.

## Verification

- Focused and backup regression suites: 185 passed before release; restore
  contract correction suite: 175 passed.
- Disposable PostgreSQL 15 verified upgrade, downgrade, re-upgrade, trigger
  behavior, effective date, and idempotent confirmation replay.
- Production-clone migration rehearsal preserved 1,144 classifications and
  0/0 journal entries/postings.
- Production Core: `d716fc52b09dd14b1d8babbe6d54e912aaf70d3f`;
  Alembic: `20260904_0040`; all runtime containers healthy with restart 0.
- Final encrypted backup and isolated restore:
  `/srv/ai-center/backups/ledgerbridge/20260903T181734Z-d716fc52b09d/restore-rehearsal-20260903T181810Z.json`.

## Rollback

Runtime rollback points are retained under
`/home/aiadmin/releases/company-auto-classification-20260904/`. Migration
downgrade removes the trigger and function without changing classification
facts already present.
