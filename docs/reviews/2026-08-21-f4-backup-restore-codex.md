# F-4 encrypted backup and isolated restore rehearsal

- Date: 2026-08-21
- Implementer/reviewer: Codex self-audit
- Implementation branch: `ai/chatgpt/f4-backup-restore`
- Deployment-record branch: `ai/chatgpt/f4-deployment-record`
- Base: `e183c962db8c84afbd262105c773ad91946f4a45`
- Implementation commit: `958a26106a3118afcdf0c27ec3b70ccb9b479733`
- PR: `#7`, merged as `0c5616f648d720da88dd37deac94610486e7e611`
- Production source revision rehearsed and deployed:
  `0c5616f648d720da88dd37deac94610486e7e611`
- Verdict: COMPLETE; merged-SHA deployment and post-merge restore rehearsal passed

## Scope and result

F-4 now has one fail-closed command-line implementation for encrypted backups
and isolated restore rehearsals. The backup command briefly quiesces the exact
production API and worker containers, captures PostgreSQL roles, a custom-format
database dump with owners and ACLs, the artifact volume, and the rendered
deployment tree, then publishes only after GPG encryption/decryption round-trip
verification and successful service restart.

The restore command decrypts only below a private `/dev/shm` directory and
restores into randomly suffixed disposable resources on an internal-only Docker
network. It restores roles before the database, never selects a production
volume as a target, checks the restored runtime identity directly, and deletes
the exact rehearsal container/network/volumes on success or failure.

## Key and storage boundary

- Dedicated primary GPG fingerprint:
  `673F5E5E5E2DF43732FF4062805548394C472B4C`.
- Hermes keyring:
  `/srv/ai-center/ledgerbridge-secrets/backup-gnupg`, mode 700.
- Off-host private-key copy:
  `G:\我的云端硬盘\凭据\LedgerBridge-Hermes-backup-private-key-2026-08-21.asc`.
- The off-host file is outside the `AI` workspace and Git. Its copy hash
  matched the remote export before the temporary export was shredded. The
  mounted-drive ACL now reports only the current operator with full control.
- No private-key material, database URL, generated password, `.env` content,
  roles SQL, or plaintext dump appears in this report or repository.

The Hermes automation key intentionally has no interactive passphrase. This
allows unattended local backup/rehearsal and makes the mode-700 Hermes account
and keyring the primary protection boundary. The approved off-host copy protects
against loss of the Hermes disk; remote backup replication is outside F-4.

## Authoritative pre-merge rehearsal evidence

- Staged tool:
  `/srv/ai-center/ledgerbridge-f4-tools/backup_restore.py`.
- Tool SHA-256:
  `c72f04eb7bf2cdee265489de51cf453530a0f8553b671783f0e8b7bc70cde112`.
- Backup:
  `/srv/ai-center/backups/ledgerbridge/20260821T121630Z-2028e3a99abe`.
- Ciphertext SHA-256:
  `14d484b1718977ad5b884da4609d3e119a0a04ec9ce633cf521038c837d978a4`.
- Restore report:
  `restore-rehearsal-20260821T121702Z.json`.
- Backup directory is mode 700. Ciphertext, sidecars, and report are mode 600.
- Durable backup contents are ciphertext plus non-secret sidecars/report only.
- No `.partial-*`, plaintext work directory, restore container, restore
  network, or restore volume remained after the final run.

Restored database evidence:

| Invariant | Restored value |
| --- | --- |
| Alembic head | `20260821_0002` |
| Data checksums | `on` |
| Database owner | `ledgerbridge` |
| `ledgerbridge_app` table grants | 17 |
| Runtime role attributes | LOGIN, no elevated role/database/replication/RLS privileges |
| Audit privileges | SELECT allowed; INSERT/UPDATE/DELETE denied |
| Public schema CREATE | denied |
| LedgerBridge functions | 9 |
| Non-internal triggers | 8 |
| Five Phase 1 table row counts | all zero, source equals restore |
| Direct runtime identity | `session_user=current_user=ledgerbridge_app` |
| Artifact archive SHA-256 | `6f1b0b5918450f42a87172e5ffc7e19a9b6bae33a01973dc24fc7634ae85c340` |
| Deployment tree SHA-256 | `64c845389cb644740436a3055d9e15670aafa8ba0c6513fe9616d68db3df1a3f` |

The automation captured production state before and after rehearsal and compared
the full deployed revision, API image, artifact volume, database metadata, and
all three production container IDs. The report records
`production_unchanged=true` and `isolated_resources_removed=true`.
Independent post-run checks found zero rehearsal resources and all production
services healthy; the API ready endpoint returned `{"status":"ready"}`.

## Merge, deployment, and final rehearsal evidence

- PR #7 head `958a26106a3118afcdf0c27ec3b70ccb9b479733` passed the
  `secrets`, `quality`, and `compose` jobs. The main push run `32482307666`
  also completed successfully at merge commit
  `0c5616f648d720da88dd37deac94610486e7e611`.
