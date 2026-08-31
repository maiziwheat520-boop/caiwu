from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError

from alembic import command

# ruff: noqa: E501

MIGRATION = Path("alembic/versions/20260824_0012_r1_candidate_evidence.py")
MIGRATION_B = Path("alembic/versions/20260824_0013_r1_ledger_reconciliation.py")
MIGRATION_HARDENING = Path("alembic/versions/20260824_0014_r1_fact_hardening.py")
MIGRATION_C = Path("alembic/versions/20260824_0015_r1_internal_read_surface.py")

INTERNAL_READ_VIEWS = (
    "candidate_current_v",
    "candidate_evidence_v",
    "evidence_metadata_v",
    "reconciliation_current_v",
    "reconciliation_blocker_v",
    "reconciliation_proposal_v",
    "reconciliation_suspense_v",
    "ledger_posted_total_v",
)
INTERNAL_READ_FUNCTIONS = (
    "current_audit_horizon",
    "get_accounting_dimensions",
    "list_candidates_as_of",
    "get_reconciliation_as_of",
    "resolve_active_evidence_blob",
    "get_ledger_summary_as_of",
    "append_internal_evidence_read_audit",
)
INTERNAL_READ_FUNCTION_IDENTITIES = {
    "current_audit_horizon": "",
    "get_accounting_dimensions": (
        "p_entity_id uuid, p_business_unit_ids uuid[], p_business_unit_refs character varying[]"
    ),
    "list_candidates_as_of": (
        "p_entity_id uuid, p_business_unit_id uuid, p_status character varying, "
        "p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea, "
        "p_last_created_at timestamp with time zone, p_last_candidate_id uuid, p_limit integer"
    ),
    "get_reconciliation_as_of": (
        "p_entity_id uuid, p_business_unit_id uuid, p_accounting_month date, "
        "p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea"
    ),
    "resolve_active_evidence_blob": "p_evidence_ref uuid",
    "get_ledger_summary_as_of": (
        "p_entity_id uuid, p_business_unit_id uuid, p_from_month date, p_to_month date, "
        "p_audit_horizon_sequence bigint, p_audit_horizon_hash bytea"
    ),
    "append_internal_evidence_read_audit": (
        "p_operation_id uuid, p_principal_ref character varying, p_verified_san character varying, "
        "p_policy_generation character varying, p_evidence_ref uuid, p_entity_id uuid, "
        "p_business_unit_id uuid, p_blob_ref uuid, p_byte_size bigint, p_plaintext_sha256 bytea"
    ),
}
RECEIPT_FUNCTION = "append_internal_evidence_read_audit"
RECEIPT_CALL_SQL = """
SELECT internal_read.append_internal_evidence_read_audit(
    CAST(:operation_id AS uuid),
    CAST(:principal_ref AS varchar),
    CAST(:principal_san_uri AS varchar),
    CAST(:policy_generation AS varchar),
    CAST(:evidence_ref AS uuid),
    CAST(:entity_ref AS uuid),
    CAST(:business_unit_id AS uuid),
    CAST(:blob_ref AS uuid),
    CAST(:byte_size AS bigint),
    CAST(:plaintext_sha256 AS bytea)
)
"""
PENDING_CORRECTION_CALL_SQL = """
SELECT internal_command.apply_candidate_decision(
    CAST(:operation_id AS uuid), CAST(:assertion_jti AS uuid), CAST(:candidate_id AS uuid),
    'human:r1-correction-test'::varchar(200),
    'workload:r1-correction-test'::varchar(200),
    'spiffe://ledgerbridge.test/r1-correction-test'::varchar(200),
    CAST(:authorized_entity_id AS uuid), CAST(:current_business_unit_id AS uuid),
    CAST(:target_business_unit_id AS uuid), 'CORRECT_AND_CONFIRM'::varchar(32),
    :expected_revision, 'R1 pending candidate correction replay'::varchar(1000),
    :set_business_unit, CAST(:business_unit_ref AS varchar(100)),
    :set_category, CAST(:category_code AS varchar(100)),
    :set_amount, CAST(:amount_minor AS bigint),
    :set_month, CAST(:accounting_month AS date),
    NULL::varchar(1000), CAST(:decided_at AS timestamptz)
)
"""
RUNTIME_ROLES = (
    "ledgerbridge_reader",
    "ledgerbridge_api",
    "ledgerbridge_worker",
    "ledgerbridge_app",
)


def _sqlstate(error: BaseException) -> str | None:
    original = getattr(error, "orig", error)
    return cast(str | None, getattr(original, "sqlstate", getattr(original, "pgcode", None)))


def _assert_db_rejection(
    engine: Engine,
    statements: Sequence[tuple[str, dict[str, Any] | None]],
    *,
    sqlstate: str,
    message: str,
) -> None:
    """Assert one rejected write in its own savepoint/transaction.

    PostgreSQL marks a transaction failed after a constraint error.  Every
    negative case therefore gets a fresh outer transaction and savepoint, and
    deferred R1 triggers are forced before the savepoint is rolled back.  This
    prevents a later ``InFailedSqlTransaction`` from masquerading as the
    expected rejection.
    """

    with engine.connect() as connection:
        transaction = connection.begin()
        savepoint = connection.begin_nested()
        try:
            with pytest.raises(SQLAlchemyError) as raised:
                for statement, parameters in statements:
                    connection.execute(text(statement), parameters or {})
                connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            assert _sqlstate(raised.value) == sqlstate
            assert message in str(getattr(raised.value, "orig", raised.value))
        finally:
            if savepoint.is_active:
                savepoint.rollback()
            if transaction.is_active:
                transaction.rollback()


def _migration_owner_url() -> Any:
    value = os.environ.get("LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    if value is None:
        pytest.skip("PostgreSQL integration tests require LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    return make_url(value)


def _test_admin_url() -> Any:
    value = os.environ.get("LEDGERBRIDGE_TEST_ADMIN_DATABASE_URL")
    if value is None:
        pytest.skip(
            "PostgreSQL integration tests require LEDGERBRIDGE_TEST_ADMIN_DATABASE_URL "
            "for isolated database bootstrap"
        )
    return make_url(value)


def _maintenance_engine(admin_url: Any) -> Engine:
    return create_engine(admin_url.set(database="postgres"), isolation_level="AUTOCOMMIT")


def _migration_owner_name(owner_url: Any) -> str:
    username = owner_url.username
    if not isinstance(username, str) or not username:
        pytest.skip("LEDGERBRIDGE_MIGRATION_DATABASE_URL must identify a database owner")
    return username


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _prepare_temporary_schema(admin_url: Any, database_name: str, owner_name: str) -> None:
    engine = create_engine(admin_url.set(database=database_name))
    try:
        with engine.begin() as connection:
            connection.execute(
                text(f"GRANT USAGE, CREATE ON SCHEMA public TO {_quote_identifier(owner_name)}")
            )
            connection.execute(
                text(f"ALTER SCHEMA public OWNER TO {_quote_identifier(owner_name)}")
            )
    finally:
        engine.dispose()


def _database_name(database_url: str) -> str:
    database = make_url(database_url).database
    if not isinstance(database, str) or not database:
        raise RuntimeError("temporary migration database URL has no database name")
    return database


@contextmanager
def _legacy_r1_database(*, reader: bool = True) -> Iterator[str]:
    """Create a disposable 0013 database owned by the restricted migration role.

    ``LEDGERBRIDGE_TEST_ADMIN_DATABASE_URL`` is used only for CREATE/DROP
    DATABASE and external role bootstrap.  Alembic and all fact writes use the
    owner from ``LEDGERBRIDGE_MIGRATION_DATABASE_URL``.
    """

    owner_url = _migration_owner_url()
    admin_url = _test_admin_url()
    owner_name = _migration_owner_name(owner_url)
    database_name = f"ledgerbridge_r1_legacy_{uuid4().hex[:12]}"
    maintenance_engine = _maintenance_engine(admin_url)
    created_roles: list[str] = []
    try:
        with maintenance_engine.connect() as connection:
            existing = set(
                connection.execute(
                    text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:roles)"),
                    {"roles": list(RUNTIME_ROLES)},
                ).scalars()
            )
            required_roles = (
                RUNTIME_ROLES
                if reader
                else tuple(role for role in RUNTIME_ROLES if role != "ledgerbridge_reader")
            )
            for role in required_roles:
                if role in existing:
                    continue
                connection.exec_driver_sql(
                    f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOREPLICATION NOBYPASSRLS"
                )
                created_roles.append(role)
            connection.execute(
                text(f'CREATE DATABASE "{database_name}" OWNER {_quote_identifier(owner_name)}')
            )
            assert (
                connection.execute(
                    text(
                        "SELECT pg_get_userbyid(datdba) FROM pg_database "
                        "WHERE datname = :database_name"
                    ),
                    {"database_name": database_name},
                ).scalar_one()
                == owner_name
            )
        _prepare_temporary_schema(admin_url, database_name, owner_name)
        temporary_url = owner_url.set(database=database_name)
        config = Config("alembic.ini")
        config.attributes["database_url"] = temporary_url.render_as_string(hide_password=False)
        command.upgrade(config, "20260824_0013")
        yield temporary_url.render_as_string(hide_password=False)
    finally:
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
            for role in reversed(created_roles):
                connection.execute(text(f"DROP ROLE IF EXISTS {role}"))
        maintenance_engine.dispose()


def _upgrade_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


def _legacy_candidate_seed(
    connection: Connection,
    *,
    entity_id: UUID | None = None,
    evidence_entity_id: UUID | None = None,
    status: str = "INCOMPLETE",
    source_record_id: UUID | None = None,
    audit_payload: dict[str, object] | None = None,
    include_default_blockers: bool = True,
) -> dict[str, Any]:
    """Insert a structurally valid 0013 Candidate graph for one preflight case."""

    entity_id = entity_id or uuid4()
    evidence_entity_id = evidence_entity_id or entity_id
    unit_id = uuid4()
    evidence_ref = uuid4()
    category_id = uuid4()
    candidate_id = uuid4()
    operation_id = uuid4()
    event_ref = uuid4()
    source_event_ref = uuid4()
    now = datetime.now(UTC)
    connection.execute(
        text(
            "INSERT INTO public.entity (id, entity_type, name) VALUES "
            "(:entity, 'COMPANY', :entity_name), (:evidence_entity, 'COMPANY', :other_name) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "entity": entity_id,
            "evidence_entity": evidence_entity_id,
            "entity_name": "legacy candidate entity",
            "other_name": "legacy evidence entity",
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.business_unit (id, entity_id, ref, label) "
            "VALUES (:unit, :entity, :ref, 'Legacy Unit')"
        ),
        {"unit": unit_id, "entity": evidence_entity_id, "ref": f"u-{unit_id.hex[:8]}"},
    )
    connection.execute(
        text(
            "INSERT INTO public.reporting_category (id, entity_id, code, label) "
            "VALUES (:category, :entity, 'legacy-category', 'Legacy Category')"
        ),
        {"category": category_id, "entity": entity_id},
    )
    evidence_audit = _append_audit_event(
        connection,
        "evidence.object.create",
        {
            "evidence_ref": str(evidence_ref),
            "entity_id": str(evidence_entity_id),
            "business_unit_id": str(unit_id),
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.evidence_object "
            "(evidence_ref, entity_id, business_unit_id, media_type, display_name, "
            "plaintext_sha256, plaintext_size, audit_event_id) VALUES "
            "(:evidence, :entity, :unit, 'application/pdf', 'legacy.pdf', :digest, 1, :audit)"
        ),
        {
            "evidence": evidence_ref,
            "entity": evidence_entity_id,
            "unit": unit_id,
            "digest": hashlib.sha256(b"legacy").digest(),
            "audit": evidence_audit,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.candidate (id, short_id, entity_id, contract_version, created_at) "
            "VALUES (:candidate, :short_id, :entity, 'ledgerbridge.candidate.v1', :now)"
        ),
        {
            "candidate": candidate_id,
            "short_id": "C-" + candidate_id.hex[:8].upper(),
            "entity": entity_id,
            "now": now,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.candidate_source "
            "(candidate_id, ingest_channel_id, source_system_id, source_event_ref, "
            "source_record_id, display_label) VALUES "
            "(:candidate, 'synthetic_upload', 'synthetic', :source_event, :source_record, "
            "'legacy source')"
        ),
        {
            "candidate": candidate_id,
            "source_event": source_event_ref,
            "source_record": source_record_id,
        },
    )
    if status in {"INCOMPLETE", "IGNORED"}:
        connection.execute(
            text(
                "INSERT INTO public.candidate_revision "
                "(candidate_id, revision, status, currency, summary, confidence_basis_points, "
                "created_at, updated_at) VALUES (:candidate, 1, :status, 'CNY', "
                "'legacy candidate', 100, :now, :now)"
            ),
            {"candidate": candidate_id, "status": status, "now": now},
        )
    else:
        connection.execute(
            text(
                "INSERT INTO public.candidate_revision "
                "(candidate_id, revision, status, business_unit_id, "
                "business_unit_ref_snapshot, business_unit_label_snapshot, category_id, "
                "category_code_snapshot, category_label_snapshot, amount_minor, currency, "
                "accounting_month, summary, confidence_basis_points, created_at, updated_at) "
                "VALUES (:candidate, 1, :status, :unit, :unit_ref, 'Legacy Unit', :category, "
                "'legacy-category', 'Legacy Category', 1, 'CNY', DATE '2026-08-01', "
                "'legacy candidate', 100, :now, :now)"
            ),
            {
                "candidate": candidate_id,
                "status": status,
                "unit": unit_id,
                "unit_ref": f"u-{unit_id.hex[:8]}",
                "category": category_id,
                "now": now,
            },
        )
    connection.execute(
        text(
            "INSERT INTO public.candidate_evidence "
            "(candidate_id, ordinal, evidence_ref, kind, media_type_snapshot, "
            "display_name_snapshot, download_available) VALUES "
            "(:candidate, 0, :evidence, 'ATTACHMENT', 'application/pdf', 'legacy.pdf', true)"
        ),
        {"candidate": candidate_id, "evidence": evidence_ref},
    )
    if status == "INCOMPLETE" and include_default_blockers:
        connection.execute(
            text(
                "INSERT INTO public.candidate_blocker "
                "(candidate_id, revision, ordinal, code, message, field) VALUES "
                "(:candidate, 1, :ordinal, :code, :message, :field)"
            ),
            [
                {
                    "candidate": candidate_id,
                    "ordinal": ordinal,
                    "code": code,
                    "message": message,
                    "field": field,
                }
                for ordinal, (code, message, field) in enumerate(
                    (
                        ("MISSING_BUSINESS_UNIT", "missing business unit", "business_unit"),
                        ("MISSING_CATEGORY", "missing category", "category"),
                        ("MISSING_AMOUNT", "missing amount", "amount_minor"),
                        (
                            "MISSING_ACCOUNTING_MONTH",
                            "missing accounting month",
                            "accounting_month",
                        ),
                    )
                )
            ],
        )
    payload: dict[str, object] = {
        "event_ref": str(event_ref),
        "candidate_id": str(candidate_id),
        "candidate_ref": str(candidate_id),
        "operation_id": str(operation_id),
        "command_fingerprint": (operation_id.bytes * 2).hex(),
        "event_type": "CREATE",
        "action": None,
        "from_revision": None,
        "to_revision": 1,
        "from_status": None,
        "to_status": status,
        "field_changes": [],
        "conflict_resolutions": [],
        "actor_ref": "legacy-test",
        "reason": "legacy candidate",
        "derived_candidate_id": None,
    }
    audit_event = _append_audit_event(
        connection,
        "candidate.create",
        payload if audit_payload is None else audit_payload,
    )
    connection.execute(
        text(
            "INSERT INTO public.candidate_event "
            "(event_ref, candidate_id, operation_id, command_fingerprint, event_type, to_revision, "
            "to_status, actor_ref, reason, occurred_at, audit_event_id) VALUES "
            "(:event_ref, :candidate, :operation, :fingerprint, 'CREATE', 1, :status, "
            "'legacy-test', 'legacy candidate', :now, :audit)"
        ),
        {
            "event_ref": event_ref,
            "candidate": candidate_id,
            "operation": operation_id,
            "fingerprint": operation_id.bytes * 2,
            "status": status,
            "now": now,
            "audit": audit_event,
        },
    )
    return {
        "entity": entity_id,
        "evidence_entity": evidence_entity_id,
        "unit": unit_id,
        "category": category_id,
        "evidence": evidence_ref,
        "candidate": candidate_id,
        "event": event_ref,
        "operation": operation_id,
        "now": now,
    }


def _legacy_transition(
    connection: Connection,
    facts: dict[str, Any],
    *,
    event_type: str,
    from_status: str,
    to_status: str,
    derived_candidate_id: UUID | None = None,
    add_field_change: bool = False,
) -> None:
    candidate_id = cast(UUID, facts["candidate"])
    revision = (
        int(
            connection.execute(
                text(
                    "SELECT max(revision) FROM public.candidate_revision WHERE candidate_id = :candidate"
                ),
                {"candidate": candidate_id},
            ).scalar_one()
        )
        + 1
    )
    operation_id = uuid4()
    event_ref = uuid4()
    now = datetime.now(UTC)
    action = event_type
    previous = connection.execute(
        text(
            "SELECT business_unit_id, business_unit_ref_snapshot, "
            "business_unit_label_snapshot, category_id, category_code_snapshot, "
            "category_label_snapshot, amount_minor, accounting_month "
            "FROM public.candidate_revision WHERE candidate_id = :candidate "
            "AND revision = :revision"
        ),
        {"candidate": candidate_id, "revision": revision - 1},
    ).one()
    if to_status in {"INCOMPLETE", "IGNORED"}:
        connection.execute(
            text(
                "INSERT INTO public.candidate_revision "
                "(candidate_id, revision, status, currency, summary, confidence_basis_points, "
                "created_at, updated_at) VALUES (:candidate, :revision, :status, 'CNY', "
                "'legacy candidate', 100, :now, :now)"
            ),
            {"candidate": candidate_id, "revision": revision, "status": to_status, "now": now},
        )
    else:
        connection.execute(
            text(
                "INSERT INTO public.candidate_revision "
                "(candidate_id, revision, status, business_unit_id, "
                "business_unit_ref_snapshot, business_unit_label_snapshot, category_id, "
                "category_code_snapshot, category_label_snapshot, amount_minor, currency, "
                "accounting_month, summary, confidence_basis_points, created_at, updated_at) "
                "VALUES (:candidate, :revision, :status, :unit, :unit_ref, :unit_label, "
                ":category, :category_code, :category_label, :amount, 'CNY', :month, "
                "'legacy candidate', 100, :now, :now)"
            ),
            {
                "candidate": candidate_id,
                "revision": revision,
                "status": to_status,
                "unit": previous.business_unit_id or facts["unit"],
                "unit_ref": previous.business_unit_ref_snapshot or f"u-{facts['unit'].hex[:8]}",
                "unit_label": previous.business_unit_label_snapshot or "Legacy Unit",
                "category": previous.category_id or facts["category"],
                "category_code": previous.category_code_snapshot or "legacy-category",
                "category_label": previous.category_label_snapshot or "Legacy Category",
                "amount": previous.amount_minor if previous.amount_minor is not None else 1,
                "month": previous.accounting_month or datetime(2026, 8, 1).date(),
                "now": now,
            },
        )
    field_changes: list[tuple[str, object, object]] = []
    if from_status != to_status:
        field_changes.append(("status", from_status, to_status))
    if add_field_change:
        field_changes.append(("amount_minor", 1, 2))
    field_changes.sort(key=lambda item: item[0])
    payload = {
        "event_ref": str(event_ref),
        "candidate_id": str(candidate_id),
        "candidate_ref": str(candidate_id),
        "operation_id": str(operation_id),
        "command_fingerprint": (operation_id.bytes * 2).hex(),
        "event_type": event_type,
        "action": action,
        "from_revision": revision - 1,
        "to_revision": revision,
        "from_status": from_status,
        "to_status": to_status,
        "field_changes": [
            {
                "field": field,
                "previous_value": previous_value,
                "new_value": new_value,
            }
            for field, previous_value, new_value in field_changes
        ],
        "conflict_resolutions": [],
        "actor_ref": "legacy-test",
        "reason": "legacy transition",
        "derived_candidate_id": str(derived_candidate_id) if derived_candidate_id else None,
    }
    audit_event = _append_audit_event(connection, "candidate.transition", payload)
    connection.execute(
        text(
            "INSERT INTO public.candidate_event "
            "(event_ref, candidate_id, operation_id, command_fingerprint, event_type, action, "
            "from_revision, to_revision, from_status, to_status, actor_ref, reason, "
            "derived_candidate_id, occurred_at, audit_event_id) VALUES "
            "(:event_ref, :candidate, :operation, :fingerprint, :event_type, :action, "
            ":from_revision, :to_revision, :from_status, :to_status, 'legacy-test', "
            "'legacy transition', :derived, :now, :audit)"
        ),
        {
            "event_ref": event_ref,
            "candidate": candidate_id,
            "operation": operation_id,
            "fingerprint": operation_id.bytes * 2,
            "event_type": event_type,
            "action": action,
            "from_revision": revision - 1,
            "to_revision": revision,
            "from_status": from_status,
            "to_status": to_status,
            "derived": derived_candidate_id,
            "now": now,
            "audit": audit_event,
        },
    )
    for field, previous_value, new_value in field_changes:
        connection.execute(
            text(
                "INSERT INTO public.candidate_field_change "
                "(event_ref, field, previous_value, new_value) VALUES "
                "(:event, :field, CAST(:previous_value AS jsonb), "
                "CAST(:new_value AS jsonb))"
            ),
            {
                "event": event_ref,
                "field": field,
                "previous_value": json.dumps(previous_value),
                "new_value": json.dumps(new_value),
            },
        )


def _assert_legacy_upgrade_rejected(
    seed: Callable[[Connection], None],
    *,
    message: str,
    sqlstate: str = "23000",
) -> None:
    with _legacy_r1_database(reader=False) as database_url:
        engine = create_engine(database_url)
        with engine.begin() as connection:
            seed(connection)
        with pytest.raises(SQLAlchemyError) as raised:
            command.upgrade(_upgrade_config(database_url), "20260824_0014")
        assert _sqlstate(raised.value) == sqlstate
        assert message in str(getattr(raised.value, "orig", raised.value))
        engine.dispose()


@contextmanager
def _hardened_r1_database() -> Iterator[str]:
    with _legacy_r1_database(reader=True) as database_url:
        command.upgrade(_upgrade_config(database_url), "20260824_0014")
        yield database_url


@contextmanager
def _fresh_head_r1_database() -> Iterator[str]:
    """Create a clean reader-surface database for tests requiring no prior facts."""

    with _legacy_r1_database(reader=True) as database_url:
        command.upgrade(_upgrade_config(database_url), "head")
        yield database_url


@contextmanager
def _temporarily_privileged_role(_database_url: str, role: str) -> Iterator[None]:
    maintenance = _maintenance_engine(_test_admin_url())
    try:
        with maintenance.connect() as connection:
            state = connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
                    "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = :role"
                ),
                {"role": role},
            ).one()
            connection.exec_driver_sql(f"ALTER ROLE {role} SUPERUSER")
        try:
            yield
        finally:
            clauses = [
                "SUPERUSER" if state[0] else "NOSUPERUSER",
                "CREATEDB" if state[1] else "NOCREATEDB",
                "CREATEROLE" if state[2] else "NOCREATEROLE",
                "INHERIT" if state[3] else "NOINHERIT",
                "REPLICATION" if state[4] else "NOREPLICATION",
                "BYPASSRLS" if state[5] else "NOBYPASSRLS",
            ]
            with maintenance.connect() as connection:
                connection.exec_driver_sql(f"ALTER ROLE {role} {' '.join(clauses)}")
    finally:
        maintenance.dispose()


