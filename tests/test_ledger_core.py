from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, inspect, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from ledgerbridge.db import build_engine
from ledgerbridge.ledger import actual_account_balances, actual_totals_by_class, post_journal_entry
from ledgerbridge.models import (
    Account,
    AccountClass,
    Entity,
    EntityType,
    JournalEntry,
    JournalStatus,
    Posting,
)


def _run_alembic(database_url: str, revision: str, *, downgrade: bool = False) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    if downgrade:
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)


@pytest.fixture(scope="session")
def database_url() -> str:
    value = os.environ.get("LEDGERBRIDGE_DATABASE_URL")
    if value is None:
        pytest.skip("PostgreSQL integration tests require LEDGERBRIDGE_DATABASE_URL")
    return value


@pytest.fixture(scope="session")
def migration_database_url() -> str:
    value = os.environ.get("LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    if value is None:
        pytest.skip("PostgreSQL integration tests require LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    return value


@pytest.fixture(scope="session")
def admin_engine(migration_database_url: str) -> Iterator[Engine]:
    _run_alembic(migration_database_url, "head")
    engine = create_engine(migration_database_url, pool_pre_ping=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def runtime_engine(database_url: str, admin_engine: Engine) -> Iterator[Engine]:
    del admin_engine
    engine = build_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def reset_ledger(admin_engine: Engine) -> Iterator[None]:
    with admin_engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE posting, journal_entry, account, entity, audit_event "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


def _append_audit(
    session: Session,
    action: str = "journal.create",
    payload: dict[str, object] | None = None,
) -> UUID:
    value = session.execute(
        text(
            """
            SELECT append_audit_event(
                :actor,
                :action,
                :reason,
                :rule_version,
                CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "actor": "pytest",
            "action": action,
            "reason": "synthetic acceptance fixture",
            "rule_version": None,
            "payload": json.dumps(payload or {}, sort_keys=True),
        },
    ).scalar_one()
    return cast(UUID, value)


def _create_accounts(session: Session) -> tuple[UUID, dict[str, UUID]]:
    entity = Entity(entity_type=EntityType.PERSON, name=f"Test {uuid4().hex[:8]}")
    session.add(entity)
    session.flush()

    definitions = {
        "bank": (AccountClass.ASSET, "Assets:Bank"),
        "wallet": (AccountClass.ASSET, "Assets:Wallet"),
        "card": (AccountClass.LIABILITY, "Liabilities:Card"),
        "expense": (AccountClass.EXPENSE, "Expenses:General"),
        "fee": (AccountClass.EXPENSE, "Expenses:Fee"),
        "income": (AccountClass.INCOME, "Income:General"),
    }
    accounts: dict[str, UUID] = {}
    for key, (account_class, name) in definitions.items():
        account = Account(
            entity_id=entity.id,
            identifier=f"{key}-{uuid4().hex[:8]}",
            name=name,
            account_class=account_class,
        )
        session.add(account)
        session.flush()
        accounts[key] = account.id
    session.commit()
    return entity.id, accounts


def _create_entry(
    session: Session,
    entity_id: UUID,
    postings: list[tuple[UUID, int]],
    *,
    status: JournalStatus = JournalStatus.POSTED,
    adjusts_entry_id: UUID | None = None,
    reverses_entry_id: UUID | None = None,
) -> UUID:
    entry_id = uuid4()
    entry = JournalEntry(
        id=entry_id,
        entity_id=entity_id,
        occurred_at=datetime.now(UTC),
        origin="pytest",
        status=JournalStatus.DRAFT if status == JournalStatus.POSTED else status,
        adjusts_entry_id=adjusts_entry_id,
        reverses_entry_id=reverses_entry_id,
        primary_account_id=postings[0][0],
        audit_event_id=_append_audit(
            session,
            payload={"journal_entry_id": str(entry_id)},
        ),
    )
    session.add(entry)
    session.flush()
    session.add_all(
        [
            Posting(
                entry_id=entry.id,
                account_id=account_id,
                amount_minor=amount_minor,
                currency="CNY",
            )
            for account_id, amount_minor in postings
        ]
    )
    session.flush()
    if status == JournalStatus.POSTED:
        post_journal_entry(
            session,
            entry.id,
            actor="pytest",
            reason="synthetic acceptance fixture",
        )
    session.commit()
    return entry.id


def test_runtime_login_remains_unprivileged_after_read_only_pool_reuse(
    admin_engine: Engine,
    runtime_engine: Engine,
) -> None:
    with Session(runtime_engine) as session:
        assert session.execute(text("SELECT 1")).scalar_one() == 1

    with runtime_engine.connect() as connection:
        identity = connection.execute(text("SELECT session_user, current_user")).one()
        assert identity == ("ledgerbridge_app", "ledgerbridge_app")
        connection.execute(text("RESET ROLE"))
        assert connection.execute(text("SELECT current_user")).scalar_one() == "ledgerbridge_app"

        forbidden_statements = (
            """
            INSERT INTO audit_event (
                id, sequence, occurred_at, actor, action, reason,
                rule_version, payload, prev_hash, hash
            )
            VALUES (
                gen_random_uuid(), 1, now(), 'direct', 'insert', 'forbidden',
                NULL, '{}'::jsonb, NULL, decode(repeat('00', 32), 'hex')
            )
            """,
            "ALTER TABLE posting DISABLE TRIGGER posting_posted_immutable",
            "TRUNCATE TABLE posting",
            "SET ROLE ledgerbridge_owner",
        )
        for statement in forbidden_statements:
            with pytest.raises(DBAPIError):
                connection.execute(text(statement))
            connection.rollback()

    with admin_engine.connect() as connection:
        assert connection.execute(
            text(
                """
                SELECT rolcanlogin
                    AND NOT rolsuper
                    AND NOT rolcreatedb
                    AND NOT rolcreaterole
                    AND NOT rolreplication
                    AND NOT rolbypassrls
                    AND NOT EXISTS (
                        SELECT 1
                        FROM pg_auth_members
                        WHERE member = pg_roles.oid
                    )
                    AND NOT EXISTS (
                        SELECT 1
                        FROM pg_database
                        WHERE datname = current_database()
                          AND datdba = pg_roles.oid
                    )
                FROM pg_roles
                WHERE rolname = 'ledgerbridge_app'
                """
            )
        ).scalar_one()


def test_runtime_role_cannot_create_temporary_tables(
    admin_engine: Engine,
    runtime_engine: Engine,
) -> None:
    with admin_engine.connect() as connection:
        assert not connection.execute(
            text("SELECT has_database_privilege('ledgerbridge_app', current_database(), 'TEMP')")
        ).scalar_one()

    with runtime_engine.connect() as connection:
        with pytest.raises(DBAPIError, match="permission denied"):
            connection.execute(text("CREATE TEMP TABLE forbidden_shadow (id uuid)"))
        connection.rollback()


def test_security_functions_pin_their_search_path(admin_engine: Engine) -> None:
    expected = {
        "account_block_protected_dimension_change",
        "append_audit_event",
        "audit_event_block_mutation",
        "import_job_enforce_transition",
        "journal_entry_assert_posted_complete",
        "journal_entry_block_posted_mutation",
        "journal_entry_validate_post_audit",
        "journal_entry_validate_relationships",
        "posting_assert_balanced",
        "posting_block_posted_mutation",
        "posting_enforce_entity",
        "raw_artifact_block_mutation",
        "raw_artifact_validate_audit",
        "source_record_block_mutation",
    }
    with admin_engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    """
                    SELECT p.proname, p.proconfig
                    FROM pg_proc AS p
                    JOIN pg_namespace AS n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public'
                      AND p.proname = ANY(CAST(:names AS text[]))
                    """
                ),
                {"names": sorted(expected)},
            )
            .tuples()
            .all()
        )
        configurations: dict[str, list[str]] = dict(rows)

    assert configurations.keys() == expected
    assert all(value == ["search_path=pg_catalog"] for value in configurations.values())


def _assert_phase1_invariants_ignore_pg_temp_shadow_tables(
    admin_engine: Engine,
    runtime_engine: Engine,
) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        posted_entry_id = _create_entry(
            session,
            entity_id,
            [(accounts["bank"], 500), (accounts["wallet"], -500)],
        )
        posting_ids = list(
            session.execute(
                text("SELECT id FROM public.posting WHERE entry_id = :entry_id ORDER BY id"),
                {"entry_id": posted_entry_id},
            ).scalars()
        )
        other_entity = Entity(entity_type=EntityType.COMPANY, name="Other entity")
        session.add(other_entity)
        session.flush()
        other_account = Account(
            entity_id=other_entity.id,
            identifier=f"other-{uuid4().hex[:8]}",
            name="Assets:Other",
            account_class=AccountClass.ASSET,
        )
        session.add(other_account)
        session.commit()
        other_entity_id = other_entity.id
        other_account_id = other_account.id

    with admin_engine.begin() as connection:
        connection.execute(
            text(
                """
                DO $do$
                BEGIN
                    EXECUTE format(
                        'GRANT TEMPORARY ON DATABASE %I TO ledgerbridge_app',
                        current_database()
                    );
                END
                $do$;
                """
            )
        )

    def append_audit(connection: Connection, action: str, entry_id: UUID) -> UUID:
        event_id = connection.execute(
            text(
                """
                SELECT public.append_audit_event(
                    'pytest',
                    :action,
                    'pg_temp shadow regression',
                    NULL,
                    CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "action": action,
                "payload": json.dumps(
                    {"journal_entry_id": str(entry_id)},
                    sort_keys=True,
                ),
            },
        ).scalar_one()
        return cast(UUID, event_id)

    def insert_draft(
        connection: Connection,
        entry_id: UUID,
        primary_account_id: UUID,
    ) -> None:
        creation_audit_id = append_audit(connection, "journal.create", entry_id)
        connection.execute(
            text(
                """
                INSERT INTO public.journal_entry (
                    id, entity_id, occurred_at, origin, status,
                    primary_account_id, audit_event_id
                ) VALUES (
                    :entry_id, :entity_id, clock_timestamp(),
                    'pytest', 'DRAFT', :primary_account_id, :audit_event_id
                )
                """
            ),
            {
                "entry_id": entry_id,
                "entity_id": entity_id,
                "primary_account_id": primary_account_id,
                "audit_event_id": creation_audit_id,
            },
        )

    try:
        runtime_engine.dispose()
        with runtime_engine.connect() as connection:
            for statement in (
                """
                CREATE TEMP TABLE journal_entry (
                    id uuid PRIMARY KEY,
                    entity_id uuid NOT NULL,
                    status public.journal_status NOT NULL
                )
                """,
                """
                CREATE TEMP TABLE account (
                    id uuid PRIMARY KEY,
                    entity_id uuid NOT NULL,
                    account_class public.account_class NOT NULL
                )
                """,
                """
                CREATE TEMP TABLE posting (
                    id uuid PRIMARY KEY,
                    entry_id uuid NOT NULL,
                    account_id uuid NOT NULL,
                    amount_minor bigint NOT NULL,
                    currency text NOT NULL
                )
                """,
            ):
                connection.execute(text(statement))
            connection.commit()

            unbalanced_entry_id = uuid4()
            with pytest.raises(DBAPIError, match="unbalanced"), connection.begin():
                connection.execute(
                    text(
                        """
                            INSERT INTO pg_temp.journal_entry
                                (id, entity_id, status)
                            VALUES (:entry_id, :entity_id, 'DRAFT')
                            """
                    ),
                    {
                        "entry_id": unbalanced_entry_id,
                        "entity_id": entity_id,
                    },
                )
                connection.execute(
                    text(
                        """
                            INSERT INTO pg_temp.account
                                (id, entity_id, account_class)
                            VALUES
                                (:bank_id, :entity_id, 'ASSET'),
                                (:wallet_id, :entity_id, 'ASSET')
                            """
                    ),
                    {
                        "entity_id": entity_id,
                        "bank_id": accounts["bank"],
                        "wallet_id": accounts["wallet"],
                    },
                )
                insert_draft(
                    connection,
                    unbalanced_entry_id,
                    accounts["bank"],
                )
                connection.execute(
                    text(
                        """
                            INSERT INTO public.posting (
                                entry_id, account_id, amount_minor, currency
                            ) VALUES
                                (:entry_id, :bank_id, 500, 'CNY'),
                                (:entry_id, :wallet_id, -599, 'CNY')
                            """
                    ),
                    {
                        "entry_id": unbalanced_entry_id,
                        "bank_id": accounts["bank"],
                        "wallet_id": accounts["wallet"],
                    },
                )

            for statement, params in (
                (
                    "DELETE FROM public.posting WHERE id = :posting_id",
                    {"posting_id": posting_ids[0]},
                ),
                (
                    "UPDATE public.posting SET amount_minor = 4242 WHERE id = :posting_id",
                    {"posting_id": posting_ids[0]},
                ),
            ):
                with pytest.raises(DBAPIError, match="immutable"), connection.begin():
                    connection.execute(text(statement), params)

            cross_entity_entry_id = uuid4()
            with pytest.raises(DBAPIError, match="same entity"), connection.begin():
                connection.execute(
                    text(
                        """
                            INSERT INTO pg_temp.journal_entry
                                (id, entity_id, status)
                            VALUES (:entry_id, :entity_id, 'DRAFT')
                            """
                    ),
                    {
                        "entry_id": cross_entity_entry_id,
                        "entity_id": entity_id,
                    },
                )
                connection.execute(
                    text(
                        """
                            INSERT INTO pg_temp.account
                                (id, entity_id, account_class)
                            VALUES (:account_id, :entity_id, 'ASSET')
                            """
                    ),
                    {
                        "account_id": other_account_id,
                        "entity_id": entity_id,
                    },
                )
                insert_draft(
                    connection,
                    cross_entity_entry_id,
                    accounts["bank"],
                )
                connection.execute(
                    text(
                        """
                            INSERT INTO public.posting (
                                entry_id, account_id, amount_minor, currency
                            ) VALUES (:entry_id, :account_id, 777, 'CNY')
                            """
                    ),
                    {
                        "entry_id": cross_entity_entry_id,
                        "account_id": other_account_id,
                    },
                )

            one_posting_entry_id = uuid4()
            with pytest.raises(DBAPIError), connection.begin():
                connection.execute(
                    text(
                        """
                            INSERT INTO pg_temp.journal_entry
                                (id, entity_id, status)
                            VALUES (:entry_id, :entity_id, 'DRAFT')
                            """
                    ),
                    {
                        "entry_id": one_posting_entry_id,
                        "entity_id": entity_id,
                    },
                )
                connection.execute(
                    text(
                        """
                            INSERT INTO pg_temp.account
                                (id, entity_id, account_class)
                            VALUES (:account_id, :entity_id, 'ASSET')
                            """
                    ),
                    {
                        "account_id": accounts["bank"],
                        "entity_id": entity_id,
                    },
                )
                connection.execute(
                    text(
                        """
                            INSERT INTO pg_temp.posting (
                                id, entry_id, account_id, amount_minor, currency
                            ) VALUES
                                (
                                    gen_random_uuid(), :entry_id, :account_id,
                                    777, 'CNY'
                                ),
                                (
                                    gen_random_uuid(), :entry_id, :account_id,
                                    -777, 'CNY'
                                )
                            """
                    ),
                    {
                        "entry_id": one_posting_entry_id,
                        "account_id": accounts["bank"],
                    },
                )
                insert_draft(
                    connection,
                    one_posting_entry_id,
                    accounts["bank"],
                )
                connection.execute(
                    text(
                        """
                            INSERT INTO public.posting (
                                entry_id, account_id, amount_minor, currency
                            ) VALUES (:entry_id, :account_id, 777, 'CNY')
                            """
                    ),
                    {
                        "entry_id": one_posting_entry_id,
                        "account_id": accounts["bank"],
                    },
                )
                post_audit_id = append_audit(
                    connection,
                    "journal.post",
                    one_posting_entry_id,
                )
                connection.execute(
                    text(
                        """
                            UPDATE public.journal_entry
                            SET status = 'POSTED',
                                posted_audit_event_id = :post_audit_id
                            WHERE id = :entry_id
                            """
                    ),
                    {
                        "entry_id": one_posting_entry_id,
                        "post_audit_id": post_audit_id,
                    },
                )

            correction_entry_id = uuid4()
            with pytest.raises(DBAPIError, match="same entity"), connection.begin():
                connection.execute(
                    text(
                        """
                        INSERT INTO pg_temp.journal_entry (id, entity_id, status)
                        VALUES (:target_id, :entity_id, 'POSTED')
                        """
                    ),
                    {
                        "target_id": posted_entry_id,
                        "entity_id": other_entity_id,
                    },
                )
                creation_audit_id = append_audit(
                    connection,
                    "journal.create",
                    correction_entry_id,
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO public.journal_entry (
                            id, entity_id, occurred_at, origin, status,
                            adjusts_entry_id, primary_account_id, audit_event_id
                        ) VALUES (
                            :entry_id, :entity_id, clock_timestamp(),
                            'pytest', 'DRAFT', :target_id, :account_id,
                            :audit_event_id
                        )
                        """
                    ),
                    {
                        "entry_id": correction_entry_id,
                        "entity_id": other_entity_id,
                        "target_id": posted_entry_id,
                        "account_id": other_account_id,
                        "audit_event_id": creation_audit_id,
                    },
                )

            with pytest.raises(DBAPIError, match="immutable after POSTED use"), connection.begin():
                connection.execute(
                    text(
                        "UPDATE public.account SET account_class = 'INCOME' WHERE id = :account_id"
                    ),
                    {"account_id": accounts["bank"]},
                )
    finally:
        runtime_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    DO $do$
                    BEGIN
                        EXECUTE format(
                            'REVOKE TEMPORARY ON DATABASE %I '
                            'FROM ledgerbridge_app',
                            current_database()
                        );
                    END
                    $do$;
                    """
                )
            )


def test_phase1_invariants_ignore_pg_temp_shadow_tables(
    admin_engine: Engine,
    runtime_engine: Engine,
) -> None:
    _assert_phase1_invariants_ignore_pg_temp_shadow_tables(admin_engine, runtime_engine)


def test_entity_safe_account_identifier_uniqueness(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        first = Entity(entity_type=EntityType.PERSON, name="First")
        second = Entity(entity_type=EntityType.COMPANY, name="Second")
        session.add_all([first, second])
        session.flush()
        session.add_all(
            [
                Account(
                    entity_id=first.id,
                    identifier="bank-main",
                    name="First bank",
                    account_class=AccountClass.ASSET,
                ),
                Account(
                    entity_id=second.id,
                    identifier="bank-main",
                    name="Second bank",
                    account_class=AccountClass.ASSET,
                ),
            ]
        )
        session.commit()

        session.add(
            Account(
                entity_id=first.id,
                identifier="bank-main",
                name="Duplicate",
                account_class=AccountClass.ASSET,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_balanced_entry_commits_and_actual_balance_is_posted_only(
    runtime_engine: Engine,
) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        _create_entry(session, entity_id, [(accounts["bank"], -500), (accounts["expense"], 500)])
        _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -900), (accounts["expense"], 900)],
            status=JournalStatus.DRAFT,
        )
        _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -700), (accounts["expense"], 700)],
            status=JournalStatus.REVERSED,
        )

        balances = actual_account_balances(session, entity_id)
        totals = actual_totals_by_class(session, entity_id)
        assert balances[accounts["bank"]] == -500
        assert balances[accounts["expense"]] == 500
        assert totals[AccountClass.EXPENSE] == 500
        assert totals[AccountClass.INCOME] == 0


