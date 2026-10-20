"""Upgrading a database that holds data, from the first schema revision to the newest.

The database is brought up one revision at a time; after each one, data is written in the
schema of that revision, the way an installation of that version would have written it.
After the last step, everything must still be there and LANPilot must work with it: the
users can sign in, an open session is still valid, a stored password still decrypts.

Every migration needs an entry in ``DATA``; a test checks that none is missing, so a new
migration comes with data that exercises it.
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from lanpilot.auth import passwords
from lanpilot.auth.roles import Role
from lanpilot.auth.sessions import cookie_spec, hash_token
from lanpilot.config import Settings
from lanpilot.core import secrets
from lanpilot.db import migrate
from tests.conftest import PASSWORD, SECRET_KEY, ClientFactory, sign_in

BOX = secrets.SecretBox(secrets._decode_key(SECRET_KEY, "test"))
TOKEN = "an-old-session-token-that-must-survive-the-upgrade"
RCON_PASSWORD = "S3cret-Rcon-Passw0rd"
GAMETRACKER = "https://image.gametracker.com/images/maps/160x120"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _0001(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES ('motd', 'hello', ?)", (_now(),)
    )


def _0002(db: sqlite3.Connection) -> None:
    for name, role in (("root", Role.ADMIN), ("viewer1", Role.VIEWER)):
        db.execute(
            "INSERT INTO users (username, password_hash, role, is_active, created_at)"
            " VALUES (?, ?, ?, 1, ?)",
            (name, passwords.hash_password(PASSWORD), role.value, _now()),
        )
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen_at,"
        " expires_at, ip, user_agent) VALUES (?, 2, 'csrf', ?, ?, '2099-01-01 00:00:00',"
        " '10.0.0.9', 'test')",
        (hash_token(TOKEN), _now(), _now()),
    )
    db.execute(
        "INSERT INTO audit_log (created_at, actor_id, actor_name, action, detail, success)"
        " VALUES (?, 1, 'root', 'user.create', '{}', 1)",
        (_now(),),
    )


def _0003(db: sqlite3.Connection) -> None:
    credentials = BOX.encrypt(
        {"rcon_password": RCON_PASSWORD},
        context=secrets.server_context("cs16", "127.0.0.1", 27015),
    )
    db.execute(
        "INSERT INTO servers (name, adapter_id, host, port, settings, credentials, enabled,"
        " created_at, updated_at) VALUES ('CS Arena', 'cs16', '127.0.0.1', 27015, '{}', ?, 1,"
        " ?, ?)",
        (credentials, _now(), _now()),
    )


def _0004(db: sqlite3.Connection) -> None:
    db.executemany(
        "INSERT INTO server_maps (server_id, key, name, game_type, source, extra, updated_at)"
        " VALUES (1, ?, NULL, '', 'protocol', '{}', ?)",
        [("de_dust2", _now()), ("de_inferno", _now())],
    )


def _0005(db: sqlite3.Connection) -> None:
    db.executemany(
        "INSERT INTO map_images (family, map_key, filename, provider, source_url, width,"
        " height, updated_at) VALUES ('counterstrike', ?, ?, ?, ?, 160, 120, ?)",
        [
            (
                "de_dust2",
                "aaaaaaaaaaaaaaaaaaaaaaaa.webp",
                "gametracker",
                f"{GAMETRACKER}/cs/de_dust2.jpg",
                _now(),
            ),
            ("de_nuke", "bbbbbbbbbbbbbbbbbbbbbbbb.webp", "upload", None, _now()),
        ],
    )


def _0006(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO map_image_misses (family, map_key, reason, checked_at)"
        " VALUES ('counterstrike', 'aim_map', 'not_found', ?)",
        (_now(),),
    )


def _0007(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO map_images (game, map_key, filename, provider, source_url, width, height,"
        " updated_at) VALUES ('cs2', 'de_anubis', 'cccccccccccccccccccccccc.webp', 'upload',"
        " NULL, 1280, 720, ?)",
        (_now(),),
    )


def _0008(db: sqlite3.Connection) -> None:
    db.execute("UPDATE users SET language = 'de' WHERE username = 'viewer1'")


# Revision -> what an installation at that revision could have stored.
DATA: dict[str, Callable[[sqlite3.Connection], None]] = {
    "0001": _0001,
    "0002": _0002,
    "0003": _0003,
    "0004": _0004,
    "0005": _0005,
    "0006": _0006,
    "0007": _0007,
    "0008": _0008,
}


def _revisions() -> list[str]:
    config = migrate.alembic_config(Path("unused.db"))
    script = ScriptDirectory.from_config(config)
    return [revision.revision for revision in reversed(list(script.walk_revisions()))]


def test_every_migration_has_test_data() -> None:
    assert list(DATA) == _revisions(), "add data for the new migration to DATA"


def _build(database: Path) -> None:
    """A database that went through every revision, with data written at each."""
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = migrate.alembic_config(database)
    for revision in _revisions():
        command.upgrade(config, revision)
        with closing(sqlite3.connect(database)) as db, db:
            DATA[revision](db)


def test_upgrading_keeps_the_data(tmp_path: Path) -> None:
    database = tmp_path / "lanpilot.db"
    _build(database)

    with closing(sqlite3.connect(database)) as db:

        def rows(sql: str) -> list[tuple[object, ...]]:
            return db.execute(sql).fetchall()

        assert rows("SELECT key, value FROM app_settings") == [("motd", "hello")]
        assert rows("SELECT username, role, language FROM users ORDER BY id") == [
            ("root", "admin", None),
            ("viewer1", "viewer", "de"),
        ]
        assert rows("SELECT name, adapter_id, host, port FROM servers") == [
            ("CS Arena", "cs16", "127.0.0.1", 27015)
        ]
        assert rows("SELECT key FROM server_maps ORDER BY key") == [("de_dust2",), ("de_inferno",)]
        # 0007 kept the image whose game is known from its URL and dropped the upload.
        assert rows("SELECT game, map_key FROM map_images ORDER BY map_key") == [
            ("cs2", "de_anubis"),
            ("cs16", "de_dust2"),
        ]
        assert rows("SELECT * FROM map_image_misses") == []
        assert rows("SELECT action FROM audit_log") == [("user.create",)]
        [(credentials,)] = rows("SELECT credentials FROM servers")
    assert BOX.decrypt(
        str(credentials), context=secrets.server_context("cs16", "127.0.0.1", 27015)
    ) == {"rcon_password": RCON_PASSWORD}


def test_lanpilot_works_with_an_upgraded_database(
    make_client: ClientFactory, settings: Settings
) -> None:
    _build(settings.database_path)
    client = make_client()

    # A session opened before the upgrade is still valid.
    client.cookies.set(cookie_spec(settings.server).name, TOKEN)
    assert client.get("/").status_code == 200
    client.cookies.clear()

    sign_in(client, "root")
    page = client.get("/admin/servers")
    assert page.status_code == 200 and "CS Arena" in page.text
    assert "de_inferno" in client.get("/admin/servers/1/maps").text
    assert client.get("/admin/audit").status_code == 200


@pytest.mark.parametrize("revision", _revisions()[:-1])
def test_upgrade_from_each_revision_with_its_data(tmp_path: Path, revision: str) -> None:
    """An installation that stopped at ``revision`` upgrades in one go."""
    database = tmp_path / "lanpilot.db"
    config = migrate.alembic_config(database)
    for step in _revisions()[: _revisions().index(revision) + 1]:
        command.upgrade(config, step)
        with closing(sqlite3.connect(database)) as db, db:
            DATA[step](db)

    result = migrate.upgrade(database)

    assert result.previous == revision and result.current == _revisions()[-1]
    assert result.backup is not None and result.backup.is_file()
    assert migrate.current_revision(database) == _revisions()[-1]