@contextmanager
def _temporarily_runtime_membership(database_url: str, role: str) -> Iterator[None]:
    maintenance = _maintenance_engine(_test_admin_url())
    owner = _migration_owner_name(make_url(database_url))
    with maintenance.connect() as connection:
        was_member = bool(
            connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_auth_members m "
                    "JOIN pg_roles granted ON granted.oid = m.roleid "
                    "JOIN pg_roles member ON member.oid = m.member "
                    "WHERE granted.rolname = :role AND member.rolname = :owner)"
                ),
                {"role": role, "owner": owner},
            ).scalar_one()
        )
        if not was_member:
            connection.exec_driver_sql(f"GRANT {role} TO {_quote_identifier(owner)}")
    try:
        yield
    finally:
        if not was_member:
            with maintenance.connect() as connection:
                connection.exec_driver_sql(f"REVOKE {role} FROM {_quote_identifier(owner)}")
        maintenance.dispose()


@contextmanager
def _temporary_backup_role(database_url: str, attributes: str) -> Iterator[None]:
    maintenance = _maintenance_engine(_test_admin_url())
    try:
        with maintenance.connect() as connection:
            connection.exec_driver_sql(
                "CREATE ROLE ledgerbridge_backup "
                "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                "NOREPLICATION NOBYPASSRLS"
            )
            if attributes:
                connection.exec_driver_sql(f"ALTER ROLE ledgerbridge_backup {attributes}")
        yield
    finally:
        with maintenance.connect() as connection:
            database_name = _database_name(database_url)
            connection.exec_driver_sql(
                f'REVOKE ALL ON DATABASE "{database_name}" FROM ledgerbridge_backup'
            )
            connection.exec_driver_sql("DROP ROLE IF EXISTS ledgerbridge_backup")
        maintenance.dispose()


@contextmanager
def _temporarily_stale_connect(database_url: str) -> Iterator[None]:
    maintenance = _maintenance_engine(_test_admin_url())
    role = "ledgerbridge_stale_" + uuid4().hex[:8]
    database_name = _database_name(database_url)
    try:
        with maintenance.connect() as connection:
            connection.exec_driver_sql(
                f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS"
            )
            connection.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{database_name}" TO {role}')
        yield
    finally:
        with maintenance.connect() as connection:
            connection.exec_driver_sql(f'REVOKE ALL ON DATABASE "{database_name}" FROM {role}')
            connection.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")
        maintenance.dispose()


def _assert_acl_upgrade_rejected(
    mutate: Callable[[str], Any], *, message: str, sqlstate: str = "28000"
) -> None:
    with _hardened_r1_database() as database_url:
        mutate(database_url)
        with pytest.raises(SQLAlchemyError) as raised:
            command.upgrade(_upgrade_config(database_url), "head")
        assert _sqlstate(raised.value) == sqlstate
        assert message in str(getattr(raised.value, "orig", raised.value))


def _assert_head_upgrade_rejected(database_url: str, *, message: str) -> None:
    with pytest.raises(SQLAlchemyError) as raised:
        command.upgrade(_upgrade_config(database_url), "head")
    assert _sqlstate(raised.value) == "28000"
    assert message in str(getattr(raised.value, "orig", raised.value))


@pytest.fixture(scope="function")
def isolated_r1_database() -> Iterator[str]:
    """Run 0013 -> 0014 -> 0015 in a fresh disposable DB with split bootstrap authority.

    The admin URL owns only temporary database/role lifecycle.  The migration
    URL must authenticate as the temporary database owner and remains the sole
    connection Alembic uses for migrations and fixture writes.
    """

    owner_url = _migration_owner_url()
    admin_url = _test_admin_url()
    owner_name = _migration_owner_name(owner_url)
    if not MIGRATION_HARDENING.exists() or not MIGRATION_C.exists():
        pytest.skip("Migration C split (0014 hardening + 0015 reader) is supplied in parallel")

    database_name = f"ledgerbridge_r1_read_{uuid4().hex[:12]}"
    maintenance_engine = _maintenance_engine(admin_url)
    created_roles: list[str] = []
    temporary_engine: Engine | None = None
    try:
        with maintenance_engine.connect() as connection:
            existing = set(
                connection.execute(
                    text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:roles)"),
                    {"roles": list(RUNTIME_ROLES)},
                ).scalars()
            )
            # These are intentionally external to Alembic: Migration C must not
            # silently create a login role.  The harness never stores or uses
            # runtime credentials; tests exercise roles through SET ROLE.
            for role in RUNTIME_ROLES:
                if role in existing:
                    continue
                connection.exec_driver_sql(
                    f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOREPLICATION NOBYPASSRLS"
                )
                created_roles.append(role)
            connection.execute(
                text(f'CREATE DATABASE "{database_name}" OWNER {_quote_identifier(owner_name)}')
            )
            assert (
                connection.execute(
                    text(
                        "SELECT pg_get_userbyid(datdba) FROM pg_database "
                        "WHERE datname = :database_name"
                    ),
                    {"database_name": database_name},
                ).scalar_one()
                == owner_name
            )
        _prepare_temporary_schema(admin_url, database_name, owner_name)

        temporary_url = owner_url.set(database=database_name)
        config = Config("alembic.ini")
        config.attributes["database_url"] = temporary_url.render_as_string(hide_password=False)
        command.upgrade(config, "20260824_0013")
        command.upgrade(config, "20260824_0014")
        command.upgrade(config, "head")
        temporary_engine = create_engine(temporary_url)
        yield temporary_url.render_as_string(hide_password=False)
    finally:
        if temporary_engine is not None:
            temporary_engine.dispose()
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
            for role in reversed(created_roles):
                connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        maintenance_engine.dispose()


def _has_db_privilege(connection: Connection, role: str, privilege: str) -> bool:
    return bool(
        connection.execute(
            text("SELECT has_database_privilege(:role, current_database(), :privilege)"),
            {"role": role, "privilege": privilege},
        ).scalar_one()
    )


def _append_audit_event(connection: Connection, action: str, payload: dict[str, object]) -> UUID:
    value = connection.execute(
        text(
            "SELECT public.append_audit_event(:actor, :action, :reason, :rule_version, "
            "CAST(:payload AS jsonb))"
        ),
        {
            "actor": "r1-test",
            "action": action,
            "reason": "R1 isolated integration fixture",
            "rule_version": "r1-test-v2",
            "payload": json.dumps(payload, separators=(",", ":")),
        },
    ).scalar_one()
    return cast(UUID, value)