def test_unbalanced_entry_fails_at_transaction_commit(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        entry_id = uuid4()
        entry = JournalEntry(
            id=entry_id,
            entity_id=entity_id,
            occurred_at=datetime.now(UTC),
            origin="pytest",
            status=JournalStatus.DRAFT,
            primary_account_id=accounts["bank"],
            audit_event_id=_append_audit(
                session,
                payload={"journal_entry_id": str(entry_id)},
            ),
        )
        session.add(entry)
        session.flush()
        session.add_all(
            [
                Posting(entry_id=entry.id, account_id=accounts["bank"], amount_minor=-100),
                Posting(entry_id=entry.id, account_id=accounts["expense"], amount_minor=99),
            ]
        )
        with pytest.raises(DBAPIError, match="unbalanced"):
            session.commit()


def test_posting_move_checks_old_entry_when_new_entry_stays_balanced(
    runtime_engine: Engine,
) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        first_id = _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -100), (accounts["expense"], 100)],
            status=JournalStatus.DRAFT,
        )
        second_id = _create_entry(
            session,
            entity_id,
            [(accounts["wallet"], -300), (accounts["fee"], 300)],
            status=JournalStatus.DRAFT,
        )
        moving_posting = session.execute(
            text("SELECT id FROM posting WHERE entry_id = :entry_id AND amount_minor = 100"),
            {"entry_id": first_id},
        ).scalar_one()
        target_posting = session.execute(
            text("SELECT id FROM posting WHERE entry_id = :entry_id AND amount_minor = 300"),
            {"entry_id": second_id},
        ).scalar_one()
        session.execute(
            update(Posting).where(Posting.id == target_posting).values(amount_minor=200)
        )
        session.execute(
            update(Posting).where(Posting.id == moving_posting).values(entry_id=second_id)
        )
        with pytest.raises(DBAPIError, match="unbalanced"):
            session.commit()


