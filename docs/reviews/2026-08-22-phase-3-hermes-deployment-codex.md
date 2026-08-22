# LedgerBridge Phase 3 Hermes Deployment and Restore-Gate Closure

Date: 2026-08-22

## Scope and authorization

The user explicitly authorized deployment of the merged Phase 3 Slice A controls to Hermes, then explicitly authorized a protected hotfix PR and redeployment after the first post-deploy restore rehearsal exposed verifier defects. No production credential was copied into the repository or this report.

## Source and hotfix

- Phase 3 Slice A mainline source: `e73e718e00224147bfee93da4916967ab0fcc809`.
- Restore-grant verifier hotfix PR: #16, `https://github.com/maiziwheat520-boop/caiwu/pull/16`.
- Hotfix implementation commit: `055b6f66c5c19c99f4d9f97cc594cb014b1d5397`.
- Hotfix merge commit deployed: `e426b488b2abb02f10ef02a61aae7ebe24c3283f`.
- Hermes image tag: `ledgerbridge-app:e426b48`; the image label was checked against the full merge revision.

The hotfix records and validates column-level runtime grants that are not covered by table-level grants. It also removes the overly broad table-level `UPDATE` grant for `import_job` and grants only the state-machine columns required by the worker. This closes the restore verifier's false negative without expanding runtime authority.

## Deployment sequence

1. Before the initial Phase 3 deployment, the existing `c56b6ff` production tree was backed up and independently restored successfully:
   - backup: `/srv/ai-center/backups/ledgerbridge/20260822T105723Z-c56b6ffdde9f`
   - rehearsal: `restore-rehearsal-20260822T105751Z.json`
2. Slice A was deployed as `e73e718`. API, worker, and PostgreSQL stayed healthy, migration `20260822_0004` reached head, and the runtime security probes passed.
3. The first post-deploy backup was created at `/srv/ai-center/backups/ledgerbridge/20260822T110759Z-e73e718e0022`. Its restore rehearsal stopped at the verifier because the expected baseline did not model column-level `UPDATE` grants. No production data was written and no service remained unhealthy.
4. The protected hotfix PR #16 was merged after local gates and all required GitHub checks passed. A first hotfix package used the six-character tag `e426b4`; its rehearsal correctly rejected that tag as invalid. The image was rebuilt and services were restarted with the seven-character tag `e426b48` before the passing evidence below was collected.

## Final Hermes evidence

Current project: `/srv/ai-center/ledgerbridge`

- API, worker, and PostgreSQL: Docker `healthy`.
- `/health/live`: `{"status":"ok"}`.
- `/health/ready`: `{"status":"ready"}`.
- `/openapi.json`: HTTP 404.
- `DEPLOYED_REVISION`: `e426b488b2abb02f10ef02a61aae7ebe24c3283f`.
- Alembic: `20260822_0004 (head)`.
- `MANIFEST.sha256`: all 35 files verified.
- Runtime database role: `TEMPORARY=false`, public schema `CREATE=false`.
- Public functions: 52 total, 16 with a pinned function `search_path` configuration.
- Public triggers: 16 enabled, 0 disabled.
- `audit_event`: SELECT allowed, INSERT denied to `ledgerbridge_app`.
- Canonical registry: SELECT allowed, INSERT denied to `ledgerbridge_app`.
- Artifact root: mode `0700`, owned by UID/GID `10001`, empty.
- Production row counts: `entity=0`, `account=0`, `journal_entry=0`, `posting=0`, `audit_event=0`, `raw_artifact=0`, `source_record=0`, `import_job=0`; seeded registries remain `ingest_channel=2`, `source_system=1`.

Final post-hotfix backup and rehearsal:

- backup: `/srv/ai-center/backups/ledgerbridge/20260822T112755Z-e426b488b2ab`
- rehearsal: `restore-rehearsal-20260822T112825Z.json`
- result: `isolated restore rehearsal passed`

The passing rehearsal validates the encrypted backup, deployment tree, database metadata, v2 security/grant baseline (including column grants), artifact controls, migration head, and isolated restore cleanup. The failed intermediate rehearsal directories remain as audit evidence of the verifier defects; they are not used as release evidence.

## Rollback anchors

The following rollback trees and cached images remain on Hermes:

- `/srv/ai-center/ledgerbridge.previous-c56b6ff-before-phase3-20260822T1103Z`
- `/srv/ai-center/ledgerbridge.previous-e73e718-before-restore-hotfix-20260822T1119Z`
- `ledgerbridge-app:c56b6ff`, `ledgerbridge-app:e73e718`, and `ledgerbridge-app:e426b4`

No rollback anchor or production volume was deleted. Temporary package archives under `/tmp` were not used as evidence after the final deployment.

## Conclusion

Phase 3 Slice A is deployed on Hermes at the protected mainline merge commit `e426b488b2abb02f10ef02a61aae7ebe24c3283f`. The post-hotfix encrypted backup and isolated restore gate pass. No real financial evidence has been imported. Slice B, real connectors, OAuth, and the mail collector remain out of scope and require a separate decision.
