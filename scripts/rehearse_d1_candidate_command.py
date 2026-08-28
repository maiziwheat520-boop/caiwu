"""Exercise the D1 migration and command surface against a disposable PostgreSQL 15 DB."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from alembic import command


def _required_url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _upgrade(url: str, revision: str) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, revision)


def _append_audit(connection: Any, action: str, payload: dict[str, object]) -> UUID:
    return connection.execute(
        text(
            "SELECT public.append_audit_event(:actor, :action, :reason, :rule_version, "
            "CAST(:payload AS jsonb))"
        ),
        {
            "actor": "d1-rehearsal",
            "action": action,
            "reason": "isolated D1 rehearsal fixture",
            "rule_version": "d1-rehearsal-v1",
            "payload": json.dumps(payload, separators=(",", ":")),
        },
    ).scalar_one()


def _seed_pending_candidate(owner_url: str) -> dict[str, UUID]:
    entity_id = uuid4()
    unit_id = uuid4()
    category_id = uuid4()
    evidence_ref = uuid4()
    candidate_id = uuid4()
    source_event_ref = uuid4()
    create_operation = uuid4()
    create_event = uuid4()
    now = datetime.now(UTC)
    engine = create_engine(owner_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO public.entity (id, entity_type, name) "
                    "VALUES (:id, 'COMPANY', 'D1 rehearsal entity')"
                ),
                {"id": entity_id},
            )
            connection.execute(
                text(
                    "INSERT INTO public.business_unit (id, entity_id, ref, label) "
                    "VALUES (:id, :entity, 'd1-rehearsal-unit', 'D1 Rehearsal Unit')"
                ),
                {"id": unit_id, "entity": entity_id},
            )
            connection.execute(
                text(
                    "INSERT INTO public.reporting_category (id, entity_id, code, label) "
                    "VALUES (:id, :entity, 'd1-rehearsal-category', 'D1 Rehearsal Category')"
                ),
                {"id": category_id, "entity": entity_id},
            )
            evidence_audit = _append_audit(
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
                    "(evidence_ref, entity_id, business_unit_id, media_type, display_name, "
                    "plaintext_sha256, plaintext_size, audit_event_id) VALUES "
                    "(:evidence, :entity, :unit, 'application/pdf', 'rehearsal.pdf', "
                    ":digest, 1, :audit)"
                ),
                {
                    "evidence": evidence_ref,
                    "entity": entity_id,
                    "unit": unit_id,
                    "digest": hashlib.sha256(b"d1-rehearsal").digest(),
                    "audit": evidence_audit,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO public.candidate "
                    "(id, short_id, entity_id, contract_version, created_at) VALUES "
                    "(:id, :short_id, :entity, 'ledgerbridge.candidate.v1', :now)"
                ),
                {
                    "id": candidate_id,
                    "short_id": "C-" + candidate_id.hex[:8].upper(),
                    "entity": entity_id,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO public.candidate_source "
                    "(candidate_id, ingest_channel_id, source_system_id, source_event_ref, "
                    "display_label) VALUES "
                    "(:candidate, 'synthetic_upload', 'synthetic', :source_event, "
                    "'D1 rehearsal source')"
                ),
                {"candidate": candidate_id, "source_event": source_event_ref},
            )
            connection.execute(
                text(
                    "INSERT INTO public.candidate_revision "
                    "(candidate_id, revision, status, business_unit_id, "
                    "business_unit_ref_snapshot, business_unit_label_snapshot, category_id, "
                    "category_code_snapshot, category_label_snapshot, amount_minor, currency, "
                    "accounting_month, summary, confidence_basis_points, created_at, updated_at) "
                    "VALUES (:candidate, 1, 'PENDING', :unit, 'd1-rehearsal-unit', "
                    "'D1 Rehearsal Unit', :category, 'd1-rehearsal-category', "
                    "'D1 Rehearsal Category', 1, 'CNY', DATE '2026-08-01', "
                    "'D1 rehearsal candidate', 100, :now, :now)"
                ),
                {
                    "candidate": candidate_id,
                    "unit": unit_id,
                    "category": category_id,
                    "now": now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO public.candidate_evidence "
                    "(candidate_id, ordinal, evidence_ref, kind, media_type_snapshot, "
                    "display_name_snapshot, download_available) VALUES "
                    "(:candidate, 0, :evidence, 'ATTACHMENT', 'application/pdf', "
                    "'rehearsal.pdf', true)"
                ),
                {"candidate": candidate_id, "evidence": evidence_ref},
            )
            payload = {
                "event_ref": str(create_event),
                "candidate_id": str(candidate_id),
                "candidate_ref": str(candidate_id),
                "operation_id": str(create_operation),
                "command_fingerprint": (create_operation.bytes * 2).hex(),
                "event_type": "CREATE",
                "action": None,
                "from_revision": None,
                "to_revision": 1,
                "from_status": None,
                "to_status": "PENDING",
                "field_changes": [],
                "conflict_resolutions": [],
                "actor_ref": "d1-rehearsal",
                "reason": "D1 rehearsal candidate",
                "derived_candidate_id": None,
            }
            create_audit = _append_audit(connection, "candidate.create", payload)
            connection.execute(
                text(
                    "INSERT INTO public.candidate_event "
                    "(event_ref, candidate_id, operation_id, command_fingerprint, event_type, "
                    "to_revision, to_status, actor_ref, reason, occurred_at, audit_event_id) "
                    "VALUES (:event, :candidate, :operation, :fingerprint, 'CREATE', 1, "
                    "'PENDING', 'd1-rehearsal', 'D1 rehearsal candidate', :now, :audit)"
                ),
                {
                    "event": create_event,
                    "candidate": candidate_id,
                    "operation": create_operation,
                    "fingerprint": create_operation.bytes * 2,
                    "now": now,
                    "audit": create_audit,
                },
            )
    finally:
        engine.dispose()
    return {
        "entity": entity_id,
        "unit": unit_id,
        "candidate": candidate_id,
    }


_DECISION_SQL = text(
    "SELECT internal_command.apply_candidate_decision("
    "CAST(:operation AS uuid), CAST(:jti AS uuid), CAST(:candidate AS uuid), "
    "'human:d1-rehearsal'::varchar(200), 'workload:d1-rehearsal'::varchar(200), "
    "'spiffe://ledgerbridge.local/d1-rehearsal'::varchar(200), CAST(:entity AS uuid), "
    "CAST(:unit AS uuid), CAST(:unit AS uuid), 'CONFIRM'::varchar(32), "
    ":revision, 'confirmed in isolated rehearsal'::varchar(1000), false, "
    "NULL::varchar(100), false, NULL::varchar(100), false, NULL::bigint, false, "
    "NULL::date, NULL::varchar(1000), CAST(:decided_at AS timestamptz))"
)


def _sqlstate(error: SQLAlchemyError) -> str | None:
    return getattr(getattr(error, "orig", None), "sqlstate", None)


def _exercise(owner_url: str, api_url: str, reader_url: str, facts: dict[str, UUID]) -> None:
    operation = uuid4()
    first_jti = uuid4()
    decided_at = datetime.now(UTC)
    parameters = {
        **facts,
        "operation": operation,
        "jti": first_jti,
        "revision": 1,
        "decided_at": decided_at,
    }
    api = create_engine(api_url)
    reader = create_engine(reader_url)
    owner = create_engine(owner_url)
    try:
        with api.begin() as connection:
            receipt = connection.execute(_DECISION_SQL, parameters).scalar_one()
            assert receipt["replayed"] is False
            assert receipt["candidate"]["status"] == "CONFIRMED"
            assert receipt["candidate"]["revision"] == 2
            assert len(receipt["events"]) == 1
            assert connection.execute(
                text(
                    "SELECT NOT has_table_privilege(current_user, 'public.candidate', 'SELECT') "
                    "AND NOT has_table_privilege(current_user, "
                    "'public.candidate_revision', 'INSERT')"
                )
            ).scalar_one()
        with api.begin() as connection:
            replay = connection.execute(
                _DECISION_SQL,
                {**parameters, "jti": uuid4()},
            ).scalar_one()
            assert replay["replayed"] is True
            assert replay["candidate"]["revision"] == 2
        try:
            with api.begin() as connection:
                connection.execute(
                    _DECISION_SQL,
                    {**parameters, "operation": uuid4(), "jti": uuid4()},
                ).scalar_one()
        except SQLAlchemyError as error:
            assert _sqlstate(error) == "LB002"
        else:
            raise AssertionError("stale revision unexpectedly succeeded")
        with reader.begin() as connection:
            horizon = (
                connection.execute(
                    text("SELECT sequence, hash FROM internal_read.current_audit_horizon()")
                )
                .mappings()
                .one()
            )
            events = (
                connection.execute(
                    text(
                        "SELECT event FROM internal_read.list_candidate_events_as_of("
                        "CAST(:entity AS uuid), CAST(:unit AS uuid), CAST(:candidate AS uuid), "
                        ":sequence, :hash, 100)"
                    ),
                    {**facts, **horizon},
                )
                .scalars()
                .all()
            )
            assert len(events) == 1
            assert events[0]["action"] == "CONFIRM"
        with owner.begin() as connection:
            counts = connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM public.candidate_revision "
                    "WHERE candidate_id=:candidate), "
                    "(SELECT count(*) FROM public.candidate_event WHERE candidate_id=:candidate), "
                    "(SELECT count(*) FROM internal_command.candidate_decision_receipt), "
                    "(SELECT count(*) FROM internal_command.candidate_assertion_use)"
                ),
                facts,
            ).one()
            assert tuple(counts) == (2, 2, 1, 2)
    finally:
        api.dispose()
        reader.dispose()
        owner.dispose()
    print("D1_REHEARSAL_OK revisions=2 events=2 receipts=1 assertions=2")


def main() -> None:
    owner_url = _required_url("LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    api_url = _required_url("LEDGERBRIDGE_API_DATABASE_URL")
    reader_url = _required_url("LEDGERBRIDGE_READER_DATABASE_URL")
    _upgrade(owner_url, "20260824_0013")
    facts = _seed_pending_candidate(owner_url)
    _upgrade(owner_url, "head")
    _exercise(owner_url, api_url, reader_url, facts)


if __name__ == "__main__":
    main()
