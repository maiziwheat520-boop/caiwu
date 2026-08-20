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
/var/backups/ledgerbridge/         encrypted backups
```

Local development equivalents live below `var/` or Docker named volumes. Both
are ignored. Raw CSV/XLSX/ZIP/EML files must not be placed in `tests/`; use
synthetic generated fixtures.

## Secrets

Production secrets belong in an external secret store or systemd credentials.
The repository stores only secret references and `.env.example`. Microsoft OAuth
tokens and mailbox credentials are never database payloads, logs, docs, or Git.

## Retention

- SourceRecord: permanent.
- AuditEvent: permanent and append-only.
- RawArtifact: default FOREVER in v0.1; a later explicit retention policy may
  remove the file while retaining its metadata and SourceRecords.
- Backups: encrypted, restore-tested, and kept outside the source repository.
