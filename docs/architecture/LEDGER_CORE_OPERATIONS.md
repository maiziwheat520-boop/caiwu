# Phase 1 Ledger Core operations

Status: implementation contract for Phase 1
Date: 2026-08-21

## Journal lifecycle

A POSTED entry is created through an explicit three-step transaction:

1. create the JournalEntry as DRAFT and reference its authorizing AuditEvent;
2. add at least two postings that balance to zero in every currency bucket;
3. change the entry status to POSTED and commit.

Directly inserting a POSTED entry cannot succeed because postings cannot be added
to an already POSTED entry and the deferred completeness trigger requires at
least two postings. DRAFT entries may temporarily have zero postings while being
assembled; abandoned-draft cleanup is deferred until a workflow introduces draft
ownership and retention policy.

`REVERSED` is retained for an entry that was never posted or for future workflow
state. A posted entry remains POSTED forever. Its effective reversal is a new,
POSTED entry whose `reverses_entry_id` points to it. A partial unique index permits
only one such reversal entry per original.

## Immutable accounting dimensions

Account `entity_id` and JournalEntry `entity_id` are immutable from creation;
they are identity/tenant keys, not editable draft attributes. A draft created for
the wrong entity must be deleted and recreated. Once an Account participates in a
POSTED entry, its `account_class` also becomes immutable. Identifier and display
name remain editable.

Posting writes validate Account and JournalEntry entity equality, and the POSTED
transition revalidates every Posting as a defense-in-depth guard. These controls
prevent cross-entity history and expense/income totals from being rewritten
without touching immutable postings.

## Audit chain

Applications append through `append_audit_event`; direct INSERT/UPDATE/DELETE is
not permitted. Unique indexes enforce one genesis event and one successor per
non-null previous hash, including under REPEATABLE READ concurrency. Advisory
locking orders normal writers, while the indexes fail closed on stale snapshots.

The sequence uses a PostgreSQL sequence and may contain gaps after rolled-back
transactions. Continuity and tamper checks use `prev_hash` and `hash`, never the
assumption that sequence values are gapless.

Phase 1 binds a JournalEntry to its creation/authorization AuditEvent. Additional
post-transition events are intentionally deferred to the workflow/API phase and
must not weaken the current creation binding.

## Runtime boundary

The API and worker log in directly as `ledgerbridge_app`, a non-owner,
non-superuser role. They do not receive migration credentials and do not use
`SET ROLE`. The runtime role cannot reset to an owner, alter tables or triggers,
truncate ledger tables, or directly forge AuditEvent rows.
