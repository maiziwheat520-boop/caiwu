"""Persist versioned payroll workbench facts and remittance export receipts.

Revision ID: 20260928_0051
Revises: 20260906_0050
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260928_0051"
down_revision: str | None = "20260906_0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


_UPGRADE_SQL = r"""
CREATE SCHEMA payroll AUTHORIZATION ledgerbridge_owner;
REVOKE ALL ON SCHEMA payroll FROM PUBLIC;
GRANT USAGE ON SCHEMA payroll TO ledgerbridge_api, ledgerbridge_worker;

CREATE TABLE payroll.employee (
    employee_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_ref uuid NOT NULL REFERENCES public.entity(id) ON DELETE RESTRICT,
    employee_code text NOT NULL CHECK (employee_code ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    display_name text NOT NULL CHECK (btrim(display_name) <> ''),
    employee_type text NOT NULL CHECK (employee_type IN ('REGULAR','TEMPORARY','PAYEE_ONLY')),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (entity_ref, employee_code),
    UNIQUE (employee_ref, entity_ref)
);

CREATE TABLE payroll.payee_account (
    payee_account_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_ref uuid NOT NULL REFERENCES public.entity(id) ON DELETE RESTRICT,
    employee_ref uuid NOT NULL,
    payee_name text NOT NULL CHECK (btrim(payee_name) <> ''),
    account_number text NOT NULL CHECK (account_number ~ '^[0-9A-Za-z@._+-]{4,64}$'),
    account_suffix text GENERATED ALWAYS AS (right(account_number, 4)) STORED,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (employee_ref, entity_ref)
        REFERENCES payroll.employee(employee_ref, entity_ref) ON DELETE RESTRICT,
    UNIQUE (entity_ref, employee_ref, account_number),
    UNIQUE (payee_account_ref, entity_ref)
);

CREATE TABLE payroll.batch (
    batch_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_ref uuid NOT NULL REFERENCES public.entity(id) ON DELETE RESTRICT,
    company_id text NOT NULL CHECK (company_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    pay_period text NOT NULL CHECK (pay_period ~ '^20[0-9]{2}-(0[1-9]|1[0-2])$'),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by text NOT NULL CHECK (btrim(created_by) <> ''),
    UNIQUE (entity_ref, pay_period),
    UNIQUE (batch_ref, entity_ref)
);

CREATE TABLE payroll.batch_version (
    batch_version_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_ref uuid NOT NULL REFERENCES payroll.batch(batch_ref) ON DELETE RESTRICT,
    revision integer NOT NULL CHECK (revision > 0),
    status text NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','LOCKED','SUPERSEDED')),
    rules_version text NOT NULL CHECK (btrim(rules_version) <> ''),
    reconciliation_month text NOT NULL
        CHECK (reconciliation_month ~ '^20[0-9]{2}-(0[1-9]|1[0-2])$'),
    source_artifact_ref uuid REFERENCES public.raw_artifact(id) ON DELETE RESTRICT,
    content_sha256 bytea CHECK (content_sha256 IS NULL OR octet_length(content_sha256) = 32),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by text NOT NULL CHECK (btrim(created_by) <> ''),
    locked_at timestamptz,
    locked_by text,
    CHECK (
        (status = 'DRAFT' AND content_sha256 IS NULL AND locked_at IS NULL AND locked_by IS NULL)
        OR
        (status IN ('LOCKED','SUPERSEDED') AND content_sha256 IS NOT NULL
         AND locked_at IS NOT NULL AND btrim(coalesce(locked_by, '')) <> '')
    ),
    UNIQUE (batch_ref, revision),
    UNIQUE (batch_version_ref, batch_ref)
);

CREATE UNIQUE INDEX payroll_one_locked_version_per_batch
    ON payroll.batch_version(batch_ref) WHERE status = 'LOCKED';

CREATE TABLE payroll.fixed_remittance_snapshot (
    fixed_remittance_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_version_ref uuid NOT NULL
        REFERENCES payroll.batch_version(batch_version_ref) ON DELETE RESTRICT,
    payee_name text NOT NULL CHECK (btrim(payee_name) <> ''),
    account_number text NOT NULL CHECK (account_number ~ '^[0-9A-Za-z@._+-]{4,64}$'),
    account_suffix text GENERATED ALWAYS AS (right(account_number, 4)) STORED,
    amount_minor bigint NOT NULL CHECK (amount_minor > 0),
    memo text NOT NULL CHECK (btrim(memo) <> '' AND char_length(memo) <= 40),
    UNIQUE (batch_version_ref, payee_name, account_number)
);

CREATE TABLE payroll.line (
    line_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_version_ref uuid NOT NULL
        REFERENCES payroll.batch_version(batch_version_ref) ON DELETE RESTRICT,
    employee_ref uuid NOT NULL REFERENCES payroll.employee(employee_ref) ON DELETE RESTRICT,
    employee_name text NOT NULL CHECK (btrim(employee_name) <> ''),
    employee_type text NOT NULL CHECK (employee_type IN ('REGULAR','TEMPORARY')),
    location text NOT NULL CHECK (
        location IN ('星汇','薇旭','雅阁','逸豪','同富','青居客','一品','粥店')
    ),
    job_group text,
    attendance_days text,
    payment_channel text NOT NULL CHECK (payment_channel IN ('MYBANK','BOC','WECHAT','CASH')),
    payee_account_ref uuid REFERENCES payroll.payee_account(payee_account_ref) ON DELETE RESTRICT,
    payee_name text,
    account_number text,
    memo text NOT NULL DEFAULT '' CHECK (char_length(memo) <= 40),
    net_amount_minor bigint NOT NULL CHECK (net_amount_minor > 0),
    cash_amount_minor bigint NOT NULL DEFAULT 0
        CHECK (cash_amount_minor >= 0 AND cash_amount_minor <= net_amount_minor),
    supplemental_amount_minor bigint NOT NULL DEFAULT 0
        CHECK (supplemental_amount_minor >= 0
               AND supplemental_amount_minor <= net_amount_minor - cash_amount_minor),
    bank_amount_minor bigint GENERATED ALWAYS AS (net_amount_minor - cash_amount_minor) STORED,
    CHECK (
        (payment_channel = 'CASH' AND cash_amount_minor = net_amount_minor
         AND payee_account_ref IS NULL)
        OR
        (payment_channel IN ('MYBANK','BOC','WECHAT') AND cash_amount_minor < net_amount_minor
         AND payee_account_ref IS NOT NULL AND btrim(coalesce(payee_name, '')) <> ''
         AND account_number ~ '^[0-9A-Za-z@._+-]{4,64}$')
    ),
    CHECK (
        employee_type <> 'REGULAR'
        OR (btrim(coalesce(job_group, '')) <> '' AND btrim(coalesce(attendance_days, '')) <> '')
    ),
    UNIQUE (batch_version_ref, employee_ref),
    UNIQUE (line_ref, batch_version_ref)
);

CREATE TABLE payroll.component (
    component_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    line_ref uuid NOT NULL REFERENCES payroll.line(line_ref) ON DELETE RESTRICT,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    component_code text NOT NULL CHECK (component_code ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    amount_minor bigint NOT NULL,
    note text,
    UNIQUE (line_ref, component_code),
    UNIQUE (line_ref, ordinal)
);

CREATE TABLE payroll.blocking_issue (
    issue_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_version_ref uuid NOT NULL
        REFERENCES payroll.batch_version(batch_version_ref) ON DELETE RESTRICT,
    line_ref uuid REFERENCES payroll.line(line_ref) ON DELETE RESTRICT,
    issue_code text NOT NULL CHECK (issue_code ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    message text NOT NULL CHECK (btrim(message) <> ''),
    resolved_at timestamptz,
    resolved_by text,
    CHECK ((resolved_at IS NULL) = (resolved_by IS NULL)),
    UNIQUE (batch_version_ref, line_ref, issue_code)
);

CREATE TABLE payroll.export_receipt (
    export_receipt_ref uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_version_ref uuid NOT NULL
        REFERENCES payroll.batch_version(batch_version_ref) ON DELETE RESTRICT,
    export_group text NOT NULL CHECK (
        export_group IN ('星汇','薇旭','雅阁','逸豪景怡','青居客一品餐饮','其他','补发')
    ),
    filename text NOT NULL CHECK (btrim(filename) <> ''),
    content_sha256 bytea NOT NULL CHECK (octet_length(content_sha256) = 32),
    row_count integer NOT NULL CHECK (row_count >= 0),
    total_amount_minor bigint NOT NULL CHECK (total_amount_minor >= 0),
    exported_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    exported_by text NOT NULL CHECK (btrim(exported_by) <> ''),
    UNIQUE (batch_version_ref, export_group, content_sha256)
);

CREATE TABLE payroll.command_receipt (
    operation_ref uuid PRIMARY KEY,
    entity_ref uuid NOT NULL REFERENCES public.entity(id) ON DELETE RESTRICT,
    command_kind text NOT NULL CHECK (command_kind IN ('SAVE_DRAFT','LOCK','EXPORT')),
    request_sha256 bytea NOT NULL CHECK (octet_length(request_sha256) = 32),
    result_ref uuid NOT NULL,
    actor_ref text NOT NULL CHECK (btrim(actor_ref) <> ''),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE VIEW payroll.workbench_line
WITH (security_barrier = true) AS
SELECT line.line_ref, line.batch_version_ref, line.employee_ref,
       line.employee_name, line.employee_type, line.location, line.job_group,
       line.attendance_days, line.payment_channel, line.memo,
       line.net_amount_minor, line.cash_amount_minor, line.supplemental_amount_minor,
       line.bank_amount_minor,
       line.payee_name,
       CASE WHEN line.account_number IS NULL THEN NULL
            ELSE '****' || right(line.account_number, 4) END AS account_masked
  FROM payroll.line AS line;

CREATE VIEW payroll.fixed_remittance_masked
WITH (security_barrier = true) AS
SELECT fixed_remittance_ref, batch_version_ref, payee_name,
       '****' || account_suffix AS account_masked, amount_minor, memo
  FROM payroll.fixed_remittance_snapshot;

CREATE FUNCTION payroll.reject_append_only_change()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation';
END
$function$;

CREATE FUNCTION payroll.guard_version_mutation()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
DECLARE
    v_old_version uuid;
    v_new_version uuid;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        IF TG_TABLE_NAME = 'component' THEN
            SELECT line.batch_version_ref INTO v_old_version
              FROM payroll.line AS line WHERE line.line_ref = OLD.line_ref;
        ELSE
            v_old_version := OLD.batch_version_ref;
        END IF;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        IF TG_TABLE_NAME = 'component' THEN
            SELECT line.batch_version_ref INTO v_new_version
              FROM payroll.line AS line WHERE line.line_ref = NEW.line_ref;
        ELSE
            v_new_version := NEW.batch_version_ref;
        END IF;
    END IF;
    IF EXISTS (
        SELECT 1 FROM payroll.batch_version AS version
         WHERE version.batch_version_ref IN (v_old_version, v_new_version)
           AND version.status <> 'DRAFT'
    ) THEN
        RAISE EXCEPTION 'locked payroll version is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION payroll.guard_version_state()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'payroll version is not deletable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'LOCKED' AND NEW.status = 'SUPERSEDED' THEN
        IF NEW.batch_ref <> OLD.batch_ref OR NEW.revision <> OLD.revision
           OR NEW.rules_version <> OLD.rules_version
           OR NEW.reconciliation_month <> OLD.reconciliation_month
           OR NEW.content_sha256 <> OLD.content_sha256
           OR NEW.source_artifact_ref IS DISTINCT FROM OLD.source_artifact_ref
           OR NEW.created_at <> OLD.created_at OR NEW.created_by <> OLD.created_by
           OR NEW.locked_at <> OLD.locked_at OR NEW.locked_by <> OLD.locked_by THEN
            RAISE EXCEPTION 'locked payroll version content is immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status <> 'DRAFT' THEN
        RAISE EXCEPTION 'terminal payroll version is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.batch_ref <> OLD.batch_ref OR NEW.revision <> OLD.revision
       OR NEW.rules_version <> OLD.rules_version
       OR NEW.reconciliation_month <> OLD.reconciliation_month
       OR NEW.source_artifact_ref IS DISTINCT FROM OLD.source_artifact_ref
       OR NEW.created_at <> OLD.created_at OR NEW.created_by <> OLD.created_by THEN
        RAISE EXCEPTION 'payroll version identity is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status <> 'LOCKED' THEN
        RAISE EXCEPTION 'invalid payroll version transition'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF EXISTS (
        SELECT 1 FROM payroll.blocking_issue AS issue
         WHERE issue.batch_version_ref = OLD.batch_version_ref AND issue.resolved_at IS NULL
    ) THEN
        RAISE EXCEPTION 'payroll version has unresolved blocking issues'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM payroll.line AS line
         WHERE line.batch_version_ref = OLD.batch_version_ref
    ) THEN
        RAISE EXCEPTION 'payroll version has no lines'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF EXISTS (
        SELECT 1 FROM payroll.line AS line
        LEFT JOIN LATERAL (
            SELECT coalesce(sum(component.amount_minor), 0) AS total
              FROM payroll.component AS component
             WHERE component.line_ref = line.line_ref
        ) AS components ON true
         WHERE line.batch_version_ref = OLD.batch_version_ref
           AND components.total <> line.net_amount_minor
    ) THEN
        RAISE EXCEPTION 'payroll components do not equal net pay'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION payroll.validate_line_scope()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM payroll.batch_version AS version
          JOIN payroll.batch AS batch ON batch.batch_ref = version.batch_ref
          JOIN payroll.employee AS employee
            ON employee.employee_ref = NEW.employee_ref
           AND employee.entity_ref = batch.entity_ref
          LEFT JOIN payroll.payee_account AS account
            ON account.payee_account_ref = NEW.payee_account_ref
           AND account.entity_ref = batch.entity_ref
           AND account.employee_ref = NEW.employee_ref
           AND account.payee_name = NEW.payee_name
           AND account.account_number = NEW.account_number
           AND account.active
         WHERE version.batch_version_ref = NEW.batch_version_ref
           AND (NEW.payee_account_ref IS NULL OR account.payee_account_ref IS NOT NULL)
    ) THEN
        RAISE EXCEPTION 'payroll line crosses employee, account, or entity scope'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION payroll.require_locked_export()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM payroll.batch_version AS version
         WHERE version.batch_version_ref = NEW.batch_version_ref
           AND version.status = 'LOCKED'
           AND version.content_sha256 IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'remittance export requires a locked payroll version'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END
$function$;

CREATE TRIGGER payroll_version_state_guard BEFORE UPDATE OR DELETE
ON payroll.batch_version FOR EACH ROW EXECUTE FUNCTION payroll.guard_version_state();
CREATE TRIGGER payroll_batch_append_only BEFORE UPDATE OR DELETE
ON payroll.batch FOR EACH ROW EXECUTE FUNCTION payroll.reject_append_only_change();
CREATE TRIGGER payroll_payee_account_append_only BEFORE UPDATE OR DELETE
ON payroll.payee_account FOR EACH ROW EXECUTE FUNCTION payroll.reject_append_only_change();
CREATE TRIGGER payroll_line_version_guard BEFORE INSERT OR UPDATE OR DELETE
ON payroll.line FOR EACH ROW EXECUTE FUNCTION payroll.guard_version_mutation();
CREATE TRIGGER payroll_line_scope_guard BEFORE INSERT OR UPDATE
ON payroll.line FOR EACH ROW EXECUTE FUNCTION payroll.validate_line_scope();
CREATE TRIGGER payroll_component_version_guard BEFORE INSERT OR UPDATE OR DELETE
ON payroll.component FOR EACH ROW EXECUTE FUNCTION payroll.guard_version_mutation();
CREATE TRIGGER payroll_fixed_version_guard BEFORE INSERT OR UPDATE OR DELETE
ON payroll.fixed_remittance_snapshot FOR EACH ROW EXECUTE FUNCTION payroll.guard_version_mutation();
CREATE TRIGGER payroll_issue_version_guard BEFORE INSERT OR UPDATE OR DELETE
ON payroll.blocking_issue FOR EACH ROW EXECUTE FUNCTION payroll.guard_version_mutation();
CREATE TRIGGER payroll_export_receipt_append_only BEFORE UPDATE OR DELETE
ON payroll.export_receipt FOR EACH ROW EXECUTE FUNCTION payroll.reject_append_only_change();
CREATE TRIGGER payroll_export_receipt_locked BEFORE INSERT
ON payroll.export_receipt FOR EACH ROW EXECUTE FUNCTION payroll.require_locked_export();
CREATE TRIGGER payroll_command_receipt_append_only BEFORE UPDATE OR DELETE
ON payroll.command_receipt FOR EACH ROW EXECUTE FUNCTION payroll.reject_append_only_change();

REVOKE ALL ON ALL TABLES IN SCHEMA payroll FROM PUBLIC, ledgerbridge_reader,
    ledgerbridge_api, ledgerbridge_worker, ledgerbridge_app;
GRANT SELECT ON payroll.employee, payroll.batch, payroll.batch_version, payroll.line,
    payroll.component, payroll.blocking_issue, payroll.export_receipt
    TO ledgerbridge_worker;
GRANT SELECT ON payroll.batch, payroll.batch_version, payroll.component,
    payroll.blocking_issue, payroll.export_receipt TO ledgerbridge_api;
GRANT SELECT ON payroll.workbench_line TO ledgerbridge_api, ledgerbridge_worker;
GRANT SELECT ON payroll.fixed_remittance_masked TO ledgerbridge_api, ledgerbridge_worker;
GRANT INSERT, UPDATE ON payroll.employee, payroll.batch_version,
    payroll.line, payroll.component, payroll.blocking_issue,
    payroll.fixed_remittance_snapshot
    TO ledgerbridge_worker;
GRANT INSERT ON payroll.payee_account, payroll.batch TO ledgerbridge_worker;
GRANT SELECT ON payroll.payee_account, payroll.fixed_remittance_snapshot
    TO ledgerbridge_worker;
GRANT INSERT ON payroll.export_receipt, payroll.command_receipt TO ledgerbridge_worker;
GRANT SELECT ON payroll.command_receipt TO ledgerbridge_worker;
"""


def downgrade() -> None:
    raise RuntimeError("Payroll history is forward-only; restore a verified backup instead")