def _seed_read_facts(
    connection: Connection,
    *,
    business_unit_retired_at: datetime | None = None,
    category_retired_at: datetime | None = None,
) -> dict[str, UUID | int | bytes]:
    entity_id = uuid4()
    other_entity_id = uuid4()
    business_unit_id = uuid4()
    category_id = uuid4()
    evidence_ref = uuid4()
    old_blob_ref = uuid4()
    active_blob_ref = uuid4()
    candidate_id = uuid4()
    operation_id = uuid4()
    source_event_ref = uuid4()
    now = datetime.now(UTC)
    connection.execute(
        text(
            "INSERT INTO public.entity (id, entity_type, name) VALUES "
            "(:id, 'COMPANY', 'R1 isolated entity'), (:other, 'COMPANY', 'R1 other entity')"
        ),
        {"id": entity_id, "other": other_entity_id},
    )
    connection.execute(
        text(
            "INSERT INTO public.business_unit "
            "(id, entity_id, ref, label, created_at, retired_at) "
            "VALUES (:id, :entity, 'unit-a', 'Unit A', :now, :retired_at)"
        ),
        {
            "id": business_unit_id,
            "entity": entity_id,
            "now": business_unit_retired_at or now,
            "retired_at": business_unit_retired_at,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.reporting_category "
            "(id, entity_id, code, label, created_at, retired_at) "
            "VALUES (:id, :entity, 'category-a', 'Category A', :now, :retired_at)"
        ),
        {
            "id": category_id,
            "entity": entity_id,
            "now": category_retired_at or now,
            "retired_at": category_retired_at,
        },
    )
    evidence_event = _append_audit_event(
        connection,
        "evidence.object.create",
        {
            "evidence_ref": str(evidence_ref),
            "entity_id": str(entity_id),
            "business_unit_id": str(business_unit_id),
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.evidence_object "
            "(evidence_ref, entity_id, business_unit_id, media_type, display_name, "
            "plaintext_sha256, plaintext_size, audit_event_id) VALUES "
            "(:evidence, :entity, :unit, 'application/pdf', 'fixture.pdf', :digest, 7, :audit)"
        ),
        {
            "evidence": evidence_ref,
            "entity": entity_id,
            "unit": business_unit_id,
            "digest": bytes.fromhex("11" * 32),
            "audit": evidence_event,
        },
    )
    # The module-scoped disposable database is intentionally reused across
    # read-surface tests; derive unique ciphertext digests per seed so the
    # append-only global digest index remains valid when a prior test leaves
    # facts behind for downgrade assertions.
    old_digest = old_blob_ref.bytes * 2
    active_digest = active_blob_ref.bytes * 2
    old_object_ref = old_blob_ref.hex * 2
    active_object_ref = active_blob_ref.hex * 2
    connection.execute(
        text(
            "INSERT INTO public.encrypted_object_identity (object_ref, evidence_ref) "
            "VALUES (:old_object, :evidence), (:active_object, :evidence)"
        ),
        {
            "old_object": old_object_ref,
            "active_object": active_object_ref,
            "evidence": evidence_ref,
        },
    )
    old_storage_key = (
        "sha256/" + old_digest.hex()[:2] + "/" + old_digest.hex()[2:4] + "/" + old_digest.hex()
    )
    old_event = _append_audit_event(
        connection,
        "evidence.blob.version",
        {
            "rotation_mode": "GENESIS",
            "blob_ref": str(old_blob_ref),
            "evidence_ref": str(evidence_ref),
            "predecessor_blob_ref": None,
            "object_ref": old_object_ref,
            "ciphertext_sha256": old_digest.hex(),
            "ciphertext_size": 7,
            "storage_key": old_storage_key,
            "envelope_schema": "ledgerbridge.secretstream.v1",
            "algorithm": "xchacha20poly1305-secretstream",
            "chunk_size": 65536,
            "stream_header": (bytes.fromhex("44" * 24)).hex(),
            "wrapped_key_generation": "generation-1",
            "wrapped_key_nonce": (bytes.fromhex("55" * 24)).hex(),
            "wrapped_key_ciphertext": (bytes.fromhex("66" * 48)).hex(),
            "purpose": "ledgerbridge-artifact-v2",
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.encrypted_blob_version "
            "(blob_ref, evidence_ref, object_ref, ciphertext_sha256, ciphertext_size, storage_key, "
            "envelope_schema, algorithm, chunk_size, stream_header, wrapped_key_generation, "
            "wrapped_key_nonce, wrapped_key_ciphertext, purpose, audit_event_id) VALUES "
            "(:blob, :evidence, :object_ref, :digest, 7, :storage_key, "
            "'ledgerbridge.secretstream.v1', 'xchacha20poly1305-secretstream', 65536, "
            ":stream_header, 'generation-1', :nonce, :wrapped, 'ledgerbridge-artifact-v2', :audit)"
        ),
        {
            "blob": old_blob_ref,
            "evidence": evidence_ref,
            "object_ref": old_object_ref,
            "digest": old_digest,
            "storage_key": old_storage_key,
            "stream_header": bytes.fromhex("44" * 24),
            "nonce": bytes.fromhex("55" * 24),
            "wrapped": bytes.fromhex("66" * 48),
            "audit": old_event,
        },
    )
    active_storage_key = (
        "sha256/"
        + active_digest.hex()[:2]
        + "/"
        + active_digest.hex()[2:4]
        + "/"
        + active_digest.hex()
    )
    active_event = _append_audit_event(
        connection,
        "evidence.blob.version",
        {
            "rotation_mode": "REENCRYPT",
            "blob_ref": str(active_blob_ref),
            "evidence_ref": str(evidence_ref),
            "predecessor_blob_ref": str(old_blob_ref),
            "object_ref": active_object_ref,
            "ciphertext_sha256": active_digest.hex(),
            "ciphertext_size": 8,
            "storage_key": active_storage_key,
            "envelope_schema": "ledgerbridge.secretstream.v1",
            "algorithm": "xchacha20poly1305-secretstream",
            "chunk_size": 65536,
            "stream_header": (bytes.fromhex("77" * 24)).hex(),
            "wrapped_key_generation": "generation-1",
            "wrapped_key_nonce": (bytes.fromhex("88" * 24)).hex(),
            "wrapped_key_ciphertext": (bytes.fromhex("99" * 48)).hex(),
            "purpose": "ledgerbridge-artifact-v2",
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.encrypted_blob_version "
            "(blob_ref, evidence_ref, predecessor_blob_ref, object_ref, ciphertext_sha256, "
            "ciphertext_size, storage_key, envelope_schema, algorithm, chunk_size, stream_header, "
            "wrapped_key_generation, wrapped_key_nonce, wrapped_key_ciphertext, "
            "purpose, audit_event_id) "
            "VALUES (:blob, :evidence, :predecessor, :object_ref, :digest, 8, :storage_key, "
            "'ledgerbridge.secretstream.v1', 'xchacha20poly1305-secretstream', 65536, "
            ":stream_header, "
            "'generation-1', :nonce, :wrapped, 'ledgerbridge-artifact-v2', :audit)"
        ),
        {
            "blob": active_blob_ref,
            "evidence": evidence_ref,
            "predecessor": old_blob_ref,
            "object_ref": active_object_ref,
            "digest": active_digest,
            "storage_key": active_storage_key,
            "stream_header": bytes.fromhex("77" * 24),
            "nonce": bytes.fromhex("88" * 24),
            "wrapped": bytes.fromhex("99" * 48),
            "audit": active_event,
        },
    )
    candidate_event_ref = uuid4()
    candidate_event = _append_audit_event(
        connection,
        "candidate.create",
        {
            "event_ref": str(candidate_event_ref),
            "candidate_id": str(candidate_id),
            "candidate_ref": str(candidate_id),
            "operation_id": str(operation_id),
            "command_fingerprint": (operation_id.bytes * 2).hex(),
            "event_type": "CREATE",
            "action": None,
            "from_revision": None,
            "to_revision": 1,
            "from_status": None,
            "to_status": "PENDING",
            "field_changes": [],
            "conflict_resolutions": [],
            "actor_ref": "r1-test",
            "reason": "fixture candidate",
            "derived_candidate_id": None,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.candidate (id, short_id, entity_id, contract_version, created_at) "
            "VALUES (:candidate, :short_id, :entity, 'ledgerbridge.candidate.v1', :now)"
        ),
        {
            "candidate": candidate_id,
            "short_id": "C-" + candidate_id.hex[:8].upper(),
            "entity": entity_id,
            "now": now,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.candidate_source "
            "(candidate_id, ingest_channel_id, source_system_id, source_event_ref, display_label) "
            "VALUES (:candidate, 'synthetic_upload', 'synthetic', :source_event, 'fixture source')"
        ),
        {"candidate": candidate_id, "source_event": source_event_ref},
    )
    connection.execute(
        text(
            "INSERT INTO public.candidate_revision "
            "(candidate_id, revision, status, business_unit_id, business_unit_ref_snapshot, "
            "business_unit_label_snapshot, category_id, category_code_snapshot, "
            "category_label_snapshot, amount_minor, currency, accounting_month, summary, "
            "confidence_basis_points, created_at, updated_at) VALUES (:candidate, 1, 'PENDING', "
            ":unit, 'unit-a', 'Unit A', :category, 'category-a', 'Category A', 1234, 'CNY', "
            "DATE '2026-08-01', 'assigned complete fixture candidate', 9500, :now, :now)"
        ),
        {"candidate": candidate_id, "unit": business_unit_id, "category": category_id, "now": now},
    )
    connection.execute(
        text(
            "INSERT INTO public.candidate_evidence "
            "(candidate_id, ordinal, evidence_ref, kind, media_type_snapshot, "
            "display_name_snapshot, download_available, candidate_entity_id, "
            "evidence_entity_id, evidence_business_unit_id) VALUES "
            "(:candidate, 0, :evidence, 'ATTACHMENT', 'application/pdf', "
            "'fixture.pdf', true, :entity, :entity, :unit)"
        ),
        {
            "candidate": candidate_id,
            "evidence": evidence_ref,
            "entity": entity_id,
            "unit": business_unit_id,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.candidate_event "
            "(event_ref, candidate_id, operation_id, command_fingerprint, event_type, to_revision, "
            "to_status, actor_ref, reason, occurred_at, audit_event_id) VALUES "
            "(:event_ref, :candidate, :operation, :fingerprint, 'CREATE', 1, 'PENDING', 'r1-test', "
            "'fixture candidate', :now, :audit)"
        ),
        {
            "candidate": candidate_id,
            "event_ref": candidate_event_ref,
            "operation": operation_id,
            "fingerprint": operation_id.bytes * 2,
            "now": now,
            "audit": candidate_event,
        },
    )
    watermark = connection.execute(
        text("SELECT sequence, hash FROM public.audit_event ORDER BY sequence DESC LIMIT 1")
    ).one()
    snapshot_ref = uuid4()
    proposal_ref = uuid4()
    suspense_ref = uuid4()
    snapshot_event = _append_audit_event(
        connection,
        "reconciliation.snapshot",
        {
            "snapshot_ref": str(snapshot_ref),
            "entity_id": str(entity_id),
            "business_unit_id": str(business_unit_id),
            "accounting_month": "2026-08-01",
            "snapshot_revision": 1,
            "ledger_audit_sequence": int(watermark.sequence),
            "ledger_audit_hash": bytes(watermark.hash).hex(),
            "posted_amount_minor": 1234,
            "currency": "CNY",
            "blocker_count": 1,
            "proposal_count": 1,
            "suspense_count": 1,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.reconciliation_snapshot "
            "(snapshot_ref, entity_id, business_unit_id, accounting_month, snapshot_revision, "
            "ledger_audit_sequence, ledger_audit_hash, posted_amount_minor, currency, created_at, "
            "audit_event_id) VALUES (:snapshot, :entity, :unit, DATE '2026-08-01', 1, :sequence, "
            ":hash, 1234, 'CNY', :now, :audit)"
        ),
        {
            "snapshot": snapshot_ref,
            "entity": entity_id,
            "unit": business_unit_id,
            "sequence": int(watermark.sequence),
            "hash": bytes(watermark.hash),
            "now": now,
            "audit": snapshot_event,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.reconciliation_snapshot_blocker "
            "(snapshot_ref, ordinal, code, message, field, evidence_ref) VALUES "
            "(:snapshot, 0, 'MISSING_FIELD', 'fixture blocker', 'amount', :evidence)"
        ),
        {"snapshot": snapshot_ref, "evidence": evidence_ref},
    )
    connection.execute(
        text(
            "INSERT INTO public.reconciliation_snapshot_proposal "
            "(snapshot_ref, proposal_ref, reconciliation_group_id, relation, status, amount_minor, "
            "currency, amount_basis) VALUES (:snapshot, :proposal, NULL, '1:1', 'PROPOSED', "
            "1234, 'CNY', 'PRIMARY_LEG')"
        ),
        {"snapshot": snapshot_ref, "proposal": proposal_ref},
    )
    connection.execute(
        text(
            "INSERT INTO public.reconciliation_snapshot_suspense "
            "(snapshot_ref, suspense_ref, suspense_item_id, status, reason, amount_minor, currency) "
            "VALUES (:snapshot, :suspense, NULL, 'OPEN', 'UNMATCHED', 1234, 'CNY')"
        ),
        {"snapshot": snapshot_ref, "suspense": suspense_ref},
    )
    horizon = connection.execute(
        text("SELECT sequence, hash FROM public.audit_event ORDER BY sequence DESC LIMIT 1")
    ).one()
    return {
        "entity": entity_id,
        "other_entity": other_entity_id,
        "unit": business_unit_id,
        "category": category_id,
        "evidence": evidence_ref,
        "old_blob": old_blob_ref,
        "active_blob": active_blob_ref,
        "candidate": candidate_id,
        "snapshot": snapshot_ref,
        "snapshot_watermark_sequence": int(watermark.sequence),
        "sequence": int(horizon.sequence),
        "hash": bytes(horizon.hash),
    }


def test_0022_replays_pending_correction_and_active_dimension_catalog_in_postgres(
    isolated_r1_database: str,
) -> None:
    engine = create_engine(isolated_r1_database)
    active_unit = uuid4()
    retired_unit = uuid4()
    active_category = uuid4()
    retired_category = uuid4()
    retired_at = datetime.now(UTC)
    with engine.begin() as connection:
        facts = _seed_read_facts(connection)
        connection.execute(
            text(
                "INSERT INTO public.business_unit "
                "(id, entity_id, ref, label, created_at, retired_at) VALUES "
                "(:active, :entity, 'unit-reviewed', 'Reviewed unit', :now, NULL), "
                "(:retired, :entity, 'unit-retired', 'Unit A', :now, :now)"
            ),
            {
                "active": active_unit,
                "retired": retired_unit,
                "entity": facts["entity"],
                "now": retired_at,
            },
        )
        connection.execute(
            text(
                "INSERT INTO public.reporting_category "
                "(id, entity_id, code, label, created_at, retired_at) VALUES "
                "(:active, :entity, 'category-reviewed', 'Reviewed category', :now, NULL), "
                "(:retired, :entity, 'category-retired', 'Category A', :now, :now)"
            ),
            {
                "active": active_category,
                "retired": retired_category,
                "entity": facts["entity"],
                "now": retired_at,
            },
        )
        immutable_rows = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM public.candidate_source WHERE candidate_id = :candidate), "
                "(SELECT count(*) FROM public.candidate_evidence WHERE candidate_id = :candidate)"
            ),
            {"candidate": facts["candidate"]},
        ).one()
        duplicate_facts = _seed_read_facts(connection)
        duplicate_unit = uuid4()
        connection.execute(
            text(
                "INSERT INTO public.business_unit (id, entity_id, ref, label) "
                "VALUES (:id, :entity, 'unit-duplicate', 'Unit A')"
            ),
            {"id": duplicate_unit, "entity": duplicate_facts["entity"]},
        )
        retired_current_unit_facts = _seed_read_facts(
            connection,
            business_unit_retired_at=retired_at,
        )
        retired_current_category_facts = _seed_read_facts(
            connection,
            category_retired_at=retired_at,
        )

    with (
        _temporarily_runtime_membership(isolated_r1_database, "ledgerbridge_reader"),
        engine.begin() as connection,
    ):
        connection.execute(text("SET LOCAL ROLE ledgerbridge_reader"))
        dimensions = connection.execute(
            text(
                "SELECT internal_read.get_accounting_dimensions("
                "CAST(:entity AS uuid), CAST(:ids AS uuid[]), "
                "CAST(:refs AS varchar[]))"
            ),
            {
                "entity": facts["entity"],
                "ids": [facts["unit"], active_unit, retired_unit],
                "refs": ["unit-a", "unit-reviewed", "unit-retired"],
            },
        ).scalar_one()
    assert dimensions == {
        "contract_version": "ledgerbridge.accounting-dimensions.v1",
        "entity_ref": str(facts["entity"]),
        "business_units": [
            {"ref": "unit-a", "label": "Unit A"},
            {"ref": "unit-reviewed", "label": "Reviewed unit"},
        ],
        "categories": [
            {"code": "category-a", "label": "Category A"},
            {"code": "category-reviewed", "label": "Reviewed category"},
        ],
    }
    _assert_db_rejection(
        engine,
        [
            (
                "SELECT internal_read.get_accounting_dimensions("
                "CAST(:entity AS uuid), CAST(:ids AS uuid[]), CAST(:refs AS varchar[]))",
                {
                    "entity": duplicate_facts["entity"],
                    "ids": [duplicate_facts["unit"], duplicate_unit],
                    "refs": ["unit-a", "unit-duplicate"],
                },
            )
        ],
        sqlstate="LB005",
        message="active accounting dimension labels require registry governance",
    )

    base_parameters: dict[str, Any] = {
        "operation_id": uuid4(),
        "assertion_jti": uuid4(),
        "candidate_id": facts["candidate"],
        "authorized_entity_id": facts["entity"],
        "current_business_unit_id": facts["unit"],
        "target_business_unit_id": active_unit,
        "expected_revision": 1,
        "set_business_unit": True,
        "business_unit_ref": "unit-reviewed",
        "set_category": True,
        "category_code": "category-reviewed",
        "set_amount": True,
        "amount_minor": -1_999,
        "set_month": True,
        "accounting_month": "2026-09-01",
        "decided_at": datetime.now(UTC),
    }
    for overrides, message in (
        (
            {
                "operation_id": uuid4(),
                "assertion_jti": uuid4(),
                "target_business_unit_id": retired_unit,
                "business_unit_ref": "unit-retired",
                "set_category": False,
                "category_code": None,
                "set_amount": False,
                "amount_minor": None,
                "set_month": False,
                "accounting_month": None,
            },
            "referenced business unit is not visible",
        ),
        (
            {
                "operation_id": uuid4(),
                "assertion_jti": uuid4(),
                "target_business_unit_id": facts["unit"],
                "set_business_unit": False,
                "business_unit_ref": None,
                "category_code": "category-retired",
                "set_amount": False,
                "amount_minor": None,
                "set_month": False,
                "accounting_month": None,
            },
            "referenced category is not visible",
        ),
        (
            {
                "operation_id": uuid4(),
                "assertion_jti": uuid4(),
                "target_business_unit_id": uuid4(),
                "business_unit_ref": "unit-unknown",
                "set_category": False,
                "category_code": None,
                "set_amount": False,
                "amount_minor": None,
                "set_month": False,
                "accounting_month": None,
            },
            "referenced business unit is not visible",
        ),
        (
            {
                "operation_id": uuid4(),
                "assertion_jti": uuid4(),
                "authorized_entity_id": facts["other_entity"],
            },
            "candidate is outside authorized entity scope",
        ),
    ):
        _assert_db_rejection(
            engine,
            [(PENDING_CORRECTION_CALL_SQL, {**base_parameters, **overrides})],
            sqlstate="LB004",
            message=message,
        )

    for current_facts, message in (
        (
            retired_current_unit_facts,
            "final business unit is not an active candidate dimension",
        ),
        (
            retired_current_category_facts,
            "final category is not an active candidate dimension",
        ),
    ):
        _assert_db_rejection(
            engine,
            [
                (
                    PENDING_CORRECTION_CALL_SQL,
                    {
                        **base_parameters,
                        "operation_id": uuid4(),
                        "assertion_jti": uuid4(),
                        "candidate_id": current_facts["candidate"],
                        "authorized_entity_id": current_facts["entity"],
                        "current_business_unit_id": current_facts["unit"],
                        "target_business_unit_id": current_facts["unit"],
                        "set_business_unit": False,
                        "business_unit_ref": None,
                        "set_category": False,
                        "category_code": None,
                        "set_amount": True,
                        "amount_minor": -1_998,
                        "set_month": False,
                        "accounting_month": None,
                    },
                )
            ],
            sqlstate="LB004",
            message=message,
        )

    success_parameters = {
        **base_parameters,
        "target_business_unit_id": facts["unit"],
        "set_business_unit": False,
        "business_unit_ref": None,
    }
    with engine.begin() as connection:
        receipt = connection.execute(
            text(PENDING_CORRECTION_CALL_SQL), success_parameters
        ).scalar_one()
    assert receipt["replayed"] is False
    assert receipt["candidate"]["revision"] == 2
    assert receipt["candidate"]["status"] == "CONFIRMED"
    assert len(receipt["events"]) == 1
    assert receipt["events"][0]["action"] == "CORRECT_AND_CONFIRM"

    with engine.connect() as connection:
        revisions = connection.execute(
            text(
                "SELECT revision, status, business_unit_ref_snapshot, category_code_snapshot, "
                "amount_minor, accounting_month FROM public.candidate_revision "
                "WHERE candidate_id = :candidate ORDER BY revision"
            ),
            {"candidate": facts["candidate"]},
        ).all()
        correction_event = connection.execute(
            text(
                "SELECT event_ref, from_status, to_status, from_revision, to_revision "
                "FROM public.candidate_event WHERE candidate_id = :candidate "
                "AND action = 'CORRECT_AND_CONFIRM'"
            ),
            {"candidate": facts["candidate"]},
        ).one()
        fields = set(
            connection.execute(
                text("SELECT field FROM public.candidate_field_change WHERE event_ref = :event"),
                {"event": correction_event.event_ref},
            ).scalars()
        )
        after_immutable_rows = connection.execute(
            text(
                "SELECT "
                "(SELECT count(*) FROM public.candidate_source WHERE candidate_id = :candidate), "
                "(SELECT count(*) FROM public.candidate_evidence WHERE candidate_id = :candidate)"
            ),
            {"candidate": facts["candidate"]},
        ).one()
        closure_source = connection.execute(
            text(
                "SELECT pg_get_functiondef("
                "'public.r1_check_candidate_closure(uuid,uuid)'::regprocedure)"
            )
        ).scalar_one()
    assert tuple(revisions[0]) == (
        1,
        "PENDING",
        "unit-a",
        "category-a",
        1234,
        date(2026, 8, 1),
    )
    assert tuple(revisions[1]) == (
        2,
        "CONFIRMED",
        "unit-a",
        "category-reviewed",
        -1_999,
        date(2026, 9, 1),
    )
    assert tuple(correction_event)[1:] == ("PENDING", "CONFIRMED", 1, 2)
    assert fields == {
        "status",
        "category_code",
        "category_label",
        "amount_minor",
        "accounting_month",
    }
    assert after_immutable_rows == immutable_rows
    assert "CORRECT_AND_CONFIRM" in closure_source

    _assert_db_rejection(
        engine,
        [
            (
                PENDING_CORRECTION_CALL_SQL,
                {
                    **success_parameters,
                    "operation_id": uuid4(),
                    "assertion_jti": uuid4(),
                },
            )
        ],
        sqlstate="LB002",
        message="candidate revision is stale",
    )
    _assert_db_rejection(
        engine,
        [
            (
                PENDING_CORRECTION_CALL_SQL,
                {
                    **success_parameters,
                    "operation_id": uuid4(),
                    "assertion_jti": uuid4(),
                    "current_business_unit_id": facts["unit"],
                    "expected_revision": 2,
                    "set_business_unit": False,
                    "business_unit_ref": None,
                    "set_category": False,
                    "category_code": None,
                    "set_month": False,
                    "accounting_month": None,
                    "amount_minor": -1_998,
                },
            )
        ],
        sqlstate="LB003",
        message="only open candidates can be corrected",
    )

    with pytest.raises(SQLAlchemyError) as raised:
        command.downgrade(_upgrade_config(isolated_r1_database), "20260830_0021")
    assert "nonempty R1 fact database prevents destructive company-reporting downgrade" in str(
        getattr(raised.value, "orig", raised.value)
    )
    engine.dispose()