def test_balance_trigger_rejects_cross_currency_net_zero(
    admin_engine: Engine,
    runtime_engine: Engine,
) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)

    with admin_engine.connect() as connection:
        constraint_name = connection.execute(
            text(
                """
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'posting'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%currency%CNY%'
                """
            )
        ).scalar_one()

    with pytest.raises(DBAPIError, match="unbalanced"), admin_engine.begin() as connection:
        quoted_name = connection.dialect.identifier_preparer.quote(constraint_name)
        connection.execute(text(f"ALTER TABLE posting DROP CONSTRAINT {quoted_name}"))
        entry_id = uuid4()
        audit_event_id = connection.execute(
            text(
                "SELECT append_audit_event("
                "'pytest', 'journal.create', 'cross-currency acceptance', NULL, "
                "CAST(:payload AS jsonb))"
            ),
            {"payload": json.dumps({"journal_entry_id": str(entry_id)})},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO journal_entry (
                    id, entity_id, occurred_at, origin, status,
                    primary_account_id, audit_event_id
                )
                VALUES (
                    :entry_id, :entity_id, now(), 'pytest', 'DRAFT',
                    :account_id, :audit_event_id
                )
                """
            ),
            {
                "entry_id": entry_id,
                "entity_id": entity_id,
                "account_id": accounts["bank"],
                "audit_event_id": audit_event_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO posting (entry_id, account_id, amount_minor, currency)
                VALUES
                    (:entry_id, :bank_id, 10, 'CNY'),
                    (:entry_id, :expense_id, -10, 'USD')
                """
            ),
            {
                "entry_id": entry_id,
                "bank_id": accounts["bank"],
                "expense_id": accounts["expense"],
            },
        )

    with admin_engine.connect() as connection:
        assert connection.execute(
            text(
                """
                SELECT COUNT(*) = 1
                FROM pg_constraint
                WHERE conname = :constraint_name
                """
            ),
            {"constraint_name": constraint_name},
        ).scalar_one()


