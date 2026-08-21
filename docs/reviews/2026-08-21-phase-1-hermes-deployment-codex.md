# Phase 1 Hermes deployment record

- Date: 2026-08-21
- Target: `aiadmin@192.168.1.39` (`ai-hub`)
- Source PR: #5
- Reviewed head: `e739088ad8f4d0eec91fda6e1e5ab3c268b1b2e6`
- Merge/deployed revision: `2028e3a99abe5e20c842a95ec22f2931878d39ee`
- Verdict: DEPLOYED AND VERIFIED

## Change control

PR #5 was merged with a merge commit only after explicit authorization. Production
deployment was requested separately. No Claude rerun was consumed; the separate
Claude clone and immutable earlier report remain available for a later fixed-SHA audit.

The existing production PostgreSQL volume was created in Phase 0 with `ledgerbridge`
as its bootstrap owner. The user explicitly chose to retain that role as the migration
owner and introduce `ledgerbridge_app` as the separate unprivileged runtime login.
No role rename or object-ownership transfer was performed.

## Pre-deploy state

- Hermes ran `ledgerbridge-app:61ad910` with API, worker, and PostgreSQL healthy.
- `DEPLOYED_REVISION` was `61ad9103d68d10a07191c7ad00a4fbb8953deddd`.
- Alembic was at `20260821_0001`; only `alembic_version` existed.
- The artifact volume contained zero files.
- PostgreSQL data checksums were on.

## Backup and release artifact

The protected backup directory is
`/srv/ai-center/backups/ledgerbridge/20260821-phase1-deploy-2028e3a`.
It contains a custom-format database dump, a roles-only cluster dump, the complete
Phase 0 deployment tree, sanitized metadata, checksums, and the release bundle.
`sha256sum --check` and `pg_restore --list` passed.

The local release artifact is
`G:\我的云端硬盘\AI\outputs\ledgerbridge-phase1-deploy-2028e3a.tar.gz`.
Its SHA-256 is
`67b8a2ca213225679b222fb853996f19b2a60452d5e9616d07407459d1abd4e5`.
The bundle contains 25 manifest-bound source/runtime files plus
`MANIFEST.sha256` and `DEPLOYED_REVISION`; it excludes Git metadata, tests,
development environments, and `.env`.

## Upgrade result

- Image tag: `ledgerbridge-app:2028e3a`
- Image ID: `sha256:0304e35445543bac2a3bea6522f39b640eb1ef3618e351bcb308e1b736cf358e`
- OCI revision label: full merge SHA above
- Migration: `20260821_0001 -> 20260821_0002`
- Runtime identity: `session_user=current_user=ledgerbridge_app` for API and worker
- Migration owner: legacy `ledgerbridge`
- Business/audit row counts: `0|0|0|0|0`

## Post-deploy acceptance

| Gate | Evidence |
|---|---|
| Containers | API, worker, and PostgreSQL healthy |
| API | `/health/live` returned `ok`; `/health/ready` returned `ready` |
| Schema exposure | `/openapi.json` returned 404 |
| Migration | `alembic current` returned `20260821_0002 (head)` |
| Runtime role | LOGIN only; no superuser, createdb, createrole, replication, or RLS bypass |
| Grants | Ledger tables usable; audit table SELECT-only; no schema CREATE |
| Worker | Heartbeat fresh |
| Process security | UID 10001, read-only rootfs, all capabilities dropped, no-new-privileges |
| Resource limits | 256 MiB memory and 128 PIDs for API/worker |
| Network | backend internal; API published only at `127.0.0.1:8650` |
| Integrity | 25-file deployment manifest verified after promotion |
| Database | checksums on; all Phase 1 tables empty |

## Verification harness notes

Two verification probes were corrected without changing production state. A direct
`psycopg.connect()` probe was initially given the SQLAlchemy dialect URL and failed
before querying; the project-accurate SQLAlchemy probe then verified the runtime
identity. A later probe tried to read revision from the process environment, but the
release contract stores it in the OCI image label; the label and deployed revision
file matched. Neither probe indicates an application or deployment failure.

## Rollback anchors and follow-up

The previous image `ledgerbridge-app:61ad910` is retained. The previous deployment
tree is `/srv/ai-center/ledgerbridge.previous-61ad910-before-phase1-20260821`.
The database, role, and deployment-tree backups remain protected on Hermes.

Before Phase 2 ingests real evidence, F-4 still requires executable backup/restore
automation and a restore-to-empty rehearsal with migration and checksum validation.
