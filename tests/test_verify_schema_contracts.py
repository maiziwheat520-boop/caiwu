from __future__ import annotations

from collections.abc import Iterator

import pytest

from scripts import verify_schema_contracts
from scripts.backup_restore import BANK_STATEMENT_SECURITY_REVISION, R1_ROLES, BackupError


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one(self) -> object:
        return self.value

    def one(self) -> object:
        return self.value

    def scalars(self) -> Iterator[object]:
        return iter(self.value if isinstance(self.value, list) else [])


class _Connection:
    def __init__(
        self,
        *,
        revision: str,
        current_user: str = "ledgerbridge_owner",
        owner: str = "ledgerbridge_owner",
        roles: list[str] | None = None,
        memberships: dict[str, list[dict[str, object]]] | None = None,
    ) -> None:
        self.revision = revision
        self.current_user = current_user
        self.owner = owner
        self.roles = list(R1_ROLES) if roles is None else roles
        self.memberships = {} if memberships is None else memberships

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: object, parameters: object = None) -> _Result:
        del parameters
        sql = str(statement)
        if sql == "SET TRANSACTION READ ONLY":
            return _Result(None)
        if "SELECT version_num FROM alembic_version" in sql:
            return _Result(self.revision)
        if "SELECT current_user" in sql:
            return _Result((self.current_user, self.owner))
        if "'r1_role_matrix'" in sql:
            return _Result(
                {
                    "r1_role_matrix": [
                        {
                            "role": role,
                            "login": role != "ledgerbridge_app",
                            "superuser": False,
                            "create_database": False,
                            "create_role": False,
                            "inherit": False,
                            "replication": False,
                            "bypass_rls": False,
                            "memberships": self.memberships.get(role, []),
                        }
                        for role in self.roles
                    ]
                }
            )
        if "'counterparty_row_counts'" in sql:
            return _Result({"counterparty": "sentinel"})
        if "'bank_statement_row_counts'" in sql:
            return _Result({"bank_statement": "sentinel"})
        raise AssertionError(f"unexpected SQL before fail-closed gate: {sql[:80]}")


class _Engine:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self) -> _Connection:
        return self.connection

    def dispose(self) -> None:
        self.disposed = True


def _install_engine(monkeypatch: pytest.MonkeyPatch, connection: _Connection) -> _Engine:
    engine = _Engine(connection)
    monkeypatch.setattr(verify_schema_contracts, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setenv(
        "LEDGERBRIDGE_MIGRATION_DATABASE_URL",
        "postgresql+psycopg://owner:redacted@postgres/ledgerbridge",
    )
    return engine


def test_verifier_rejects_non_target_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _install_engine(
        monkeypatch,
        _Connection(revision="20260829_0019"),
    )

    with pytest.raises(BackupError, match=BANK_STATEMENT_SECURITY_REVISION):
        verify_schema_contracts.main()

    assert engine.disposed is True


def test_verifier_rejects_non_owner_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_engine(
        monkeypatch,
        _Connection(
            revision=BANK_STATEMENT_SECURITY_REVISION,
            current_user="ledgerbridge_app",
        ),
    )

    with pytest.raises(BackupError, match="database owner"):
        verify_schema_contracts.main()


def test_verifier_rejects_missing_required_role(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_engine(
        monkeypatch,
        _Connection(
            revision=BANK_STATEMENT_SECURITY_REVISION,
            roles=list(R1_ROLES[1:]),
        ),
    )

    with pytest.raises(BackupError, match="role matrix is incomplete"):
        verify_schema_contracts.main()


def test_verifier_runs_r1_then_revision_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_engine(
        monkeypatch,
        _Connection(revision=BANK_STATEMENT_SECURITY_REVISION),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        verify_schema_contracts,
        "_validate_r1_database_security",
        lambda metadata: calls.append("R1"),
    )
    monkeypatch.setattr(
        verify_schema_contracts,
        "_validate_counterparty_security",
        lambda metadata: calls.append("counterparty"),
    )
    monkeypatch.setattr(
        verify_schema_contracts,
        "_validate_bank_statement_security",
        lambda metadata: calls.append("bank_statement"),
    )

    assert verify_schema_contracts.main() == 0
    assert calls == ["R1", "counterparty", "bank_statement"]


def test_verifier_rejects_role_membership_before_revision_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_engine(
        monkeypatch,
        _Connection(
            revision=BANK_STATEMENT_SECURITY_REVISION,
            memberships={
                "ledgerbridge_worker": [
                    {
                        "direction": "granted",
                        "role": "rogue_login",
                        "admin_option": False,
                    }
                ]
            },
        ),
    )
    monkeypatch.setattr(
        verify_schema_contracts,
        "_validate_counterparty_security",
        lambda metadata: pytest.fail("counterparty validation must not run"),
    )
    monkeypatch.setattr(
        verify_schema_contracts,
        "_validate_bank_statement_security",
        lambda metadata: pytest.fail("bank statement validation must not run"),
    )

    with pytest.raises(BackupError, match="privileged or non-isolated"):
        verify_schema_contracts.main()


def test_database_url_fallback_requires_migrate_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGERBRIDGE_MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "LEDGERBRIDGE_DATABASE_URL",
        "postgresql+psycopg://owner:redacted@postgres/ledgerbridge",
    )
    monkeypatch.setenv("LEDGERBRIDGE_RUNTIME_ROLE", "api")

    with pytest.raises(BackupError, match="MIGRATION_DATABASE_URL"):
        verify_schema_contracts.main()
