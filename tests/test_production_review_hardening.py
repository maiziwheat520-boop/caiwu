from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_review_migration_closes_deployment_gaps() -> None:
    migration = (ROOT / "alembic/versions/20260829_0018_production_review_hardening.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision: str | None = "20260828_0017"' in migration
    assert "ledgerbridge(\\.test|\\.local)?" in migration
    assert "r1_validate_evidence_read_receipt() SECURITY DEFINER" in migration
    assert "REVOKE ALL ON FUNCTION %s FROM PUBLIC" in migration
    assert "ALTER ROLE ledgerbridge_app NOLOGIN" in migration
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public" in migration


def test_core_review_compose_initializes_socket_and_uses_literal_safe_healthcheck() -> None:
    compose = (ROOT / "docker-compose.core-review.yml").read_text(encoding="utf-8")

    assert "internal-socket-init:" in compose
    assert "condition: service_completed_successfully" in compose
    assert "chown 10001:10001 /run/ledgerbridge-internal" in compose
    assert "e=bytes((13,10))" in compose
    assert "sendall(b'GET /health/ready HTTP/1.0'+e" in compose
