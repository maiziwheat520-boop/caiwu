# Hermes deployment

Verified target: `aiadmin@192.168.1.39` (`ai-hub`)
Host path: `/srv/ai-center/ledgerbridge`
API binding: `127.0.0.1:8650` only

## Boundaries

- The private GitHub repository is the source of reviewed code and migrations.
- The Hermes directory is a rendered deployment tree, not a Git checkout or a
  second source of truth.
- `DEPLOYED_REVISION` contains the full reviewed Git SHA. `MANIFEST.sha256`
  binds that revision to every non-secret file in the rendered tree. The manifest
  is an unsigned drift detector, not an authenticity signature; Git review and the
  image revision label remain the trust anchors.
- PostgreSQL and artifacts use dedicated Docker named volumes.
- The production `.env` is mode 600 on Hermes and is excluded from the manifest,
  never copied back, and never committed.
- No public/LAN port is opened. Hermes integrations call the local API endpoint.

## Database identities

Use distinct generated passwords in the protected `.env`. For new volumes:

- `POSTGRES_USER=ledgerbridge_owner` and `POSTGRES_PASSWORD` bootstrap the cluster
  and own migrations. The API and worker never receive these credentials.
- `ledgerbridge_app` is an unprivileged LOGIN role. Its password is supplied as
  `LEDGERBRIDGE_APP_DB_PASSWORD`, and its URL is `LEDGERBRIDGE_DATABASE_URL`.
- `LEDGERBRIDGE_MIGRATION_DATABASE_URL` is provided only to the one-shot `migrate`
  service. It must use the volume's existing owner role.

The production volume created before Phase 1 uses `ledgerbridge` as its Phase 0
bootstrap owner. Keep that existing role as the migration owner during an in-place
upgrade, while API and worker switch to `ledgerbridge_app`. Do not rename the
cluster role or transfer object ownership solely to match the new-volume example.

On a new database volume, `docker/postgres-init-runtime-role.sh` creates the
runtime role. For an existing pre-Phase-1 volume, run the same idempotent bootstrap
before the Phase 1 migration:

```bash
set -euo pipefail
cd /srv/ai-center/ledgerbridge
docker compose exec -T postgres sh \
  /docker-entrypoint-initdb.d/10-ledgerbridge-runtime-role.sh
```

The Phase 1 migration fails closed if `ledgerbridge_app` is missing, cannot log
in, or has superuser/database/role/replication/RLS-bypass privileges. PostgreSQL
roles are cluster objects and are not dropped by Alembic downgrade. Include a
protected `pg_dumpall --roles-only` artifact in backup/restore work and re-check
role attributes plus grants during the F-4 restore rehearsal.

## Build a deployment bundle

Run these commands only from a clean checkout of the reviewed merge commit:

```bash
set -euo pipefail
test -z "$(git status --porcelain)"
revision="$(git rev-parse HEAD)"
python scripts/deployment_manifest.py create \
  --root . \
  --output MANIFEST.sha256 \
  --revision "$revision"
printf '%s\n' "$revision" > DEPLOYED_REVISION
```

Transfer the rendered files, `MANIFEST.sha256`, and `DEPLOYED_REVISION` to
Hermes. Do not transfer `.git`, a development virtual environment, tests,
financial fixtures, or a local `.env`.

## Initial deploy

Before building, set `LEDGERBRIDGE_REVISION` to the short Git SHA and
`DEPLOYED_REVISION` to the full Git SHA in the protected Hermes `.env`. Then:

```bash
set -euo pipefail
cd /srv/ai-center/ledgerbridge
revision="$(cat DEPLOYED_REVISION)"
python scripts/deployment_manifest.py verify \
  --root . \
  --manifest MANIFEST.sha256 \
  --expected-revision "$revision"
docker compose config --quiet
docker compose build api worker migrate
image_revision="$(docker image inspect "ledgerbridge-app:${LEDGERBRIDGE_REVISION}" \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
test "$image_revision" = "$revision"
docker compose exec -T postgres sh \
  /docker-entrypoint-initdb.d/10-ledgerbridge-runtime-role.sh
docker compose --profile tools run --rm migrate
docker compose up -d postgres api worker
curl --fail http://127.0.0.1:8650/health/live
curl --fail http://127.0.0.1:8650/health/ready
test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  http://127.0.0.1:8650/openapi.json)" = "404"
```