def test_posted_entry_and_postings_are_immutable(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        entry_id = _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -100), (accounts["expense"], 100)],
        )

        with pytest.raises(DBAPIError, match="immutable"):
            session.execute(
                update(JournalEntry).where(JournalEntry.id == entry_id).values(origin="tampered")
            )
        session.rollback()

        posting_id = session.execute(
            text("SELECT id FROM posting WHERE entry_id = :entry_id LIMIT 1"),
            {"entry_id": entry_id},
        ).scalar_one()
        with pytest.raises(DBAPIError, match="immutable"):
            session.execute(
                update(Posting).where(Posting.id == posting_id).values(amount_minor=101)
            )
        session.rollback()

        with pytest.raises(DBAPIError, match="immutable"):
            session.execute(
                text("DELETE FROM posting WHERE id = :posting_id"),
                {"posting_id": posting_id},
            )


def test_reversal_preserves_original_posted_history(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        original_id = _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -500), (accounts["expense"], 500)],
        )
        reversal_id = _create_entry(
            session,
            entity_id,
            [(accounts["bank"], 500), (accounts["expense"], -500)],
            reverses_entry_id=original_id,
        )

        original_status = session.execute(
            text("SELECT status::text FROM journal_entry WHERE id = :entry_id"),
            {"entry_id": original_id},
        ).scalar_one()
        linked_target = session.execute(
            text("SELECT reverses_entry_id FROM journal_entry WHERE id = :entry_id"),
            {"entry_id": reversal_id},
        ).scalar_one()
        assert original_status == "POSTED"
        assert linked_target == original_id
        assert actual_totals_by_class(session, entity_id)[AccountClass.EXPENSE] == 0


