"""Fail CI if common credential or financial-evidence paths are trackable."""

import subprocess  # nosec B404
from pathlib import Path

CANDIDATES = (
    ".env.local",
    ".env.production",
    ".env.hermes",
    "token.json",
    "credentials.json",
    "oauth_token.json",
    "id_rsa",
    "server.key",
    "cert.pem",
    "private.pem",
    "report.ofx",
    "report.qif",
    "statement.pdf",
    "dump.sql",
    "backup.tar.gz",
)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for candidate in CANDIDATES:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", candidate],
            cwd=root,
            check=False,
        )  # nosec B603 B607
        if result.returncode != 0:
            failures.append(candidate)
    if failures:
        raise SystemExit("sensitive paths are not ignored: " + ", ".join(failures))


if __name__ == "__main__":
    main()
