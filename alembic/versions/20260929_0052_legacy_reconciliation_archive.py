"""Archive historical reconciliation workbook values without reinterpreting them.

Revision ID: 20260929_0052
Revises: 20260928_0051
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260929_0052"
down_revision: str | None = "20260928_0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_UPGRADE_SQL = r"""
CREATE SCHEMA reconciliation_legacy AUTHORIZATION ledgerbridge_owner;
REVOKE ALL ON SCHEMA reconciliation_legacy FROM PUBLIC;
GRANT USAGE ON SCHEMA reconciliation_legacy TO ledgerbridge_api, ledgerbridge_worker;

CREATE TABLE reconciliation_legacy.source (
    source_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_ref uuid NOT NULL REFERENCES public.entity(id) ON DELETE RESTRICT,
    scope_pairs jsonb NOT NULL
        CHECK (jsonb_typeof(scope_pairs) = 'array'
               AND jsonb_array_length(scope_pairs) BETWEEN 1 AND 32),
    source_sha256 bytea NOT NULL UNIQUE CHECK (octet_length(source_sha256) = 32),
    source_bytes bytea NOT NULL CHECK (octet_length(source_bytes) BETWEEN 1 AND 10485760),
    original_filename text NOT NULL CHECK (length(original_filename) BETWEEN 1 AND 255),
    sheet_count integer NOT NULL CHECK (sheet_count BETWEEN 1 AND 120),
    imported_by text NOT NULL CHECK (btrim(imported_by) <> ''),
    imported_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE reconciliation_legacy.sheet (
    sheet_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_ref uuid NOT NULL REFERENCES reconciliation_legacy.source(source_ref)
        ON DELETE RESTRICT,
    period text NOT NULL CHECK (period ~ '^20[0-9]{2}-(0[1-9]|1[0-2])$'),
    sheet_name text NOT NULL CHECK (btrim(sheet_name) <> ''),
    row_count integer NOT NULL CHECK (row_count BETWEEN 1 AND 10000),
    cell_count integer NOT NULL CHECK (cell_count BETWEEN 1 AND 100000),
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    cells jsonb NOT NULL CHECK (jsonb_typeof(cells) = 'array'),
    UNIQUE (source_ref, period)
);

CREATE FUNCTION reconciliation_legacy.reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'legacy reconciliation archive is append-only';
END;
$$;
CREATE TRIGGER legacy_source_immutable BEFORE UPDATE OR DELETE
ON reconciliation_legacy.source FOR EACH ROW
EXECUTE FUNCTION reconciliation_legacy.reject_mutation();
CREATE TRIGGER legacy_sheet_immutable BEFORE UPDATE OR DELETE
ON reconciliation_legacy.sheet FOR EACH ROW
EXECUTE FUNCTION reconciliation_legacy.reject_mutation();

CREATE VIEW reconciliation_legacy.month_read AS
SELECT source.source_ref, source.entity_ref,
       source.scope_pairs,
       encode(source.source_sha256, 'hex') AS source_sha256,
       source.imported_at, source.sheet_count,
       sheet.period, sheet.sheet_name, sheet.row_count, sheet.cell_count,
       encode(sheet.content_sha256, 'hex') AS content_sha256, sheet.cells
  FROM reconciliation_legacy.source AS source
  JOIN reconciliation_legacy.sheet AS sheet ON sheet.source_ref = source.source_ref;

REVOKE ALL ON ALL TABLES IN SCHEMA reconciliation_legacy FROM PUBLIC,
    ledgerbridge_reader, ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
GRANT INSERT, SELECT ON reconciliation_legacy.source,
    reconciliation_legacy.sheet TO ledgerbridge_worker;
GRANT SELECT ON reconciliation_legacy.month_read TO ledgerbridge_api;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "Historical reconciliation archive is forward-only; restore a verified backup"
    )
