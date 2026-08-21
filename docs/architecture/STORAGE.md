# Storage and repository layout

## Source-controlled

```text
LedgerBridge/
├── src/ledgerbridge/       application code
├── alembic/                reviewed schema migrations
├── tests/                  synthetic tests only
├── docs/architecture/      frozen technical baseline
├── docs/governance/        role and handoff rules
├── docs/tasks/             task specifications and acceptance evidence
└── docs/reviews/           independent review reports
```

This directory is an independent Git repository and should map to one dedicated
GitHub repository. It is not committed into the parent `AI` document repository.

## Runtime, never Git

```text
/srv/ai-center/ledgerbridge/       deployment files on Hermes
/var/lib/ledgerbridge/artifacts/   immutable raw evidence inside its named volume
/var/lib/postgresql/data/          PostgreSQL data inside its named volume
/srv/ai-center/backups/ledgerbridge/ encrypted backups (mode 700)
```

Local development equivalents live below `var/` or Docker named volumes. Both
are ignored. Raw CSV/XLSX/ZIP/EML files must not be placed in `tests/`; use
synthetic generated fixtures.

## Secrets

Production secrets belong in an external secret store or systemd credentials.
The repository stores only secret references and `.env.example`. Microsoft OAuth
tokens and mailbox credentials are never database payloads, logs, docs, or Git.

The dedicated backup keyring lives at
`/srv/ai-center/ledgerbridge-secrets/backup-gnupg` with mode 700. Its private
key is never stored in the repository, deployment tree, backup directory, or
reports. The approved off-host private-key copy lives in the operator's external
`G:\我的云端硬盘\凭据\` directory, outside the `AI` workspace.

## Retention

- SourceRecord: permanent.
- AuditEvent: permanent and append-only.
- RawArtifact: default FOREVER in v0.1; a later explicit retention policy may
  remove the file while retaining its metadata and SourceRecords.
- Backups: encrypted, restore-tested, and kept outside the source repository.

## Encrypted backup layout

Each successfully published backup is one mode-700 direct child of
`/srv/ai-center/backups/ledgerbridge/`:

```text
<UTC timestamp>-<full revision prefix>/
├── ledgerbridge-backup.tar.gpg   encrypted database, roles, artifacts, and deployment tree
├── backup.json                   non-secret revision, key fingerprint, image pin, and digest
├── SHA256SUMS                    ciphertext integrity sidecar
└── restore-rehearsal-*.json      non-secret evidence, present only after a passing rehearsal
```

Plaintext dumps and extracted files exist only in a randomly named mode-700
directory under `/dev/shm` and are removed on success or failure. A backup is
renamed from a guarded `.partial-*` directory into its durable name only after
GPG decrypt-round-trip verification and successful API/worker restart.

The restore rehearsal creates a fresh PostgreSQL volume, artifact volume,
internal-only Docker network, and container whose exact names carry a random
eight-hex suffix. It restores roles before the database and removes those exact
resources in a `finally` path. Production database and artifact volumes are
never restore targets.
