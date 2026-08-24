from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection
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
    "list_candidates_as_of",
    "get_reconciliation_as_of",
    "resolve_active_evidence_blob",
    "append_internal_evidence_read_audit",
)
RUNTIME_ROLES = (
    "ledgerbridge_reader",
    "ledgerbridge_api",
    "ledgerbridge_worker",
    "ledgerbridge_app",
)


@pytest.fixture(scope="module")
def isolated_r1_database() -> Iterator[str]:
    """Run 0013 -> 0014 -> 0015 after external role bootstrap in a disposable database."""

    value = os.environ.get("LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    if value is None:
        pytest.skip("PostgreSQL integration tests require LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    if not MIGRATION_HARDENING.exists() or not MIGRATION_C.exists():
        pytest.skip("Migration C split (0014 hardening + 0015 reader) is supplied in parallel")

    owner_url = create_engine(value).url
    database_name = f"ledgerbridge_r1_read_{uuid4().hex[:12]}"
    maintenance_engine = create_engine(
        owner_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
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
            # silently create a login role or choose its credentials.
            for role in RUNTIME_ROLES:
                if role in existing:
                    continue
                if role == "ledgerbridge_reader":
                    password = f"r1-test-{uuid4().hex}"
                    connection.exec_driver_sql(
                        f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                    )
                else:
                    connection.exec_driver_sql(
                        f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOINHERIT NOREPLICATION NOBYPASSRLS"
                    )
                created_roles.append(role)
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))

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


def _seed_read_facts(connection: Connection) -> dict[str, UUID | int | bytes]:
    entity_id = uuid4()
    other_entity_id = uuid4()
    business_unit_id = uuid4()
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
            "INSERT INTO public.business_unit (id, entity_id, ref, label) "
            "VALUES (:id, :entity, 'unit-a', 'Unit A')"
        ),
        {"id": business_unit_id, "entity": entity_id},
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
            "to_status": "INCOMPLETE",
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
            "(candidate_id, revision, status, currency, summary, confidence_basis_points, "
            "created_at, updated_at) VALUES (:candidate, 1, 'INCOMPLETE', 'CNY', "
            "'unassigned fixture candidate', 100, :now, :now)"
        ),
        {"candidate": candidate_id, "now": now},
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
            "(:event_ref, :candidate, :operation, :fingerprint, 'CREATE', 1, 'INCOMPLETE', 'r1-test', "
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
    horizon = connection.execute(
        text("SELECT sequence, hash FROM public.audit_event ORDER BY sequence DESC LIMIT 1")
    ).one()
    return {
        "entity": entity_id,
        "other_entity": other_entity_id,
        "unit": business_unit_id,
        "evidence": evidence_ref,
        "old_blob": old_blob_ref,
        "active_blob": active_blob_ref,
        "candidate": candidate_id,
        "sequence": int(horizon.sequence),
        "hash": bytes(horizon.hash),
    }


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
        "append_internal_evidence_read_audit",
        "R1 internal-read data prevents destructive downgrade",
    ):
        assert literal in source


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
        # The owner is the only test principal allowed to retain database-wide power.
        for role_name in ("ledgerbridge_reader", "ledgerbridge_api", "ledgerbridge_worker"):
            assert _has_db_privilege(connection, role_name, "CONNECT")
            assert not _has_db_privilege(connection, role_name, "TEMPORARY")
            assert not _has_db_privilege(connection, role_name, "CREATE")
        assert not _has_db_privilege(connection, "ledgerbridge_app", "CONNECT")

        public_acl = connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM aclexplode(COALESCE(d.datacl, '{}'::aclitem[])) "
                "WHERE grantee = 0 AND privilege_type IN ('CONNECT', 'TEMPORARY', 'CREATE')) "
                "FROM pg_database AS d WHERE d.datname = current_database()"
            )
        ).scalar_one()
        assert public_acl is False


