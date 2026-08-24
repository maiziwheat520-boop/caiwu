# LedgerBridge

The R1 synthetic Core read API foundation is documented in
`docs/tasks/2026-08-24-r1-synthetic-core-read-api.md`. It installs the six
versioned `/internal/v1` GET routes over an integrity-checked packaged fixture,
but keeps them disabled by default and rejects production enablement. It has no
database, production mTLS verifier, durable audit backend, or real-data source,
so it is not an operational R1 deployment. The frozen R0 contract remains in
`docs/tasks/2026-08-24-r0-synthetic-contract.md`.

The S1 synthetic online-encryption foundation is documented in
`docs/tasks/2026-08-24-s1-online-encryption.md` and
`docs/architecture/ONLINE_ENCRYPTION.md`. It adds secretstream, encrypted
artifact/state/spool primitives and a host-attestation parser, but Hermes volumes
and production key custody have not passed the operational gate. Real ingest is
still unconditionally unavailable.

LedgerBridge is a self-hosted financial ledger gateway for importing personal
financial evidence, normalizing source records, and building a traceable
double-entry ledger for trusted queries through Hermes.

The implementation priority is deliberately conservative:

1. ledger correctness;
2. evidence preservation;
3. deterministic imports;
4. reconciliation;
5. API and AI integration.

When the system is uncertain, it creates a review item or uses a suspense
account. It must not invent financial facts.

## Repository status

Phase 3 Slice A is deployed on Hermes at revision
`e426b488b2abb02f10ef02a61aae7ebe24c3283f` with migration `20260822_0004`.
The review-only branch `ai/chatgpt/phase-3-connector-runner` contains the
subsequent async dispatch, isolated runner, bounded upload adapter, role split,
and release-readiness hardening through `e2c31be`, including forward migration
`20260824_0009`. Those changes are not deployed, and no real evidence or
Connector is registered. Phase 4 framework commit `bbe776f` adds a default-disabled,
fail-closed Microsoft Graph provider adapter and explicit Connector factory
registry; it still has no OAuth client, manifest, real parser, or production
switch. The current Phase 5 framework adds side-effect-free deduplication,
zero-sum reconciliation proposals, explicit Suspense resolution contracts, and
migration `20260824_0010` for their review-only persistence boundary; it still
has no automatic posting or production switch. A default-disabled Review API
and worker persistence boundary now expose only explicit human decisions; the
deployed service remains on
Slice A with no real evidence imported.
Phase 6 adds a credential-free synthetic bank-statement Connector fixture for
isolated tests only; the default Connector registry and production manifest
remain empty.
See [PROJECT_STATUS.md](PROJECT_STATUS.md) and
[docs/architecture/IMPLEMENTATION_BASELINE.md](docs/architecture/IMPLEMENTATION_BASELINE.md).

## Local development

Requirements: Python 3.12+, Docker Compose, and PostgreSQL 15+.

```bash
cp .env.example .env
# Replace both example database passwords in .env before continuing.
uv sync --frozen --extra dev
docker compose up -d postgres
docker compose exec -T postgres sh /docker-entrypoint-initdb.d/10-ledgerbridge-runtime-role.sh
docker compose --profile tools run --rm migrate
docker compose up -d api worker
```

### Quick R1 synthetic demo

The six R1 internal-read GET routes can be exercised locally without Docker or
PostgreSQL.  The demo uses only the packaged synthetic fixture, a fixed
loopback-only principal, and a process-local evidence-read audit sink; it does
not enable production mTLS, connect to a database, or read real data.

```bash
uv run --frozen --extra dev python scripts/r1_synthetic_demo.py
```

For a one-command smoke check that starts no listener:

```bash
uv run --frozen --extra dev python scripts/r1_synthetic_demo.py --check
```

In another terminal:

```bash
curl http://127.0.0.1:8651/internal/v1/capabilities
curl "http://127.0.0.1:8651/internal/v1/candidates?month=2026-08&business_unit=unit-demo-a"
curl "http://127.0.0.1:8651/internal/v1/reconciliations/2026-08?entity_ref=10000000-0000-4000-8000-000000000001&business_unit=unit-demo-a"
curl "http://127.0.0.1:8651/internal/v1/ledger-summary?entity_ref=10000000-0000-4000-8000-000000000001&business_unit=unit-demo-a&from_month=2026-08&to_month=2026-08"
```

This launcher is for a local walkthrough only and must not be treated as an
operational authentication or audit deployment.

### Quick synthetic review demo

The review workflow can also be exercised without PostgreSQL. This demo reuses
the same `/v1/reviews` route handlers and response models with one deterministic
in-memory candidate. It binds to `127.0.0.1:8652`, resolves the candidate, and
shows that a second decision is rejected as a terminal conflict.

```bash
uv run --frozen --extra dev python scripts/r1_synthetic_review_demo.py --check
```

To start the loopback listener for manual checks:

```bash
uv run --frozen --extra dev python scripts/r1_synthetic_review_demo.py
curl http://127.0.0.1:8652/v1/reviews?review_status=OPEN
```

The fixture is synthetic-only; it does not write a database, read real
financial evidence, or enable the production review API.

Quality gate:

```bash
uv run --frozen --extra dev ruff check .
uv run --frozen --extra dev ruff format --check .
uv run --frozen --extra dev mypy src alembic tests scripts
uv run --frozen --extra dev pytest
uv run --frozen --extra dev bandit -c pyproject.toml -r src alembic scripts
uv export --quiet --frozen --extra dev --no-emit-project --format requirements.txt --output-file /tmp/ledgerbridge-audit-requirements.txt
uv run --frozen --extra dev pip-audit --strict --requirement /tmp/ledgerbridge-audit-requirements.txt
```

## Storage boundary

- Source code and design decisions: this repository.
- Runtime artifacts and database data: Docker volumes / `var/`, ignored by Git.
- Credentials and OAuth tokens: external secret store, never this repository.
- Historical design reviews: the parent workspace's `outputs/` directory.
- Artifact defaults: 50 MiB per file, 10 GiB published, 512 MiB staging, with
  no automatic deletion under quota pressure.

See [docs/architecture/STORAGE.md](docs/architecture/STORAGE.md) for the full
layout and retention rules, [docs/architecture/LEDGER_CORE_OPERATIONS.md](docs/architecture/LEDGER_CORE_OPERATIONS.md)
for the Phase 1 lifecycle and audit contract,
[docs/architecture/ONLINE_ENCRYPTION.md](docs/architecture/ONLINE_ENCRYPTION.md)
for the S1 application/host split, and
[docs/architecture/DEPLOYMENT_HERMES.md](docs/architecture/DEPLOYMENT_HERMES.md)
for the split runtime/migration database identities.