The `mail-collector` profile remains disabled until Phase 3 OAuth and secret-store
work is complete.

## Encrypted backup and isolated restore rehearsal

F-4 uses a dedicated, non-expiring GPG encryption key in
`/srv/ai-center/ledgerbridge-secrets/backup-gnupg`. The keyring and backup root
must both be owned by the deployment operator and mode 700. The automation key
has no interactive passphrase so unattended local restore is possible; the
security boundary is the protected Hermes account and keyring. Keep the approved
private-key export only in the external credentials directory, never in this
repository or the rendered deployment tree.

Before either command, verify the full secret-key fingerprint and keep it in a
shell variable. The fingerprint is public metadata; the key material is not.

```bash
set -euo pipefail
project=/srv/ai-center/ledgerbridge
backup_root=/srv/ai-center/backups/ledgerbridge
gpg_home=/srv/ai-center/ledgerbridge-secrets/backup-gnupg
test "$(stat -c '%a' "$backup_root")" = 700
test "$(stat -c '%a' "$gpg_home")" = 700
fingerprint="$(gpg --homedir "$gpg_home" --batch --with-colons \
  --list-secret-keys | awk -F: '$1 == "fpr" {print $10; exit}')"
test -n "$fingerprint"
```

Create a backup:

```bash
python "$project/scripts/backup_restore.py" \
  --project-dir "$project" \
  --backup-root "$backup_root" \
  --work-root /dev/shm \
  --gpg-home "$gpg_home" \
  --fingerprint "$fingerprint" \
  backup
```

The backup command verifies manifest/image/database state, stops only the
production API and worker by their exact container IDs, captures cluster roles,
a custom-format database dump with owners/ACLs, artifacts, and the rendered
deployment tree, encrypts and decrypts the payload for a round-trip check, then
starts the same containers and waits for health. A failure before publication
removes only its guarded `.partial-*` directory and still attempts to restart
the stopped services.

Rehearse the returned backup directory:

```bash
backup=/srv/ai-center/backups/ledgerbridge/<returned-backup-directory>
python "$project/scripts/backup_restore.py" \
  --project-dir "$project" \
  --backup-root "$backup_root" \
  --work-root /dev/shm \
  --gpg-home "$gpg_home" \
  --fingerprint "$fingerprint" \
  rehearse --backup "$backup"
```

The rehearsal decrypts into a private `/dev/shm` directory, validates every
component hash, restores roles before the database into a new PostgreSQL 15
checksummed volume on an internal-only network, verifies the migration head,
owners, row counts, functions, triggers, runtime grants, audit/schema
restrictions, direct `ledgerbridge_app` identity, artifact digest, and
deployment manifest. It never points `pg_restore` or artifact extraction at a
production resource. Passing evidence is written as a non-secret
`restore-rehearsal-*.json` file beside the ciphertext after all disposable
resources are confirmed absent and production state is unchanged.

## Upgrade sequence

1. Back up PostgreSQL, cluster roles, and the current deployment directory.
2. Render the reviewed code revision, manifest, and `DEPLOYED_REVISION`.
3. Verify the manifest before rendering Compose or building images.
4. Render Compose configuration and build revision-labeled images.
5. Verify the image revision label equals the full reviewed SHA.
6. Bootstrap/validate the runtime role, then run `docker compose --profile tools
   run --rm migrate`; never run Alembic through the API or worker service.
7. Recreate API/worker and verify live/ready, OpenAPI 404, worker heartbeat,
   migration head, runtime-role attributes/grants, and the loopback-only port.
8. Keep the previous image/revision until post-deploy checks pass.

Rollback is revision-specific. Never run an Alembic downgrade against production
financial data unless the migration's review explicitly proves it safe.
