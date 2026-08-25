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

### Synthetic Hermes private-message boundary

The next intake seam can be replayed without Hermes or network credentials:

```bash
uv run --frozen --extra dev python scripts/r1_synthetic_hermes_message_demo.py
```

The output keeps only an eligible primary-profile private message for later
triage, ignores pre-activation history, and tombstones group/assistant traffic.
It does not classify financial intent or delete anything itself.

The following triage seam demonstrates the fail-closed fallback when no
reviewed classifier is available:

```bash
uv run --frozen --extra dev python scripts/r1_synthetic_hermes_triage_demo.py
```

The synthetic keyword classifier marks the fixture as a candidate; the
unavailable classifier keeps the same message as `AMBIGUOUS_RETAIN`.

The candidate-intent handoff is also replayable without persistence:

```bash
uv run --frozen --extra dev python scripts/r1_synthetic_candidate_intent_demo.py
```

It binds the triaged message, source event, entity, and evidence digest into an
immutable intent and explicitly reports `writes_posting: false`.

### Quick synthetic data gateway

For a usable local input/output loop, start the loopback-only JSON gateway:

```bash
uv run --frozen --extra dev python scripts/r1_synthetic_data_gateway.py
```

Submit one bounded message and base64 evidence:

```bash
curl -X POST http://127.0.0.1:8653/v1/intake \
  -H 'content-type: application/json' \
  -d '{"message_id":"demo-1","source_event_ref":"40000000-0000-4000-8000-000000000099","entity_ref":"10000000-0000-4000-8000-000000000001","text":"请处理发票","evidence":[{"evidence_ref":"20000000-0000-4000-8000-000000000099","media_type":"text/plain","content_base64":"c3ludGhldGljIGludm9pY2U="}]}'
curl http://127.0.0.1:8653/v1/candidates
```

The gateway is synthetic and, by default, process-local: it does not persist
raw bytes or create postings. Its response is the input/output contract for the
next Core persistence adapter.

For a restart-persistent local staging view, opt in to metadata-only SQLite
(the directory is ignored by Git):

```powershell
$env:LEDGERBRIDGE_SYNTHETIC_PERSISTENCE_PATH = "$(Join-Path (Get-Location) 'var\synthetic-gateway.sqlite3')"
uv run --frozen --extra dev python scripts/r1_synthetic_data_gateway.py
```

This stores only candidate JSON projections and evidence digests/metadata; it
does not store raw message bytes, create PostgreSQL rows, or enable production
Core writes. Remove the file when the staging review is complete.

With that opt-in store, review a candidate through the same versioned command
state machine:

```powershell
curl -X POST "http://127.0.0.1:8653/v1/candidates/<CANDIDATE_UUID>/command" `
  -H 'content-type: application/json' `
  -d '{"actor_ref":"operator:staging","command":{"operation_id":"<OPERATION_UUID>","action":"IGNORE","expected_revision":1,"reason":"not relevant","decided_at":"2026-08-25T04:00:00Z"}}'
```

The endpoint is disabled without SQLite persistence and remains synthetic-only;
production authentication, Core PostgreSQL writes, and Posting remain closed.

For the same loopback flow without hand-written curl, use the CLI:

```powershell
uv run --frozen --extra dev python scripts/r1_staging_cli.py intake-eml message.eml --entity-ref <ENTITY_UUID>
uv run --frozen --extra dev python scripts/r1_staging_cli.py list
uv run --frozen --extra dev python scripts/r1_staging_cli.py command <CANDIDATE_UUID> --action IGNORE --expected-revision 1 --reason "not relevant" --actor-ref operator:staging
```

`intake-json` also accepts `-` for stdin. The CLI rejects non-loopback URLs and
caps file/response sizes; it is a staging convenience, not an auth boundary.
The complete copy/paste acceptance flow is in
`docs/tasks/2026-08-25-r1-synthetic-staging-release-candidate.md`.

An exported RFC 5322 message can use the same boundary. Supply the target
entity explicitly; the parser derives a stable source event from `Message-ID`:

```bash
curl -X POST http://127.0.0.1:8653/v1/intake/eml \
  -H 'content-type: message/rfc822' \
  -H 'X-LedgerBridge-Entity-Ref: 10000000-0000-4000-8000-000000000001' \
  --data-binary '@message.eml'
```

The EML route is still synthetic and process-local; it does not connect to
Outlook or persist the original message. Its output includes the source
subject/time/format and each attachment filename, media type, byte size, and
SHA-256 digest.

The Outlook/Microsoft Graph authentication seam is kept separate and disabled:
`src/ledgerbridge/mail_oauth.py` builds PKCE authorization URLs and validates an
injected token exchange, but does not create network clients, read secrets, or
persist refresh tokens. See
`docs/tasks/2026-08-25-outlook-oauth-framework.md` before enabling any mailbox.

For an explicitly enabled, isolated staging replay, start the loopback gateway
and provide either a short-lived token through the process environment or a
generic credential target stored in Windows Credential Manager:

```powershell
$env:LEDGERBRIDGE_STAGING_NETWORK = "1"
$env:LEDGERBRIDGE_STAGING_ACCESS_TOKEN = "<short-lived-token>"
# Alternative to the line above (use exactly one):
# $env:LEDGERBRIDGE_STAGING_CREDENTIAL_TARGET = "LedgerBridge/Staging/Graph"
# or: $env:LEDGERBRIDGE_STAGING_CREDENTIAL_FILE = "G:\\我的云端硬盘\\凭据\\home-infra-credentials.md"
$env:LEDGERBRIDGE_STAGING_MAILBOX = "staging@example.test"
$env:LEDGERBRIDGE_STAGING_ENTITY_REF = "10000000-0000-4000-8000-000000000001"
uv run --frozen --extra dev python scripts/r1_staging_graph_replay.py
```

