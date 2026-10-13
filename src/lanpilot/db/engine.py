"""Engine and session factory for the SQLite database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def database_url(path: Path, *, async_driver: bool) -> URL:
    driver = "sqlite+aiosqlite" if async_driver else "sqlite"
    return URL.create(driver, database=str(path))


def _configure_connection(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def create_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(database_url(path, async_driver=True))
    event.listen(engine.sync_engine, "connect", _configure_connection)
    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
