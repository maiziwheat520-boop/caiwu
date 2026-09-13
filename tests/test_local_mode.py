"""Local mode must refuse every production-shaped configuration.

Almost every test here exercises a refusal, deliberately. What local mode
offers is "fewer gates", and a missing gate leaking into production looks
exactly like a working system on screen. So the evidence that it is safe is
that it refuses when it should, not that it starts on this machine.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from tempfile import gettempdir
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

import ledgerbridge.db
import scripts.local_mode as local_mode_script
from ledgerbridge.config import Settings
from ledgerbridge.internal_read_contract import READ_CAPABILITIES, Capability
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
#: Local Core answers only when addressed as this machine.
LOCAL_BASE_URL = "http://127.0.0.1:8661"


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

    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
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
        for router in local_mode_script._routers()
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


def test_the_local_app_refuses_a_writing_route_it_did_not_list() -> None:
    """What local mode may write has to be a property of the assembly.

    Otherwise it holds only for as long as everyone adding a router remembers,
    and the failure is silent: a POST that works locally and is refused in
    production is the wrong way round.
    """
    from ledgerbridge.company_transaction_classification_routes import (
        router as classification_router,
    )

    routers = local_mode_script._routers()
    # The check must see something, or "no writer found" is vacuously true.
    assert sum(len(router.routes) for router in routers) > 5
    local_mode_script._refuse_unlisted_writes(routers)

    with pytest.raises(LocalModeRefused, match="candidate decisions only"):
        local_mode_script._refuse_unlisted_writes((*routers, classification_router))


def test_the_company_classification_router_contributes_its_reads_only() -> None:
    """Reviewing a company transaction's classification was not what was opened.

    Taking the route objects across rather than re-declaring them is what keeps
    each one's own dependencies - including the module gate its source router
    applies - instead of growing a second copy of somebody else's authorization.
    """
    from ledgerbridge.company_transaction_classification_routes import (
        router as classification_router,
    )

    taken = local_mode_script._reads_of(classification_router)

    assert {route.path for route in taken.routes if isinstance(route, APIRoute)} == {
        "/internal/v1/company-transaction-classifications",
        "/internal/v1/company-transaction-classification-summary",
    }
    assert all(route in classification_router.routes for route in taken.routes)


def test_the_assembled_app_writes_exactly_the_two_candidate_decisions() -> None:
    """The user opened review: confirm, ignore, correct - and nothing beside it.

    Walked over the routers exactly as mounted. Not over `app.routes`: FastAPI
    wraps an included router, so walking the app finds no route at all - an
    earlier version of this test did that and passed by seeing nothing.
    """
    from ledgerbridge.local_commands import router as local_commands

    writers = [
        (method, route)
        for router in local_mode_script._routers()
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in (route.methods or set()) - {"GET", "HEAD", "OPTIONS"}
    ]

    # A list, not a set: the signed production route and the local one share a
    # path, and a set would not notice both being mounted.
    assert sorted((method, route.path) for method, route in writers) == sorted(
        local_mode_script.LOCAL_COMMAND_ROUTES
    )
    assert all(route in local_commands.routes for _, route in writers)


def test_a_rebound_name_or_a_browser_write_is_refused() -> None:
    """DNS rebinding: a website whose own name now resolves to 127.0.0.1.

    Local decisions carry no signature, so the loopback bind alone would let such
    a page decide candidates. It cannot change the Host header, and a browser
    sends Origin on every POST.
    """
    app = local_mode_script._build_app(profile())
    path = f"/internal/v1/candidates/{uuid4()}/decisions"
    body = {"decision": "CONFIRM", "expected_revision": 1}
    idempotency = {"Idempotency-Key": str(uuid4())}

    with TestClient(app, base_url="http://evil.example:8661") as rebound:
        assert rebound.get("/internal/v1/capabilities").status_code == 421
        assert rebound.post(path, json=body, headers=idempotency).status_code == 421
    with TestClient(app, base_url="http://127.0.0.1:1") as wrong_port:
        assert wrong_port.get("/internal/v1/capabilities").status_code == 421
    with TestClient(app, base_url=LOCAL_BASE_URL) as local:
        browser = {**idempotency, "Origin": "http://evil.example:8661"}
        assert local.post(path, json=body, headers=browser).status_code == 421
    with TestClient(app, base_url="http://localhost:8661") as by_name:
        assert by_name.get("/internal/v1/capabilities").status_code == 200


def test_the_unsigned_decisions_refuse_a_deployed_configuration() -> None:
    """Mounted in the deployed app by mistake, they must still not answer."""
    from ledgerbridge.internal_candidate_command_routes import InternalCandidateCommandProblem
    from ledgerbridge.local_commands import require_local_profile

    require_local_profile(local_settings(profile()))
    deployed_like = (
        local_settings(profile()).model_copy(update={"env": "production"}),
        local_settings(profile()).model_copy(update={"internal_read_transport": "unix-mtls-proxy"}),
        local_settings(profile()).model_copy(
            update={"internal_candidate_command_operational_gate": "d1-production-v1"}
        ),
    )
    for built in deployed_like:
        with pytest.raises(InternalCandidateCommandProblem):
            require_local_profile(built)


def test_the_deployed_app_never_mounts_the_unsigned_decisions() -> None:
    from ledgerbridge.main import app

    def endpoints(routes: object) -> list[str]:
        found: list[str] = []
        for route in routes:  # type: ignore[attr-defined]
            endpoint = getattr(route, "endpoint", None)
            if endpoint is not None:
                found.append(endpoint.__module__)
            # FastAPI wraps an included router; its routes sit on the original.
            inner = getattr(route, "original_router", route)
            found.extend(endpoints(getattr(inner, "routes", ())))
        return found

    modules = endpoints(app.router.routes)
    assert "ledgerbridge.internal_candidate_command_routes" in modules
    assert "ledgerbridge.local_commands" not in modules


def test_the_signed_decision_router_cannot_be_mounted_beside_the_local_one() -> None:
    from ledgerbridge.internal_candidate_command_routes import router as signed

    routers = local_mode_script._routers()
    with pytest.raises(LocalModeRefused, match=r"must come from ledgerbridge.local_commands"):
        local_mode_script._refuse_unlisted_writes((signed, *routers))


def test_the_local_identity_may_decide_candidates_and_nothing_else() -> None:
    principal = local_principal((BOOK,))

    assert Capability.CANDIDATE_DECIDE in principal.capabilities
    assert principal.capabilities == READ_CAPABILITIES | {Capability.CANDIDATE_DECIDE}
    for withheld in (
        Capability.CANDIDATE_CREATE,
        Capability.CANDIDATE_SUPERSEDE,
        Capability.EVIDENCE_UNLOCK,
        Capability.ACCOUNT_REGISTRY_WRITE,
        Capability.BANK_STATEMENT_REVIEW_DECIDE,
        Capability.PAYROLL_COMMAND,
    ):
        assert withheld not in principal.capabilities


def test_a_decision_needs_no_signature_and_a_retry_replays() -> None:
    """The user dropped the signed assertion locally ("去掉签名").

    The service is the synthetic one, so no database is touched. What is under
    test is the assembly: a decision with no assertion header is accepted, a
    retry of the same operation replays rather than conflicting on the derived
    assertion id, and a stale revision is still refused - the checks below the
    envelope are all still there.
    """
    from ledgerbridge.internal_candidate_command import SyntheticInternalReviewService
    from ledgerbridge.internal_candidate_command_routes import get_candidate_command_service

    candidate = UUID("30000000-0000-4000-8000-000000000003")
    service = SyntheticInternalReviewService()
    # The synthetic candidates sit in these two units of the synthetic entity.
    synthetic_book = LocalBook(
        entity_ref=ENTITY,
        business_units=(
            ("unit-demo-a", UUID("10000000-0000-4000-8000-00000000000a")),
            ("unit-reviewed", UUID("10000000-0000-4000-8000-00000000000b")),
        ),
    )
    app = local_mode_script._build_app(profile(books=(synthetic_book,)))
    app.dependency_overrides[get_candidate_command_service] = lambda: service

    def post(revision: int, operation: UUID) -> tuple[int, dict[str, object]]:
        response = client.post(
            f"/internal/v1/candidates/{candidate}/decisions",
            content=json.dumps(
                {"decision": "CONFIRM", "expected_revision": revision, "reason": "synthetic"}
            ),
            headers={"Content-Type": "application/json", "Idempotency-Key": str(operation)},
        )
        return response.status_code, response.json()

    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
        stale_status, stale = post(99, uuid4())
        operation = uuid4()
        first_status, first = post(1, operation)
        again_status, again = post(1, operation)

    assert (stale_status, stale["code"]) == (409, "STALE_REVISION")
    assert first_status == 200 and first["replayed"] is False
    assert again_status == 200 and again["replayed"] is True


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

    with TestClient(app, base_url=LOCAL_BASE_URL) as client:
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
