"""Local mode must refuse every production-shaped configuration.

Almost every test here exercises a refusal, deliberately. What local mode
offers is "fewer gates", and a missing gate leaking into production looks
exactly like a working system on screen. So the evidence that it is safe is
that it refuses when it should, not that it starts on this machine.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from tempfile import gettempdir
from uuid import UUID

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

import ledgerbridge.db
import scripts.local_mode as local_mode_script
from ledgerbridge.config import Settings
from ledgerbridge.local_mode import (
    LOCAL_POLICY_GENERATION,
    LocalBook,
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
UNIT = UUID("10000000-0000-4000-8000-000000000002")
BOOK = LocalBook(entity_ref=ENTITY, business_units=(("household", UNIT),))


def _password_of(written: str, key: str) -> str:
    line = next(row for row in written.splitlines() if row.startswith(f"{key}="))
    return line.partition("=")[2]


def profile(**overrides: object) -> LocalProfile:
    fields: dict[str, object] = {
        "database_url": LOCAL_DATABASE,
        "api_database_url": LOCAL_DATABASE,
        "artifact_root": ARTIFACT_ROOT,
        "books": (BOOK,),
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


def test_refuses_a_remote_writer_even_when_the_reader_is_local() -> None:
    """Local mode holds two connections, so both have to be checked.

    The writer only ever appends "this document was opened", but it appends it
    somewhere, and a remote somewhere means a local session writing into
    production's audit chain.
    """
    with pytest.raises(LocalModeRefused, match="local database only"):
        guard(settings(api_database_url=REMOTE_DATABASE))


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
    verify = local_verifier(local_principal([BOOK]))

    verified = verify({"headers": [(b"x-ledgerbridge-principal", b"workload:somebody-else")]})

    assert verified.principal.principal_ref == "workload:local-single-user"
    assert verified.principal.grants[0].entity_ref == ENTITY


def test_unassigned_facts_stay_visible_locally() -> None:
    """Most personal rows carry no business unit at all.

    Refusing unassigned facts would open the local version on a book with most
    of it missing, which is the very thing that motivated writing a separate
    program in the first place.
    """
    principal = local_principal([BOOK])

    assert principal.grants[0].allow_unassigned_candidates


def test_assigned_facts_stay_visible_locally() -> None:
    """The grant carries both keys the read design keeps for a business unit.

    The HTTP layer authorizes on the human ref and the scoped SQL functions
    query the UUID, and the contract refuses to resolve one into the other. A
    grant naming only the entity therefore sees nothing that was ever assigned -
    which the live database demonstrated: an imported candidate was invisible
    until the units were granted, while every unit test still passed.
    """
    grant = local_principal([BOOK]).grants[0]

    assert grant.business_unit_refs == frozenset({"household"})
    assert grant.business_unit_ids == frozenset({UNIT})
    assert grant.business_unit_bindings == (("household", UNIT),)


def test_a_book_with_no_business_unit_is_still_granted() -> None:
    """A purely personal book has no units, and must still open."""
    grant = local_principal([LocalBook(entity_ref=ENTITY)]).grants[0]

    assert grant.business_unit_refs == frozenset()
    assert grant.allow_unassigned_candidates


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
    """The URL init writes must be one guard() accepts.

    Otherwise step three refuses what step one produced, and the contradiction
    is between two files rather than anywhere a reader would look.
    """
    env_file = tmp_path / ".env.local"
    monkeypatch.setattr(local_mode_script, "ENV_FILE", env_file)
    local_mode_script.command_init(argparse.Namespace())

    reader = local_mode_script._env_value("LEDGERBRIDGE_LOCAL_READER_DATABASE_URL")
    writer = local_mode_script._env_value("LEDGERBRIDGE_LOCAL_API_DATABASE_URL")

    guard(settings(database_url=reader, reader_database_url=reader, api_database_url=writer))


def test_each_connection_uses_the_role_its_job_needs() -> None:
    """Three jobs, three roles, exactly as production separates them.

    The reader can execute the internal_read definer functions and can select
    from no base table at all. That restriction is what the read design rests
    on, so serving as ledgerbridge_api or the owner would quietly delete the
    boundary while every page still rendered. The real database caught this:
    an api-role connection is refused "permission denied for table entity".
    """
    env_file = Path(tempfile.mkdtemp()) / ".env.local"
    original = local_mode_script.ENV_FILE
    local_mode_script.ENV_FILE = env_file
    try:
        local_mode_script.command_init(argparse.Namespace())
        served = local_mode_script._env_value("LEDGERBRIDGE_LOCAL_READER_DATABASE_URL")
        audited = local_mode_script._env_value("LEDGERBRIDGE_LOCAL_API_DATABASE_URL")
        bootstrap = local_mode_script._env_value("LEDGERBRIDGE_MIGRATION_DATABASE_URL")
    finally:
        local_mode_script.ENV_FILE = original

    assert "ledgerbridge_reader:" in served
    # Opening a document appends an audit event, which the reader cannot write.
    assert "ledgerbridge_api:" in audited
    # The entity list is a bootstrap question, asked once through the owner role
    # before the first request, never through the connection that serves them.
    assert "ledgerbridge_owner:" in bootstrap


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
        local_mode_script._books(LOCAL_DATABASE)


def test_the_local_app_answers_on_the_capabilities_route() -> None:
    """Wire the real app and ask it something that needs no database.

    Middleware order, the principal verifier and the settings override are all
    runtime wiring: get any of them wrong and the module still imports, the
    tests above still pass, and the failure only appears when someone opens the
    page. This is the cheapest thing that proves the assembly holds.
    """
    app = local_mode_script._build_app(profile())

    with TestClient(app) as client:
        response = client.get("/internal/v1/capabilities")

    assert response.status_code == 200


def test_the_local_app_serves_the_routes_the_ui_asks_for() -> None:
    """Mounting one router was enough to answer /capabilities and nothing else.

    The Web client asks for statements, reconciliations and reports as well, and
    a missing router does not fail at startup - it fails as a 404 on a page,
    which reads like missing data rather than a missing mount.
    """
    paths = {
        route.path
        for router in local_mode_script._read_routers()
        for route in router.routes
        if isinstance(route, APIRoute)
    }

    assert {
        "/internal/v1/candidates",
        "/internal/v1/personal-finance-summary",
        "/internal/v1/company-bank-statements/{statement_ref}",
        "/internal/v1/reconciliations/{month}",
        "/internal/v1/ledger-summary",
    } <= paths


def test_the_local_app_refuses_to_serve_a_writing_route() -> None:
    """Read-only has to be a property of the assembly, not of a curated list.

    Otherwise it holds only for as long as everyone adding a router remembers,
    and the failure is silent: a POST that works locally and is refused in
    production is the wrong way round.
    """
    from ledgerbridge.internal_candidate_command_routes import router as command_router

    routers = local_mode_script._read_routers()
    # The check must see something, or "no writer found" is vacuously true.
    assert sum(len(router.routes) for router in routers) > 5  # not vacuously true
    local_mode_script._refuse_non_read_routes(routers)

    with pytest.raises(LocalModeRefused, match="reads only"):
        local_mode_script._refuse_non_read_routes((*routers, command_router))


def test_a_command_router_contributes_its_reads_and_not_its_commands() -> None:
    """Two review views live inside a router that also writes decisions.

    Taking the route objects across rather than re-declaring them is what keeps
    each one's own dependencies - including the module gate its source router
    applies - instead of growing a second copy of somebody else's
    authorization that drifts quietly.
    """
    from ledgerbridge.internal_candidate_command_routes import router as command_router

    taken = local_mode_script._reads_of(command_router)

    assert {route.path for route in taken.routes if isinstance(route, APIRoute)} == {
        "/internal/v1/candidate-events",
        "/internal/v1/candidate-classification-groups",
    }
    assert all(route in command_router.routes for route in taken.routes)


def test_the_four_review_views_are_no_longer_disabled() -> None:
    """They answered 404 CANDIDATE_COMMAND_DISABLED until the module was on.

    The flag gates two whole routers, commands and reads alike, so the review
    screen's event history and its classification groups were dark for want of
    a setting. Turning it on is what the user asked for; this is the assertion
    that it reaches the routes rather than only the config object.

    The service is overridden because the real one opens the local database,
    which this test has no business needing: what is under test is whether the
    module gate lets the request through, and that is decided before any
    handler runs.
    """
    from ledgerbridge.internal_candidate_command_routes import (
        get_candidate_command_service,
        get_synthetic_review_service,
    )

    app = local_mode_script._build_app(profile())
    app.dependency_overrides[get_candidate_command_service] = get_synthetic_review_service

    with TestClient(app) as client:
        answers = {
            path: client.get(path).status_code
            for path in (
                "/internal/v1/candidate-events",
                "/internal/v1/candidate-classification-groups",
            )
        }

    assert set(answers.values()) == {200}


def test_the_command_module_reads_the_same_books_the_reads_do() -> None:
    """A synthetic command backend beside real candidates would be a fiction.

    The classification groups and event history would come from the packaged
    fixture while the candidates beside them came from the user's own ledger,
    and nothing on screen would say which was which.
    """
    built = local_settings(profile())

    assert built.internal_candidate_command_backend == "database"
    assert built.internal_read_backend == "database"


def test_the_command_assertion_key_is_minted_per_start_and_never_reused() -> None:
    """Nothing here presents an assertion, so nothing should keep a key.

    `Settings` requires one before it will accept the module, because in
    production a command carries a signed assertion. Local mode serves no
    command, so the key authorizes nothing - and a secret on disk that
    authorizes nothing is a liability with no compensating use.
    """
    first = local_settings(profile()).internal_command_assertion_key
    second = local_settings(profile()).internal_command_assertion_key

    assert first is not None and second is not None
    assert first.get_secret_value() != second.get_secret_value()


def test_enabling_the_module_still_mounts_no_command() -> None:
    """The point of the flag is four GET routes, not a writer on a laptop.

    Read-only stays a property the assembly asserts about itself: the routers
    contribute their reads, and the start fails if a writer is ever among them.
    """
    app = local_mode_script._build_app(profile())

    writers = {
        (route.path, sorted((route.methods or set()) - {"GET", "HEAD", "OPTIONS"}))
        for route in app.routes
        if isinstance(route, APIRoute) and (route.methods or set()) - {"GET", "HEAD", "OPTIONS"}
    }

    assert writers == set()


def test_refuses_an_open_command_operational_gate() -> None:
    """The command gate is production's, the same as the read gate above it."""
    with pytest.raises(LocalModeRefused, match="internal_candidate_command_operational_gate"):
        guard(
            settings(
                enable_internal_candidate_command_api=True,
                internal_candidate_command_backend="database",
                internal_candidate_command_operational_gate="d1-production-v1",
                internal_command_assertion_key="k" * 48,
                internal_command_assertion_issuer="local",
                internal_command_assertion_audience="local",
            )
        )
