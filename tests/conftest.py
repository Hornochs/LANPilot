import asyncio
import base64
import logging
import re
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from lanpilot.app import create_app
from lanpilot.auth import passwords, users
from lanpilot.auth.roles import Role
from lanpilot.config import Settings, load_settings
from lanpilot.db import migrate
from lanpilot.db.engine import create_engine, create_sessionmaker

PUBLIC_URL = "https://lanpilot.test"
# Derived from readable text so that no secret scanner mistakes it for a real key.
SECRET_KEY = base64.urlsafe_b64encode(b"lanpilot-test-key-0123456789abcd").decode()
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep LANPILOT_* variables of the developer's shell out of the tests."""
    import os

    for name in list(os.environ):
        if name.startswith("LANPILOT_"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _fast_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real Argon2 parameters would add seconds to the suite without testing anything more."""
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr(passwords, "_hasher", cheap)
    passwords._dummy_hash.cache_clear()


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    """`lanpilot serve` installs a log handler on the captured stderr; undo that per test."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "LANPILOT_SERVER__PUBLIC_URL": PUBLIC_URL,
        "LANPILOT_DATA_DIR": str(tmp_path / "data"),
        "LANPILOT_SECRET_KEY": SECRET_KEY,
    }


@pytest.fixture
def settings(base_env: dict[str, str]) -> Settings:
    return load_settings(environ=base_env)


@pytest.fixture(scope="session")
def _migrated_database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One database at the newest schema; running every migration per test is slow."""
    database = tmp_path_factory.mktemp("template") / "lanpilot.db"
    migrate.upgrade(database)
    return database


Prepare = Callable[[Settings], None]


@pytest.fixture
def prepare_database(_migrated_database: Path) -> Prepare:
    def prepare(settings: Settings) -> None:
        if not settings.database_path.exists():
            settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy(_migrated_database, settings.database_path)

    return prepare


ClientFactory = Callable[..., TestClient]


@pytest.fixture
def make_client(base_env: dict[str, str], prepare_database: Prepare) -> Iterator[ClientFactory]:
    """Build clients for the same database, optionally with different settings.

    Requests carry a same-origin ``Origin`` header like a browser would send; tests of the
    origin check override it.
    """
    opened: list[TestClient] = []

    def factory(**env: str) -> TestClient:
        settings = load_settings(environ={**base_env, **env})
        prepare_database(settings)
        origin = settings.server.public_origin
        client = TestClient(
            create_app(settings),
            base_url=settings.server.public_url,
            headers={"Origin": origin},
            follow_redirects=False,
        )
        client.__enter__()
        opened.append(client)
        return client

    yield factory
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    return make_client()


UserFactory = Callable[..., None]


@pytest.fixture
def make_user(settings: Settings, prepare_database: Prepare) -> UserFactory:
    def factory(username: str, role: Role = Role.VIEWER, password: str = PASSWORD) -> None:
        async def create() -> None:
            engine = create_engine(settings.database_path)
            try:
                async with create_sessionmaker(engine)() as db:
                    await users.create_user(db, username, password, role)
                    await db.commit()
            finally:
                await engine.dispose()

        prepare_database(settings)
        asyncio.run(create())

    return factory


Query = Callable[..., list[tuple[Any, ...]]]


@pytest.fixture
def query(settings: Settings) -> Query:
    """Run SQL directly against the database, for assertions on stored state."""

    def run(sql: str, *parameters: object) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(settings.database_path)) as connection:
            rows = connection.execute(sql, parameters).fetchall()
            connection.commit()
        return rows

    return run


def sign_in(client: TestClient, username: str, password: str = PASSWORD) -> None:
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 303, response.text


def csrf_token(client: TestClient, path: str = "/account") -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text)
    assert match, "no CSRF token on the page"
    return match.group(1)