def test_duplicate_reversal_is_rejected_and_totals_stay_zero(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        original_id = _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -500), (accounts["expense"], 500)],
        )
        _create_entry(
            session,
            entity_id,
            [(accounts["bank"], 500), (accounts["expense"], -500)],
            reverses_entry_id=original_id,
        )

        with pytest.raises(IntegrityError):
            _create_entry(
                session,
                entity_id,
                [(accounts["bank"], 500), (accounts["expense"], -500)],
                reverses_entry_id=original_id,
            )
        session.rollback()
        assert actual_totals_by_class(session, entity_id)[AccountClass.EXPENSE] == 0


def test_posted_account_dimensions_are_immutable(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        other_entity_id, _ = _create_accounts(session)
        _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -500), (accounts["expense"], 500)],
        )

        with pytest.raises(DBAPIError, match="immutable after POSTED use"):
            session.execute(
                update(Account)
                .where(Account.id == accounts["expense"])
                .values(account_class=AccountClass.INCOME)
            )
        session.rollback()

        with pytest.raises(DBAPIError, match="account entity_id is immutable"):
            session.execute(
                update(Account)
                .where(Account.id == accounts["expense"])
                .values(entity_id=other_entity_id)
            )
        session.rollback()

        session.execute(
            update(Account).where(Account.id == accounts["expense"]).values(name="Expenses:Renamed")
        )
        session.commit()
        account_row = session.execute(
            text("SELECT entity_id, account_class::text, name FROM account WHERE id = :id"),
            {"id": accounts["expense"]},
        ).one()
        assert account_row == (entity_id, "EXPENSE", "Expenses:Renamed")
        assert actual_totals_by_class(session, entity_id)[AccountClass.EXPENSE] == 500


def test_entity_identity_is_immutable_from_creation(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        other_entity_id, other_accounts = _create_accounts(session)

        with pytest.raises(DBAPIError, match="account entity_id is immutable"):
            session.execute(
                update(Account)
                .where(Account.id == accounts["expense"])
                .values(entity_id=other_entity_id)
            )
        session.rollback()

        entry_id = uuid4()
        entry = JournalEntry(
            id=entry_id,
            entity_id=entity_id,
            occurred_at=datetime.now(UTC),
            origin="pytest",
            status=JournalStatus.DRAFT,
            primary_account_id=accounts["bank"],
            audit_event_id=_append_audit(
                session,
                payload={"journal_entry_id": str(entry_id)},
            ),
        )
        session.add(entry)
        session.commit()

        with pytest.raises(DBAPIError, match="journal entry entity_id is immutable"):
            session.execute(
                update(JournalEntry)
                .where(JournalEntry.id == entry.id)
                .values(
                    entity_id=other_entity_id,
                    primary_account_id=other_accounts["bank"],
                )
            )


def test_posted_transition_rejects_preexisting_cross_entity_drift(
    admin_engine: Engine,
    runtime_engine: Engine,
) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        other_entity_id, _ = _create_accounts(session)
        entry_id = _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -500), (accounts["expense"], 500)],
            status=JournalStatus.DRAFT,
        )

    with admin_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE account DISABLE TRIGGER account_protected_dimensions_immutable")
        )
        connection.execute(
            text("UPDATE account SET entity_id = :entity_id WHERE id = :account_id"),
            {"entity_id": other_entity_id, "account_id": accounts["expense"]},
        )
        connection.execute(
            text("ALTER TABLE account ENABLE TRIGGER account_protected_dimensions_immutable")
        )

    with Session(runtime_engine) as session:
        post_journal_entry(
            session,
            entry_id,
            actor="pytest",
            reason="cross-entity drift test",
        )
        with pytest.raises(DBAPIError, match="account from another entity"):
            session.commit()


