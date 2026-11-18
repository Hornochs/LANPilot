"""Player list, kick, ban and the raw console, through the web interface (demo adapter)."""

import asyncio
import time
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lanpilot.auth.roles import Role
from lanpilot.core import runtime as runtime_module
from lanpilot.core import secrets
from lanpilot.core.actions import PLAYER_LIST_MAX_AGE_SECONDS, Actions, clean_players, clean_text
from lanpilot.core.adapters import GameAdapter, Player
from lanpilot.core.adapters.registry import AdapterRegistry
from lanpilot.core.events import EventBus
from lanpilot.core.runtime import ServerBusyError, ServerManager, ServerRuntime, ServerState
from tests.conftest import ClientFactory, Query, UserFactory
from tests.test_live import add_server, post, switch_user


@pytest.fixture
def users(make_user: UserFactory) -> None:
    make_user("root", Role.ADMIN)
    make_user("olga", Role.OPERATOR)
    make_user("vera", Role.VIEWER)


def wait_online(client: TestClient, server_id: int) -> None:
    deadline = time.monotonic() + 10
    while "online" not in client.get(f"/servers/{server_id}/live").text:
        assert time.monotonic() < deadline
        time.sleep(0.05)


def demo(client_factory: ClientFactory, query: Query, **env: str) -> tuple[TestClient, int]:
    client = client_factory(LANPILOT_ENABLE_DEMO_ADAPTER="true", **env)
    switch_user(client, "root")
    server_id = add_server(client, query)
    wait_online(client, server_id)
    return client, server_id


def test_players_are_listed_with_the_controls_the_role_allows(
    users: None, make_client: ClientFactory, query: Query
) -> None:
    client, server_id = demo(make_client, query)

    page = client.get(f"/servers/{server_id}").text
    assert "<h2>Players</h2>" in page and "Ada" in page and "Linus" in page
    assert "/players/kick" in page
    assert "/players/ban" not in page  # the demo game cannot ban

    switch_user(client, "vera")
    page = client.get(f"/servers/{server_id}").text
    assert "Grace" in page and "/players/kick" not in page
    fragment = client.get(f"/servers/{server_id}/players").text
    assert fragment.startswith('<section class="card" id="players-') and "Ada" in fragment


def test_kick_is_for_operators_and_audited(
    users: None, make_client: ClientFactory, query: Query
) -> None:
    client, server_id = demo(make_client, query)

    switch_user(client, "vera")
    assert post(client, f"/servers/{server_id}/players/kick", player_id="1")[0] == 403

    switch_user(client, "olga")
    status, _ = post(client, f"/servers/{server_id}/players/kick", player_id="1", reason="afk\x00!")
    assert status == 303
    assert "Ada" not in client.get(f"/servers/{server_id}/players").text

    status, page = post(client, f"/servers/{server_id}/players/kick", player_id="1")
    assert status == 502 and "no longer on the server" in page
    status, _ = post(client, f"/servers/{server_id}/players/ban", player_id="2")
    assert status == 403  # banning is for administrators

    rows = query(
        "SELECT actor_name, action, success, detail FROM audit_log "
        "WHERE action = 'server.kick' ORDER BY id"
    )
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("olga", "server.kick", 1),
        ("olga", "server.kick", 0),
    ]
    assert '"player": "Ada"' in rows[0][3] and '"reason": "afk!"' in rows[0][3]


def test_players_the_server_cannot_kick_have_no_button(
    users: None, make_client: ClientFactory, query: Query
) -> None:
    client = make_client(LANPILOT_ENABLE_DEMO_ADAPTER="true")
    switch_user(client, "root")
    server_id = add_server(client, query, c_players="4")
    wait_online(client, server_id)

    fragment = client.get(f"/servers/{server_id}/players").text
    row = fragment[fragment.index("Margaret") :].split("</tr>", 1)[0]
    assert "(bot)" in row and "/players/kick" not in row

    status, page = post(client, f"/servers/{server_id}/players/kick", player_id="4")
    assert status == 502 and "Margaret cannot be kicked" in page


def test_ban_needs_the_capability(users: None, make_client: ClientFactory, query: Query) -> None:
    client, server_id = demo(make_client, query)

    status, page = post(client, f"/servers/{server_id}/players/ban", player_id="2")

    assert status == 400 and "does not support this" in page


def test_console_is_off_unless_switched_on(
    users: None, make_client: ClientFactory, query: Query
) -> None:
    client, server_id = demo(make_client, query)

    assert "switched off" in client.get(f"/servers/{server_id}/console").text
    status, page = post(client, f"/servers/{server_id}/console", command="status")
    assert status == 403 and "switched off" in page
    assert query("SELECT count(*) FROM audit_log WHERE action = 'server.console'") == [(0,)]


