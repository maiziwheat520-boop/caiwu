# Historical classification backfill P01-P07

Status: production complete
Date: 2026-09-04

The user approved all seven high-confidence historical groups. The owner-only
one-shot binds every group to fixed count, signed total, and ordered fact digest,
then applies the complete batch in one PostgreSQL transaction. P01/P02/P05/P07
are versioned cash-reconciliation rules; P03/P04/P06 are append-only confirmed
company transaction classifications. No Candidate, Journal Entry, or Posting is
created or changed.

Production ran against Core `4f0e9b5c3c972da26dda055d21e52141bf0b6929`
and schema `20260904_0039`. A rollback rehearsal exercised all 111 company
classification writes and rule revisions before restoring the exact original
state. The committed run created 111 classifications, added P01 revision 1 and
P02/P07 revision 2, reused P05 revision 1, and recorded one batch audit receipt.
Immediate replay created zero rows. Journal Entry and Posting remain zero.

Pre-change encrypted backup and isolated restore:
`/srv/ai-center/backups/ledgerbridge/20260903T174838Z-4f0e9b5c3c97`.
Post-change encrypted backup and isolated restore:
`/srv/ai-center/backups/ledgerbridge/20260903T175453Z-4f0e9b5c3c97`.