def test_correction_target_must_be_posted_and_same_entity(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        draft_id = _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -100), (accounts["expense"], 100)],
            status=JournalStatus.DRAFT,
        )
        with pytest.raises(DBAPIError, match="must be POSTED"):
            _create_entry(
                session,
                entity_id,
                [(accounts["bank"], 100), (accounts["expense"], -100)],
                adjusts_entry_id=draft_id,
            )


def test_posting_cannot_cross_entity_boundary(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        first_entity, first_accounts = _create_accounts(session)
        _, second_accounts = _create_accounts(session)
        entry_id = uuid4()
        entry = JournalEntry(
            id=entry_id,
            entity_id=first_entity,
            occurred_at=datetime.now(UTC),
            origin="pytest",
            status=JournalStatus.DRAFT,
            primary_account_id=first_accounts["bank"],
            audit_event_id=_append_audit(
                session,
                payload={"journal_entry_id": str(entry_id)},
            ),
        )
        session.add(entry)
        session.flush()
        session.add(
            Posting(
                entry_id=entry.id,
                account_id=second_accounts["bank"],
                amount_minor=0,
            )
        )
        with pytest.raises(DBAPIError, match="same entity"):
            session.flush()


def test_posted_entry_requires_at_least_two_postings(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        entry_id = uuid4()
        entry = JournalEntry(
            id=entry_id,
            entity_id=entity_id,
            occurred_at=datetime.now(UTC),
            origin="pytest",
            status=JournalStatus.DRAFT,
            primary_account_id=accounts["bank"],
            audit_event_id=_append_audit(
                session,
                payload={"journal_entry_id": str(entry_id)},
            ),
        )
        session.add(entry)
        session.flush()
        session.add(Posting(entry_id=entry.id, account_id=accounts["bank"], amount_minor=0))
        session.flush()
        post_journal_entry(
            session,
            entry.id,
            actor="pytest",
            reason="completeness test",
        )
        with pytest.raises(DBAPIError, match="at least two postings"):
            session.commit()


def test_direct_posted_insert_fails_without_draft_lifecycle(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        entry_id = uuid4()
        session.add(
            JournalEntry(
                id=entry_id,
                entity_id=entity_id,
                occurred_at=datetime.now(UTC),
                origin="pytest",
                status=JournalStatus.POSTED,
                primary_account_id=accounts["bank"],
                audit_event_id=_append_audit(
                    session,
                    payload={"journal_entry_id": str(entry_id)},
                ),
            )
        )
        with pytest.raises(DBAPIError, match="created before"):
            session.commit()


def test_correction_target_cannot_cross_entity(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        first_entity, first_accounts = _create_accounts(session)
        original_id = _create_entry(
            session,
            first_entity,
            [(first_accounts["bank"], -100), (first_accounts["expense"], 100)],
        )
        second_entity, second_accounts = _create_accounts(session)
        with pytest.raises(DBAPIError, match="same entity"):
            _create_entry(
                session,
                second_entity,
                [(second_accounts["bank"], 100), (second_accounts["expense"], -100)],
                adjusts_entry_id=original_id,
            )


def test_audit_function_acl_append_only_and_hash_chain(
    admin_engine: Engine,
    runtime_engine: Engine,
) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        with pytest.raises(DBAPIError):
            session.execute(
                text(
                    """
                    INSERT INTO audit_event (
                        id, sequence, occurred_at, actor, action, reason,
                        rule_version, payload, prev_hash, hash
                    )
                    VALUES (
                        gen_random_uuid(), 1, now(), 'direct', 'insert', 'forbidden',
                        NULL, '{}'::jsonb, NULL, decode(repeat('00', 32), 'hex')
                    )
                    """
                )
            )
        session.rollback()
        first_id = _append_audit(session, "first", {"z": 2, "a": 1})
        second_id = _append_audit(session, "second", {"value": "ok"})
        session.commit()

    with admin_engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    """
                SELECT id, sequence, prev_hash, hash,
                    hash = public.digest(
                        convert_to(
                            jsonb_build_object(
                                'id', id,
                                'sequence', sequence,
                                'occurred_at', to_char(
                                    occurred_at AT TIME ZONE 'UTC',
                                    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
                                ),
                                'actor', actor,
                                'action', action,
                                'reason', reason,
                                'rule_version', rule_version,
                                'payload', payload,
                                'prev_hash', CASE
                                    WHEN prev_hash IS NULL THEN NULL
                                    ELSE encode(prev_hash, 'hex')
                                END
                            )::text,
                            'UTF8'
                        ),
                        'sha256'
                    ) AS valid_hash
                FROM audit_event
                ORDER BY sequence
                """
                )
            )
            .mappings()
            .all()
        )
        assert [row["id"] for row in rows] == [first_id, second_id]
        assert rows[0]["prev_hash"] is None
        assert rows[1]["prev_hash"] == rows[0]["hash"]
        assert all(row["valid_hash"] for row in rows)

        with pytest.raises(DBAPIError, match="append-only"):
            connection.execute(text("UPDATE audit_event SET reason = 'tampered'"))
        connection.rollback()

        with pytest.raises(DBAPIError, match="append-only"):
            connection.execute(text("DELETE FROM audit_event"))
        connection.rollback()


def test_audit_chain_rejects_repeatable_read_fork(runtime_engine: Engine) -> None:
    with Session(runtime_engine) as session:
        _append_audit(session, "seed")
        session.commit()

    barrier = Barrier(2)

    def append_from_stale_snapshot(action: str) -> str:
        with runtime_engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            transaction = connection.begin()
            connection.execute(text("SELECT COUNT(*) FROM audit_event")).scalar_one()
            barrier.wait(timeout=10)
            try:
                connection.execute(
                    text("SELECT append_audit_event(:actor, :action, :reason, NULL, '{}'::jsonb)"),
                    {"actor": "pytest", "action": action, "reason": "concurrency test"},
                ).scalar_one()
                transaction.commit()
                return "committed"
            except DBAPIError:
                transaction.rollback()
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append_from_stale_snapshot, ("concurrent-a", "concurrent-b")))

    assert sorted(results) == ["committed", "rejected"]
    with runtime_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM audit_event")).scalar_one() == 2
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM audit_event WHERE prev_hash IS NULL")
            ).scalar_one()
            == 1
        )


