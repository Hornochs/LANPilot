"""ElDewrito transport and adapter, against the simulator."""

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from ipaddress import IPv4Network

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from lanpilot.auth.roles import Role
from lanpilot.core import catalog
from lanpilot.core.adapters import (
    AuthenticationError,
    Capability,
    CommandRejectedError,
    MapRef,
    Player,
    ProtocolError,
    RestartMode,
    ServerUnreachableError,
    UnsupportedError,
)
from lanpilot.core.adapters.registry import load_registry
from lanpilot.core.adapters.schema import describe_fields
from lanpilot.core.transports.dewrito import DewRcon, get_info
from lanpilot.games import eldewrito
from lanpilot.games.eldewrito import (
    GAME_TYPE_KEY,
    GAME_TYPES,
    ElDewritoAdapter,
    ElDewritoConnection,
    parse_list_players,
)
from lanpilot.testing.contract import check_adapter
from lanpilot.testing.eldewrito import ElDewritoSimulator, split_arguments
from tests.conftest import ClientFactory, Query, UserFactory, csrf_token, sign_in

PASSWORD = "rcon-s3cret"
UID_A = "0123456789abcdef"
UID_B = "fedcba9876543210"


@pytest.fixture(autouse=True)
def fast_lobby(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eldewrito, "LOBBY_POLL_SECONDS", 0.01)


@pytest.fixture
async def server() -> AsyncIterator[ElDewritoSimulator]:
    async with ElDewritoSimulator(password=PASSWORD) as simulator:
        yield simulator


def adapter(server: ElDewritoSimulator, password: str = PASSWORD) -> ElDewritoAdapter:
    return ElDewritoAdapter(
        ElDewritoConnection(
            host="127.0.0.1",
            port=server.rcon_port,
            status_port=server.status_port,
            rcon_password=SecretStr(password),
        )
    )


# --- parsing ---------------------------------------------------------------------------------


def test_list_players_rows_are_parsed_from_both_ends() -> None:
    answer = (
        '[0] "Polo" (uid: 0123456789abcdef, ip: 10.0.0.5:11774)\n'
        '[3] "a "quoted" (name)" (uid: fedcba9876543210, ip: 10.0.0.6:11774)\n'
        '[4] "Local" (uid: 1111111111111111)\n'
        "No session found, are you in a game?\n"
        '[99] "x" (uid: nothex)\n'
    )
    rows = parse_list_players(answer)

    assert [(row.slot, row.name, row.uid, row.ip) for row in rows] == [
        ("0", "Polo", UID_A, "10.0.0.5"),
        ("3", 'a "quoted" (name)', UID_B, "10.0.0.6"),
        ("4", "Local", "1111111111111111", None),
    ]


def test_simulator_splits_arguments_like_the_server() -> None:
    # Measured on 0.7.1: quotes group, text after a closing quote is glued on, ";" is text.
    assert split_arguments('Server.Name "HaloOnline Server"') == [
        "Server.Name",
        "HaloOnline Server",
    ]
    assert split_arguments("Server.Name HaloOnline Server") == [
        "Server.Name",
        "HaloOnline",
        "Server",
    ]
    assert split_arguments('Server.Name "x"; Server.Name') == ["Server.Name", "x;", "Server.Name"]
    assert split_arguments('Game.Map "guardian') == ["Game.Map", "guardian"]


# --- transport -------------------------------------------------------------------------------


async def test_status_is_read_without_password(server: ElDewritoSimulator) -> None:
    info = await get_info("127.0.0.1", server.status_port)
    assert info["mapFile"] == "guardian" and info["players"] == []


async def test_join_password_hides_players_unless_given(server: ElDewritoSimulator) -> None:
    server.join_password = "joinpw"
    assert (await get_info("127.0.0.1", server.status_port))["passworded"] is True
    info = await get_info("127.0.0.1", server.status_port, join_password="joinpw")
    assert "passworded" not in info and info["players"] == []


async def test_rcon_logs_in_once_and_keeps_the_connection(server: ElDewritoSimulator) -> None:
    client = DewRcon("127.0.0.1", server.rcon_port, PASSWORD)
    try:
        assert await client.execute("Game.Version") == "0.7.1"
        assert await client.execute("Server.Say hello") == ""
        assert await client.execute("Foo.Bar") == "Command/Variable not found"
    finally:
        await client.close()
    assert server.logins == 1


