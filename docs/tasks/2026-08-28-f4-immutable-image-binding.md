# Task: F-4 immutable application-image binding

- Status: implementation complete locally; operator rehearsal and review pending
- Implementation owner: Codex
- Branch: `ai/chatgpt/storage-cutover-image-binding`
- Base commit: `cd4edd8`
- Target: backup/restore identity validation and the one-time Hermes storage cutover

## Trigger

The 2026-08-28 encrypted-storage cutover stopped safely during Stage 2. Backup
creation published ciphertext, but isolated restore rejected the application
image revision label. Read-only inspection proved a mutable-tag drift:

- healthy API and worker containers share immutable image ID `f417a464f7d0...`;
- their container configuration retains the full deployed revision
  `e426b488b2abb02f10ef02a61aae7ebe24c3283f`;
- `ledgerbridge-app:e426b48` instead resolves to image ID `cb2cd5c13bcb...`;
- that current tagged image has only the short label `e426b48`.

The restore tool correctly failed closed, but backup metadata did not retain the
immutable image identity needed to distinguish this condition before publication.

## Scope

- Add a v3 encrypted-backup format containing `api_image_id`.
- Require API and worker to share one immutable image ID during backup.
- Require the revision tag to resolve to that exact ID and validate the full
  revision label on the immutable image.
- Use the immutable ID for artifact backup and v3 isolated-restore containers.
- Preserve v1/v2 restore compatibility.
- Add a regression that proves a retargeted mutable tag is rejected.
- Harden the ignored one-time cutover wizard to repair this already-observed
  deployment condition without restarting the current production containers.

## Safety boundary

- Do not relax the full-revision label check.
- Do not delete the running image, drifted tagged image, existing backups, or
  legacy Docker volumes.
- Candidate rebuilding must use the manifest-verified deployed source tree and
  retain the drifted image under a separate recovery tag.
- Reuse the encrypted backup at
  `/srv/ai-center/backups/ledgerbridge/20260828T061115Z-e426b488b2ab` for the
  isolated rehearsal before any disk, LUKS, filesystem, or service cutover.
- The operator must execute and confirm all PVE/root actions.

## Acceptance gates

- The tag-drift regression fails against the old implementation and passes with
  the immutable-ID implementation.
- The complete backup/restore unit file passes.
- Python compilation and diff checks pass.
- The one-time wizard passes `bash -n` on the target PVE shell.
- The rebuilt candidate has the full revision label and the existing encrypted
  backup passes isolated restore before Stage 3 can run.
- Ruff check/format, strict mypy, Bandit, dependency/sensitive-path checks, and
  independent review remain required before merge.

## Current evidence

- Red regression: old `_validate_backup_image` had no immutable-ID parameter.
- Green regression: `42 passed` in `tests/test_backup_restore.py`.
- `py_compile` and `git diff --check`: passed.
- Ruff check/format, strict mypy on the changed Python files, and Bandit: passed.
- Revised wizard target-PVE `bash -n`: passed.
- Production services remain unchanged and healthy; Compose renders
  `ledgerbridge-app:e426b48` from explicit `.env` revision values.

## Pending

- Operator copies and reruns the revised one-time wizard.
- Capture the candidate image ID and isolated restore report.
- Complete the remaining repository quality gates and independent review before
  any protected merge.
