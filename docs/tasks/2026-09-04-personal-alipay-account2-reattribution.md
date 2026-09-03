# Personal Alipay account 2 reattribution

Status: production complete on 2026-09-04

The user identified the reviewed annual Alipay statement as their second
personal account. The accepted target is the authoritative personal Entity,
`personal-funds` business unit, and the private stable account reference ending
in `5002`; the earlier proposed company attribution is rejected.

The cutover selects the exact reviewed 1,574 legacy Candidate set using a fixed
content digest over Candidate identity, source event, amount, month, summary,
and confidence. It creates deterministic personal replacements and a private
old/new mapping receipt. The controlled import, managed-account registration,
replacement confirmations, predecessor ignores, and one batch audit event share
one caller-owned PostgreSQL transaction. Every mismatch fails closed. The
command has no Journal Entry or Posting path.

Production safety gates validate the encrypted backup ciphertext and checksum,
the matching isolated-restore inventory, the deployed revision file, database
identity and owner role, writable/non-recovery state, schema revision, the live
pre-state inventory, and zero Journal Entries/Postings. A completed replay skips
only the obsolete pre-state inventory comparison and still proves the exact
batch receipt, account, Candidate terminal states, unique audit receipt, and
zero-posting invariant.

Validation evidence:

- focused tests: 15 passed; Ruff and strict mypy passed;
- complete suite: 1,344 passed, 212 skipped, with three unrelated pre-existing
  payroll fixture failures;
- production-clone full cutover rollback preserved all before counts;
- gated production-clone commit plus immediate replay was zero-delta;
- encrypted pre-cutover backup and isolated restore passed;
- production cutover and immediate replay passed;
- encrypted post-cutover backup and isolated restore passed;
- production result: 1,574 CONFIRMED replacements totaling 21,362,070 minor
  units, 1,574 IGNORED predecessors, 34 unrelated PENDING rows untouched,
  one ACTIVE personal Alipay account, one batch receipt, one audit receipt,
  zero Journal Entries, and zero Postings.
