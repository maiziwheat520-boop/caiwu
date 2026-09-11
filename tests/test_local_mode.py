"""Local mode must refuse every production-shaped configuration.

Almost every test here exercises a refusal, deliberately. What local mode
offers is "fewer gates", and a missing gate leaking into production looks
exactly like a working system on screen. So the evidence that it is safe is
that it refuses when it should, not that it starts on this machine.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from tempfile import gettempdir
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

import ledgerbridge.db
import scripts.local_mode as local_mode_script
from ledgerbridge.config import Settings
from ledgerbridge.local_mode import (
    LOCAL_POLICY_GENERATION,
    LocalModeRefused,
    LocalProfile,
    guard,
    local_principal,
    local_settings,
    local_verifier,
)

LOCAL_DATABASE = "postgresql+psycopg://ledgerbridge:local@127.0.0.1:5432/ledgerbridge"
REMOTE_DATABASE = "postgresql+psycopg://ledgerbridge:pw@ledger.internal:5432/ledgerbridge"
# artifact_root must be absolute, and "absolute" on Windows does not mean /var.
ARTIFACT_ROOT = Path(gettempdir()) / "ledgerbridge-local-mode-test"
ENTITY = UUID("10000000-0000-4000-8000-000000000001")


def _password_of(written: str, key: str) -> str:
    line = next(row for row in written.splitlines() if row.startswith(f"{key}="))
    return line.partition("=")[2]


def profile(**overrides: object) -> LocalProfile:
    fields: dict[str, object] = {
        "database_url": LOCAL_DATABASE,
        "artifact_root": ARTIFACT_ROOT,
        "entity_refs": (ENTITY,),
    }
    fields.update(overrides)
    return LocalProfile(**fields)  # type: ignore[arg-type]


def settings(**overrides: object) -> Settings:
    fields: dict[str, object] = {
        "env": "development",
        "runtime_role": "api",
        "database_url": LOCAL_DATABASE,
        "api_database_url": LOCAL_DATABASE,
        "reader_database_url": LOCAL_DATABASE,
        "artifact_root": ARTIFACT_ROOT,
        "enable_internal_read_api": True,
        "internal_read_backend": "database",
        "internal_read_transport": "disabled",
        "internal_read_operational_gate": "closed",
        "internal_read_policy_generation": LOCAL_POLICY_GENERATION,
        # The database backend signs keyset cursors; this is a test key.
        "internal_read_cursor_key": "x" * 48,
    }
    fields.update(overrides)
    return Settings(**fields)  # type: ignore[arg-type]


def test_a_local_profile_starts() -> None:
    """Prove the accepting path works, or every refusal below proves nothing."""
    built = local_settings(profile())

    assert built.env == "development"
    assert built.internal_read_backend == "database"
    assert built.internal_read_transport == "disabled"


def test_a_production_read_api_cannot_even_be_built_without_the_gate() -> None:
    """This one is not local mode's to enforce, which is why it is tested here.

    ``Settings`` already refuses production plus internal read API plus a closed
    gate, so local mode's own check is a second net rather than the only one.
    Pinning both here means that relaxing the upstream validator turns this test
    red instead of silently emptying the net.
    """
    with pytest.raises(ValidationError, match="R1 operational gate"):
        settings(env="production")

    # model_copy skips validation, standing in for a future where Settings has
    # been relaxed. Local mode must still refuse the result.
    with pytest.raises(LocalModeRefused, match="env=production"):
        guard(settings().model_copy(update={"env": "production"}))


def test_refuses_a_non_loopback_bind() -> None:
    """With no transport verification, any other bind hands over the books."""
    with pytest.raises(LocalModeRefused, match="loopback only"):
        guard(settings(), host="0.0.0.0")


def test_refuses_a_remote_database() -> None:
    """The most dangerous misconfiguration: local identity, production data.

    That is neither the local version nor production. It is an identity that
    verifies nothing, reading the real books.
    """
    with pytest.raises(LocalModeRefused, match="local database only"):
        guard(settings(database_url=REMOTE_DATABASE, reader_database_url=REMOTE_DATABASE))


def test_reads_the_host_out_of_a_driver_qualified_url() -> None:
    """The +psycopg scheme suffix must not hide the host name.

    If it did, the host would read as empty, empty is not on the allow-list, and
    every valid local profile would be refused. That fails safe and is still a
    failure.
    """
    guard(settings())


def test_refuses_an_open_operational_gate() -> None:
    with pytest.raises(LocalModeRefused, match="operational_gate"):
        guard(settings(internal_read_operational_gate="r1-production-v1"))


def test_refuses_an_mtls_transport() -> None:
    """An mTLS transport means someone believes this is production."""
    with pytest.raises(LocalModeRefused, match="transport"):
        guard(settings(internal_read_transport="unix-mtls-proxy"))


def test_real_ingest_cannot_even_be_built() -> None:
    """Local mode never ingests on its own; statements are imported explicitly.

    As above, ``Settings`` is what refuses this. Local mode simply never touches
    the switch, and its own check stays as the second net.
    """
    with pytest.raises(ValidationError, match="real ingest remains unavailable"):
        settings(enable_real_ingest=True)


def test_refuses_an_empty_book() -> None:
    """No entities means no identity is issued.

    An identity holding no grants renders a clean empty page, which is
    indistinguishable from "nothing has been imported on this machine yet" -
    and the second of those is the fact.
    """
    with pytest.raises(LocalModeRefused, match="no entity"):
        local_principal(())


def test_the_local_identity_does_not_come_from_the_request() -> None:
    """The identity comes from process arguments, not from client headers.

    Reading headers would let anything that can reach the port name the identity
    it wants.
    """
    verify = local_verifier(local_principal([ENTITY]))

    verified = verify({"headers": [(b"x-ledgerbridge-principal", b"workload:somebody-else")]})

    assert verified.principal.principal_ref == "workload:local-single-user"
    assert verified.principal.grants[0].entity_ref == ENTITY


def test_unassigned_facts_stay_visible_locally() -> None:
    """Most personal rows carry no business unit at all.

    Refusing unassigned facts would open the local version on a book with most
    of it missing, which is the very thing that motivated writing a separate
    program in the first place.
    """
    principal = local_principal([ENTITY])

    assert principal.grants[0].allow_unassigned_candidates
    assert principal.grants[0].business_unit_refs == frozenset()


def test_init_writes_an_env_file_without_printing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The generated passwords must reach the file and nothing else.

    A password echoed once lives in a scrollback buffer forever, so the check is
    not that init prints something harmless but that nothing it prints appears in
    the file it wrote.
    """
    env_file = tmp_path / ".env.local"
    monkeypatch.setattr(local_mode_script, "ENV_FILE", env_file)

    assert local_mode_script.command_init(argparse.Namespace()) == 0

    written = env_file.read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    assert "LEDGERBRIDGE_API_DB_PASSWORD=" in written
    assert printed.strip() == f"wrote {env_file.name}"
    secret = _password_of(written, "LEDGERBRIDGE_API_DB_PASSWORD")
    assert secret not in printed