def test_0022_nonempty_r1_database_without_pending_correction_rejects_downgrade(
    isolated_r1_database: str,
) -> None:
    config = _upgrade_config(isolated_r1_database)
    command.downgrade(config, "20260830_0022")
    engine = create_engine(isolated_r1_database)
    with engine.begin() as connection:
        _seed_read_facts(connection)

    with pytest.raises(SQLAlchemyError) as raised:
        command.downgrade(config, "20260830_0021")
    assert "nonempty R1 fact database prevents destructive downgrade" in str(
        getattr(raised.value, "orig", raised.value)
    )
    engine.dispose()


def _seed_nonempty_downgrade_marker(connection: Connection) -> None:
    entity_id = uuid4()
    unit_id = uuid4()
    evidence_ref = uuid4()
    object_ref = uuid4().hex * 2
    connection.execute(
        text(
            "INSERT INTO public.entity (id, entity_type, name) "
            "VALUES (:id, 'COMPANY', 'downgrade marker entity')"
        ),
        {"id": entity_id},
    )
    connection.execute(
        text(
            "INSERT INTO public.business_unit (id, entity_id, ref, label) "
            "VALUES (:id, :entity, 'downgrade-unit', 'Downgrade Unit')"
        ),
        {"id": unit_id, "entity": entity_id},
    )
    audit_id = _append_audit_event(
        connection,
        "evidence.object.create",
        {
            "evidence_ref": str(evidence_ref),
            "entity_id": str(entity_id),
            "business_unit_id": str(unit_id),
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.evidence_object "
            "(evidence_ref, entity_id, business_unit_id, media_type, plaintext_sha256, "
            "plaintext_size, audit_event_id) VALUES "
            "(:evidence, :entity, :unit, 'application/pdf', :digest, 0, :audit)"
        ),
        {
            "evidence": evidence_ref,
            "entity": entity_id,
            "unit": unit_id,
            "digest": bytes.fromhex("aa" * 32),
            "audit": audit_id,
        },
    )
    connection.execute(
        text(
            "INSERT INTO public.encrypted_object_identity (object_ref, evidence_ref) "
            "VALUES (:object_ref, :evidence)"
        ),
        {"object_ref": object_ref, "evidence": evidence_ref},
    )


def test_r1_candidate_evidence_migration_is_forward_only_and_owner_written() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "20260824_0012"' in source
    assert 'down_revision: str | None = "20260824_0011"' in source
    for table in (
        "business_unit",
        "reporting_category",
        "evidence_object",
        "encrypted_blob_version",
        "candidate",
        "candidate_source",
        "candidate_revision",
        "candidate_event",
        "candidate_evidence",
    ):
        assert f'op.create_table(\n        "{table}"' in source
    assert "_append_only(table)" in source
    assert "REVOKE ALL ON TABLE" in source
    assert "GRANT INSERT" not in source
    assert "ledgerbridge_reader" in source
    assert "R1 Candidate/evidence data prevents destructive downgrade" in source


def test_r1_pg_harness_separates_admin_bootstrap_from_migration_owner() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("LEDGERBRIDGE_MIGRATION_DATABASE_URL")' in source
    assert 'os.environ.get("LEDGERBRIDGE_TEST_ADMIN_DATABASE_URL")' in source
    assert 'CREATE DATABASE "{database_name}" ' in source
    assert "OWNER {_quote_identifier(owner_name)}" in source
    assert "pg_get_userbyid(datdba)" in source
    assert "maintenance_engine = _maintenance_engine(admin_url)" in source
    assert "PASSWORD " + "'" not in source
    assert "create_engine(" + "value).url" not in source


def test_r1_migration_pins_secretstream_and_candidate_scope_contracts() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    for literal in (
        "ledgerbridge.secretstream.v1",
        "xchacha20poly1305-secretstream",
        "ledgerbridge-artifact-v2",
        "r1_validate_candidate_scope",
        "r1_validate_revision_dimensions",
        "candidate_revision_status_allowed",
        "candidate_event_audit_event",
    ):
        assert literal in source


def test_r1_migration_b_keeps_attribution_and_snapshot_facts_owner_written() -> None:
    source = MIGRATION_B.read_text(encoding="utf-8")
    assert 'revision: str = "20260824_0013"' in source
    assert 'down_revision: str | None = "20260824_0012"' in source
    for table in (
        "journal_entry_attribution",
        "posting_attribution",
        "reconciliation_snapshot",
        "reconciliation_snapshot_proposal",
        "reconciliation_snapshot_suspense",
    ):
        assert f'op.create_table(\n        "{table}"' in source
    assert "reconciliation_leg_primary_shape" in source
    assert "PRIMARY_LEG" in source
    assert "REVOKE ALL ON TABLE" in source
    assert "GRANT INSERT" not in source
    assert "v_posting_id := OLD.posting_id" in source
    assert "v_posting_id := NEW.posting_id" in source
    assert "COALESCE(NEW.posting_id, OLD.posting_id)" not in source


def test_r1_fact_hardening_binds_legacy_facts_and_rejects_bad_history() -> None:
    source = MIGRATION_HARDENING.read_text(encoding="utf-8")
    assert 'revision: str = "20260824_0014"' in source
    assert 'down_revision: str | None = "20260824_0013"' in source
    for literal in (
        "encrypted_object_identity",
        "fk_encrypted_blob_object_identity",
        "candidate_entity_id",
        "evidence_business_unit_id",
        "candidate evidence scope cannot be inferred",
        "candidate event audit binding is invalid",
        "candidate transition has no unique predecessor event",
        "candidate creation requires at least one evidence link",
        "candidate reporting category scope or snapshot is invalid",
        "POSTED journal entry requires exactly one attribution",
        "POSTED posting requires exactly one category attribution",
        "reconciliation group requires exactly one scoped primary leg",
        "snapshot audit binding is invalid",
        "blob predecessor would branch",
        "blob predecessor chain contains a cycle",
        "encrypted evidence must have exactly one genesis",
        "encrypted evidence must have exactly one active tip",
        "R1 fact hardening data prevents destructive downgrade",
    ):
        assert literal in source
    # Every deferred validator must be allowed to see the audit row in the
    # same transaction; a test-only r1.* escape hatch would mask bad facts.
    assert "IF v_action LIKE 'r1.%'" not in source


def test_r1_internal_read_surface_has_typed_receipt_and_dynamic_owner_contract() -> None:
    source = MIGRATION_C.read_text(encoding="utf-8")
    assert 'revision: str = "20260824_0015"' in source
    assert 'down_revision: str | None = "20260824_0014"' in source
    for literal in (
        "evidence_read_receipt",
        "operation_id",
        "UNIQUE",
        "current_database()",
        "pg_get_userbyid",
        "current_audit_horizon",
        "list_candidates_as_of",
        "get_reconciliation_as_of",
        "resolve_active_evidence_blob",
        "get_ledger_summary_as_of",
        "append_internal_evidence_read_audit",
        "R1 internal-read data prevents destructive downgrade",
    ):
        assert literal in source
    assert "GRANT USAGE ON SCHEMA internal_read TO ledgerbridge_api" in source
    assert (
        "internal_read.append_internal_evidence_read_audit(\n"
        "                uuid, varchar(200), varchar(200), varchar(128), uuid, uuid, "
        "uuid, uuid, bigint, bytea\n"
        "            ) TO ledgerbridge_api;"
    ) in source
    reader_grant_start = source.index(
        "GRANT EXECUTE ON FUNCTION internal_read.current_audit_horizon"
    )
    receipt_grant_start = source.index(
        "GRANT EXECUTE ON FUNCTION internal_read.append_internal_evidence_read_audit"
    )
    assert (
        "append_internal_evidence_read_audit" not in source[reader_grant_start:receipt_grant_start]
    )


def test_r1_reader_surface_explicitly_rejects_cross_scope_cursor_and_malformed_blob() -> None:
    source = MIGRATION_C.read_text(encoding="utf-8")
    for literal in (
        "invalid candidate read parameters",
        "invalid reconciliation read parameters",
        "audit horizon is not an exact chain row",
        "business unit does not belong to entity",
        "candidate cursor is outside requested scope",
        "evidence audit receipt",
        "active blob",
        "plaintext digest or size does not match immutable evidence",
    ):
        assert literal in source


def test_r1_migration_c_declares_closed_reader_surface_and_fail_closed_downgrade() -> None:
    source = MIGRATION_C.read_text(encoding="utf-8")
    assert 'revision: str = "20260824_0015"' in source
    assert 'down_revision: str | None = "20260824_0014"' in source
    for literal in (
        "ledgerbridge_reader",
        "internal_read",
        "security_barrier",
        "security_invoker",
        "current_audit_horizon",
        "list_candidates_as_of",
        "get_reconciliation_as_of",
        "resolve_active_evidence_blob",
        "get_ledger_summary_as_of",
        "append_internal_evidence_read_audit",
        "REVOKE ALL ON DATABASE",
        "R1 internal-read data prevents destructive downgrade",
    ):
        assert literal in source
    assert "ALTER DEFAULT PRIVILEGES" in source


def test_r1_fact_hardening_migration_is_identity_and_history_fail_closed() -> None:
    source = MIGRATION_HARDENING.read_text(encoding="utf-8")
    assert 'revision: str = "20260824_0014"' in source
    assert 'down_revision: str | None = "20260824_0013"' in source
    for literal in (
        "encrypted_object_identity",
        "object_ref",
        "candidate_event",
        "candidate_evidence",
        "POSTED",
        "primary",
        "journal_entry_attribution",
        "posting_attribution",
        "reconciliation_snapshot",
        "reconciliation_snapshot_blocker",
        "reconciliation_snapshot_proposal",
        "reconciliation_snapshot_suspense",
        "REVOKE ALL ON TABLE",
        "ledgerbridge_reader",
    ):
        assert literal in source
    assert "event_type" in source and "CREATE" in source
    assert "R1 fact hardening data prevents destructive downgrade" in source


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("status", "existing candidate event history has an invalid edge"),
        ("blocker", "existing candidate blocker evidence is outside scope"),
        ("null_shape", "existing candidate event history has an invalid edge"),
        ("delta", "candidate field changes do not exactly match revisions"),
        ("supersede", "candidate supersede requires one same-entity successor"),
        ("provenance", "existing candidate source provenance is invalid"),
    ],
)
def test_r1_legacy_candidate_upgrade_rejects_each_bad_history_shape(
    case: str, message: str
) -> None:
    def seed(connection: Connection) -> None:
        if case == "status":
            _legacy_candidate_seed(connection, status="CONFIRMED")
            return
        if case == "blocker":
            facts = _legacy_candidate_seed(connection)
            other = _legacy_candidate_seed(connection)
            connection.exec_driver_sql("ALTER TABLE public.candidate_blocker DISABLE TRIGGER USER")
            connection.execute(
                text(
                    "UPDATE public.candidate_blocker SET evidence_ref = :evidence "
                    "WHERE candidate_id = :candidate AND revision = 1 AND ordinal = 0"
                ),
                {"candidate": facts["candidate"], "evidence": other["evidence"]},
            )
            connection.exec_driver_sql("ALTER TABLE public.candidate_blocker ENABLE TRIGGER USER")
            return
        if case == "null_shape":
            facts = _legacy_candidate_seed(connection)
            event_ref = cast(UUID, facts["event"])
            operation_id = uuid4()
            bad_payload = {
                "event_ref": str(event_ref),
                "candidate_id": str(facts["candidate"]),
                "candidate_ref": str(facts["candidate"]),
                "operation_id": str(operation_id),
                "command_fingerprint": (operation_id.bytes * 2).hex(),
                "event_type": "CREATE",
                "action": "COMPLETE_FIELDS",
                "from_revision": 0,
                "to_revision": 1,
                "from_status": "INCOMPLETE",
                "to_status": "INCOMPLETE",
                "field_changes": [],
                "conflict_resolutions": [],
                "actor_ref": "legacy-test",
                "reason": "legacy candidate",
                "derived_candidate_id": None,
            }
            bad_audit = _append_audit_event(connection, "candidate.create", bad_payload)
            connection.exec_driver_sql("ALTER TABLE public.candidate_event DISABLE TRIGGER USER")
            connection.execute(
                text(
                    "UPDATE public.candidate_event SET operation_id = :operation, "
                    "command_fingerprint = :fingerprint, action = 'COMPLETE_FIELDS', "
                    "from_revision = 0, from_status = 'INCOMPLETE', audit_event_id = :audit "
                    "WHERE event_ref = :event"
                ),
                {
                    "operation": operation_id,
                    "fingerprint": operation_id.bytes * 2,
                    "audit": bad_audit,
                    "event": event_ref,
                },
            )
            connection.exec_driver_sql("ALTER TABLE public.candidate_event ENABLE TRIGGER USER")
            return
        if case == "delta":
            facts = _legacy_candidate_seed(connection, status="INCOMPLETE")
            _legacy_transition(
                connection,
                facts,
                event_type="COMPLETE_FIELDS",
                from_status="INCOMPLETE",
                to_status="PENDING",
            )
            return
        if case == "supersede":
            facts = _legacy_candidate_seed(connection, status="PENDING")
            _legacy_transition(
                connection,
                facts,
                event_type="CONFIRM",
                from_status="PENDING",
                to_status="CONFIRMED",
            )
            successor = _legacy_candidate_seed(connection, status="PENDING")
            connection.exec_driver_sql("ALTER TABLE public.candidate DISABLE TRIGGER USER")
            connection.execute(
                text(
                    "UPDATE public.candidate SET supersedes_candidate_id = :candidate "
                    "WHERE id = :successor"
                ),
                {"candidate": facts["candidate"], "successor": successor["candidate"]},
            )
            connection.exec_driver_sql("ALTER TABLE public.candidate ENABLE TRIGGER USER")
            _legacy_transition(
                connection,
                facts,
                event_type="SUPERSEDE",
                from_status="CONFIRMED",
                to_status="SUPERSEDED",
                derived_candidate_id=cast(UUID, successor["candidate"]),
            )
            return
        assert case == "provenance"
        facts = _legacy_candidate_seed(connection)
        artifact_id = uuid4()
        source_record_id = uuid4()
        import_job_id = uuid4()
        digest = bytes.fromhex("ab" * 32)
        storage_key = "sha256/ab/" + "ab" + "/" + digest.hex()
        artifact_audit = _append_audit_event(
            connection,
            "artifact.ingest",
            {
                "sha256": digest.hex(),
                "byte_size": 3,
                "storage_key": storage_key,
                "source": "manual_upload",
                "original_filename_sha256": hashlib.sha256(b"legacy.csv").hexdigest(),
                "media_type": "text/csv",
            },
        )
        connection.execute(
            text(
                "INSERT INTO public.raw_artifact "
                "(id, sha256, source, original_filename, media_type, byte_size, storage_key, "
                "audit_event_id) VALUES (:id, :digest, 'manual_upload', 'legacy.csv', 'text/csv', "
                "3, :storage_key, :audit)"
            ),
            {
                "id": artifact_id,
                "digest": digest,
                "storage_key": storage_key,
                "audit": artifact_audit,
            },
        )
        connection.execute(
            text(
                "INSERT INTO public.import_job "
                "(id, artifact_id, connector_name, connector_version, source_system, status) VALUES "
                "(:job, :artifact, 'legacy', '1', 'synthetic', 'PENDING')"
            ),
            {"job": import_job_id, "artifact": artifact_id},
        )
        connection.execute(
            text(
                "INSERT INTO public.source_record "
                "(id, artifact_id, import_job_id, record_locator, source, parser_version, "
                "raw_fields, normalized_fields) VALUES (:id, :artifact, :job, 'row-1', "
                "'synthetic', '1', '{}'::jsonb, '{}'::jsonb)"
            ),
            {"id": source_record_id, "artifact": artifact_id, "job": import_job_id},
        )
        connection.exec_driver_sql("ALTER TABLE public.candidate_source DISABLE TRIGGER USER")
        connection.execute(
            text(
                "UPDATE public.candidate_source SET source_record_id = :record "
                "WHERE candidate_id = :candidate"
            ),
            {"record": source_record_id, "candidate": facts["candidate"]},
        )
        connection.exec_driver_sql("ALTER TABLE public.candidate_source ENABLE TRIGGER USER")

    _assert_legacy_upgrade_rejected(seed, message=message)


