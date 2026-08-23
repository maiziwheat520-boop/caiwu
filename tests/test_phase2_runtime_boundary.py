from pathlib import Path

import yaml


def test_only_worker_has_a_writable_artifact_volume() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    api = services["api"]
    worker = services["worker"]

    for service in (api, worker):
        assert service["read_only"] is True
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["cap_drop"] == ["ALL"]
        assert service["environment"]["LEDGERBRIDGE_ARTIFACT_MAX_BYTES"] == (
            "${LEDGERBRIDGE_ARTIFACT_MAX_BYTES:-52428800}"
        )
        assert "egress" not in service["networks"]

    assert api["volumes"] == ["artifacts:/var/lib/ledgerbridge/artifacts:ro"]
    assert worker["volumes"] == [
        "artifacts:/var/lib/ledgerbridge/artifacts",
        "connector-socket:/run/ledgerbridge-connector",
    ]
    assert api["environment"]["LEDGERBRIDGE_API_DATABASE_URL"] == (
        "${LEDGERBRIDGE_API_DATABASE_URL:?api database URL is required}"
    )
    assert worker["environment"]["LEDGERBRIDGE_WORKER_DATABASE_URL"] == (
        "${LEDGERBRIDGE_WORKER_DATABASE_URL:?worker database URL is required}"
    )
    assert "LEDGERBRIDGE_DATABASE_URL" not in api["environment"]
    assert "LEDGERBRIDGE_DATABASE_URL" not in worker["environment"]
    assert "?api database URL is required" in api["environment"]["LEDGERBRIDGE_API_DATABASE_URL"]


def test_connector_runner_is_networkless_and_has_no_application_secrets() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    runner = compose["services"]["connector-runner"]
    assert runner["profiles"] == ["connector-runner"]
    assert runner["network_mode"] == "none"
    assert runner["read_only"] is True
    assert runner["security_opt"] == ["no-new-privileges:true"]
    assert runner["cap_drop"] == ["ALL"]
    assert runner["pids_limit"] == 64
    assert runner["mem_limit"] == "128m"
    assert runner["cpus"] == "0.50"
    assert runner["volumes"] == ["connector-socket:/run/ledgerbridge-connector"]
    assert "environment" not in runner
    assert "networks" not in runner
    assert "depends_on" not in runner
    assert "connector-runner.Dockerfile" in runner["build"]["dockerfile"]


def test_only_worker_mounts_the_runner_socket() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert all(
        "connector-socket:/run/ledgerbridge-connector" not in services[name].get("volumes", [])
        for name in ("api", "postgres", "mail-collector")
    )
    assert "connector-socket:/run/ledgerbridge-connector" in services["worker"]["volumes"]


def test_artifact_directory_and_database_temp_privilege_are_hardened() -> None:
    dockerfile = Path("docker/app.Dockerfile").read_text(encoding="utf-8")
    init_script = Path("docker/postgres-init-runtime-role.sh").read_text(encoding="utf-8")

    assert "install -d -m 0700" in dockerfile
    assert "REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC" in init_script
    assert "ledgerbridge_api" in init_script
    assert "ledgerbridge_worker" in init_script
    assert "LEDGERBRIDGE_API_DB_PASSWORD:?" in init_script
    assert "LEDGERBRIDGE_WORKER_DB_PASSWORD:?" in init_script
    assert "runtime database passwords must be distinct" in init_script


def test_dispatch_acceptance_is_database_bound() -> None:
    migration = Path("alembic/versions/20260823_0008_dispatch_acceptance_binding.py").read_text(
        encoding="utf-8"
    )
    assert "SET search_path = pg_catalog" in migration
    assert "v_audit_xid IS DISTINCT FROM pg_current_xact_id()::text::xid" in migration
    assert "v_action IS DISTINCT FROM 'import.dispatch.accepted'" in migration
    assert "v_payload IS DISTINCT FROM v_expected" in migration
    assert (
        "REVOKE INSERT ON TABLE public.evidence_import_dispatch FROM ledgerbridge_api" in migration
    )
    assert "SECURITY DEFINER" in migration