- The exact merge commit was rendered as a 26-file manifest-protected
  production tree and deployed as `ledgerbridge-app:0c5616f`. API, worker,
  and PostgreSQL are healthy; API live/ready passed, OpenAPI remained 404,
  and the runtime identity remained `ledgerbridge_app|ledgerbridge_app`.
- The previous deployment remains at
  `/srv/ai-center/ledgerbridge.previous-2028e3a-before-f4` and records
  revision `2028e3a99abe5e20c842a95ec22f2931878d39ee`.
- Final merged-SHA backup:
  `/srv/ai-center/backups/ledgerbridge/20260821T124742Z-0c5616f648d7`.
- Final ciphertext SHA-256:
  `9d09705ebb482fc7a96f161e7f1b7db6b40f8e0000c6024b2d3e10f479d44e69`.
- Final restore report:
  `restore-rehearsal-20260821T124802Z.json`, status passed.
- Final artifact archive SHA-256:
  `6f1b0b5918450f42a87172e5ffc7e19a9b6bae33a01973dc24fc7634ae85c340`.
- Final deployment-tree SHA-256:
  `0dbbbdcfd4d2c6f2c42058cf6f0650eb9a975f1f08aaf7a763a44c760b9952c6`.

The final restored database again reported migration `20260821_0002`, data
checksums on, owner `ledgerbridge`, 17 runtime table grants, nine LedgerBridge
functions, eight non-internal triggers, all five table counts at zero, an
unprivileged runtime role, SELECT-only audit access, and denied public-schema
CREATE. The report records `production_unchanged=true` and
`isolated_resources_removed=true`. Independent checks found no rehearsal
container, network, volume, or `/dev/shm/ledgerbridge-*` path after completion.

## Fail-closed findings resolved during rehearsal

1. The first backup run could not write `artifacts.tar` through a mode-700
   `/dev/shm` bind mount after all container capabilities were dropped. No
   backup was published; the API/worker restarted healthy; partial/plaintext
   paths were absent. The archive helpers now use no network, a read-only root,
   all capabilities dropped, and add back only `DAC_OVERRIDE` plus
   `DAC_READ_SEARCH` for the protected bind mount.
2. The first restore run removed every disposable resource, but the cleanup
   verifier treated Docker's `[]` stdout for a failed `inspect` as evidence
   that resources remained. No report was published. The verifier now checks
   the subprocess exit code; the next runs passed and independent listing
   confirmed zero residuals.
3. An intermediate database function count included 36 extension functions.
   The final query counts the nine LedgerBridge migration-defined functions
   explicitly, making the evidence specific rather than merely non-zero.

## Quality gates

- Live ciphertext tamper test: changing the first ciphertext byte caused
  `encrypted backup checksum mismatch` before any restore resource was created;
  the exact test clone was removed and the authoritative backup was untouched.
- F-4 unit tests: 12 passed.
- Full PostgreSQL suite through a loopback-only Hermes test database and SSH
  tunnel: 61 passed, 2 Windows symlink tests skipped, 99.49% application
  coverage (98% required).
- The temporary test container, named volume, 0600 env file, and SSH tunnel
  were removed; independent checks returned zero remaining resources.
- Ruff check and format: passed.
- Strict mypy over `src alembic tests scripts`: 22 source files, no issues.
- Bandit over `src alembic scripts`: no issues.
- Sensitive-path scan: passed.
- Strict `pip-audit`: no known vulnerabilities.
- `git diff --check`: passed.
- Hermes `docker compose config --quiet`, image revision binding, deployment
  manifest, and ready endpoint: passed.
- GitHub PR and merged-main CI: all three jobs passed on both required heads.

The Windows workstation has no Docker CLI. The clean-checkout Compose build was
therefore verified by GitHub CI and the exact merged SHA was independently built
and started on Hermes.

## Public-repository security gate

Before the repository visibility change, the full-history Gitleaks job passed
on merged main and the local audit inspected all 27 historical commits. It found
no private-key header, GitHub/AWS/Slack token, or JWT. Generic credential-pattern
matches were confined to `.env.example`, CI-only test passwords, and runtime
`secrets.token_urlsafe()` generation. The only tracked sensitive-looking
filename is `.env.example`; real `.env`, key, credential, financial-evidence,
dump, and archive paths remain ignored.

The repository intentionally documents internal Hermes addresses, service
paths, and the public GPG fingerprint. These are operational metadata, not
credentials or public endpoints, and were accepted as low-risk disclosure. The
repository was changed from private to public at
`https://github.com/maiziwheat520-boop/caiwu`.

The Codex Security desktop launcher itself could not start because its Windows
subprocess decoded this workspace's Chinese path with GBK. It failed before
source review or artifact creation, so no scanner result is claimed. The
successful CI scanners and the source/history checks above form the recorded
visibility gate.

## Closure and later audit hook

F-4 is closed. Claude's later audit hook remains available without consuming
quota now: review fixed merge SHA
`0c5616f648d720da88dd37deac94610486e7e611` and add a new immutable report
without modifying implementation files.
