from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ledgerbridge.config import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def build_engine(database_url: str, database_role: str | None = None) -> Engine:
    if database_role is not None and not database_role.replace("_", "").isalnum():
        raise ValueError("database_role must contain only letters, digits, and underscores")
    engine = create_engine(database_url, pool_pre_ping=True)
    if database_role is not None and engine.dialect.name == "postgresql":

        @event.listens_for(engine, "connect")
        def set_runtime_role(dbapi_connection: object, _connection_record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute(f'SET ROLE "{database_role}"')
            finally:
                cursor.close()

    return engine


@lru_cache
def get_session_factory(
    database_url: str, database_role: str | None = None
) -> sessionmaker[Session]:
    return sessionmaker(
        bind=build_engine(database_url, database_role),
        expire_on_commit=False,
    )


def get_session() -> Iterator[Session]:
    settings = get_settings()
    session_factory = get_session_factory(settings.database_url, settings.database_role)
    with session_factory() as session:
        yield session