async def test_a_closed_connection_is_opened_again(server: ElDewritoSimulator) -> None:
    client = DewRcon("127.0.0.1", server.rcon_port, PASSWORD)
    try:
        await client.execute("Game.Version")
        assert client._socket is not None
        await client._socket.close()
        assert await client.execute("Game.Version") == "0.7.1"
    finally:
        await client.close()
    assert server.logins == 2


async def test_wrong_password_is_sent_only_once(server: ElDewritoSimulator) -> None:
    client = DewRcon("127.0.0.1", server.rcon_port, "wrong-password")
    try:
        with pytest.raises(AuthenticationError) as first:
            await client.execute("Game.Version")
        with pytest.raises(AuthenticationError, match="Edit the server"):
            await client.execute("Game.Version")
    finally:
        await client.close()
    assert "wrong-password" not in str(first.value)
    assert server.commands == []


@pytest.mark.parametrize("command", ["", "  ", "a\nb", "a\rb", "x" * 1001])
async def test_commands_that_are_not_one_line_are_rejected(
    server: ElDewritoSimulator, command: str
) -> None:
    client = DewRcon("127.0.0.1", server.rcon_port, PASSWORD)
    with pytest.raises(CommandRejectedError):
        await client.execute(command)
    await client.close()
    assert server.logins == 0


async def test_a_port_without_the_subprotocol_is_reported(server: ElDewritoSimulator) -> None:
    # The status port speaks HTTP but no WebSocket.
    client = DewRcon("127.0.0.1", server.status_port, PASSWORD)
    with pytest.raises(ProtocolError, match="does not speak"):
        await client.execute("Game.Version")
    await client.close()


async def test_unreachable_server(server: ElDewritoSimulator) -> None:
    status_port, rcon_port = server.status_port, server.rcon_port
    await server.stop()
    with pytest.raises(ServerUnreachableError):
        await get_info("127.0.0.1", status_port, seconds=1)
    client = DewRcon("127.0.0.1", rcon_port, PASSWORD, seconds=1)
    with pytest.raises(ServerUnreachableError):
        await client.execute("Game.Version")
    await client.close()


# --- adapter ---------------------------------------------------------------------------------


async def test_adapter_fulfils_the_contract(server: ElDewritoSimulator) -> None:
    await check_adapter(adapter(server))


def test_adapter_is_registered_and_describes_its_form() -> None:
    assert load_registry().get("eldewrito") is ElDewritoAdapter
    fields = {field.name: field for field in describe_fields(ElDewritoConnection)}
    assert list(fields) == ["host", "port", "status_port", "rcon_password"]
    assert (fields["port"].default, fields["status_port"].default) == (11776, 11775)
    assert fields["rcon_password"].kind == "password"
    assert Capability.RESTART_SOFT not in ElDewritoAdapter.capabilities


def test_catalogue_names_every_stock_map() -> None:
    assert len(catalog.load().keys("eldewrito")) == 12
    assert catalog.load().name("eldewrito", "zanzibar") == "Last Resort"


async def test_status(server: ElDewritoSimulator) -> None:
    server.add_player("Polo", UID_A)
    status = await adapter(server).query()

    assert (status.name, status.players, status.max_players) == ("HaloOnline Server", 1, 16)
    assert status.map == MapRef("guardian", "Guardian")
    assert status.game_type is None  # "none" in the lobby
    assert status.password_protected is False

    server.join_password = "joinpw"
    assert (await adapter(server).query()).password_protected is True


async def test_status_works_with_a_wrong_rcon_password(server: ElDewritoSimulator) -> None:
    wrong = adapter(server, "wrong-password")
    assert (await wrong.query()).map is not None
    with pytest.raises(AuthenticationError):
        await wrong.list_maps()
    await wrong.close()


async def test_maps_are_listed_once_per_game_type(server: ElDewritoSimulator) -> None:
    maps = await adapter(server).list_maps()

    assert len(maps) == 12 * len(GAME_TYPES) == 384
    assert MapRef("zanzibar", None, "Team Slayer", {GAME_TYPE_KEY: "Team Slayer"}) in maps


async def test_map_change_in_an_empty_lobby_does_not_start(server: ElDewritoSimulator) -> None:
    await adapter(server).change_map(
        MapRef("chill", None, "Team Oddball", {GAME_TYPE_KEY: "Team Oddball"})
    )

    assert (server.map, server.variant) == ("chill", "Team Oddball")
    assert "Game.Start" not in server.commands  # it would reset the lobby to guardian
    assert server.commands == [
        "Game.Info",
        'Game.GameType "Team Oddball"',
        'Game.Map "chill"',
        "Server.ListPlayers",
    ]


