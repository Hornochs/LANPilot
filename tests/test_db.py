import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, select

from lanpilot.db import migrate
from lanpilot.db.base import Base
from lanpilot.db.engine import create_engine as create_async_engine
from lanpilot.db.engine import create_sessionmaker, database_url
from lanpilot.db.models import AppSetting


def tables(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {name for (name,) in rows}


def test_upgrade_creates_database_at_head(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "lanpilot.db"

    result = migrate.upgrade(database)

    assert result.previous is None
    assert result.current == migrate.head_revision()
    assert result.changed
    assert result.backup is None
    assert migrate.current_revision(database) == migrate.head_revision()
    assert "app_settings" in tables(database)


def test_database_and_directory_are_private(tmp_path: Path) -> None:
    database = tmp_path / "data" / "lanpilot.db"

    migrate.upgrade(database)

    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "lanpilot.db"
    migrate.upgrade(database)

    result = migrate.upgrade(database)

    assert not result.changed
    assert result.backup is None
    assert list(tmp_path.glob("*.backup-*")) == []


def test_existing_database_is_backed_up_before_migrating(tmp_path: Path) -> None:
    database = tmp_path / "lanpilot.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE precious (value TEXT)")
        connection.execute("INSERT INTO precious VALUES ('keep me')")
        connection.commit()

    result = migrate.upgrade(database)

    assert result.backup is not None
    assert result.backup.name.startswith("lanpilot.db.backup-")
    assert result.backup.name.endswith("-unversioned")
    assert stat.S_IMODE(result.backup.stat().st_mode) == 0o600
    with closing(sqlite3.connect(result.backup)) as connection:
        assert connection.execute("SELECT value FROM precious").fetchall() == [("keep me",)]
    assert "app_settings" not in tables(result.backup)
    assert "app_settings" in tables(database)


def test_current_revision_of_missing_database_is_none(tmp_path: Path) -> None:
    assert migrate.current_revision(tmp_path / "absent.db") is None


def test_models_and_migrations_describe_the_same_schema(tmp_path: Path) -> None:
    database = tmp_path / "lanpilot.db"
    migrate.upgrade(database)

    engine = create_engine(database_url(database, async_driver=False))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == []


async def test_connections_use_wal_and_foreign_keys(tmp_path: Path) -> None:
    database = tmp_path / "lanpilot.db"
    migrate.upgrade(database)
    engine = create_async_engine(database)
    try:
        async with engine.connect() as connection:
            journal = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar_one()
            foreign_keys = (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()
    finally:
        await engine.dispose()

    assert journal == "wal"
    assert foreign_keys == 1


async def test_datetimes_round_trip_as_aware_utc(tmp_path: Path) -> None:
    database = tmp_path / "lanpilot.db"
    migrate.upgrade(database)
    engine = create_async_engine(database)
    berlin_evening = datetime(2026, 10, 13, 21, 30, tzinfo=timezone(timedelta(hours=2)))
    try:
        sessions = create_sessionmaker(engine)
        async with sessions() as session:
            session.add(AppSetting(key="k", value="v", updated_at=berlin_evening))
            await session.commit()
        async with sessions() as session:
            stored = (await session.execute(select(AppSetting))).scalar_one()
    finally:
        await engine.dispose()

    assert stored.updated_at == datetime(2026, 10, 13, 19, 30, tzinfo=UTC)
    assert stored.updated_at.tzinfo is UTC


async def test_naive_datetimes_are_rejected(tmp_path: Path) -> None:
    database = tmp_path / "lanpilot.db"
    migrate.upgrade(database)
    engine = create_async_engine(database)
    try:
        async with create_sessionmaker(engine)() as session:
            naive = datetime(2026, 10, 13, 21, 30)  # noqa: DTZ001 - the point of the test
            session.add(AppSetting(key="k", value="v", updated_at=naive))
            with pytest.raises(Exception, match="naive datetime"):
                await session.commit()
    finally:
        await engine.dispose()


def test_map_images_are_moved_from_families_to_games(tmp_path: Path) -> None:
    """Migration 0007: images belong to a game now; only attributable ones survive."""
    database = tmp_path / "lanpilot.db"
    config = migrate.alembic_config(database)
    command.upgrade(config, "0006")
    gametracker = "https://image.gametracker.com/images/maps/160x120"
    with closing(sqlite3.connect(database)) as connection, connection:
        rows = [
            ("counterstrike", "de_dust2", "a", "gametracker", f"{gametracker}/cs/de_dust2.jpg"),
            ("counterstrike", "de_tides", "b", "gametracker", f"{gametracker}/css/de_tides.jpg"),
            ("counterstrike", "de_bank", "c", "gametracker", f"{gametracker}/csgo/de_bank.jpg"),
            ("counterstrike", "aim_map", "d", "gamebanana", "https://images.gamebanana.com/x"),
            ("counterstrike", "de_nuke", "e", "upload", None),
            ("demo", "dm_foundry", "f", "upload", None),
        ]
        connection.executemany(
            "INSERT INTO map_images (family, map_key, filename, provider, source_url, width,"
            " height, updated_at) VALUES (?, ?, ?, ?, ?, 1, 1, '2026-10-14 00:00:00')",
            rows,
        )
        connection.execute(
            "INSERT INTO map_image_misses (family, map_key, reason, checked_at)"
            " VALUES ('counterstrike', 'de_x', 'not_found', '2026-10-14 00:00:00')"
        )

    migrate.upgrade(database)

    with closing(sqlite3.connect(database)) as connection, connection:
        images = connection.execute(
            "SELECT game, map_key, filename FROM map_images ORDER BY filename"
        ).fetchall()
        misses = connection.execute("SELECT * FROM map_image_misses").fetchall()
    assert images == [
        ("cs16", "de_dust2", "a"),
        ("css", "de_tides", "b"),
        ("csgo", "de_bank", "c"),
        ("demo", "dm_foundry", "f"),
    ]
    assert misses == []


def test_only_the_newest_backups_are_kept(tmp_path: Path) -> None:
    database = tmp_path / "lanpilot.db"
    migrate.upgrade(database)
    for day in range(1, 9):  # older backups from earlier upgrades
        (tmp_path / f"lanpilot.db.backup-2026010{day}T000000Z-0001").touch()

    migrate._backup(database, "0007")

    backups = sorted(path.name for path in tmp_path.glob("lanpilot.db.backup-*"))
    assert len(backups) == migrate.KEEP_BACKUPS
    assert backups[0] == "lanpilot.db.backup-20260105T000000Z-0001"
