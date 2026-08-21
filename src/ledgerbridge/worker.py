import logging
import signal
import time
from pathlib import Path
from types import FrameType

from ledgerbridge.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
_running = True
HEARTBEAT_FILENAME = ".worker-heartbeat"
HEARTBEAT_INTERVAL_SECONDS = 5.0
HEARTBEAT_MAX_AGE_SECONDS = 30.0


def _stop(_signum: int, _frame: FrameType | None) -> None:
    global _running
    _running = False


def heartbeat_path(artifact_root: Path | None = None) -> Path:
    root = artifact_root if artifact_root is not None else get_settings().artifact_root
    return root / HEARTBEAT_FILENAME


def write_heartbeat(path: Path | None = None, now: float | None = None) -> None:
    target = path if path is not None else heartbeat_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.time() if now is None else now
    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_text(f"{timestamp:.6f}\n", encoding="ascii")
    temporary.replace(target)


def heartbeat_is_fresh(
    path: Path | None = None,
    now: float | None = None,
    max_age_seconds: float = HEARTBEAT_MAX_AGE_SECONDS,
) -> bool:
    target = path if path is not None else heartbeat_path()
    current_time = time.time() if now is None else now
    try:
        heartbeat_time = float(target.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    age = current_time - heartbeat_time
    return 0 <= age <= max_age_seconds


def main() -> None:
    global _running
    _running = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("LedgerBridge worker started")
    while _running:
        write_heartbeat()
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)
    logger.info("LedgerBridge worker stopped")


if __name__ == "__main__":
    main()
