"""Source RCON transport and the CS:Source, CS:GO and CS2 adapters, against the simulator."""

import asyncio
import struct
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from lanpilot.auth.roles import Role
from lanpilot.core.adapters import (
    AuthenticationError,
    Capability,
    CommandRejectedError,
    MapRef,
    ProtocolError,
    RestartMode,
    ServerUnreachableError,
)
from lanpilot.core.adapters.registry import load_registry
from lanpilot.core.adapters.schema import describe_fields
from lanpilot.core.transports import source_rcon
from lanpilot.core.transports.source_rcon import SourceRcon
from lanpilot.games.counterstrike.source import (
    CounterStrike2Adapter,
    CounterStrikeGlobalOffensiveAdapter,
    CounterStrikeSourceAdapter,
    SourceConnection,
    SourceCounterStrikeAdapter,
    parse_maps,
    parse_status,
    parse_workshop_maps,
)
from lanpilot.testing.contract import check_adapter
from lanpilot.testing.source import CS2_RESOURCES, SPLIT_SIZE, SourceSimulator
from tests.conftest import ClientFactory, Query, UserFactory, csrf_token, sign_in

PASSWORD = "rcon-s3cret"


@pytest.fixture(params=["cs2", "csgo", "css"])
async def server(request: pytest.FixtureRequest) -> AsyncIterator[SourceSimulator]:
    async with SourceSimulator(password=PASSWORD, game=request.param) as simulator:
        yield simulator


@pytest.fixture
async def cs2() -> AsyncIterator[SourceSimulator]:
    async with SourceSimulator(password=PASSWORD, workshop_maps=("bhop_water",)) as simulator:
        yield simulator


@pytest.fixture
async def css() -> AsyncIterator[SourceSimulator]:
    async with SourceSimulator(password=PASSWORD, game="css") as simulator:
        yield simulator


def rcon(server: SourceSimulator, password: str = PASSWORD) -> SourceRcon:
    return SourceRcon("127.0.0.1", server.port, password, timeout_seconds=0.5, quiet_seconds=0.05)


def connection(server: SourceSimulator, password: str = PASSWORD) -> SourceConnection:
    return SourceConnection(host="127.0.0.1", port=server.port, rcon_password=SecretStr(password))


# --- transport -----------------------------------------------------------------------------


async def test_execute_returns_the_output(server: SourceSimulator) -> None:
    client = rcon(server)

    space = "" if server.cs2 else " "  # CS:Source's echo adds a space
    assert await client.execute("echo hello") == f"hello{space}\n"
    assert await client.execute("mp_restartgame 1") == ""  # prints nothing
    assert "nosuchcommand" in await client.execute("nosuchcommand")
    assert await client.execute("echo again") == f"again{space}\n"
    await client.close()

    assert server.commands == ["echo hello", "mp_restartgame 1", "nosuchcommand", "echo again"]
    assert server.connections == 1  # one login for all commands


async def test_long_output_arrives_complete(server: SourceSimulator) -> None:
    server.maps = [f"de_map_{index:04d}" for index in range(800)]
    client = rcon(server)

    listing = await client.execute("maps *")
    await client.close()

    assert len(listing) > 2 * SPLIT_SIZE  # Source 1 sends this in several packets
    assert parse_maps(listing, cs2=server.cs2) == server.maps


async def test_wrong_password_is_sent_only_once(server: SourceSimulator) -> None:
    client = rcon(server, "wrong-password")

    for _ in range(5):
        with pytest.raises(AuthenticationError) as refused:
            await client.execute("status")
        assert "wrong-password" not in str(refused.value)

    assert server.bad_passwords == 1
    assert server.commands == []


@pytest.mark.parametrize("password", ["", "null\x00byte"])
def test_passwords_the_protocol_cannot_carry_are_refused_locally(password: str) -> None:
    with pytest.raises(AuthenticationError):
        SourceRcon("127.0.0.1", 27015, password)


