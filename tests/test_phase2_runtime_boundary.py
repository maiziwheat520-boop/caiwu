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
    assert worker["volumes"] == ["artifacts:/var/lib/ledgerbridge/artifacts"]


def test_artifact_directory_and_database_temp_privilege_are_hardened() -> None:
    dockerfile = Path("docker/app.Dockerfile").read_text(encoding="utf-8")
    init_script = Path("docker/postgres-init-runtime-role.sh").read_text(encoding="utf-8")

    assert "install -d -m 0700" in dockerfile
    assert "REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC" in init_script
