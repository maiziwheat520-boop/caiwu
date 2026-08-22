# Phase 2 Claude audit remediation

| Item | Value |
| --- | --- |
| Date | 2026-08-22 |
| Implementer | Codex |
| Branch | `ai/chatgpt/phase-2-audit-fixes` |
| Vulnerable merged base | `23bfbd3bcc79068c3744dab05a961d497590ec8e` |
| Claude report commit | `cc07e08aa264c5a2aa42d9b422a049cf5c926ee9` |
| Remediation executable SHA | `40fcd022ae6d3127aa7bdc17afecb6b1a159cda0` |
| Production deployment | Not authorized; not performed |

## Outcome

Claude's 1 BLOCKER, 1 HIGH, 8 MEDIUM, and 8 LOW findings were reproduced or
validated against the merged base. The current remediation tree closes the two
release blockers and every applicable lower-severity item. It also adds
behavior-sensitive tests for the controls whose removal previously left the
suite green. No real connector, real financial evidence, production migration,
or production service change is part of this work.

## Finding disposition

| Finding | Disposition | Remediation and evidence |
| --- | --- | --- |
| P2-B1 | Closed | Migration and role bootstrap revoke database `TEMPORARY` from `PUBLIC`. All Phase 1/2 security functions pin `search_path=pg_catalog`; business relations are `public.*`. Tests first prove runtime TEMP is absent, then temporarily grant it and replay the reported unbalanced insert, POSTED posting delete/update, cross-entity posting, one-posting +777 POST with valid audit, and account-class mutation. Correction-target shadowing is also rejected. Privilege is revoked in `finally`. |
| P2-H1 | Closed | `open_verified()` opens once, checks regular-file type/size/digest with `fstat()` and reads that descriptor, rewinds it, then gives the same descriptor to the read-only wrapper. A Linux race test replaces the pathname after verification and proves the parsed bytes still come from the verified inode. |
| P2-M1 | Closed | `ArtifactIntegrityError`, evidence I/O, and connector-contract errors have separate terminal codes and summaries. A tampering connector produces `FAILED/EVIDENCE_INTEGRITY`, including the audit payload. |
| P2-M2 | Closed | Publication failures before a trustworthy RawArtifact can exist raise bounded `EvidenceIngestionError` codes. After an artifact exists, normal processing failures terminalize an ImportJob and append `import.complete`; a database failure that prevents that state is mapped to controlled `IMPORT_DATABASE`. Corrupt duplicate publication, connector identity drift, audit-write rollback, and job-creation failure are regression-tested. |
| P2-M3 | Closed | `artifact.ingest` binds digest, size, storage key, source, media type, and a SHA-256 of the original filename. Tests independently alter source, media type, byte size, and filename hash while keeping table checks valid; each trigger branch rejects the row. |
| P2-M4 | Closed | Downgrade refuses to run if any RawArtifact, ImportJob, or SourceRecord exists. A data-bearing temporary database proves the version and rows remain; the empty `head -> base -> head` round trip still succeeds. Phase 1 hardening and the database-wide TEMP revocation intentionally persist after empty downgrade. |
| P2-M5 | Closed | Journal creation now requires a fresh same-transaction `journal.create` event targeting the preallocated entry UUID. Wrong action, target, or stale events fail. Evidence events cannot cross-authorize creation because their action is different. |
| P2-M6 | Closed with boundary clarification | The SDK object exposes only `read()`—no path, `fileno()`, write method, or underlying file object. Internally it owns a raw descriptor. Documentation now states that this is capability minimization, not a Python sandbox: installed in-process connectors are trusted code; untrusted connectors require out-of-process isolation before Phase 3. |
| P2-M7 | Closed under frozen identity rule | A source or media-type conflict routes to `NEEDS_REVIEW/PROVENANCE_CONFLICT` without changing the existing artifact or records. A filename-only change remains idempotent by the pre-existing acceptance rule that duplicate bytes under different filenames form one artifact; documentation now makes that exception explicit. |
| P2-M8 | Closed | Linux tests observe real directory `fsync`, fail if `O_NOFOLLOW` is removed during a symlink swap, verify inode continuity, and confirm existing artifact directories are tightened to 0700. |
| P2-L1 | Closed | Connector name/version properties are read once, validated once, and stored as one immutable binding snapshot. The Protocol accepts read-only properties. |
| P2-L2 | Closed | Descriptor ownership has a single close path; the previous `fdopen` double-close branch no longer exists. |
| P2-L3 | Closed | Normalized minor-unit amounts must fit signed PostgreSQL `BIGINT`; both overflow directions are tested. |
| P2-L4 | Closed | JSON validation is iterative, caps depth at 64 and serialized UTF-8 size at 1,000,000 bytes, and maps recursion failures to `ConnectorContractError`. |
| P2-L5 | Closed | The ineffective `os.link(..., follow_symlinks=False)` claim was removed. Documentation identifies create-if-absent `EEXIST` handling plus descriptor `O_NOFOLLOW`/`fstat()` as the actual defense. |
| P2-L6 | Closed | The image creates the artifact root as 0700; store startup tightens existing POSIX directories to 0700 and synchronizes the metadata. |
| P2-L7 | Closed | Operations documentation states that a new parser version gets a distinct job but cannot overwrite an existing locator; re-parsing therefore needs human approval/review. |
| P2-L8 | Closed | The runtime-boundary test uses `yaml.safe_load()` so anchors and merged service values are asserted structurally. PyYAML and its type stubs are explicit locked development dependencies. |

