# Task: company-report policy rollout runner

- Status: prepared and self-tested; production execution intentionally not run
- Base: Core release integration `87894a64893476d84823cd157e8a54528cb53b2a`
- Scope: add only `ledger:read` while advancing the bound Core policy, Core runtime,
  and Web runtime from generation 2 to generation 3

## Execution boundary

Run only after the approved company-report Web patch is merged into the unified
Web release and its build artifact is ready. The default runner mode is `plan`.
Production execution additionally requires root and the exact acknowledgement
`2-to-3`; no policy contents, environment values, financial identifiers, or
container IDs are printed.

## Atomic order

1. Validate the current policy and both environment files are exactly generation
   2. Build a semantic policy delta that changes only the two bound generation
   fields and appends `ledger:read` once.
2. Create a root-private backup of the policy, Core environment, and Web
   environment files. Record only their digests and sizes in the backup manifest.
3. Snapshot every running container identity and require the configured reader
   and Web targets to exist.
4. Atomically replace all three files. No container has been restarted yet, so a
   crash in this step leaves the running generation unchanged.
5. Force-recreate only the internal reader with `--no-deps`; require healthy
   state and Core generation 3.
6. Force-recreate only Web with `--no-deps`; require healthy state and Web
   generation 3.
7. Require both target container identities to have changed and every non-target
   container identity to be byte-for-byte unchanged.
8. Retain the private backup. If any step after backup fails, restore all three
   files, recreate reader then Web at generation 2, require both healthy, and
   re-run the non-target identity gate before returning failure.

## Commands

Self-test:

```text
python -m unittest tests.test_company_report_policy_rollout
```

Plan and execute use the same absolute policy, Core/Web environment, compose,
service, and container paths. First run with `--mode plan`; after reviewing its
eight non-sensitive steps, repeat with `--mode execute
--acknowledge-generation 2-to-3`. Never place policy JSON or environment values
on the command line. Repeat `--core-compose-path` in the exact order recorded on
the running reader container so every production overlay remains active.

## Post-execution acceptance

- Reader and Web report healthy at generation 3.
- No non-target container identity changed.
- An authenticated, read-only company-report request no longer returns
  `CAPABILITY_REQUIRED`.
- A successful empty posted-ledger result remains `AVAILABLE` and displays real
  zeros; only a posted-ledger 404/503 displays `待接正式账簿`.
