# Task: F-4 encrypted backup and isolated restore rehearsal

- Status: implementation and pre-merge Hermes rehearsal passed; PR/CI/deployment pending
- Implementation owner: Codex
- Review owner: Codex self-audit; Claude fixed-SHA audit hook preserved
- Base commit: `e183c962db8c84afbd262105c773ad91946f4a45`
- Branch: `ai/chatgpt/f4-backup-restore`
- Target: Hermes `/srv/ai-center/ledgerbridge`

## Goal

Close F-4 before Phase 2 introduces real financial evidence by making backup and
restore executable, encrypted, fail-closed, and independently verifiable. Record
one restore-to-empty rehearsal on Hermes without restoring into, downgrading, or
mutating the production database or artifact volume.

## Confirmed key boundary

The user selected a dedicated GPG key stored on Hermes for automated backup and
restore. A private-key export is also stored in `G:\我的云端硬盘\凭据\`, outside
the `AI` workspace and Git. Fingerprints may be recorded; private-key material,
passwords, database URLs, and `.env` contents may not appear in logs or reports.

## In scope

- Back up PostgreSQL with owners and ACLs intact.
- Back up cluster roles before database restore semantics are exercised.
- Back up the artifact volume and current rendered deployment tree.
- Encrypt the payload before it enters the durable backup directory.
- Bind the ciphertext, source revision, component hashes, migration head, row
  counts, and artifact state to machine-readable metadata.
- Restore into a fresh PostgreSQL 15 volume initialized with data checksums.
- Restore roles before the database, then assert `ledgerbridge_app` grants are
  non-empty and its LOGIN remains unprivileged.
- Restore artifacts into a fresh volume and compare deterministic content hashes.
- Verify the restored deployment manifest and direct runtime login.
- Remove every rehearsal container, network, volume, and plaintext work directory
  on success or failure while retaining the encrypted backup and non-secret report.

## Out of scope

- Restoring into or downgrading the production database.
- Rotating production database credentials or changing the Phase 1 schema.
- Introducing real financial evidence, Phase 2 tables, connectors, or ingestion.
- Uploading backups to a remote object store; the off-host key copy is the only
  cross-host credential action in this task.

## Acceptance gates

- Missing/invalid GPG home, fingerprint, deployment revision, healthy service, or
  safe absolute path stops before a durable backup is published.
- The durable backup directory contains encrypted payload and non-secret metadata,
  never plaintext dumps, `.env`, roles SQL, or artifacts.
- Ciphertext and all decrypted component hashes verify; tampering fails closed.
- `pg_restore --list` validates the database archive before publication.
- Rehearsal uses unique `ledgerbridge-restore-*` resources on an internal network.
- Restored Alembic head equals the source head and `data_checksums=on`.
- Restored database owner matches the source owner; all recorded table row counts
  match; Phase 1 functions/triggers exist.
- `information_schema.role_table_grants` for `ledgerbridge_app` is non-empty;
  audit remains SELECT-only and schema CREATE remains denied.
- Direct restored runtime login reports
  `session_user=current_user=ledgerbridge_app`.
- Restored artifact content digest matches the source digest.
- Production revision, container IDs, health, row counts, and volumes are unchanged
  by rehearsal.
- Ruff, format, mypy, pytest, Bandit, sensitive-path scan, strict dependency audit,
  migration round-trip, Compose, push CI, and PR CI pass.

## Evidence to record

- Git commit/PR/CI IDs.
- Public GPG fingerprint and external private-key backup path, never key material.
- Encrypted backup path, ciphertext SHA-256, and source revision.
- Rehearsal report with restored migration head, checksum status, grant count,
  row-count comparison, artifact digest, runtime identity, cleanup result, and
  post-rehearsal production invariants.

## Rollback and safety

Code and documentation roll back through Git. The rehearsal is disposable and
must clean only exact names generated with the `ledgerbridge-restore-` prefix.
The production backup command is read-only against database/artifact/deployment
sources. If automation fails, keep the last known-good encrypted backup and do not
remove any production image, deployment tree, named volume, or prior backup.

## Pre-merge evidence

- Codex report:
  `docs/reviews/2026-08-21-f4-backup-restore-codex.md`.
- Dedicated GPG fingerprint:
  `673F5E5E5E2DF43732FF4062805548394C472B4C`.
- Off-host private-key copy exists in the approved external credentials
  directory; copy hash matched and no key material entered this repository.
- Authoritative pre-merge backup:
  `/srv/ai-center/backups/ledgerbridge/20260821T121630Z-2028e3a99abe`.
- Ciphertext SHA-256:
  `14d484b1718977ad5b884da4609d3e119a0a04ec9ce633cf521038c837d978a4`.
- Restore report:
  `restore-rehearsal-20260821T121702Z.json`, status passed.
- Restored head/checksums/owner/grants/functions/triggers:
  `20260821_0002` / `on` / `ledgerbridge` / 17 / 9 / 8.
- All five Phase 1 tables remained empty, runtime identity was
  `ledgerbridge_app`, artifacts/deployment hashes matched, disposable
  resources were absent, and production state was unchanged.
- Full PostgreSQL test gate: 61 passed, 2 Windows-only symlink skips, 99.49%
  coverage. Ruff, format, strict mypy, Bandit, sensitive-path scan, strict
  pip-audit, diff check, Compose render, image revision binding, manifest, and
  ready probe passed.
- Remaining: local commit, explicit push/PR/merge authorization, CI, merged-SHA
  Hermes deployment, and final post-merge backup/rehearsal.