@pytest.mark.parametrize(
    "command", ["", "   ", "status\nquit", "status\rquit", "a\x00b", "x" * 1001]
)
async def test_commands_that_would_break_the_packet_are_rejected(
    server: SourceSimulator, command: str
) -> None:
    with pytest.raises(CommandRejectedError):
        await rcon(server).execute(command)

    assert server.connections == 0


async def test_silent_server_is_unreachable(server: SourceSimulator) -> None:
    server.silent = True

    with pytest.raises(ServerUnreachableError):
        await rcon(server).execute("status")


async def test_closed_port_is_unreachable(server: SourceSimulator) -> None:
    client = rcon(server)
    server.stop()

    with pytest.raises(ServerUnreachableError):
        await client.execute("status")


async def test_dropped_connection_is_reported_and_reopened(server: SourceSimulator) -> None:
    client = rcon(server)
    server.drop_after_login = True

    with pytest.raises(ServerUnreachableError):
        await client.execute("echo one")

    server.drop_after_login = False
    assert (await client.execute("echo two")).strip() == "two"
    await client.close()
    assert server.connections == 2


async def test_idle_connection_is_replaced(
    server: SourceSimulator, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = rcon(server)
    await client.execute("echo one")
    monkeypatch.setattr(source_rcon, "IDLE_RECONNECT_SECONDS", 0.0)

    await client.execute("echo two")
    await client.close()

    assert server.connections == 2


async def test_command_after_a_csgo_map_change_uses_a_new_connection() -> None:
    async with SourceSimulator(password=PASSWORD, game="csgo") as server:
        client = rcon(server)
        assert await client.execute("changelevel cs_office") == ""  # log line removed
        assert await client.execute("echo after") == "after \n"
        await client.close()

    assert server.commands == ["changelevel cs_office", "echo after"]  # each sent once
    assert server.connections == 2


async def test_connection_lost_during_an_answer_is_not_retried() -> None:
    """Once the server has started answering, it ran the command: never send it twice."""
    commands: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def reply(packet_id: int, packet_type: int, body: bytes) -> bytes:
            payload = struct.pack("<ii", packet_id, packet_type) + body + b"\x00\x00"
            return struct.pack("<i", len(payload)) + payload

        await reader.read(1024)
        writer.write(reply(1, 2, b""))
        commands.append(await reader.read(1024))
        writer.write(reply(2, 0, b"first\n") + reply(3, 0, b""))  # a complete answer
        commands.append(await reader.read(1024))
        writer.write(reply(4, 0, b"partial"))  # half an answer, then the line drops
        await writer.drain()
        writer.close()

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        client = SourceRcon("127.0.0.1", port, "pw", timeout_seconds=0.5)
        assert await client.execute("echo first") == "first\n"
        with pytest.raises(ServerUnreachableError):
            await client.execute("changelevel de_dust2")  # on the kept connection
    finally:
        listener.close()

    assert len(commands) == 2  # the second command was not sent again


async def test_malformed_packet_is_a_protocol_error() -> None:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1024)
        writer.write(struct.pack("<i", 10_000_000) + b"x" * 64)  # absurd size field
        await writer.drain()
        await reader.read(1024)
        writer.close()

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ProtocolError):
            await SourceRcon("127.0.0.1", port, "pw", timeout_seconds=0.5).execute("status")
    finally:
        listener.close()


