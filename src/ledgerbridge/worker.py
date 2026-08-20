import logging
import signal
import time
from types import FrameType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
_running = True


def _stop(_signum: int, _frame: FrameType | None) -> None:
    global _running
    _running = False


def main() -> None:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("LedgerBridge worker scaffold started")
    while _running:
        time.sleep(1)
    logger.info("LedgerBridge worker stopped")


if __name__ == "__main__":
    main()