def test_r1_legacy_upgrade_rejects_cross_transaction_typed_candidate_audit() -> None:
    with _legacy_r1_database(reader=False) as database_url:
        engine = create_engine(database_url)
        with engine.begin() as connection:
            facts = _legacy_candidate_seed(connection, status="PENDING")
        event_ref = uuid4()
        operation_id = uuid4()
        event_now = datetime.now(UTC)
        payload = {
            "event_ref": str(event_ref),
            "candidate_id": str(facts["candidate"]),
            "candidate_ref": str(facts["candidate"]),
            "operation_id": str(operation_id),
            "command_fingerprint": (operation_id.bytes * 2).hex(),
            "event_type": "CONFIRM",
            "action": "CONFIRM",
            "from_revision": 1,
            "to_revision": 2,
            "from_status": "PENDING",
            "to_status": "CONFIRMED",
            "field_changes": [],
            "conflict_resolutions": [],
            "actor_ref": "legacy-test",
            "reason": "cross transaction typed audit",
            "derived_candidate_id": None,
        }
        with engine.begin() as connection:
            audit_event = _append_audit_event(connection, "candidate.transition", payload)
            connection.execute(
                text(
                    "INSERT INTO public.candidate_revision "
                    "(candidate_id, revision, status, business_unit_id, "
                    "business_unit_ref_snapshot, business_unit_label_snapshot, category_id, "
                    "category_code_snapshot, category_label_snapshot, amount_minor, currency, "
                    "accounting_month, summary, confidence_basis_points, created_at, updated_at) "
                    "VALUES (:candidate, 2, 'CONFIRMED', :unit, :unit_ref, 'Legacy Unit', "
                    ":category, 'legacy-category', 'Legacy Category', 1, 'CNY', DATE '2026-08-01', "
                    "'legacy candidate', 100, :now, :now)"
                ),
                {
                    "candidate": facts["candidate"],
                    "unit": facts["unit"],
                    "unit_ref": f"u-{facts['unit'].hex[:8]}",
                    "category": facts["category"],
                    "now": event_now,
                },
            )
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO public.candidate_event "
                    "(event_ref, candidate_id, operation_id, command_fingerprint, event_type, "
                    "action, from_revision, to_revision, from_status, to_status, actor_ref, "
                    "reason, occurred_at, audit_event_id) VALUES (:event, :candidate, :operation, "
                    ":fingerprint, 'CONFIRM', 'CONFIRM', 1, 2, 'PENDING', 'CONFIRMED', "
                    "'legacy-test', 'cross transaction typed audit', :now, :audit)"
                ),
                {
                    "event": event_ref,
                    "candidate": facts["candidate"],
                    "operation": operation_id,
                    "fingerprint": operation_id.bytes * 2,
                    "now": event_now,
                    "audit": audit_event,
                },
            )
        with pytest.raises(SQLAlchemyError) as raised:
            command.upgrade(_upgrade_config(database_url), "20260824_0014")
        assert _sqlstate(raised.value) == "23000"
        assert "existing candidate event audit payload is invalid" in str(
            getattr(raised.value, "orig", raised.value)
        )
        engine.dispose()


def test_r1_legacy_attribution_upgrade_rejects_cross_entity_and_posted_primary_gaps() -> None:
    def seed(connection: Connection) -> None:
        first = _legacy_candidate_seed(connection)
        second = _legacy_candidate_seed(connection)
        # The attribution tables are present in 0013 and accept these rows;
        # 0014 must reject facts that silently cross entity boundaries.
        entry_id = uuid4()
        create_audit = _append_audit_event(connection, "journal.entry.create", {})
        posted_audit = _append_audit_event(connection, "journal.entry.post", {})
        connection.exec_driver_sql(
            "ALTER TABLE public.journal_entry DISABLE TRIGGER USER; "
            "ALTER TABLE public.journal_entry_attribution DISABLE TRIGGER USER"
        )
        connection.execute(
            text(
                "INSERT INTO public.journal_entry "
                "(id, entity_id, status, occurred_at, origin, source_record_id, audit_event_id, "
                "posted_audit_event_id) VALUES (:entry, :entity, 'POSTED', :occurred, 'legacy', "
                "NULL, :create_audit, :posted_audit)"
            ),
            {
                "entry": entry_id,
                "entity": first["entity"],
                "occurred": datetime.now(UTC),
                "create_audit": create_audit,
                "posted_audit": posted_audit,
            },
        )
        connection.execute(
            text(
                "INSERT INTO public.journal_entry_attribution "
                "(entry_id, entity_id, business_unit_id, accounting_month) "
                "VALUES (:entry, :entity, :unit, DATE '2026-08-01')"
            ),
            {"entry": entry_id, "entity": second["entity"], "unit": second["unit"]},
        )
        connection.exec_driver_sql(
            "ALTER TABLE public.journal_entry_attribution ENABLE TRIGGER USER; "
            "ALTER TABLE public.journal_entry ENABLE TRIGGER USER"
        )

    _assert_legacy_upgrade_rejected(
        seed, message="existing POSTED attribution or category scope is incomplete"
    )


@pytest.mark.parametrize(
    ("primary_case", "message"),
    [
        ("null", "existing POSTED journal entry lacks one non-null primary account posting"),
        ("zero", "existing POSTED journal entry lacks one non-null primary account posting"),
        ("multiple", "existing POSTED journal entry lacks one non-null primary account posting"),
    ],
)
def test_r1_legacy_posted_primary_shape_is_explicitly_required(
    primary_case: str, message: str
) -> None:
    def seed(connection: Connection) -> None:
        facts = _legacy_candidate_seed(connection)
        category_id = uuid4()
        account_one = uuid4()
        account_two = uuid4()
        entry_id = uuid4()
        posting_one = uuid4()
        posting_two = uuid4()
        audit_event = _append_audit_event(connection, "journal.entry.create", {})
        posted_audit_event = _append_audit_event(connection, "journal.entry.post", {})
        connection.execute(
            text(
                "INSERT INTO public.reporting_category (id, entity_id, code, label) "
                "VALUES (:id, :entity, 'legacy-expense', 'Legacy Expense')"
            ),
            {"id": category_id, "entity": facts["entity"]},
        )
        connection.execute(
            text(
                "INSERT INTO public.account (id, entity_id, identifier, name, account_class) "
                "VALUES (:one, :entity, 'legacy-one', 'Legacy One', 'EXPENSE'), "
                "(:two, :entity, 'legacy-two', 'Legacy Two', 'ASSET')"
            ),
            {"one": account_one, "two": account_two, "entity": facts["entity"]},
        )
        primary = None if primary_case == "null" else account_one
        if primary_case == "zero":
            posting_accounts = [(posting_one, account_two, 1)]
        elif primary_case == "multiple":
            posting_accounts = [
                (posting_one, account_one, 1),
                (posting_two, account_one, -1),
            ]
        else:
            posting_accounts = [(posting_one, account_one, 1)]
        # These are legacy rows; disable the old write-time checks so the
        # 0014 read-only preflight, rather than an insert trigger, diagnoses
        # the contradictory historical fact.
        connection.exec_driver_sql(
            "ALTER TABLE public.journal_entry DISABLE TRIGGER USER; "
            "ALTER TABLE public.posting DISABLE TRIGGER USER; "
            "ALTER TABLE public.journal_entry_attribution DISABLE TRIGGER USER; "
            "ALTER TABLE public.posting_attribution DISABLE TRIGGER USER"
        )
        connection.execute(
            text(
                "INSERT INTO public.journal_entry "
                "(id, entity_id, occurred_at, origin, status, primary_account_id, audit_event_id, "
                "posted_audit_event_id) VALUES (:entry, :entity, :occurred, 'legacy', 'POSTED', "
                ":primary, :audit, :posted_audit)"
            ),
            {
                "entry": entry_id,
                "entity": facts["entity"],
                "occurred": datetime.now(UTC),
                "primary": primary,
                "audit": audit_event,
                "posted_audit": posted_audit_event,
            },
        )
        connection.execute(
            text(
                "INSERT INTO public.journal_entry_attribution "
                "(entry_id, entity_id, business_unit_id, accounting_month) "
                "VALUES (:entry, :entity, :unit, DATE '2026-08-01')"
            ),
            {"entry": entry_id, "entity": facts["entity"], "unit": facts["unit"]},
        )
        for posting_id, account_id, amount in posting_accounts:
            connection.execute(
                text(
                    "INSERT INTO public.posting (id, entry_id, account_id, amount_minor, currency) "
                    "VALUES (:posting, :entry, :account, :amount, 'CNY')"
                ),
                {
                    "posting": posting_id,
                    "entry": entry_id,
                    "account": account_id,
                    "amount": amount,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO public.posting_attribution "
                    "(posting_id, reporting_category_id, category_code_snapshot, "
                    "category_label_snapshot) VALUES (:posting, :category, 'legacy-expense', "
                    "'Legacy Expense')"
                ),
                {"posting": posting_id, "category": category_id},
            )
        connection.exec_driver_sql(
            "ALTER TABLE public.posting_attribution ENABLE TRIGGER USER; "
            "ALTER TABLE public.journal_entry_attribution ENABLE TRIGGER USER; "
            "ALTER TABLE public.posting ENABLE TRIGGER USER; "
            "ALTER TABLE public.journal_entry ENABLE TRIGGER USER"
        )

    _assert_legacy_upgrade_rejected(seed, message=message)