def test_console_for_administrators(users: None, make_client: ClientFactory, query: Query) -> None:
    client, server_id = demo(make_client, query, LANPILOT_ENABLE_CONSOLE="true")

    assert 'href="/servers/' in client.get(f"/servers/{server_id}").text
    status, page = post(client, f"/servers/{server_id}/console", command="echo <b>hi</b>")
    assert status == 200
    assert "&lt;b&gt;hi&lt;/b&gt;" in page  # the server's answer is shown escaped
    status, page = post(client, f"/servers/{server_id}/console", command="two\nlines")
    assert status == 400

    switch_user(client, "olga")
    assert client.get(f"/servers/{server_id}/console").status_code == 403
    assert post(client, f"/servers/{server_id}/console", command="status")[0] == 403

    rows = query(
        "SELECT actor_name, success, detail FROM audit_log WHERE action = 'server.console'"
    )
    assert rows == [("root", 1, '{"command": "echo <b>hi</b>", "server": "Arena"}')]


@pytest.mark.parametrize(
    ("command", "recorded"),
    [
        ("rcon_password hunter2", "rcon_password (arguments not recorded)"),
        ("sv_password  s3cret x", "sv_password (arguments not recorded)"),
        ("set Engine.GameInfo AdminPassword x", "set (arguments not recorded)"),
        ("changelevel de_dust2", "changelevel de_dust2"),
        ("say " + "x" * 300, "say " + "x" * 196),
    ],
)
def test_console_commands_with_passwords_are_audited_without_them(
    command: str, recorded: str
) -> None:
    from lanpilot.core.actions import audited_command

    assert audited_command(command) == recorded


def test_player_entries_are_cleaned() -> None:
    reported = [
        Player(id="1", name="Ada\x1b[31m", score=3, ping=20, team="Red\n"),
        Player(id="1", name="duplicate"),
        Player(id="", name="no id"),
        Player(id="x" * 65, name="long id"),
        Player(id="2", name="  "),
    ]

    assert clean_players(reported) == [
        Player(id="1", name="Ada[31m", score=3, ping=20, team="Red"),
        Player(id="2", name="(no name)"),
    ]
    assert clean_text("a\tb\x00c" + "d" * 200, 10) == "a bcdddddd"


# --- sharing the player list (one query per server at a time) ------------------------------


class SlowManager:
    """Stands in for ServerManager: counts player queries and answers when released."""

    def __init__(self) -> None:
        self.queries = 0
        self.release = asyncio.Event()

    def state(self, server_id: int) -> SimpleNamespace:
        return SimpleNamespace(usable=True, adapter_name="Fake", can=lambda capability: True)

    async def call(self, server_id: int, operation: object, *, seconds: float) -> list[Player]:
        self.queries += 1
        await self.release.wait()
        return [Player(id="1", name="Ada")]


async def test_concurrent_player_lists_share_one_query(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SlowManager()
    actions = Actions(cast(ServerManager, manager), cast(async_sessionmaker[AsyncSession], None))

    waiting = [asyncio.create_task(actions.list_players(1)) for _ in range(50)]
    await asyncio.sleep(0)
    waiting[0].cancel()  # one viewer leaving does not cancel the query for the others
    manager.release.set()
    lists = await asyncio.gather(*waiting[1:])

    assert manager.queries == 1
    assert all([p.name for p in players] == ["Ada"] for players in lists)
    await actions.list_players(1)
    assert manager.queries == 1  # still fresh

    actions.forget_players(1)  # as after a kick
    await actions.list_players(1)
    assert manager.queries == 2

    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + PLAYER_LIST_MAX_AGE_SECONDS + 1)
    await actions.list_players(1)
    assert manager.queries == 3


async def test_waiting_for_the_lock_does_not_count_against_the_timeout() -> None:
    manager = ServerManager(
        sessionmaker=cast(async_sessionmaker[AsyncSession], None),
        registry=AdapterRegistry(),
        box=cast(secrets.SecretBox, None),
        bus=EventBus(),
        interval=3600,
        timeout=1,
    )
    runtime = ServerRuntime(state=cast(ServerState, None), adapter=cast(GameAdapter, object()))
    manager._runtimes[1] = runtime

    async def quick(adapter: GameAdapter) -> str:
        await asyncio.sleep(0.05)
        return "done"

    async with runtime.lock:
        later = asyncio.create_task(manager.call(1, quick, seconds=0.2))
        await asyncio.sleep(0.3)  # longer than the operation's own bound
    assert await later == "done"

    async def slow(adapter: GameAdapter) -> str:
        await asyncio.sleep(1)
        return "late"

    with pytest.raises(TimeoutError):
        await manager.call(1, slow, seconds=0.05)
    assert not runtime.lock.locked()


async def test_a_lock_that_stays_taken_means_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "LOCK_WAIT_SECONDS", 0.05)
    manager = ServerManager(
        sessionmaker=cast(async_sessionmaker[AsyncSession], None),
        registry=AdapterRegistry(),
        box=cast(secrets.SecretBox, None),
        bus=EventBus(),
        interval=3600,
        timeout=1,
    )
    runtime = ServerRuntime(state=cast(ServerState, None), adapter=cast(GameAdapter, object()))
    manager._runtimes[1] = runtime

    async def never(adapter: GameAdapter) -> str:
        raise AssertionError("must not run")

    async with runtime.lock:
        with pytest.raises(ServerBusyError):
            await manager.call(1, never, seconds=1)