## Executed verification

### Local Windows gate

- Ruff check: passed.
- Ruff format check: 59 files formatted.
- strict mypy over `src alembic tests scripts`: 32 source files, no issues.
- local pytest: 61 passed; 67 PostgreSQL/POSIX-only cases skipped by platform.

### Hermes disposable Linux/PostgreSQL 15 gate

The working tree was copied as tracked files only to
`/tmp/ledgerbridge-p2fix-20260822-1420`. Tests used a new internal Docker network,
an ephemeral PostgreSQL container with data checksums, a separate runner
container, fixed synthetic test credentials, and no production mount or network.

- First run exposed one obsolete test assumption that TEMP remained available;
  the test was corrected to grant/revoke it only inside its own fixture.
- Critical TEMP/shadow tests: 3 passed.
- Full coverage run: 128 passed, total coverage 95.79%.
- Empty migration round trip: `head -> base -> head` passed.
- Full suite after the round trip: 128 passed.
- Ruff, format, strict mypy, and Bandit passed in Linux.
- Strict audit of the frozen exported dependency set found no known vulnerabilities.
- Compose configuration parsed with isolated synthetic values. The production
  Dockerfile built as `ledgerbridge-app:p2fix-local`; its default user is
  `ledgerbridge` (UID 10001), the artifact root is `0700` and owned by UID/GID
  10001, and a clean container imported the installed package successfully.

The production `ledgerbridge-api-1`, `ledgerbridge-worker-1`, and
`ledgerbridge-postgres-1` containers remained healthy and untouched. Production
still runs image `ledgerbridge-app:0c5616f` at Alembic `20260821_0002`; therefore
the P2-B1 path remains live there until a later explicitly authorized migration
and deployment.

## Audit metadata corrections

Two report-header statements do not affect the technical findings but should be
corrected in any addendum: the GitHub repository is public, and F-6 branch
protection is enabled with PR-required strict `secrets`, `quality`, and `compose`
checks. The technical BLOCKER/HIGH reproduction and remediation do not depend on
either metadata point.

## Remaining Phase 3 gates

- Add aggregate artifact/staging quotas before unattended ingestion.
- Canonicalize `source_record.source` before real connector identities are used.
- Run untrusted third-party connectors out of process with separate credentials.
- Restore/deployment verification must assert every security trigger exists and
  has `tgenabled = 'O'` because a PostgreSQL table owner can disable triggers by
  design.

## Verdict

**REMEDIATION IMPLEMENTED, COMMITTED, AND LOCALLY/HERMES VALIDATED; PROTECTED CI
AND MERGE DECISIONS PENDING.** Production deployment and real-data ingestion remain
explicitly out of scope.