def test_init_refuses_to_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """New passwords against an initialised volume lock everyone out.

    The roles were created on first boot with the old passwords; rewriting the
    file produces a database nobody can log into, and the failure would surface
    one step later looking like a connection bug.
    """
    env_file = tmp_path / ".env.local"
    monkeypatch.setattr(local_mode_script, "ENV_FILE", env_file)
    local_mode_script.command_init(argparse.Namespace())
    first = env_file.read_text(encoding="utf-8")

    local_mode_script.command_init(argparse.Namespace())

    assert env_file.read_text(encoding="utf-8") == first


def test_every_generated_role_password_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database init script rejects duplicate role passwords outright."""
    env_file = tmp_path / ".env.local"
    monkeypatch.setattr(local_mode_script, "ENV_FILE", env_file)
    local_mode_script.command_init(argparse.Namespace())

    written = env_file.read_text(encoding="utf-8")
    passwords = [
        _password_of(written, f"LEDGERBRIDGE_{role.upper()}_DB_PASSWORD")
        for role in ("app", "api", "worker", "reader")
    ]

    assert len(set(passwords)) == len(passwords)


def test_a_missing_env_file_says_which_step_was_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a missing file looks like a database that will not connect."""
    monkeypatch.setattr(local_mode_script, "ENV_FILE", tmp_path / ".env.local")

    with pytest.raises(LocalModeRefused, match="init"):
        local_mode_script._env_value("LEDGERBRIDGE_LOCAL_DATABASE_URL")


def test_the_generated_local_url_points_at_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The URL init writes must be one guard() accepts; otherwise step three
    refuses what step one produced."""
    env_file = tmp_path / ".env.local"
    monkeypatch.setattr(local_mode_script, "ENV_FILE", env_file)
    local_mode_script.command_init(argparse.Namespace())

    url = local_mode_script._env_value("LEDGERBRIDGE_LOCAL_DATABASE_URL")

    guard(settings(database_url=url, reader_database_url=url))


def test_an_unreachable_database_says_so_instead_of_raising_a_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "The launcher is wrong" and "the database is not up" must not look alike.

    Both otherwise surface as a page that will not load, and a raw psycopg
    traceback sends the reader into the wrong half of the system.
    """

    def refuse_to_connect(_url: str) -> object:
        raise OperationalError("select 1", {}, Exception("connection refused"))

    monkeypatch.setattr(ledgerbridge.db, "get_session_factory", refuse_to_connect)

    with pytest.raises(LocalModeRefused, match="did not answer"):
        local_mode_script._entity_refs(LOCAL_DATABASE)