async def test_answers_of_an_abandoned_command_are_skipped() -> None:
    """Packets with ids the client is not waiting for are ignored."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def reply(packet_id: int, packet_type: int, body: bytes) -> bytes:
            payload = struct.pack("<ii", packet_id, packet_type) + body + b"\x00\x00"
            return struct.pack("<i", len(payload)) + payload

        await reader.read(1024)
        writer.write(reply(1, 2, b""))  # login accepted (the client's first id is 1)
        await reader.read(1024)
        writer.write(reply(999, 0, b"stale output\n") + reply(2, 0, b"fresh\n") + reply(3, 0, b""))
        await writer.drain()
        await reader.read(1024)
        writer.close()

    listener = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        client = SourceRcon("127.0.0.1", port, "pw", timeout_seconds=0.5)
        assert await client.execute("echo fresh") == "fresh\n"
        await client.close()
    finally:
        listener.close()


# --- parsing -------------------------------------------------------------------------------


def test_cs2_map_listing_leaves_out_other_resources() -> None:
    listing = "".join(f"\t{name}\n" for name in ("de_dust2", *CS2_RESOURCES, "cs_office"))
    listing += "\tevil;quit\n\ttwo words\n\tde_dust2\n"

    assert parse_maps(listing, cs2=True) == ["de_dust2", "cs_office"]


def test_source1_map_listing() -> None:
    # As measured on CS:Source, plus a ".bsp" variant and lines that must not count.
    listing = (
        "-------------\n"
        "PENDING:   (fs) de_dust2\n"
        "PENDING:   (fs) cs_office\n"
        "PENDING:   (fs) de_nuke.bsp\n"
        "PENDING:   (fs) de_dust2\n"
        "readme.txt\nPENDING:   (fs) evil;quit\nPENDING:   (fs) two words\nMaps listed\n"
    )

    assert parse_maps(listing, cs2=False) == ["de_dust2", "cs_office", "de_nuke"]


def test_workshop_listing() -> None:
    assert parse_workshop_maps("bhop_water\n\nsurf_x\nbad name\nbhop_water\n") == [
        "bhop_water",
        "surf_x",
    ]


# --- adapters ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter_class", "game"),
    [
        (CounterStrike2Adapter, "cs2"),
        (CounterStrikeGlobalOffensiveAdapter, "csgo"),
        (CounterStrikeSourceAdapter, "css"),
    ],
)
async def test_adapters_fulfil_the_contract(
    adapter_class: type[SourceCounterStrikeAdapter], game: Literal["cs2", "csgo", "css"]
) -> None:
    async with SourceSimulator(password=PASSWORD, game=game) as simulator:
        await check_adapter(adapter_class(connection(simulator)))


def test_adapters_are_registered_and_describe_their_form() -> None:
    registry = load_registry()
    assert registry.get("cs2") is CounterStrike2Adapter
    assert registry.get("csgo") is CounterStrikeGlobalOffensiveAdapter
    assert registry.get("css") is CounterStrikeSourceAdapter
    assert Capability.LOAD_STATS not in CounterStrike2Adapter.capabilities
    assert Capability.LOAD_STATS in CounterStrikeSourceAdapter.capabilities

    fields = {field.name: field for field in describe_fields(SourceConnection)}
    assert list(fields) == ["host", "port", "rcon_password"]
    assert fields["port"].default == 27015
    assert fields["rcon_password"].kind == "password"


async def test_cs2_status_maps_and_restarts(cs2: SourceSimulator) -> None:
    game = CounterStrike2Adapter(connection(cs2))

    status = await game.query()
    assert (status.name, status.players, status.max_players) == ("Simulated srcds", 2, 16)
    assert status.map == MapRef("de_dust2")
    assert status.load == {}

    maps = await game.list_maps()
    assert [item.key for item in maps] == [*sorted(cs2.maps), "bhop_water"]
    assert maps[-1].extra == {"workshop": "1"}

    await game.change_map(MapRef("cs_office"))
    assert (await game.query()).map == MapRef("cs_office")
    await game.restart(RestartMode.SOFT)
    await game.restart(RestartMode.HARD)
    await game.close()

    assert cs2.restarts == ["soft", "hard"]
    assert cs2.commands == [
        "maps *",
        "ds_workshop_listmaps",
        "changelevel cs_office",
        "mp_restartgame 1",
        "ds_workshop_listmaps",
        "changelevel cs_office",
    ]


async def test_cs2_workshop_maps(cs2: SourceSimulator) -> None:
    game = CounterStrike2Adapter(connection(cs2))
    [workshop] = [item for item in await game.list_maps() if item.extra]

    await game.change_map(workshop)
    assert (await game.query()).map == MapRef("bhop_water")
    await game.restart(RestartMode.HARD)  # the level is reloaded the workshop way, too
    await game.close()

    assert cs2.commands[-3:] == [
        "ds_workshop_changelevel bhop_water",
        "ds_workshop_listmaps",
        "ds_workshop_changelevel bhop_water",
    ]
    assert cs2.restarts == ["hard"]


async def test_unknown_map_is_reported(server: SourceSimulator) -> None:
    game = CounterStrikeSourceAdapter(connection(server))

    with pytest.raises(CommandRejectedError, match="does not have the map de_train"):
        await game.change_map(MapRef("de_train"))
    await game.close()


async def test_change_during_loading_is_reported(cs2: SourceSimulator) -> None:
    game = CounterStrike2Adapter(connection(cs2))
    cs2.loading = True

    with pytest.raises(CommandRejectedError, match="still loading"):
        await game.change_map(MapRef("cs_office"))
    with pytest.raises(CommandRejectedError, match="still loading"):
        await game.restart(RestartMode.HARD)
    await game.close()

    assert cs2.current_map == "de_dust2"


@pytest.mark.parametrize("name", ["de_dust2; quit", "de_dust2\nquit", 'x" quit', "", "a" * 65])
async def test_unsafe_map_names_never_reach_the_server(cs2: SourceSimulator, name: str) -> None:
    game = CounterStrike2Adapter(connection(cs2))

    with pytest.raises(CommandRejectedError, match="cannot be sent"):
        await game.change_map(MapRef(name))
    with pytest.raises(CommandRejectedError, match="cannot be sent"):
        await game.change_map(MapRef(name, extra={"workshop": "1"}))

    assert cs2.commands == []


async def test_source1_load_is_asked_once_a_minute(css: SourceSimulator) -> None:
    game = CounterStrikeSourceAdapter(connection(css))

    for _ in range(5):
        assert (await game.query()).load == {"cpu": 2.5, "fps": 66.67}
    await game.close()

    assert css.commands == ["stats"]


async def test_cs2_never_asks_for_stats(cs2: SourceSimulator) -> None:
    game = CounterStrike2Adapter(connection(cs2))

    await game.query()

    assert cs2.commands == []
    assert cs2.connections == 0


async def test_wrong_password_keeps_status_working(css: SourceSimulator) -> None:
    game = CounterStrikeSourceAdapter(connection(css, "wrong-password"))

    for _ in range(3):
        status = await game.query()
        assert status.map == MapRef("de_dust2")
        assert status.load == {}
    with pytest.raises(AuthenticationError):
        await game.list_maps()

    assert css.bad_passwords == 1


async def test_unreachable_server(cs2: SourceSimulator) -> None:
    game = CounterStrike2Adapter(connection(cs2))
    cs2.stop()

    with pytest.raises(ServerUnreachableError):
        await game.query()


# --- players and console -------------------------------------------------------------------


def test_source1_status_players_are_parsed() -> None:
    css = (
        "# userid name                uniqueid            connected ping loss state  adr\n"
        '#      2 "Ada"               [U:1:12345]         01:23       25    0 active '
        "10.0.0.5:27005\n"
        '#      3 "Bot01"             BOT                                     active\n'
        '#      4 "Eve" "x"           [U:1:9]             1:01:23     40    0 spawning '
        "10.0.0.6:27005\n"
    )
    csgo = (
        "# userid name uniqueid connected ping loss state rate adr\n"
        '#  2 1 "Ada" STEAM_1:1:12345 01:23 25 0 active 196608 10.0.0.5:27005\n'
        '# 3 2 "GOTV" BOT active 64\n'
        "#end\n"
    )

    assert [(p.id, p.name, p.ping, p.bot, ip) for p, ip in parse_status(css)] == [
        ("2", "Ada", 25, False, "10.0.0.5"),
        ("3", "Bot01", None, True, None),
        ("4", 'Eve" "x', 40, False, "10.0.0.6"),
    ]
    assert [(p.id, p.name, p.ping, p.bot, ip) for p, ip in parse_status(csgo)] == [
        ("2", "Ada", 25, False, "10.0.0.5"),
        ("3", "GOTV", None, True, None),
    ]


@pytest.mark.parametrize("game", ["css", "csgo"])
async def test_source1_players_kick_and_ban(game: Literal["css", "csgo"]) -> None:
    async with SourceSimulator(password=PASSWORD, game=game, players=3) as server:
        adapter_class = load_registry().get(game)
        assert adapter_class is not None
        adapter = adapter_class(connection(server))

        players = await adapter.list_players()
        assert [(p.id, p.name, p.bot, p.can_ban) for p in players] == [
            ("2", "Ada", False, True),
            ("3", "Bot01", True, False),
            ("4", "Player 3", False, True),
        ]
        await adapter.kick(players[1])
        await adapter.ban(players[2], "teamkilling!")

        commands = server.player_commands
        assert commands.kicks == [(3, ""), (4, "teamkilling!")]
        assert (commands.banned, commands.saved) == (["10.0.0.7"], 1)
        assert server.commands[-3:] == ["addip 0 10.0.0.7", "kickid 4 teamkilling!", "writeip"]
        await adapter.close()


async def test_cs2_has_a_console_but_no_player_control(cs2: SourceSimulator) -> None:
    adapter = CounterStrike2Adapter(connection(cs2))

    assert not adapter.supports(Capability.PLAYERS) and not adapter.supports(Capability.KICK)
    assert await adapter.console("echo hi") == "hi\n"
    await adapter.close()


# --- through the web interface -------------------------------------------------------------


@pytest.fixture
def background_server() -> Iterator[SourceSimulator]:
    """A simulator in its own thread: the test client blocks the test's event loop."""
    simulator = SourceSimulator(password=PASSWORD)
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(simulator.start())
        ready.set()
        loop.run_forever()
        simulator.stop()
        loop.run_until_complete(asyncio.sleep(0.01))
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5)
    yield simulator
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


