from pathlib import Path

from ledgerbridge.worker import heartbeat_is_fresh, write_heartbeat


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