For the credential-file option, add one unique line with the key
`LEDGERBRIDGE_STAGING_ACCESS_TOKEN=...` to the external credentials file; do
not copy that value into this repository. This mode fetches at most five inbox messages from Microsoft Graph and posts
only bounded projections to `127.0.0.1:8653`. The environment token or
Credential Manager value is read once in-process and is never written or
logged. The default remains no network;
run `...r1_staging_graph_replay.py --check` for a no-network self-check. This
is not production mail ingestion: it has no refresh-token store, persistence,
posting write path, or authenticated entity grant. See
`docs/tasks/2026-08-25-staging-graph-replay.md` for the boundary and cleanup.

The common `MailProvider` boundary also supports the lower-cost Outlook IMAP
staging path. Enable IMAP in Outlook.com **Settings → Mail → Forwarding and
IMAP**, then place a separately generated app password in the external file:

```text
LEDGERBRIDGE_STAGING_IMAP_APP_PASSWORD=<app-password>
```

Run the same wrapper; it now defaults to IMAP OAuth2 staging:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/r1_staging_run.ps1
```

To avoid editing the credentials file manually, use the hidden local prompt:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/r1_set_staging_credential.ps1
```

Paste the app password only into that prompt; it is not echoed and is never a
command-line argument.

For IMAP OAuth2, store
`LEDGERBRIDGE_STAGING_IMAP_ACCESS_TOKEN=...` and run
`scripts/r1_staging_run.ps1 -ImapAuth xoauth2`. The token must carry
`https://outlook.office.com/IMAP.AccessAsUser.All`, not Graph `Mail.Read`.
Outlook.com rejects basic authentication for this mailbox (`Basic
authentication is disabled`), so app passwords are not usable here. The
adapter retains password mode only for other compatible providers. See
[Outlook IMAP settings](https://support.microsoft.com/en-US/Outlook/pop-imap-and-smtp-settings-for-outlook-com)
and [IMAP OAuth](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth).

To obtain that token without a paid Azure subscription, use the staging-only
helper (it follows Thunderbird's public OAuth client configuration):

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/r1_imap_oauth_login.ps1
```

Open the printed authorization URL, sign in, and paste the resulting callback
URL into the hidden prompt. The helper validates PKCE/state and stores only the
access token outside the repository. Its public-client ID is documented by
[Mozilla's Microsoft OAuth guide](https://support.mozilla.org/en-US/kb/microsoft-oauth-authentication-and-thunderbird-202);
this convenience is staging-only and must be replaced by a separately audited
application registration before production.

If you want zero application registration and no token handling in this
project, use the free Thunderbird client: add `redeatt@outlook.com` with its
built-in OAuth2 login, allow it to synchronize the Inbox locally, then point
the demo at Thunderbird's `INBOX` mbox file:

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/r1_staging_run.ps1 `
  -Transport mbox -MboxPath 'C:\Users\<you>\AppData\Roaming\Thunderbird\Profiles\<profile>\ImapMail\outlook.office365.com\INBOX'
```

The mbox adapter is read-only and never sees Thunderbird's OAuth token. This
is the recommended no-subscription fallback when direct IMAP OAuth credentials
are unavailable.

For the shortest operator flow, after placing that one line in the external
credentials file, run from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/r1_staging_run.ps1
```

The wrapper supplies the approved mailbox (`redeatt@outlook.com`), synthetic
entity reference, loopback gateway, and explicit staging-only network gate. It
does not print or persist the token. Override `-CredentialFile`, `-Mailbox`,
`-EntityRef`, or `-GatewayUrl` only for another approved staging fixture.

If no token is available yet, the lowest-friction free path is Graph Explorer;
it uses Microsoft's existing developer app, so no Azure subscription or new
application registration is needed. Open
<https://developer.microsoft.com/en-us/graph/graph-explorer>, sign in as
`redeatt@outlook.com`, choose **Modify permissions**, consent to delegated
`Mail.Read`, then open the **Access token** tab and copy the short-lived token
only into the external credentials file. Graph Explorer documents both the
permission-consent flow and the Access token tab.

Then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/r1_staging_run.ps1
```

The Graph Explorer token normally expires; repeat only the Access token step
when it expires. Do not use `Mail.Send`, `Mail.ReadWrite`, or any application
permission. The optional `r1_graph_device_login.ps1` helper remains available
for a later, separately approved public-client registration; it is not needed
for the free staging path. See Microsoft's [Graph Explorer access-token
guide](https://learn.microsoft.com/en-us/graph/graph-explorer/graph-explorer-features)
and [permission reference](https://learn.microsoft.com/en-us/graph/permissions-reference).

The first Core write seam is available as an explicit, un-wired adapter:
`ledgerbridge.candidate_persistence.persist_initial_candidate` accepts an
already validated Candidate aggregate and an injected SQLAlchemy session, then
appends the revision-1 candidate/evidence links and `candidate.create` audit
binding in the caller's transaction. It never accepts raw bytes or creates a
posting. See `docs/tasks/2026-08-25-candidate-persistence-adapter.md`; runtime
database grants remain closed until the write API and workload gate are
reviewed.

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
