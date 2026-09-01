"""Run one private, evidence-bound account-registry intake."""

from __future__ import annotations

import sys

from sqlalchemy import create_engine

from ledgerbridge.account_registry_intake import (
    AccountRegistryIntakeReceipt,
    LoadedAccountRegistryIntake,
    run_transactional_account_registry_intake,
)
from ledgerbridge.account_registry_intake_command import (
    run_account_registry_intake_command,
)


def _execute(
    loaded: LoadedAccountRegistryIntake,
    database_url: str,
    *,
    commit: bool,
) -> AccountRegistryIntakeReceipt:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        return run_transactional_account_registry_intake(
            engine,
            loaded,
            commit=commit,
        )
    finally:
        engine.dispose()


def main() -> int:
    try:
        return run_account_registry_intake_command(executor=_execute)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        print("ACCOUNT_REGISTRY_INTAKE_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
