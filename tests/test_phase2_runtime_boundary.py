from pathlib import Path


def test_only_worker_has_a_writable_artifact_volume() -> None:
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    shared, services = compose.split("services:\n", maxsplit=1)
    api, after_api = services.split("  worker:\n", maxsplit=1)
    worker, _after_worker = after_api.split("  migrate:\n", maxsplit=1)

    assert "read_only: true" in shared
    assert "- no-new-privileges:true" in shared
    assert "- ALL" in shared
    assert "artifacts:/var/lib/ledgerbridge/artifacts:ro" in api
    assert "artifacts:/var/lib/ledgerbridge/artifacts\n" in worker
    assert "artifacts:/var/lib/ledgerbridge/artifacts:ro" not in worker
    assert "egress" not in api
    assert "egress" not in worker
