from pathlib import Path
from tempfile import gettempdir

import ledgerbridge.worker as worker
from ledgerbridge.worker import heartbeat_is_fresh, heartbeat_path, write_heartbeat


def test_worker_heartbeat_uses_ephemeral_runtime_path() -> None:
    assert heartbeat_path() == Path(gettempdir()) / "ledgerbridge-worker-heartbeat"


def test_worker_heartbeat_is_fresh_within_window(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    write_heartbeat(path, now=100.0)

    assert heartbeat_is_fresh(path, now=129.0, max_age_seconds=30.0)


def test_worker_heartbeat_rejects_missing_stale_future_and_malformed(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    assert not heartbeat_is_fresh(path, now=100.0)

    write_heartbeat(path, now=100.0)
    assert not heartbeat_is_fresh(path, now=131.0, max_age_seconds=30.0)
    assert not heartbeat_is_fresh(path, now=99.0, max_age_seconds=30.0)

    path.write_text("not-a-timestamp", encoding="ascii")
    assert not heartbeat_is_fresh(path, now=100.0)


def test_worker_main_writes_once_and_stops(monkeypatch: object) -> None:
    calls: list[str] = []

    monkeypatch.setattr(worker.signal, "signal", lambda *_args: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "build_evidence_importer", lambda: None)  # type: ignore[attr-defined]
    monkeypatch.setattr(worker, "write_heartbeat", lambda: calls.append("write"))  # type: ignore[attr-defined]
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: worker._stop(0, None))  # type: ignore[attr-defined]

    worker.main()

    assert calls == ["write"]


def test_worker_composition_enables_production_connector_boundary(
    monkeypatch: object,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class Settings:
        env = "production"
        database_url = "postgresql+psycopg://runtime"
        artifact_root = tmp_path
        artifact_max_bytes = 100
        artifact_total_max_bytes = 200
        artifact_staging_max_bytes = 300
        artifact_staging_ttl_seconds = 400

    monkeypatch.setattr(worker, "get_settings", lambda: Settings())  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        worker,
        "get_session_factory",
        lambda database_url: captured.update(database_url=database_url) or "sessions",
    )
    monkeypatch.setattr(worker, "ArtifactStore", lambda *args, **kwargs: (args, kwargs))  # type: ignore[attr-defined]

    def fake_importer(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(worker, "EvidenceImporter", fake_importer)  # type: ignore[attr-defined]

    worker.build_evidence_importer()

    assert captured["database_url"] == "postgresql+psycopg://runtime"
    assert captured["production"] is True