async def test_map_change_during_a_game_stops_and_starts_it(server: ElDewritoSimulator) -> None:
    server.add_player("Polo", UID_A)
    server.running, server.lobby_delay = "Slayer", 3

    await adapter(server).change_map(
        MapRef("shrine", None, "One Flag", {GAME_TYPE_KEY: "One Flag"})
    )

    assert (server.map, server.running) == ("shrine", "One Flag")
    assert server.commands[:2] == ["Game.Info", "Game.Stop"]
    assert server.commands[-1] == "Game.Start"
    status = await adapter(server).query()
    assert status.game_type == "One Flag" and status.map == MapRef("shrine", "Sandtrap")


async def test_a_game_that_does_not_stop_is_reported(
    server: ElDewritoSimulator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eldewrito, "LOBBY_SECONDS", 0.1)
    server.add_player("Polo", UID_A)
    server.running, server.lobby_delay = "Slayer", 10_000

    with pytest.raises(CommandRejectedError, match="lobby"):
        await adapter(server).change_map(MapRef("chill"))
    assert server.map == "guardian"


async def test_unknown_maps_are_reported(server: ElDewritoSimulator) -> None:
    with pytest.raises(CommandRejectedError, match="nosuchmap"):
        await adapter(server).change_map(MapRef("nosuchmap"))


@pytest.mark.parametrize(
    "target",
    [
        MapRef('chill" ; Server.Name "x'),
        MapRef("chill map"),
        MapRef("chill", extra={GAME_TYPE_KEY: "slayer"}),  # case matters
        MapRef("chill", extra={GAME_TYPE_KEY: 'Slayer"'}),
    ],
)
async def test_unsafe_targets_never_reach_the_server(
    server: ElDewritoSimulator, target: MapRef
) -> None:
    with pytest.raises(CommandRejectedError, match="cannot be sent"):
        await adapter(server).change_map(target)
    assert server.commands == []


async def test_hard_restart_only(server: ElDewritoSimulator) -> None:
    server.add_player("Polo", UID_A)
    server.running = "Team Slayer"

    await adapter(server).restart(RestartMode.HARD)

    assert server.commands[-1] == "Game.Start" and "Game.Stop" in server.commands
    with pytest.raises(UnsupportedError):
        await adapter(server).restart(RestartMode.SOFT)


async def test_players_with_scores_and_teams(server: ElDewritoSimulator) -> None:
    server.add_player("Polo", UID_A, "10.0.0.5", score=7, team=1)
    server.add_player("Marco", UID_B, "10.0.0.6", score=3, team=0)
    server.teams = True

    players = await adapter(server).list_players()

    assert [(p.id, p.name, p.score, p.team, p.can_ban) for p in players] == [
        ("0", "Polo", 7, "Blue", True),
        ("1", "Marco", 3, "Red", True),
    ]


async def test_players_sharing_a_uid_are_told_apart_by_name(server: ElDewritoSimulator) -> None:
    # A copied game folder gives every copy the same UID.
    server.add_player("Polo", UID_A, score=7)
    server.add_player("Marco", UID_A, score=3)

    players = await adapter(server).list_players()

    assert [(p.id, p.name, p.score) for p in players] == [("0", "Polo", 7), ("1", "Marco", 3)]


async def test_scores_with_a_join_password(server: ElDewritoSimulator) -> None:
    server.join_password = "joinpw"
    server.add_player("Polo", UID_A, score=7)

    [player] = await adapter(server).list_players()

    assert player.score == 7
    assert "Server.Password" in server.commands


async def test_kick_by_slot(server: ElDewritoSimulator) -> None:
    server.add_player("Polo", UID_A)
    slot = server.add_player("Marco", UID_A)  # same UID: a kick by UID would hit both

    await adapter(server).kick(Player(str(slot), "Marco"), "reason is not sent")

    assert [p.name for p in server.players.values()] == ["Polo"]
    assert server.commands[-1] == f"Server.KickIndex {slot}"


async def test_ban_by_slot_bans_the_address(server: ElDewritoSimulator) -> None:
    slot = server.add_player("Marco", UID_B, "10.0.0.6")

    await adapter(server).ban(Player(str(slot), "Marco"))

    assert server.banned == ["10.0.0.6"] and not server.players