def test_r1_legacy_posted_without_scope_attribution_is_rejected() -> None:
    def seed(connection: Connection) -> None:
        facts = _legacy_candidate_seed(connection)
        account_id = uuid4()
        entry_id = uuid4()
        posting_id = uuid4()
        entry_audit = _append_audit_event(connection, "journal.entry.create", {})
        posted_audit = _append_audit_event(connection, "journal.entry.post", {})
        connection.exec_driver_sql(
            "ALTER TABLE public.journal_entry DISABLE TRIGGER USER; "
            "ALTER TABLE public.posting DISABLE TRIGGER USER; "
            "ALTER TABLE public.journal_entry_attribution DISABLE TRIGGER USER; "
            "ALTER TABLE public.posting_attribution DISABLE TRIGGER USER"
        )
        connection.execute(
            text(
                "INSERT INTO public.account "
                "(id, entity_id, identifier, name, account_class) "
                "VALUES (:account, :entity, 'legacy-unattributed', 'Legacy Unattributed', 'EXPENSE')"
            ),
            {"account": account_id, "entity": facts["entity"]},
        )
        connection.execute(
            text(
                "INSERT INTO public.journal_entry "
                "(id, entity_id, occurred_at, origin, status, primary_account_id, "
                "audit_event_id, posted_audit_event_id) VALUES "
                "(:entry, :entity, :occurred, 'legacy', 'POSTED', :account, :audit, :posted)"
            ),
            {
                "entry": entry_id,
                "entity": facts["entity"],
                "occurred": datetime.now(UTC),
                "account": account_id,
                "audit": entry_audit,
                "posted": posted_audit,
            },
        )
        connection.execute(
            text(
                "INSERT INTO public.posting "
                "(id, entry_id, account_id, amount_minor, currency) "
                "VALUES (:posting, :entry, :account, 1, 'CNY')"
            ),
            {"posting": posting_id, "entry": entry_id, "account": account_id},
        )
        connection.exec_driver_sql(
            "ALTER TABLE public.posting_attribution ENABLE TRIGGER USER; "
            "ALTER TABLE public.journal_entry_attribution ENABLE TRIGGER USER; "
            "ALTER TABLE public.posting ENABLE TRIGGER USER; "
            "ALTER TABLE public.journal_entry ENABLE TRIGGER USER"
        )

    _assert_legacy_upgrade_rejected(
        seed,
        message="existing POSTED attribution or category scope is incomplete",
    )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("null_posting", "existing reconciliation scope or primary posting is invalid"),
        ("scope", "existing reconciliation scope or primary posting is invalid"),
    ],
)
def test_r1_legacy_reconciliation_leg_scope_and_posting_are_required(
    case: str, message: str
) -> None:
    def seed(connection: Connection) -> None:
        facts = _legacy_candidate_seed(connection)
        scope_entity = facts["entity"]
        if case == "scope":
            scope_entity = uuid4()
            connection.execute(
                text(
                    "INSERT INTO public.entity (id, entity_type, name) "
                    "VALUES (:entity, 'COMPANY', 'contradictory reconciliation entity')"
                ),
                {"entity": scope_entity},
            )
        artifact_id = uuid4()
        job_id = uuid4()
        source_record_id = uuid4()
        digest = bytes.fromhex("de" * 32)
        storage_key = "sha256/de/de/" + digest.hex()
        artifact_audit = _append_audit_event(
            connection,
            "artifact.ingest",
            {
                "sha256": digest.hex(),
                "byte_size": 3,
                "storage_key": storage_key,
                "source": "manual_upload",
                "original_filename_sha256": hashlib.sha256(b"leg.csv").hexdigest(),
                "media_type": "text/csv",
            },
        )
        connection.execute(
            text(
                "INSERT INTO public.raw_artifact "
                "(id, sha256, source, original_filename, media_type, byte_size, storage_key, "
                "audit_event_id) VALUES (:id, :digest, 'manual_upload', 'leg.csv', 'text/csv', "
                "3, :storage_key, :audit)"
            ),
            {
                "id": artifact_id,
                "digest": digest,
                "storage_key": storage_key,
                "audit": artifact_audit,
            },
        )
        connection.execute(
            text(
                "INSERT INTO public.import_job "
                "(id, artifact_id, connector_name, connector_version, source_system, status) VALUES "
                "(:job, :artifact, 'legacy', '1', 'synthetic', 'PENDING')"
            ),
            {"job": job_id, "artifact": artifact_id},
        )
        connection.execute(
            text(
                "INSERT INTO public.source_record "
                "(id, artifact_id, import_job_id, record_locator, source, parser_version, "
                "raw_fields, normalized_fields) VALUES (:id, :artifact, :job, 'leg-1', "
                "'synthetic', '1', '{}'::jsonb, '{}'::jsonb)"
            ),
            {"id": source_record_id, "artifact": artifact_id, "job": job_id},
        )
        review_id = uuid4()
        review_audit = _append_audit_event(
            connection,
            "review.create",
            {"review_item_id": str(review_id), "kind": "RECONCILIATION"},
        )
        connection.execute(
            text(
                "INSERT INTO public.review_item "
                "(id, kind, status, source_record_id, summary, payload, audit_event_id) "
                "VALUES (:id, 'RECONCILIATION', 'OPEN', :source, 'legacy review', "
                "'{}'::jsonb, :audit)"
            ),
            {"id": review_id, "source": source_record_id, "audit": review_audit},
        )
        group_id = uuid4()
        leg_id = uuid4()
        posting_id = None
        if case == "scope":
            account_id = uuid4()
            entry_id = uuid4()
            posting_id = uuid4()
            entry_audit = _append_audit_event(connection, "journal.entry.create", {})
            posted_audit = _append_audit_event(connection, "journal.entry.post", {})
            connection.exec_driver_sql(
                "ALTER TABLE public.journal_entry DISABLE TRIGGER USER; "
                "ALTER TABLE public.posting DISABLE TRIGGER USER; "
                "ALTER TABLE public.journal_entry_attribution DISABLE TRIGGER USER; "
                "ALTER TABLE public.posting_attribution DISABLE TRIGGER USER"
            )
            connection.execute(
                text(
                    "INSERT INTO public.account "
                    "(id, entity_id, identifier, name, account_class) "
                    "VALUES (:account, :entity, 'legacy-leg', 'Legacy Leg', 'EXPENSE')"
                ),
                {"account": account_id, "entity": facts["entity"]},
            )
            connection.execute(
                text(
                    "INSERT INTO public.journal_entry "
                    "(id, entity_id, occurred_at, origin, status, primary_account_id, "
                    "audit_event_id, posted_audit_event_id) VALUES "
                    "(:entry, :entity, :occurred, 'legacy', 'POSTED', :account, :audit, :posted)"
                ),
                {
                    "entry": entry_id,
                    "entity": facts["entity"],
                    "occurred": datetime.now(UTC),
                    "account": account_id,
                    "audit": entry_audit,
                    "posted": posted_audit,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO public.journal_entry_attribution "
                    "(entry_id, entity_id, business_unit_id, accounting_month) "
                    "VALUES (:entry, :entity, :unit, DATE '2026-08-01')"
                ),
                {"entry": entry_id, "entity": facts["entity"], "unit": facts["unit"]},
            )
            connection.execute(
                text(
                    "INSERT INTO public.posting "
                    "(id, entry_id, account_id, amount_minor, currency) "
                    "VALUES (:posting, :entry, :account, 1, 'CNY')"
                ),
                {"posting": posting_id, "entry": entry_id, "account": account_id},
            )
            connection.execute(
                text(
                    "INSERT INTO public.posting_attribution "
                    "(posting_id, reporting_category_id, category_code_snapshot, "
                    "category_label_snapshot) VALUES (:posting, :category, "
                    "'legacy-category', 'Legacy Category')"
                ),
                {"posting": posting_id, "category": facts["category"]},
            )
            connection.exec_driver_sql(
                "ALTER TABLE public.posting_attribution ENABLE TRIGGER USER; "
                "ALTER TABLE public.journal_entry_attribution ENABLE TRIGGER USER; "
                "ALTER TABLE public.posting ENABLE TRIGGER USER; "
                "ALTER TABLE public.journal_entry ENABLE TRIGGER USER"
            )
        connection.exec_driver_sql(
            "ALTER TABLE public.reconciliation_group DISABLE TRIGGER USER; "
            "ALTER TABLE public.reconciliation_leg DISABLE TRIGGER USER"
        )
        connection.execute(
            text(
                "INSERT INTO public.reconciliation_group "
                "(id, review_item_id, relation, status, currency) VALUES "
                "(:group, :review, '1:1', 'PROPOSED', 'CNY')"
            ),
            {"group": group_id, "review": review_id},
        )
        connection.execute(
            text(
                "INSERT INTO public.reconciliation_leg "
                "(id, reconciliation_group_id, source_record_id, amount_minor, currency, "
                "posting_id, is_primary, entity_id, business_unit_id, accounting_month) VALUES "
                "(:leg, :group, :source, 1, 'CNY', :posting, :primary, :entity, :unit, "
                "DATE '2026-08-01')"
            ),
            {
                "leg": leg_id,
                "group": group_id,
                "source": source_record_id,
                "posting": posting_id,
                "primary": True if case == "scope" else None,
                "entity": scope_entity,
                "unit": facts["unit"],
            },
        )
        connection.exec_driver_sql(
            "ALTER TABLE public.reconciliation_leg ENABLE TRIGGER USER; "
            "ALTER TABLE public.reconciliation_group ENABLE TRIGGER USER"
        )

    _assert_legacy_upgrade_rejected(seed, message=message)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("first_revision", "snapshot revision history must start at one"),
        ("gap", "snapshot revision history has a gap"),
        ("watermark", "snapshot audit watermark is invalid"),
    ],
)
def test_r1_legacy_snapshot_upgrade_rejects_noncontiguous_or_fake_watermarks(
    case: str, message: str
) -> None:
    def seed(connection: Connection) -> None:
        facts = _legacy_candidate_seed(connection)
        watermark = connection.execute(
            text("SELECT sequence, hash FROM public.audit_event ORDER BY sequence DESC LIMIT 1")
        ).one()
        revisions = (2,) if case == "first_revision" else (1, 3) if case == "gap" else (1,)
        for revision in revisions:
            snapshot_ref = uuid4()
            audit = _append_audit_event(
                connection,
                "reconciliation.snapshot",
                {
                    "snapshot_ref": str(snapshot_ref),
                    "entity_id": str(facts["entity"]),
                    "business_unit_id": str(facts["unit"]),
                    "accounting_month": "2026-08-01",
                    "snapshot_revision": revision,
                    "ledger_audit_sequence": int(watermark.sequence)
                    + (1 if case == "watermark" else 0),
                    "ledger_audit_hash": (
                        "00" * 32 if case == "watermark" else bytes(watermark.hash).hex()
                    ),
                    "posted_amount_minor": 1,
                    "currency": "CNY",
                    "blocker_count": 0,
                    "proposal_count": 0,
                    "suspense_count": 0,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO public.reconciliation_snapshot "
                    "(snapshot_ref, entity_id, business_unit_id, accounting_month, snapshot_revision, "
                    "ledger_audit_sequence, ledger_audit_hash, posted_amount_minor, currency, "
                    "created_at, audit_event_id) VALUES (:snapshot, :entity, :unit, "
                    "DATE '2026-08-01', :revision, :sequence, :hash, 1, 'CNY', :now, :audit)"
                ),
                {
                    "snapshot": snapshot_ref,
                    "entity": facts["entity"],
                    "unit": facts["unit"],
                    "revision": revision,
                    "sequence": int(watermark.sequence) + (1 if case == "watermark" else 0),
                    "hash": bytes(watermark.hash)
                    if case != "watermark"
                    else bytes.fromhex("00" * 32),
                    "now": datetime.now(UTC),
                    "audit": audit,
                },
            )

    _assert_legacy_upgrade_rejected(seed, message=message)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("rotation_null", "existing encrypted blob rotation mode is invalid"),
        ("rewrap", "existing encrypted blob audit or lineage is invalid"),
    ],
)
def test_r1_legacy_blob_upgrade_rejects_unclosed_rotation_lineage(case: str, message: str) -> None:
    def seed(connection: Connection) -> None:
        facts = _legacy_candidate_seed(connection)
        blob_ref = uuid4()
        object_ref = blob_ref.hex * 2
        digest = bytes.fromhex("cd" * 32)
        storage_key = "sha256/cd/cd/" + digest.hex()
        payload: dict[str, object] = {
            "rotation_mode": None if case == "rotation_null" else "REWRAP",
            "blob_ref": str(blob_ref),
            "evidence_ref": str(facts["evidence"]),
            "predecessor_blob_ref": None,
            "object_ref": object_ref,
            "ciphertext_sha256": digest.hex(),
            "ciphertext_size": 1,
            "storage_key": storage_key,
            "envelope_schema": "ledgerbridge.secretstream.v1",
            "algorithm": "xchacha20poly1305-secretstream",
            "chunk_size": 1,
            "stream_header": "11" * 24,
            "wrapped_key_generation": "generation-1",
            "wrapped_key_nonce": "22" * 24,
            "wrapped_key_ciphertext": "33" * 48,
            "purpose": "ledgerbridge-artifact-v2",
        }
        audit = _append_audit_event(connection, "evidence.blob.version", payload)
        connection.execute(
            text(
                "INSERT INTO public.encrypted_blob_version "
                "(blob_ref, evidence_ref, predecessor_blob_ref, object_ref, ciphertext_sha256, "
                "ciphertext_size, storage_key, envelope_schema, algorithm, chunk_size, stream_header, "
                "wrapped_key_generation, wrapped_key_nonce, wrapped_key_ciphertext, purpose, audit_event_id) "
                "VALUES (:blob, :evidence, NULL, :object_ref, :digest, 1, :storage_key, "
                "'ledgerbridge.secretstream.v1', 'xchacha20poly1305-secretstream', 1, :header, "
                "'generation-1', :nonce, :wrapped, 'ledgerbridge-artifact-v2', :audit)"
            ),
            {
                "blob": blob_ref,
                "evidence": facts["evidence"],
                "object_ref": object_ref,
                "digest": digest,
                "storage_key": storage_key,
                "header": bytes.fromhex("11" * 24),
                "nonce": bytes.fromhex("22" * 24),
                "wrapped": bytes.fromhex("33" * 48),
                "audit": audit,
            },
        )
        if case == "rewrap":
            child = uuid4()
            child_digest = bytes.fromhex("ef" * 32)
            child_key = "sha256/ef/ef/" + child_digest.hex()
            child_payload = payload | {
                "rotation_mode": "REWRAP",
                "blob_ref": str(child),
                "predecessor_blob_ref": str(blob_ref),
                "object_ref": child.hex * 2,
                "ciphertext_sha256": child_digest.hex(),
                "storage_key": child_key,
            }
            child_audit = _append_audit_event(connection, "evidence.blob.version", child_payload)
            connection.execute(
                text(
                    "INSERT INTO public.encrypted_blob_version "
                    "(blob_ref, evidence_ref, predecessor_blob_ref, object_ref, ciphertext_sha256, "
                    "ciphertext_size, storage_key, envelope_schema, algorithm, chunk_size, stream_header, "
                    "wrapped_key_generation, wrapped_key_nonce, wrapped_key_ciphertext, purpose, audit_event_id) "
                    "VALUES (:blob, :evidence, :predecessor, :object_ref, :digest, 1, :storage_key, "
                    "'ledgerbridge.secretstream.v1', 'xchacha20poly1305-secretstream', 1, :header, "
                    "'generation-1', :nonce, :wrapped, 'ledgerbridge-artifact-v2', :audit)"
                ),
                {
                    "blob": child,
                    "evidence": facts["evidence"],
                    "predecessor": blob_ref,
                    "object_ref": child.hex * 2,
                    "digest": child_digest,
                    "storage_key": child_key,
                    "header": bytes.fromhex("44" * 24),
                    "nonce": bytes.fromhex("55" * 24),
                    "wrapped": bytes.fromhex("66" * 48),
                    "audit": child_audit,
                },
            )

    _assert_legacy_upgrade_rejected(seed, message=message)


@pytest.mark.parametrize("role", ["ledgerbridge_reader", "ledgerbridge_api", "ledgerbridge_worker"])
def test_r1_acl_upgrade_rejects_privileged_reader_or_runtime_role(role: str) -> None:
    with _hardened_r1_database() as database_url, _temporarily_privileged_role(database_url, role):
        _assert_head_upgrade_rejected(
            database_url,
            message=(
                "ledgerbridge_reader must be an unprivileged"
                if role == "ledgerbridge_reader"
                else "runtime role has unexpected privilege"
            ),
        )


def test_r1_acl_upgrade_rejects_privileged_backup_role() -> None:
    with _hardened_r1_database() as database_url, _temporary_backup_role(database_url, "SUPERUSER"):
        _assert_head_upgrade_rejected(
            database_url,
            message="runtime role has unexpected privilege or inheritance: ledgerbridge_backup",
        )


def test_r1_clean_backup_role_receives_connect_only() -> None:
    with _hardened_r1_database() as database_url, _temporary_backup_role(database_url, ""):
        command.upgrade(_upgrade_config(database_url), "head")
        with create_engine(database_url).connect() as connection:
            assert _has_db_privilege(connection, "ledgerbridge_backup", "CONNECT")
            assert not _has_db_privilege(connection, "ledgerbridge_backup", "TEMPORARY")
            assert not _has_db_privilege(connection, "ledgerbridge_backup", "CREATE")
            assert not connection.execute(
                text("SELECT has_schema_privilege('ledgerbridge_backup', 'internal_read', 'USAGE')")
            ).scalar_one()
            assert not connection.execute(
                text(
                    "SELECT has_table_privilege('ledgerbridge_backup', "
                    "'internal_read.candidate_current_v', 'SELECT')"
                )
            ).scalar_one()


@pytest.mark.parametrize(
    "role",
    ["ledgerbridge_reader", "ledgerbridge_api", "ledgerbridge_worker", "ledgerbridge_app"],
)
def test_r1_acl_upgrade_rejects_owner_runtime_membership(role: str) -> None:
    with (
        _hardened_r1_database() as database_url,
        _temporarily_runtime_membership(database_url, role),
    ):
        _assert_head_upgrade_rejected(
            database_url,
            message=(
                "ledgerbridge_reader has unexpected bidirectional role membership"
                if role == "ledgerbridge_reader"
                else "runtime roles must not have role membership"
            ),
        )


def test_r1_acl_upgrade_rejects_stale_connect_and_public_create_overloads() -> None:
    with _hardened_r1_database() as database_url, _temporarily_stale_connect(database_url):
        _assert_head_upgrade_rejected(
            database_url,
            message="database CONNECT allowlist contains a stale principal",
        )

    with _hardened_r1_database() as database_url:
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.exec_driver_sql("GRANT CREATE ON SCHEMA public TO PUBLIC")
        _assert_head_upgrade_rejected(
            database_url,
            message="PUBLIC must not retain CREATE on schema public",
        )
        engine.dispose()


def test_r1_acl_upgrade_rejects_default_acl_drift() -> None:
    with _hardened_r1_database() as database_url:
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO PUBLIC"
            )
        _assert_head_upgrade_rejected(
            database_url,
            message="default privileges grant PUBLIC or a runtime role",
        )
        engine.dispose()


def test_r1_no_reader_0013_0014_0013_round_trip_is_supported() -> None:
    with _legacy_r1_database(reader=False) as database_url:
        command.upgrade(_upgrade_config(database_url), "20260824_0014")
        command.downgrade(_upgrade_config(database_url), "20260824_0013")
        with create_engine(database_url).connect() as connection:
            assert (
                connection.execute(
                    text("SELECT to_regclass('public.journal_entry_attribution')")
                ).scalar_one()
                is not None
            )
            assert (
                connection.execute(
                    text("SELECT to_regclass('public.encrypted_object_identity')")
                ).scalar_one()
                is None
            )