def web_page(client: TestClient, server_id: int) -> str:
    return client.get(f"/servers/{server_id}").text


def test_cs2_server_in_the_web_interface(
    background_server: SourceSimulator,
    make_client: ClientFactory,
    make_user: UserFactory,
    query: Query,
) -> None:
    make_user("root", Role.ADMIN)
    client: TestClient = make_client()
    sign_in(client, "root")

    def post(path: str, **data: str) -> tuple[int, str]:
        response = client.post(path, data={**data, "csrf_token": csrf_token(client)})
        return response.status_code, response.text

    assert 'href="/admin/servers/new?adapter=cs2"' in client.get("/admin/servers/new").text

    status, page = post(
        "/admin/servers/new",
        adapter="cs2",
        name="CS2 Arena",
        enabled="1",
        c_host="127.0.0.1",
        c_port=str(background_server.port),
        c_rcon_password=PASSWORD,
    )
    assert status == 303, page
    [(server_id,)] = query("SELECT id FROM servers")
    assert query("SELECT count(*) FROM server_maps") == [(6,)]

    card = client.get(f"/servers/{server_id}/card").text
    assert "Counter-Strike 2" in card and "Dust II" in card and "online" in card  # catalogue name

    assert ">Office (cs_office)</option>" in web_page(client, server_id)
    [(office,)] = query("SELECT id FROM server_maps WHERE key = 'cs_office'")
    assert post(f"/servers/{server_id}/map", map_id=str(office))[0] == 303
    assert ">Office<" in client.get(f"/servers/{server_id}/card").text
    assert background_server.current_map == "cs_office"

    assert post(f"/servers/{server_id}/restart", mode="hard")[0] == 303
    assert background_server.restarts == ["hard"]
    for path in (f"/servers/{server_id}", "/admin/servers", "/admin/audit"):
        assert PASSWORD not in client.get(path).text
