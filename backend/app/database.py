from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def create_engine(database_url: str):
    return create_async_engine(
        database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


def create_session_factory(engine):
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


# Initialized in main.py lifespan
engine = None
async_session_factory = None


async def get_db() -> AsyncSession:
    async with async_session_factory() as session:
        yield session


def sqlstate(exc: DBAPIError) -> str | None:
    """The PostgreSQL error code behind a DBAPI error ("23505" for a unique violation)."""
    # The asyncpg dialect's own error carries it, or the asyncpg error behind it.
    return getattr(exc.orig, "sqlstate", None) or getattr(
        getattr(exc.orig, "__cause__", None), "sqlstate", None)
