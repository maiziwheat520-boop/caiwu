"""Phase 3 process boundary for Microsoft Graph mail collection."""

import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.error("Mail collector is reserved for Phase 3 and is not implemented")
    raise SystemExit(2)


if __name__ == "__main__":
    main()
