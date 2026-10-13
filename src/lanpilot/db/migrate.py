"""Schema migrations.

Migrations ship inside the package, so an installed LANPilot can upgrade its own database
without a source checkout. Before an existing database is changed, a copy is written next
to it.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from lanpilot.db.engine import database_url

SCRIPT_LOCATION = "lanpilot.db:migrations"


@dataclass(frozen=True)
class MigrationResult:
    previous: str | None
    current: str
    backup: Path | None

    @property
    def changed(self) -> bool:
        return self.previous != self.current


def alembic_config(database: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    # ConfigParser treats % as interpolation syntax.
    url = database_url(database, async_driver=False).render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def head_revision() -> str:
    config = Config()
    config.set_main_option("script_location", SCRIPT_LOCATION)
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:  # pragma: no cover - the package always ships migrations
        raise RuntimeError("no migrations found")
    return head


def current_revision(database: Path) -> str | None:
    """Revision the database is at, or ``None`` if it does not exist or is unversioned."""
    if not database.is_file():
        return None
    engine = create_engine(database_url(database, async_driver=False))
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def _backup(database: Path, revision: str | None) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = database.with_name(f"{database.name}.backup-{stamp}-{revision or 'unversioned'}")
    target.touch(mode=0o600, exist_ok=False)
    # The backup API yields a consistent copy even while WAL frames are pending.
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(target)) as dest:
        source.backup(dest)
    return target


def upgrade(database: Path) -> MigrationResult:
    """Bring the database to the newest schema, creating it if necessary."""
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    head = head_revision()
    previous = current_revision(database)
    if previous == head:
        return MigrationResult(previous=previous, current=head, backup=None)

    backup = None
    if database.is_file() and database.stat().st_size > 0:
        backup = _backup(database, previous)

    command.upgrade(alembic_config(database), "head")
    database.chmod(0o600)
    return MigrationResult(previous=previous, current=head, backup=backup)
