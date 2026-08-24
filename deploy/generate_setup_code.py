from __future__ import annotations

import argparse
import hashlib
import secrets
import time


ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a one-use LedgerBridge Passkey setup code")
    parser.add_argument("--ttl", type=int, default=600, help="validity in seconds (default: 600)")
    args = parser.parse_args()
    if not 60 <= args.ttl <= 1800:
        raise SystemExit("ttl must be between 60 and 1800 seconds")
    raw = "".join(secrets.choice(ALPHABET) for _ in range(32))
    displayed = "-".join(raw[index : index + 4] for index in range(0, 32, 4))
    print(f"setup_code={displayed}")
    print(f"SETUP_CODE_SHA256={hashlib.sha256(raw.encode()).hexdigest()}")
    print(f"SETUP_CODE_EXPIRES_AT={int(time.time()) + args.ttl}")


if __name__ == "__main__":
    main()