def test_r1_internal_read_objects_are_fixed_owner_security_definer_and_exactly_granted(
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
                "SELECT p.proname, pg_get_userbyid(p.proowner), p.prosecdef, "
                "coalesce(p.proconfig, '{}') "
                "FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace "
                "WHERE n.nspname = 'internal_read' ORDER BY p.proname"
            )
        ).all()
        assert {row[0] for row in functions} == set(INTERNAL_READ_FUNCTIONS)
        for _name, owner, security_definer, config in functions:
            assert owner not in RUNTIME_ROLES
            assert security_definer is True
            assert any(str(setting) == "search_path=pg_catalog" for setting in config)

        assert connection.execute(
            text("SELECT has_schema_privilege('ledgerbridge_reader', 'internal_read', 'USAGE')")
        ).scalar_one()
        for role_name in ("ledgerbridge_api", "ledgerbridge_worker", "ledgerbridge_app"):
            assert not connection.execute(
                text("SELECT has_schema_privilege(:role, 'internal_read', 'USAGE')"),
                {"role": role_name},
            ).scalar_one()
        assert not connection.execute(
            text("SELECT has_schema_privilege('ledgerbridge_reader', 'public', 'CREATE')")
        ).scalar_one()

        for view_name in INTERNAL_READ_VIEWS:
            assert connection.execute(
                text("SELECT has_table_privilege('ledgerbridge_reader', :object_name, 'SELECT')"),
                {"object_name": f"internal_read.{view_name}"},
            ).scalar_one()
            for role_name in ("ledgerbridge_api", "ledgerbridge_worker", "ledgerbridge_app"):
                assert not connection.execute(
                    text("SELECT has_table_privilege(:role, :object_name, 'SELECT')"),
                    {"role": role_name, "object_name": f"internal_read.{view_name}"},
                ).scalar_one()

        for function_name in INTERNAL_READ_FUNCTIONS:
            function_oid = connection.execute(
                text(
                    "SELECT p.oid FROM pg_proc AS p JOIN pg_namespace AS n "
                    "ON n.oid = p.pronamespace WHERE n.nspname = 'internal_read' "
                    "AND p.proname = :name"
                ),
                {"name": function_name},
            ).scalar_one()
            assert connection.execute(
                text("SELECT has_function_privilege('ledgerbridge_reader', :oid, 'EXECUTE')"),
                {"oid": function_oid},
            ).scalar_one()
            for role_name in ("ledgerbridge_api", "ledgerbridge_worker", "ledgerbridge_app"):
                assert not connection.execute(
                    text("SELECT has_function_privilege(:role, :oid, 'EXECUTE')"),
                    {"role": role_name, "oid": function_oid},
                ).scalar_one()
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


def test_r1_internal_read_empty_database_downgrade_round_trips(
    isolated_r1_database: str,
) -> None:
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
    engine = create_engine(isolated_r1_database)
    with engine.begin() as connection:
        _seed_nonempty_downgrade_marker(connection)
    config = Config("alembic.ini")
    config.attributes["database_url"] = isolated_r1_database
    with pytest.raises(RuntimeError, match="R1 internal-read data"):
        command.downgrade(config, "20260824_0014")


def test_r1_reader_horizon_as_of_scope_resolver_and_audit_wrapper_fail_closed(
    isolated_r1_database: str,
) -> None:
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
        assert [row[0] for row in unassigned] == [facts["candidate"]]
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
        assert assigned_scope == []
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
        assert reconciliation == []

        active = connection.execute(
            text("SELECT blob_ref FROM internal_read.resolve_active_evidence_blob(:evidence)"),
            {"evidence": facts["evidence"]},
        ).one()
        assert active[0] == facts["active_blob"]
        assert active[0] != facts["old_blob"]

        receipt = connection.execute(
            text(
                "SELECT internal_read.append_internal_evidence_read_audit("
                ":operation, :principal, :san, :generation, :evidence, :entity, :unit, :blob, :size, :sha)"
            ),
            {
                "operation": uuid4(),
                "principal": "principal-r1",
                "san": "spiffe://ledgerbridge/r1-reader",
                "generation": "generation-1",
                "evidence": facts["evidence"],
                "entity": facts["entity"],
                "unit": facts["unit"],
                "blob": facts["active_blob"],
                "size": 7,
                "sha": bytes.fromhex("11" * 32),
            },
        ).scalar_one()
        assert isinstance(receipt, UUID)

        with pytest.raises(SQLAlchemyError):
            connection.execute(
                text(
                    "SELECT * FROM internal_read.list_candidates_as_of("
                    ":entity, NULL, NULL, :sequence, :hash, NULL, NULL, 10)"
                ),
                {
                    "entity": facts["entity"],
                    "sequence": int(horizon.sequence) + 1,
                    "hash": horizon.hash,
                },
            )
        with pytest.raises(SQLAlchemyError):
            connection.execute(
                text(
                    "SELECT internal_read.append_internal_evidence_read_audit("
                    ":operation, :principal, :san, :generation, :evidence, :entity, :unit, :blob, :size, :sha)"
                ),
                {
                    "operation": uuid4(),
                    "principal": "principal-r1",
                    "san": "spiffe://ledgerbridge/r1-reader",
                    "generation": "generation-1",
                    "evidence": facts["evidence"],
                    "entity": facts["entity"],
                    "unit": facts["unit"],
                    "blob": facts["old_blob"],
                    "size": 7,
                    "sha": bytes.fromhex("11" * 32),
                },
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
