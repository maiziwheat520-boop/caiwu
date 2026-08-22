# Phase 2 Hermes deployment record

- Date: 2026-08-22
- Target: `aiadmin@192.168.1.39` (`ai-hub`)
- Source PR: #11
- Remediation executable: `40fcd022ae6d3127aa7bdc17afecb6b1a159cda0`
- PR head: `8d64ba60fd44962f4f2ce0ecf4617edc8fa07940`
- Merge/deployed revision: `c56b6ffdde9f723efe1792ae1312ec8795bba165`
- Verdict: DEPLOYED, RESTORED IN ISOLATION, AND VERIFIED

## Change control

The user separately authorized PR #11 publication, merge-commit merge, and the
complete production deployment workflow. The deployment imported no real
evidence and created no business rows. Claude was not invoked again; its
immutable report and the finding-by-finding Codex response remain the audit
trail.

## Pre-deploy state and rollback gate

Production ran `ledgerbridge-app:0c5616f` at Alembic `20260821_0002`; API,
worker, and PostgreSQL were healthy. All five Phase 1 tables and the artifact
volume were empty, PostgreSQL data checksums were on, the 26-file deployment
manifest verified, and the protected `.env`, backup root, and GPG home retained
their required modes.

Before changing production, F-4 created encrypted backup
`/srv/ai-center/backups/ledgerbridge/20260822T073427Z-0c5616f648d7` and restored
it successfully into fresh isolated resources. The report is
`restore-rehearsal-20260822T073457Z.json` in that directory. No production
volume or network was used as a restore target.

## Release artifact and promotion

The reviewed merge was rendered into 32 manifest-bound runtime files. The local
release bundle is
`G:\我的云端硬盘\AI\outputs\ledgerbridge-phase2-deploy-c56b6ffdde9f.tar.gz`;
its SHA-256 is
`2844f9c8743aded203ef83f4f8fb41aa73774f5185663f82847b973e4f268da6`.
It excludes Git metadata, tests, documentation-only review material, development
environments, and `.env`.

The candidate Compose configuration parsed successfully. Image
`ledgerbridge-app:c56b6ff` built with OCI revision label equal to the full merge
SHA; its image ID is
`sha256:fb345b045da0ecf1282073be5ff37b919fb021631699795e46205af8462a35ff`.
The image runs as UID 10001, imports the installed package, and creates the
artifact root as mode 0700 owned by UID/GID 10001.

The old rendered tree remains at
`/srv/ai-center/ledgerbridge.previous-0c5616f-before-phase2-20260822`, and image
`ledgerbridge-app:0c5616f` remains present. The new role bootstrap revoked
database TEMP from PUBLIC, the owner-only migration service advanced Alembic
from `20260821_0002` to `20260821_0003`, and API/worker were recreated from the
merge-labeled image.

## Post-deploy acceptance

| Gate | Evidence |
| --- | --- |
| Containers | API, worker, and PostgreSQL healthy; application services run `ledgerbridge-app:c56b6ff` |
| API | `/health/live` and `/health/ready` passed; `/openapi.json` returned 404 |
| Network | LedgerBridge listens only on `127.0.0.1:8650` |
| Migration | Alembic is `20260821_0003` |
| Data boundary | Entity, Account, JournalEntry, Posting, AuditEvent, RawArtifact, ImportJob, and SourceRecord are all empty |
| Runtime identity | API connects as `session_user=current_user=ledgerbridge_app` |
| TEMP blocker | `has_database_privilege(..., 'TEMP')` is false and a runtime `CREATE TEMP TABLE` probe is rejected |
| Function hardening | All 14 named Phase 1/2 security functions exist and have exactly `search_path=pg_catalog` |
| Trigger hardening | All 13 non-internal public triggers are enabled (`tgenabled='O'`) |
| Runtime role | LOGIN only; no superuser, createdb, createrole, replication, or RLS bypass; no public-schema CREATE |
| Artifact boundary | Root mode is 0700 with zero files; API mount is read-only and worker mount is read-write |
| Container hardening | UID 10001, read-only rootfs, all capabilities dropped, 256 MiB and 128 PID limits |
| Worker | Heartbeat is fresh |
| Integrity | The 32-file deployment manifest verifies against the full merge SHA |

## Post-deploy backup and restore

F-4 then created encrypted backup
`/srv/ai-center/backups/ledgerbridge/20260822T074234Z-c56b6ffdde9f`.
The ciphertext SHA-256 is
`6c02de22c75ec63528031db90edbdc4eac581f260db98071b346522b0d52f24a`.
The isolated restore report
`restore-rehearsal-20260822T074308Z.json` passed with revision `c56b6ff...`,
Alembic `20260821_0003`, data checksums on, 23 runtime grants, 13 triggers,
matching deployment/artifact digests, disposable resources removed, and
production unchanged.

The F-4 v1 report schema still serializes the five Phase 1 row counts and its
original function-count metric. Separate deployment probes therefore explicitly
verified the three Phase 2 tables, all 14 hardened function configurations, and
all 13 enabled triggers. Extending the durable restore-report schema remains a
future hardening item; it did not reduce this deployment's executable checks.

## Final state

Production is deployed at the reviewed merge SHA with the Claude BLOCKER/HIGH
paths closed. No real connector, real financial evidence, automatic posting, or
mail collector was enabled. The pre-deploy and post-deploy encrypted backups,
the previous rendered tree, and the previous image remain rollback anchors.