def test_r1_0014_0015_downgrade_restores_0013_candidate_contract_width() -> None:
    with _legacy_r1_database(reader=True) as database_url:
        config = _upgrade_config(database_url)
        command.upgrade(config, "20260824_0015")
        command.downgrade(config, "20260824_0013")

        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT character_maximum_length "
                            "FROM information_schema.columns "
                            "WHERE table_schema = 'public' "
                            "AND table_name = 'candidate' "
                            "AND column_name = 'contract_version'"
                        )
                    ).scalar_one()
                    == 32
                )
                entity_id = uuid4()
                connection.execute(
                    text(
                        "INSERT INTO public.entity (id, entity_type, name) "
                        "VALUES (:id, 'COMPANY', 'contract round-trip entity')"
                    ),
                    {"id": entity_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO public.candidate "
                        "(id, short_id, entity_id, contract_version, created_at) "
                        "VALUES (:id, 'C-R1RT', :entity, 'ledgerbridge.candidate.v1', "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {"id": uuid4(), "entity": entity_id},
                )
        finally:
            engine.dispose()


def test_r1_downgrade_rejects_a_fact_using_0013_added_reconciliation_columns() -> None:
    with _legacy_r1_database(reader=False) as database_url:
        engine = create_engine(database_url)
        with engine.begin() as connection:
            facts = _legacy_candidate_seed(connection)
            artifact_id = uuid4()
            job_id = uuid4()
            source_record_id = uuid4()
            digest = bytes.fromhex("ef" * 32)
            storage_key = "sha256/ef/ef/" + digest.hex()
            artifact_audit = _append_audit_event(
                connection,
                "artifact.ingest",
                {
                    "sha256": digest.hex(),
                    "byte_size": 3,
                    "storage_key": storage_key,
                    "source": "manual_upload",
                    "original_filename_sha256": hashlib.sha256(b"downgrade.csv").hexdigest(),
                    "media_type": "text/csv",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO public.raw_artifact "
                    "(id, sha256, source, original_filename, media_type, byte_size, storage_key, "
                    "audit_event_id) VALUES (:id, :digest, 'manual_upload', 'downgrade.csv', "
                    "'text/csv', 3, :storage_key, :audit)"
                ),
                {
                    "id": artifact_id,
                    "digest": digest,
                    "storage_key": storage_key,
                    "audit": artifact_audit,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO public.import_job "
                    "(id, artifact_id, connector_name, connector_version, source_system, status) "
                    "VALUES (:job, :artifact, 'legacy', '1', 'synthetic', 'PENDING')"
                ),
                {"job": job_id, "artifact": artifact_id},
            )
            connection.execute(
                text(
                    "INSERT INTO public.source_record "
                    "(id, artifact_id, import_job_id, record_locator, source, parser_version, "
                    "raw_fields, normalized_fields) VALUES (:id, :artifact, :job, 'downgrade-1', "
                    "'synthetic', '1', '{}'::jsonb, '{}'::jsonb)"
                ),
                {"id": source_record_id, "artifact": artifact_id, "job": job_id},
            )
            review_id = uuid4()
            review_audit = _append_audit_event(
                connection,
                "review.create",
                {"review_item_id": str(review_id), "kind": "RECONCILIATION"},
            )
            connection.execute(
                text(
                    "INSERT INTO public.review_item "
                    "(id, kind, status, source_record_id, summary, payload, audit_event_id) "
                    "VALUES (:id, 'RECONCILIATION', 'OPEN', :source, 'downgrade review', "
                    "'{}'::jsonb, :audit)"
                ),
                {"id": review_id, "source": source_record_id, "audit": review_audit},
            )
            account_id = uuid4()
            entry_id = uuid4()
            posting_id = uuid4()
            entry_audit = _append_audit_event(connection, "journal.entry.create", {})
            posted_audit = _append_audit_event(connection, "journal.entry.post", {})
            connection.exec_driver_sql(
                "ALTER TABLE public.journal_entry DISABLE TRIGGER USER; "
                "ALTER TABLE public.posting DISABLE TRIGGER USER; "
                "ALTER TABLE public.journal_entry_attribution DISABLE TRIGGER USER; "
                "ALTER TABLE public.posting_attribution DISABLE TRIGGER USER"
            )
            connection.execute(
                text(
                    "INSERT INTO public.account "
                    "(id, entity_id, identifier, name, account_class) "
                    "VALUES (:account, :entity, 'downgrade-leg', 'Downgrade Leg', 'EXPENSE')"
                ),
                {"account": account_id, "entity": facts["entity"]},
            )
            connection.execute(
                text(
                    "INSERT INTO public.journal_entry "
                    "(id, entity_id, occurred_at, origin, status, primary_account_id, "
                    "audit_event_id, posted_audit_event_id) VALUES "
                    "(:entry, :entity, :occurred, 'legacy', 'POSTED', :account, :audit, :posted)"
                ),
                {
                    "entry": entry_id,
                    "entity": facts["entity"],
                    "occurred": datetime.now(UTC),
                    "account": account_id,
                    "audit": entry_audit,
                    "posted": posted_audit,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO public.journal_entry_attribution "
                    "(entry_id, entity_id, business_unit_id, accounting_month) "
                    "VALUES (:entry, :entity, :unit, DATE '2026-08-01')"
                ),
                {"entry": entry_id, "entity": facts["entity"], "unit": facts["unit"]},
            )
            connection.execute(
                text(
                    "INSERT INTO public.posting "
                    "(id, entry_id, account_id, amount_minor, currency) "
                    "VALUES (:posting, :entry, :account, 1, 'CNY')"
                ),
                {"posting": posting_id, "entry": entry_id, "account": account_id},
            )
            connection.execute(
                text(
                    "INSERT INTO public.posting_attribution "
                    "(posting_id, reporting_category_id, category_code_snapshot, "
                    "category_label_snapshot) VALUES (:posting, :category, "
                    "'legacy-category', 'Legacy Category')"
                ),
                {"posting": posting_id, "category": facts["category"]},
            )
            connection.exec_driver_sql(
                "ALTER TABLE public.posting_attribution ENABLE TRIGGER USER; "
                "ALTER TABLE public.journal_entry_attribution ENABLE TRIGGER USER; "
                "ALTER TABLE public.posting ENABLE TRIGGER USER; "
                "ALTER TABLE public.journal_entry ENABLE TRIGGER USER"
            )
            group_id = uuid4()
            connection.exec_driver_sql(
                "ALTER TABLE public.reconciliation_group DISABLE TRIGGER USER; "
                "ALTER TABLE public.reconciliation_leg DISABLE TRIGGER USER"
            )
            connection.execute(
                text(
                    "INSERT INTO public.reconciliation_group "
                    "(id, review_item_id, relation, status, currency) VALUES "
                    "(:group, :review, '1:1', 'PROPOSED', 'CNY')"
                ),
                {"group": group_id, "review": review_id},
            )
            connection.execute(
                text(
                    "INSERT INTO public.reconciliation_leg "
                    "(id, reconciliation_group_id, source_record_id, amount_minor, currency, "
                    "posting_id, is_primary, entity_id, business_unit_id, accounting_month) VALUES "
                    "(:leg, :group, :source, 1, 'CNY', :posting, true, :entity, :unit, "
                    "DATE '2026-08-01')"
                ),
                {
                    "leg": uuid4(),
                    "group": group_id,
                    "source": source_record_id,
                    "posting": posting_id,
                    "entity": facts["entity"],
                    "unit": facts["unit"],
                },
            )
            connection.exec_driver_sql(
                "ALTER TABLE public.reconciliation_leg ENABLE TRIGGER USER; "
                "ALTER TABLE public.reconciliation_group ENABLE TRIGGER USER"
            )
        engine.dispose()
        with pytest.raises(RuntimeError, match="R1 reconciliation-leg columns contain data"):
            command.downgrade(_upgrade_config(database_url), "20260824_0012")


def test_r1_parent_audit_commit_then_child_rejection_keeps_horizon_fixed(
    isolated_r1_database: str,
) -> None:
    engine = create_engine(isolated_r1_database)
    with engine.begin() as connection:
        facts = _seed_read_facts(connection)
    event_ref = uuid4()
    operation_id = uuid4()
    payload = {
        "event_ref": str(event_ref),
        "candidate_id": str(facts["candidate"]),
        "candidate_ref": str(facts["candidate"]),
        "operation_id": str(operation_id),
        "command_fingerprint": (operation_id.bytes * 2).hex(),
        "event_type": "CONFIRM",
        "action": "CONFIRM",
        "from_revision": 1,
        "to_revision": 2,
        "from_status": "PENDING",
        "to_status": "CONFIRMED",
        "field_changes": [],
        "conflict_resolutions": [],
        "actor_ref": "r1-test",
        "reason": "committed parent audit",
        "derived_candidate_id": None,
    }
    with engine.begin() as connection:
        audit_event = _append_audit_event(connection, "candidate.transition", payload)
    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT sequence, hash FROM public.audit_event ORDER BY sequence DESC LIMIT 1")
        ).one()
    child_now = datetime.now(UTC)
    _assert_db_rejection(
        engine,
        [
            (
                "INSERT INTO public.candidate_revision "
                "(candidate_id, revision, status, business_unit_id, "
                "business_unit_ref_snapshot, business_unit_label_snapshot, category_id, "
                "category_code_snapshot, category_label_snapshot, amount_minor, currency, "
                "accounting_month, summary, confidence_basis_points, created_at, updated_at) "
                "VALUES (:candidate, 2, 'CONFIRMED', :unit, 'unit-a', 'Unit A', :category, "
                "'category-a', 'Category A', 1234, 'CNY', DATE '2026-08-01', "
                "'committed parent child', 100, :now, :now)",
                {
                    "candidate": facts["candidate"],
                    "unit": facts["unit"],
                    "category": facts["category"],
                    "now": child_now,
                },
            ),
            (
                "INSERT INTO public.candidate_event "
                "(event_ref, candidate_id, operation_id, command_fingerprint, event_type, action, "
                "from_revision, to_revision, from_status, to_status, actor_ref, reason, "
                "occurred_at, audit_event_id) VALUES (:event, :candidate, :operation, :fingerprint, "
                "'CONFIRM', 'CONFIRM', 1, 2, 'PENDING', 'CONFIRMED', 'r1-test', "
                "'committed parent child', :now, :audit)",
                {
                    "event": event_ref,
                    "candidate": facts["candidate"],
                    "operation": operation_id,
                    "fingerprint": operation_id.bytes * 2,
                    "now": child_now,
                    "audit": audit_event,
                },
            ),
        ],
        sqlstate="23000",
        message="candidate event and its audited children must share one transaction",
    )
    with engine.connect() as connection:
        after = connection.execute(
            text("SELECT sequence, hash FROM public.audit_event ORDER BY sequence DESC LIMIT 1")
        ).one()
    assert (after.sequence, bytes(after.hash)) == (before.sequence, bytes(before.hash))
    engine.dispose()


def test_r1_reader_role_and_database_acl_are_minimal(isolated_r1_database: str) -> None:
    with create_engine(isolated_r1_database).connect() as connection:
        role = connection.execute(
            text(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
                "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = 'ledgerbridge_reader'"
            )
        ).one()
        assert tuple(role) == (True, False, False, False, False, False, False)
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM pg_auth_members AS m "
                    "JOIN pg_roles AS member ON member.oid = m.member "
                    "JOIN pg_roles AS granted ON granted.oid = m.roleid "
                    "WHERE member.rolname = 'ledgerbridge_reader' "
                    "OR granted.rolname = 'ledgerbridge_reader'"
                )
            ).scalar_one()
            == 0
        )
        assert connection.execute(
            text(
                "SELECT current_user NOT IN "
                "('ledgerbridge_reader', 'ledgerbridge_api', 'ledgerbridge_worker', "
                "'ledgerbridge_app')"
            )
        ).scalar_one()
        # Runtime roles retain only CONNECT; owner remains the only database-wide
        # principal and the runtime roles have no TEMPORARY/CREATE capability.
        for role_name in (
            "ledgerbridge_reader",
            "ledgerbridge_api",
            "ledgerbridge_worker",
            "ledgerbridge_app",
        ):
            assert _has_db_privilege(connection, role_name, "CONNECT")
            assert not _has_db_privilege(connection, role_name, "TEMPORARY")
            assert not _has_db_privilege(connection, role_name, "CREATE")

        public_acl = connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM aclexplode(COALESCE(d.datacl, '{}'::aclitem[])) "
                "WHERE grantee = 0 AND privilege_type IN ('CONNECT', 'TEMPORARY', 'CREATE')) "
                "FROM pg_database AS d WHERE d.datname = current_database()"
            )
        ).scalar_one()
        assert public_acl is False


def test_r1_internal_read_objects_are_fixed_owner_security_definer_and_fail_closed(
    isolated_r1_database: str,
) -> None:
    with create_engine(isolated_r1_database).connect() as connection:
        views = connection.execute(
            text(
                "SELECT c.relname, pg_get_userbyid(c.relowner), coalesce(c.reloptions, '{}') "
                "FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'internal_read' AND c.relkind = 'v' ORDER BY c.relname"
            )
        ).all()
        assert {row[0] for row in views} == set(INTERNAL_READ_VIEWS)
        for _name, owner, options in views:
            assert owner not in RUNTIME_ROLES
            option_text = ",".join(options)
            assert "security_barrier=true" in option_text
            assert "security_invoker=false" in option_text

        functions = connection.execute(
            text(
                "SELECT p.proname, pg_get_function_identity_arguments(p.oid), "
                "pg_get_userbyid(p.proowner), p.prosecdef, "
                "coalesce(p.proconfig, '{}') "
                "FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'internal_read' ORDER BY p.proname"
            )
        ).all()
        assert {(row[0], row[1]) for row in functions} == set(
            INTERNAL_READ_FUNCTION_IDENTITIES.items()
        )
        for _name, _identity, owner, security_definer, config in functions:
            assert owner not in RUNTIME_ROLES
            assert security_definer is True
            assert any(str(setting) == "search_path=pg_catalog" for setting in config)

        assert connection.execute(
            text("SELECT has_schema_privilege('ledgerbridge_reader', 'internal_read', 'USAGE')")
        ).scalar_one()
        assert connection.execute(
            text("SELECT has_schema_privilege('ledgerbridge_api', 'internal_read', 'USAGE')")
        ).scalar_one()
        for role_name in (
            "ledgerbridge_worker",
            "ledgerbridge_app",
            "ledgerbridge_backup",
        ):
            if (
                role_name == "ledgerbridge_backup"
                and not connection.execute(
                    text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                    {"role": role_name},
                ).scalar_one()
            ):
                continue
            assert not connection.execute(
                text("SELECT has_schema_privilege(:role, 'internal_read', 'USAGE')"),
                {"role": role_name},
            ).scalar_one()
        assert not connection.execute(
            text("SELECT has_schema_privilege('ledgerbridge_reader', 'public', 'CREATE')")
        ).scalar_one()

        for view_name in INTERNAL_READ_VIEWS:
            assert not connection.execute(
                text("SELECT has_table_privilege('ledgerbridge_reader', :object_name, 'SELECT')"),
                {"object_name": f"internal_read.{view_name}"},
            ).scalar_one()
            for role_name in (
                "ledgerbridge_api",
                "ledgerbridge_worker",
                "ledgerbridge_app",
                "ledgerbridge_backup",
            ):
                if (
                    role_name == "ledgerbridge_backup"
                    and not connection.execute(
                        text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                        {"role": role_name},
                    ).scalar_one()
                ):
                    continue
                assert not connection.execute(
                    text("SELECT has_table_privilege(:role, :object_name, 'SELECT')"),
                    {"role": role_name, "object_name": f"internal_read.{view_name}"},
                ).scalar_one()

        for function_name in INTERNAL_READ_FUNCTIONS:
            function_oid = connection.execute(
                text(
                    "SELECT p.oid FROM pg_proc AS p JOIN pg_namespace AS n "
                    "ON n.oid = p.pronamespace WHERE n.nspname = 'internal_read' "
                    "AND p.proname = :name "
                    "AND pg_get_function_identity_arguments(p.oid) = :identity_arguments"
                ),
                {
                    "name": function_name,
                    "identity_arguments": INTERNAL_READ_FUNCTION_IDENTITIES[function_name],
                },
            ).scalar_one()
            assert bool(
                connection.execute(
                    text("SELECT has_function_privilege('ledgerbridge_reader', :oid, 'EXECUTE')"),
                    {"oid": function_oid},
                ).scalar_one()
            ) is (function_name != RECEIPT_FUNCTION)
            for role_name in (
                "ledgerbridge_api",
                "ledgerbridge_worker",
                "ledgerbridge_app",
                "ledgerbridge_backup",
            ):
                if (
                    role_name == "ledgerbridge_backup"
                    and not connection.execute(
                        text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                        {"role": role_name},
                    ).scalar_one()
                ):
                    continue
                assert bool(
                    connection.execute(
                        text("SELECT has_function_privilege(:role, :oid, 'EXECUTE')"),
                        {"role": role_name, "oid": function_oid},
                    ).scalar_one()
                ) is (role_name == "ledgerbridge_api" and function_name == RECEIPT_FUNCTION)
            assert not connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM aclexplode(COALESCE(p.proacl, '{}'::aclitem[])) "
                    "WHERE grantee = 0 AND privilege_type = 'EXECUTE') "
                    "FROM pg_proc AS p WHERE p.oid = :oid"
                ),
                {"oid": function_oid},
            ).scalar_one()

        base_tables = connection.execute(
            text(
                "SELECT c.oid, c.relname FROM pg_class AS c "
                "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')"
            )
        ).all()
        for oid, _name in base_tables:
            assert not connection.execute(
                text("SELECT has_table_privilege('ledgerbridge_reader', :oid, 'SELECT')"),
                {"oid": oid},
            ).scalar_one()
        assert not connection.execute(
            text(
                "SELECT has_function_privilege('ledgerbridge_reader', "
                "'public.append_audit_event(text,text,text,text,jsonb)'::regprocedure, 'EXECUTE')"
            )
        ).scalar_one()
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not connection.execute(
                text(
                    "SELECT has_table_privilege('ledgerbridge_api', "
                    "'internal_read.evidence_read_receipt', :privilege)"
                ),
                {"privilege": privilege},
            ).scalar_one()

        for oid, _name in connection.execute(
            text(
                "SELECT c.oid, c.relname FROM pg_class AS c "
                "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'S'"
            )
        ).all():
            assert not connection.execute(
                text("SELECT has_sequence_privilege('ledgerbridge_reader', :oid, 'USAGE')"),
                {"oid": oid},
            ).scalar_one()


