"""Alembic environment. Used by ``lanpilot db upgrade`` and by ``alembic`` during development."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from lanpilot.db import models  # noqa: F401  (registers the tables on Base.metadata)
from lanpilot.db.base import Base

target_metadata = Base.metadata


def run_migrations_online() -> None:
    url = context.config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("sqlalchemy.url is not configured")
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,  # SQLite cannot ALTER most things in place
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported")

run_migrations_online()
