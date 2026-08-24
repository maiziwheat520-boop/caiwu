from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

import ledgerbridge.models  # noqa: F401
from alembic import context
from ledgerbridge.config import escape_alembic_ini_value, get_settings
from ledgerbridge.db import Base

config = context.config
database_url = config.attributes.get("database_url")
if database_url is None:
    database_url = get_settings().database_url
if database_url is None:
    raise RuntimeError("database_url is required for Alembic")
config.set_main_option("sqlalchemy.url", escape_alembic_ini_value(database_url))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