def test_equal_internal_transfer_changes_no_income_or_expense(
    runtime_engine: Engine,
) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -50_000), (accounts["wallet"], 50_000)],
        )
        totals = actual_totals_by_class(session, entity_id)
        assert totals[AccountClass.INCOME] == 0
        assert totals[AccountClass.EXPENSE] == 0


def test_transfer_fee_is_exactly_ten_minor_units(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        _create_entry(
            session,
            entity_id,
            [
                (accounts["wallet"], -10_000),
                (accounts["bank"], 9_990),
                (accounts["fee"], 10),
            ],
        )
        totals = actual_totals_by_class(session, entity_id)
        assert totals[AccountClass.EXPENSE] == 10
        assert totals[AccountClass.INCOME] == 0


def test_credit_card_repayment_does_not_duplicate_expense(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        _create_entry(
            session,
            entity_id,
            [(accounts["expense"], 1_000), (accounts["card"], -1_000)],
        )
        _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -1_000), (accounts["card"], 1_000)],
        )
        totals = actual_totals_by_class(session, entity_id)
        assert totals[AccountClass.EXPENSE] == 1_000
        assert totals[AccountClass.INCOME] == 0


def test_partial_refund_reduces_expense_without_income(runtime_engine: Engine) -> None:
    with Session(runtime_engine, expire_on_commit=False) as session:
        entity_id, accounts = _create_accounts(session)
        _create_entry(
            session,
            entity_id,
            [(accounts["bank"], -500), (accounts["expense"], 500)],
        )
        _create_entry(
            session,
            entity_id,
            [(accounts["bank"], 300), (accounts["expense"], -300)],
        )
        totals = actual_totals_by_class(session, entity_id)
        assert totals[AccountClass.EXPENSE] == 200
        assert totals[AccountClass.INCOME] == 0


def test_phase1_migration_real_round_trip(migration_database_url: str) -> None:
    url = create_engine(migration_database_url).url
    database_name = f"ledgerbridge_rt_{uuid4().hex[:12]}"
    maintenance_url = url.set(database="postgres")
    temporary_url = url.set(database=database_name)
    maintenance_engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    temporary_engine: Engine | None = None

    try:
        with maintenance_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        _run_alembic(temporary_url.render_as_string(hide_password=False), "head")
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.connect() as connection:
            inspector = inspect(connection)
            assert {"entity", "account", "journal_entry", "posting", "audit_event"} <= set(
                inspector.get_table_names()
            )
            trigger_names = set(
                connection.execute(
                    text(
                        """
                        SELECT tgname
                        FROM pg_trigger
                        WHERE NOT tgisinternal
                        """
                    )
                ).scalars()
            )
            assert {
                "posting_balanced_per_currency",
                "posting_posted_immutable",
                "audit_event_no_update_delete",
            } <= trigger_names

        temporary_engine.dispose()
        temporary_engine = None
        _run_alembic(
            temporary_url.render_as_string(hide_password=False),
            "20260821_0001",
            downgrade=True,
        )
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.connect() as connection:
            assert not (
                {"entity", "account", "journal_entry", "posting", "audit_event"}
                & set(inspect(connection).get_table_names())
            )
            assert connection.execute(
                text(
                    "SELECT to_regprocedure("
                    "'append_audit_event(text,text,text,text,jsonb)') IS NULL"
                )
            ).scalar_one()

        temporary_engine.dispose()
        temporary_engine = None
        _run_alembic(temporary_url.render_as_string(hide_password=False), "head")
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.connect() as connection:
            assert {"entity", "account", "journal_entry", "posting", "audit_event"} <= set(
                inspect(connection).get_table_names()
            )
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
        maintenance_engine.dispose()


