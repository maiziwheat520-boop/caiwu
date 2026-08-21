#!/bin/sh
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${LEDGERBRIDGE_APP_DB_PASSWORD:?LEDGERBRIDGE_APP_DB_PASSWORD is required}"

psql --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set=ON_ERROR_STOP=1 \
    --set=app_password="$LEDGERBRIDGE_APP_DB_PASSWORD" <<'SQL'
SELECT format(
    'CREATE ROLE ledgerbridge_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledgerbridge_app') \gexec

ALTER ROLE ledgerbridge_app
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    PASSWORD :'app_password';
SQL