def test_r1_receipt_function_is_only_callable_by_trusted_api_writer(
    isolated_r1_database: str,
) -> None:
    engine = create_engine(isolated_r1_database)
    with engine.begin() as connection:
        facts = _seed_read_facts(connection)
    receipt_parameters = {
        "operation_id": uuid4(),
        "principal_ref": "principal-r1",
        "principal_san_uri": "spiffe://ledgerbridge/r1-reader",
        "policy_generation": "generation-1",
        "evidence_ref": facts["evidence"],
        "entity_ref": facts["entity"],
        "business_unit_id": facts["unit"],
        "blob_ref": facts["active_blob"],
        "byte_size": 7,
        "plaintext_sha256": bytes.fromhex("11" * 32),
    }

    with (
        _temporarily_runtime_membership(isolated_r1_database, "ledgerbridge_reader"),
        engine.connect() as connection,
    ):
        connection.execute(text("SET ROLE ledgerbridge_reader"))
        with pytest.raises(SQLAlchemyError) as raised:
            connection.execute(text(RECEIPT_CALL_SQL), receipt_parameters)
        assert _sqlstate(raised.value) == "42501"
        assert "permission denied" in str(getattr(raised.value, "orig", raised.value))

    with (
        _temporarily_runtime_membership(isolated_r1_database, "ledgerbridge_api"),
        engine.connect() as connection,
    ):
        connection.execute(text("SET ROLE ledgerbridge_api"))
        receipt = connection.execute(text(RECEIPT_CALL_SQL), receipt_parameters).scalar_one()
        assert isinstance(receipt, UUID)


def test_r1_internal_read_empty_database_downgrade_round_trips() -> None:
    with _fresh_head_r1_database() as isolated_r1_database:
        config = Config("alembic.ini")
        config.attributes["database_url"] = isolated_r1_database
        command.downgrade(config, "20260824_0014")
        with create_engine(isolated_r1_database).connect() as connection:
            assert (
                connection.execute(
                    text("SELECT to_regclass('internal_read.current_audit_horizon')")
                ).scalar_one()
                is None
            )
        command.upgrade(config, "head")


def test_r1_internal_read_nonempty_downgrade_is_rejected(
    isolated_r1_database: str,
) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = isolated_r1_database
    command.downgrade(config, "20260824_0015")
    engine = create_engine(isolated_r1_database)
    with engine.begin() as connection:
        _seed_nonempty_downgrade_marker(connection)
    with pytest.raises(RuntimeError, match="R1 internal-read data"):
        command.downgrade(config, "20260824_0014")


def test_company_reporting_nonempty_downgrade_preserves_immutable_snapshots(
    isolated_r1_database: str,
) -> None:
    engine = create_engine(isolated_r1_database)
    with engine.begin() as connection:
        _seed_nonempty_downgrade_marker(connection)

    config = _upgrade_config(isolated_r1_database)
    with pytest.raises(SQLAlchemyError) as raised:
        command.downgrade(config, "20260830_0023")
    assert "nonempty R1 fact database prevents destructive company-reporting downgrade" in str(
        getattr(raised.value, "orig", raised.value)
    )

    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260831_0026"
        )
        snapshot_columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'journal_entry_attribution' "
                    "AND column_name IN "
                    "('business_unit_ref_snapshot', 'business_unit_label_snapshot')"
                )
            ).scalars()
        )
        assert snapshot_columns == {
            "business_unit_ref_snapshot",
            "business_unit_label_snapshot",
        }


def test_evidence_unlock_round_trip_is_scoped_idempotent_and_projected(
    isolated_r1_database: str,
) -> None:
    engine = create_engine(isolated_r1_database)
    with engine.begin() as connection:
        facts = _seed_read_facts(connection)
        source_ref = uuid4()
        source_request = {
            "contract_version": "ledgerbridge.evidence-unlock-source.v1",
            "source_ref": str(source_ref),
            "source_evidence_ref": str(facts["evidence"]),
            "entity_ref": str(facts["entity"]),
            "business_unit_id": str(facts["unit"]),
            "actor_ref": "human:evidence-unlock-test",
            "reason": "reviewed evidence unlock integration fixture",
        }
        registered = connection.execute(
            text(
                "SELECT * FROM internal_import.register_evidence_unlock_source("
                "CAST(:request AS jsonb))"
            ),
            {"request": json.dumps(source_request)},
        ).one()
        operation_id = uuid4()
        assertion_jti = uuid4()
        identity = {
            "source_ref": str(source_ref),
            "operation_id": str(operation_id),
            "assertion_jti": str(assertion_jti),
            "actor_ref": "human:evidence-unlock-test",
            "authentication_generation": 1,
            "workload_principal_ref": "workload:evidence-api",
            "verified_san": "spiffe://ledgerbridge.test/evidence-api",
            "policy_generation": "integration-v1",
            "scope_bindings": [
                {
                    "entity_ref": str(facts["entity"]),
                    "business_unit_id": str(facts["unit"]),
                }
            ],
        }
        command_request = {
            "contract_version": "ledgerbridge.evidence-unlock-command.v1",
            **identity,
        }
        prepared = connection.execute(
            text("SELECT * FROM internal_command.prepare_evidence_unlock(CAST(:request AS jsonb))"),
            {"request": json.dumps(command_request)},
        ).one()
        output_ref = uuid4()
        output = {
            "evidence_ref": str(output_ref),
            "media_type": "application/pdf",
            "display_name": "unlocked.pdf",
            "object_ref": "aa" * 32,
            "plaintext_sha256": "bb" * 32,
            "plaintext_size": 10,
            "ciphertext_sha256": "cc" * 32,
            "ciphertext_size": 20,
            "storage_key": "sha256/cc/cc/" + "cc" * 32,
            "chunk_size": 65536,
            "stream_header": "dd" * 24,
            "wrapped_key_generation": "integration-generation",
            "wrapped_key_nonce": "ee" * 24,
            "wrapped_key_ciphertext": "ff" * 48,
        }
        completion_request = {
            "contract_version": "ledgerbridge.evidence-unlock-completion.v1",
            **identity,
            "outputs": [output],
        }
        completed = connection.execute(
            text(
                "SELECT * FROM internal_command.complete_evidence_unlock(CAST(:request AS jsonb))"
            ),
            {"request": json.dumps(completion_request)},
        ).one()
        replay = connection.execute(
            text(
                "SELECT outcome FROM internal_command.prepare_evidence_unlock("
                "CAST(:request AS jsonb))"
            ),
            {"request": json.dumps(command_request)},
        ).scalar_one()
        horizon = connection.execute(
            text("SELECT sequence, hash FROM public.audit_event ORDER BY sequence DESC LIMIT 1")
        ).one()
        evidence = connection.execute(
            text(
                "SELECT evidence FROM internal_read.list_candidates_as_of("
                "CAST(:entity AS uuid),CAST(:unit AS uuid),NULL,:sequence,:hash,NULL,NULL,100)"
            ),
            {
                "entity": facts["entity"],
                "unit": facts["unit"],
                "sequence": horizon.sequence,
                "hash": horizon.hash,
            },
        ).scalar_one()

        assert registered == (source_ref, facts["evidence"])
        assert prepared.outcome == "READY"
        assert completed == (source_ref, "UNLOCKED")
        assert replay == "REPLAY_UNLOCKED"
        assert [(item["unlock_status"], item["kind"]) for item in evidence] == [
            ("UNLOCKED", "ATTACHMENT"),
            ("UNLOCKED", "ATTACHMENT"),
        ]
        assert evidence[1]["evidence_ref"] == str(output_ref)

    conflicting_identity = {
        **command_request,
        "operation_id": str(uuid4()),
    }
    _assert_db_rejection(
        engine,
        [
            (
                "SELECT * FROM internal_command.prepare_evidence_unlock(CAST(:request AS jsonb))",
                {"request": json.dumps(conflicting_identity)},
            )
        ],
        sqlstate="LB005",
        message="evidence unlock assertion was reused",
    )
    out_of_scope = {
        **command_request,
        "operation_id": str(uuid4()),
        "assertion_jti": str(uuid4()),
        "scope_bindings": [
            {
                "entity_ref": str(facts["other_entity"]),
                "business_unit_id": str(facts["unit"]),
            }
        ],
    }
    _assert_db_rejection(
        engine,
        [
            (
                "SELECT * FROM internal_command.prepare_evidence_unlock(CAST(:request AS jsonb))",
                {"request": json.dumps(out_of_scope)},
            )
        ],
        sqlstate="LB004",
        message="reviewed evidence source was not found in granted scope",
    )
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM internal_command.evidence_unlock_operation "
                    "WHERE operation_id = CAST(:operation_id AS uuid)"
                ),
                {"operation_id": out_of_scope["operation_id"]},
            ).scalar_one()
            == 0
        )
    engine.dispose()


def test_r1_reader_horizon_as_of_scope_resolver_and_audit_wrapper_fail_closed() -> None:
    # The restricted owner cannot SET ROLE to an unrelated LOGIN role.  Grant
    # this membership only for the isolated read-surface session.
    with (
        _fresh_head_r1_database() as database_url,
        _temporarily_runtime_membership(database_url, "ledgerbridge_reader"),
    ):
        _exercise_r1_reader_horizon_as_of_scope_resolver(database_url)


def _exercise_r1_reader_horizon_as_of_scope_resolver(isolated_r1_database: str) -> None:
    engine = create_engine(isolated_r1_database)
    with engine.begin() as connection:
        facts = _seed_read_facts(connection)

    with engine.connect() as connection:
        connection.execute(text("SET ROLE ledgerbridge_reader"))
        horizon = connection.execute(
            text("SELECT sequence, hash FROM internal_read.current_audit_horizon()")
        ).one()
        assert int(horizon.sequence) == facts["sequence"]
        assert bytes(horizon.hash) == facts["hash"]

        # NULL business_unit_id is the explicit unassigned mode, never a wildcard.
        unassigned = connection.execute(
            text(
                "SELECT candidate_ref FROM internal_read.list_candidates_as_of("
                ":entity, NULL, NULL, :sequence, :hash, NULL, NULL, 10)"
            ),
            {"entity": facts["entity"], "sequence": horizon.sequence, "hash": horizon.hash},
        ).all()
        assert unassigned == []
        assigned_scope = connection.execute(
            text(
                "SELECT candidate_ref FROM internal_read.list_candidates_as_of("
                ":entity, :unit, NULL, :sequence, :hash, NULL, NULL, 10)"
            ),
            {
                "entity": facts["entity"],
                "unit": facts["unit"],
                "sequence": horizon.sequence,
                "hash": horizon.hash,
            },
        ).all()
        assert [row[0] for row in assigned_scope] == [facts["candidate"]]
        other_entity_scope = connection.execute(
            text(
                "SELECT candidate_ref FROM internal_read.list_candidates_as_of("
                ":other, NULL, NULL, :sequence, :hash, NULL, NULL, 10)"
            ),
            {
                "other": facts["other_entity"],
                "sequence": horizon.sequence,
                "hash": horizon.hash,
            },
        ).all()
        assert other_entity_scope == []

        reconciliation = connection.execute(
            text(
                "SELECT * FROM internal_read.get_reconciliation_as_of("
                ":entity, :unit, DATE '2026-08-01', :sequence, :hash)"
            ),
            {
                "entity": facts["entity"],
                "unit": facts["unit"],
                "sequence": horizon.sequence,
                "hash": horizon.hash,
            },
        ).all()
        assert len(reconciliation) == 1
        reconciliation_row = reconciliation[0]
        assert tuple(reconciliation_row[:4]) == (facts["entity"], "unit-a", "2026-08", 1)
        assert reconciliation_row[4] == [
            {
                "code": "MISSING_FIELD",
                "message": "fixture blocker",
                "field": "amount",
                "conflict_ref": None,
                "evidence_ref": str(facts["evidence"]),
            }
        ]
        assert reconciliation_row[5][0]["status"] == "PROPOSED"
        assert reconciliation_row[6][0]["status"] == "OPEN"
        assert tuple(reconciliation_row[7:]) == (1234, "CNY")

        ledger_summary = connection.execute(
            text(
                "SELECT * FROM internal_read.get_ledger_summary_as_of("
                ":entity, :unit, DATE '2026-08-01', DATE '2026-08-01', :sequence, :hash)"
            ),
            {
                "entity": facts["entity"],
                "unit": facts["unit"],
                "sequence": horizon.sequence,
                "hash": horizon.hash,
            },
        ).all()
        # The fixture has no POSTED journal entries; the scoped aggregate must
        # therefore return an empty result rather than infer totals from the
        # reconciliation snapshot.
        assert ledger_summary == []

        active = connection.execute(
            text("SELECT blob_ref FROM internal_read.resolve_active_evidence_blob(:evidence)"),
            {"evidence": facts["evidence"]},
        ).one()
        assert active[0] == facts["active_blob"]
        assert active[0] != facts["old_blob"]

        _assert_db_rejection(
            engine,
            [
                ("SET ROLE ledgerbridge_reader", None),
                (
                    "SELECT * FROM internal_read.list_candidates_as_of("
                    ":entity, NULL, NULL, :sequence, :hash, NULL, NULL, 10)",
                    {
                        "entity": facts["entity"],
                        "sequence": int(horizon.sequence) + 1,
                        "hash": horizon.hash,
                    },
                ),
            ],
            sqlstate="22023",
            message="audit horizon is not an exact chain row",
        )
        _assert_db_rejection(
            engine,
            [
                ("SET ROLE ledgerbridge_reader", None),
                (
                    "SELECT * FROM internal_read.get_reconciliation_as_of("
                    ":entity, :unit, DATE '2026-08-01', :sequence, :hash)",
                    {
                        "entity": facts["other_entity"],
                        "unit": facts["unit"],
                        "sequence": horizon.sequence,
                        "hash": horizon.hash,
                    },
                ),
            ],
            sqlstate="22023",
            message="business unit does not belong to entity",
        )
        _assert_db_rejection(
            engine,
            [
                ("SET ROLE ledgerbridge_reader", None),
                (
                    "SELECT * FROM internal_read.get_ledger_summary_as_of("
                    ":entity, :unit, DATE '2026-08-01', DATE '2026-08-01', :sequence, :hash)",
                    {
                        "entity": facts["other_entity"],
                        "unit": facts["unit"],
                        "sequence": horizon.sequence,
                        "hash": horizon.hash,
                    },
                ),
            ],
            sqlstate="22023",
            message="business unit does not belong to entity",
        )


def test_r1_fact_hardening_identity_history_and_write_acl_invariants(
    isolated_r1_database: str,
) -> None:
    with create_engine(isolated_r1_database).connect() as connection:
        # Every blob object has one immutable, composite lineage identity.
        assert connection.execute(
            text(
                "SELECT count(*) = 0 FROM public.encrypted_blob_version AS b "
                "LEFT JOIN public.encrypted_object_identity AS i "
                "ON i.object_ref = b.object_ref AND i.evidence_ref = b.evidence_ref "
                "WHERE i.object_ref IS NULL"
            )
        ).scalar_one()
        assert connection.execute(
            text(
                "SELECT count(*) = count(DISTINCT candidate_id) FROM public.candidate_event "
                "WHERE event_type = 'CREATE'"
            )
        ).scalar_one()
        assert connection.execute(
            text(
                "SELECT count(*) = 0 FROM public.candidate_event AS e "
                "LEFT JOIN public.audit_event AS a ON a.id = e.audit_event_id "
                "WHERE e.event_type = 'CREATE' AND a.id IS NULL"
            )
        ).scalar_one()

        for table_name in (
            "encrypted_object_identity",
            "reconciliation_leg",
            "journal_entry_attribution",
            "posting_attribution",
            "reconciliation_snapshot",
            "reconciliation_snapshot_blocker",
            "reconciliation_snapshot_proposal",
            "reconciliation_snapshot_suspense",
        ):
            assert (
                connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_class AS c "
                        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                        "CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, '{}'::aclitem[])) AS a "
                        "WHERE n.nspname = 'public' AND c.relname = :table_name AND a.grantee = 0 "
                        "AND a.privilege_type IN ('INSERT', 'UPDATE', 'DELETE'))"
                    ),
                    {"table_name": table_name},
                ).scalar_one()
                is False
            )
            assert (
                connection.execute(
                    text(
                        "SELECT has_table_privilege('ledgerbridge_reader', :table_name, 'INSERT') "
                        "OR has_table_privilege('ledgerbridge_reader', :table_name, 'UPDATE') "
                        "OR has_table_privilege('ledgerbridge_reader', :table_name, 'DELETE')"
                    ),
                    {"table_name": f"public.{table_name}"},
                ).scalar_one()
                is False
            )
            for role_name in ("ledgerbridge_api", "ledgerbridge_worker", "ledgerbridge_app"):
                assert (
                    connection.execute(
                        text(
                            "SELECT has_table_privilege(:role, :table_name, 'INSERT') "
                            "OR has_table_privilege(:role, :table_name, 'UPDATE') "
                            "OR has_table_privilege(:role, :table_name, 'DELETE')"
                        ),
                        {"role": role_name, "table_name": f"public.{table_name}"},
                    ).scalar_one()
                    is False
                )

        assert connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname LIKE '%primary%')"
            )
        ).scalar_one()