async def test_a_reused_slot_is_not_kicked(server: ElDewritoSimulator) -> None:
    slot = server.add_player("Newcomer", UID_B)

    with pytest.raises(CommandRejectedError, match="no longer"):
        await adapter(server).kick(Player(str(slot), "Marco"))
    assert server.players


@pytest.mark.parametrize("player_id", ["1; Game.Stop", "-1", "16", "1 2", ""])
async def test_unsafe_player_ids_never_reach_the_server(
    server: ElDewritoSimulator, player_id: str
) -> None:
    with pytest.raises(CommandRejectedError, match="cannot be named"):
        await adapter(server).kick(Player(player_id, "Marco"))
    assert server.commands == []


async def test_console_hides_the_rcon_password(server: ElDewritoSimulator) -> None:
    answer = await adapter(server).console("help")

    assert PASSWORD not in answer
    assert "Server.RconPassword [redacted]" in answer


class Echo(asyncio.DatagramProtocol):
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.transport.sendto(data, addr)  # type: ignore[attr-defined]


# --- LAN scan --------------------------------------------------------------------------------


async def test_discovery_finds_the_server(
    server: ElDewritoSimulator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eldewrito, "SCAN_PORT", server.beacon_port)
    monkeypatch.setattr(eldewrito, "STATUS_PORT_OFFSET", server.status_port - server.beacon_port)

    found = await ElDewritoAdapter.discover([IPv4Network("127.0.0.1/32")], 0.3)

    assert [(item.adapter_id, item.host, item.port, item.name, item.values) for item in found] == [
        (
            "eldewrito",
            "127.0.0.1",
            None,
            "HaloOnline Server",
            {"status_port": str(server.status_port)},
        )
    ]


async def test_discovery_skips_short_answers_and_other_status_servers(
    server: ElDewritoSimulator, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scan's own query (23 bytes) comes back from LANPilot's host; it is not a server.
    echo = await asyncio.get_running_loop().create_datagram_endpoint(
        Echo, local_addr=("127.0.0.1", 0)
    )
    try:
        monkeypatch.setattr(eldewrito, "SCAN_PORT", echo[0].get_extra_info("sockname")[1])
        assert await ElDewritoAdapter.discover([IPv4Network("127.0.0.1/32")], 0.2) == []
    finally:
        echo[0].close()
    # A beacon whose status port is not ElDewrito's: the RCON port answers no JSON.
    monkeypatch.setattr(eldewrito, "SCAN_PORT", server.beacon_port)
    monkeypatch.setattr(eldewrito, "STATUS_PORT_OFFSET", server.rcon_port - server.beacon_port)
    assert await ElDewritoAdapter.discover([IPv4Network("127.0.0.1/32")], 0.3) == []


# --- through the web interface ---------------------------------------------------------------


@pytest.fixture
def background_server() -> Iterator[ElDewritoSimulator]:
    """A simulator in its own thread: the test client blocks the test's event loop."""
    server = ElDewritoSimulator(password=PASSWORD)
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        ready.set()
        loop.run_forever()
        loop.run_until_complete(server.stop())
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5)
    yield server
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


def test_eldewrito_server_in_the_web_interface(
    background_server: ElDewritoSimulator,
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

    form = client.get("/admin/servers/new?adapter=eldewrito").text
    assert "RCON password" in form and 'name="c_port" type="number" value="11776"' in form
    assert 'name="c_status_port" type="number" value="11775"' in form
    status, page = post(
        "/admin/servers/new",
        adapter="eldewrito",
        name="Halo",
        enabled="1",
        c_host="127.0.0.1",
        c_port=str(background_server.rcon_port),
        c_status_port=str(background_server.status_port),
        c_rcon_password=PASSWORD,
    )
    assert status == 303, page
    [(server_id,)] = query("SELECT id FROM servers")
    assert query("SELECT count(*) FROM server_maps") == [(12 * 32,)]

    card = client.get(f"/servers/{server_id}/card").text
    assert "ElDewrito" in card and "Guardian" in card
    page = client.get(f"/servers/{server_id}").text
    assert '<optgroup label="Team Slayer">' in page and "Last Resort" in page

    [(target,)] = query(
        "SELECT id FROM server_maps WHERE key = 'zanzibar' AND game_type = 'Team Slayer'"
    )
    assert post(f"/servers/{server_id}/map", map_id=str(target))[0] == 303
    assert (background_server.map, background_server.variant) == ("zanzibar", "Team Slayer")
    for path in (f"/servers/{server_id}", "/admin/servers", "/admin/audit"):
        assert PASSWORD not in client.get(path).text