def test_security_function_forward_migration_repairs_historical_definitions(
    migration_database_url: str,
    database_url: str,
) -> None:
    owner_source = create_engine(migration_database_url)
    runtime_source = create_engine(database_url)
    owner_url = owner_source.url
    runtime_url = runtime_source.url
    owner_source.dispose()
    runtime_source.dispose()

    database_name = f"ledgerbridge_search_path_{uuid4().hex[:10]}"
    maintenance_engine = create_engine(
        owner_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    temporary_owner_url = owner_url.set(database=database_name)
    temporary_runtime_url = runtime_url.set(database=database_name)
    temporary_admin_engine: Engine | None = None
    temporary_runtime_engine: Engine | None = None

    table_reading_functions = (
        "public.account_block_protected_dimension_change()",
        "public.journal_entry_validate_relationships()",
        "public.posting_enforce_entity()",
        "public.posting_block_posted_mutation()",
        "public.posting_assert_balanced()",
        "public.journal_entry_assert_posted_complete()",
    )
    fixed_search_path_functions = (
        *table_reading_functions,
        "public.append_audit_event(text,text,text,text,jsonb)",
        "public.audit_event_block_mutation()",
        "public.import_job_enforce_transition()",
        "public.journal_entry_block_posted_mutation()",
        "public.journal_entry_validate_post_audit()",
        "public.raw_artifact_block_mutation()",
        "public.raw_artifact_validate_audit()",
        "public.source_record_block_mutation()",
    )

    try:
        with maintenance_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        rendered = temporary_owner_url.render_as_string(hide_password=False)
        _run_alembic(rendered, "20260823_0008")
        temporary_admin_engine = create_engine(temporary_owner_url)
        with temporary_admin_engine.begin() as connection:
            for signature in table_reading_functions:
                definition = connection.execute(
                    text("SELECT pg_get_functiondef(to_regprocedure(:signature))"),
                    {"signature": signature},
                ).scalar_one()
                historical_definition = re.sub(
                    r"(?m)^\s*SET search_path TO 'pg_catalog'\s*$",
                    "",
                    definition,
                )
                for qualified, unqualified in (
                    ("FROM public.posting", "FROM posting"),
                    ("JOIN public.journal_entry", "JOIN journal_entry"),
                    ("FROM public.journal_entry", "FROM journal_entry"),
                    ("FROM public.account", "FROM account"),
                    ("JOIN public.account", "JOIN account"),
                    ("public.journal_status", "journal_status"),
                ):
                    historical_definition = historical_definition.replace(
                        qualified,
                        unqualified,
                    )
                connection.exec_driver_sql(historical_definition)

            connection.exec_driver_sql(
                "ALTER FUNCTION public.append_audit_event(text,text,text,text,jsonb) "
                "SET search_path = pg_catalog, public"
            )
            for signature in (
                "public.audit_event_block_mutation()",
                "public.import_job_enforce_transition()",
                "public.journal_entry_block_posted_mutation()",
                "public.raw_artifact_block_mutation()",
                "public.source_record_block_mutation()",
            ):
                connection.exec_driver_sql(f"ALTER FUNCTION {signature} RESET search_path")

            assert (
                connection.execute(
                    text(
                        "SELECT proconfig FROM pg_proc "
                        "WHERE oid = to_regprocedure("
                        "'public.posting_assert_balanced()')"
                    )
                ).scalar_one()
                is None
            )
            assert (
                "FROM posting"
                in connection.execute(
                    text(
                        "SELECT pg_get_functiondef("
                        "to_regprocedure('public.posting_assert_balanced()'))"
                    )
                ).scalar_one()
            )

        temporary_admin_engine.dispose()
        temporary_admin_engine = None
        _run_alembic(rendered, "head")

        temporary_admin_engine = create_engine(temporary_owner_url)
        temporary_runtime_engine = create_engine(temporary_runtime_url)
        with temporary_admin_engine.connect() as connection:
            configurations = connection.execute(
                text(
                    "SELECT to_regprocedure(signature)::text, function_definition.proconfig "
                    "FROM unnest(CAST(:signatures AS text[])) AS required(signature) "
                    "JOIN pg_proc AS function_definition "
                    "ON function_definition.oid = to_regprocedure(signature) "
                    "ORDER BY signature"
                ),
                {"signatures": list(fixed_search_path_functions)},
            ).all()
        assert len(configurations) == len(fixed_search_path_functions)
        assert all(
            configuration == ["search_path=pg_catalog"] for _, configuration in configurations
        )

        _assert_phase1_invariants_ignore_pg_temp_shadow_tables(
            temporary_admin_engine,
            temporary_runtime_engine,
        )

        temporary_runtime_engine.dispose()
        temporary_runtime_engine = None
        temporary_admin_engine.dispose()
        temporary_admin_engine = None
        _run_alembic(rendered, "20260823_0008", downgrade=True)

        temporary_admin_engine = create_engine(temporary_owner_url)
        with temporary_admin_engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "20260823_0008"
            )
            assert connection.execute(
                text(
                    "SELECT proconfig FROM pg_proc "
                    "WHERE oid = to_regprocedure("
                    "'public.posting_assert_balanced()')"
                )
            ).scalar_one() == ["search_path=pg_catalog"]
    finally:
        if temporary_runtime_engine is not None:
            temporary_runtime_engine.dispose()
        if temporary_admin_engine is not None:
            temporary_admin_engine.dispose()
        with maintenance_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        maintenance_engine.dispose()


def test_runtime_role_split_removes_preexisting_owner_membership(
    migration_database_url: str,
) -> None:
    url = create_engine(migration_database_url).url
    database_name = f"ledgerbridge_role_split_{uuid4().hex[:12]}"
    maintenance_url = url.set(database="postgres")
    temporary_url = url.set(database=database_name)
    maintenance_engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    temporary_engine: Engine | None = None

    try:
        with maintenance_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))

        rendered = temporary_url.render_as_string(hide_password=False)
        _run_alembic(rendered, "20260823_0005")
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.begin() as connection:
            assert (
                connection.execute(
                    text(
                        """
                    SELECT count(*)
                    FROM pg_auth_members AS membership
                    JOIN pg_roles AS member_role ON member_role.oid = membership.member
                    WHERE member_role.rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
                      AND membership.roleid = 'ledgerbridge_owner'::regrole
                    """
                    )
                ).scalar_one()
                == 0
            )
            connection.execute(
                text("GRANT ledgerbridge_owner TO ledgerbridge_api, ledgerbridge_worker")
            )
        temporary_engine.dispose()
        temporary_engine = None

        _run_alembic(rendered, "20260823_0006")
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        """
                    SELECT count(*)
                    FROM pg_auth_members AS membership
                    JOIN pg_roles AS member_role ON member_role.oid = membership.member
                    WHERE member_role.rolname IN ('ledgerbridge_api', 'ledgerbridge_worker')
                    """
                    )
                ).scalar_one()
                == 0
            )

            # The migration owner is a superuser in the disposable CI database;
            # SET SESSION AUTHORIZATION models an API login without storing
            # another password, so the following SET ROLE check is evaluated
            # against ledgerbridge_api rather than the owner session.
            connection.execute(text("SET SESSION AUTHORIZATION ledgerbridge_api"))
            with pytest.raises(DBAPIError):
                connection.execute(text("SET ROLE ledgerbridge_owner"))
            connection.rollback()
    finally:
        if temporary_engine is not None:
            temporary_engine.dispose()
        with maintenance_engine.connect() as connection:
            connection.execute(
                text("REVOKE ledgerbridge_owner FROM ledgerbridge_api, ledgerbridge_worker")
            )
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name"
                ),
                {"database_name": database_name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        maintenance_engine.dispose()
