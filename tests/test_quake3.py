"""Quake 3 transport and the Quake III Arena, Call of Duty and Call of Duty 4 adapters,
against the simulator."""

import asyncio
import dataclasses
import threading
import time
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
)
from lanpilot.core.adapters.registry import load_registry
from lanpilot.core.adapters.schema import describe_fields
from lanpilot.core.transports.quake3 import (
    Quake3Rcon,
    get_status,
    parse_info_string,
    parse_status_response,
    printed_text,
    strip_colors,
)
from lanpilot.games.quake3 import common
from lanpilot.games.quake3.callofduty import (
    CallOfDuty2Adapter,
    CallOfDuty4Adapter,
    CallOfDutyAdapter,
    CallOfDutyConnection,
    CallOfDutyUnitedOffensiveAdapter,
    CallOfDutyWorldAtWarAdapter,
)
from lanpilot.games.quake3.common import (
    GAME_TYPE_KEY,
    Quake3Connection,
    Quake3FamilyAdapter,
    cvar_value,
    directory_maps,
    parse_status,
    rotation_maps,
    safe_message,
)
from lanpilot.games.quake3.quake3 import Quake3ArenaAdapter
from lanpilot.testing.contract import check_adapter
from lanpilot.testing.quake3 import (
    CALL_OF_DUTY,
    CALL_OF_DUTY_2,
    COD4X,
    QUAKE3,
    UNITED_OFFENSIVE,
    WORLD_AT_WAR,
    Flavour,
    Quake3Simulator,
)
from tests.conftest import ClientFactory, Query, UserFactory, csrf_token, sign_in

PASSWORD = "rcon-s3cret"
ADAPTERS: dict[str, type[Quake3FamilyAdapter]] = {
    "q3a": Quake3ArenaAdapter,
    "cod": CallOfDutyAdapter,
    "coduo": CallOfDutyUnitedOffensiveAdapter,
    "cod2": CallOfDuty2Adapter,
    "cod4": CallOfDuty4Adapter,
    "codwaw": CallOfDutyWorldAtWarAdapter,
}
FLAVOURS = {
    "q3a": QUAKE3,
    "cod": CALL_OF_DUTY,
    "coduo": UNITED_OFFENSIVE,
    "cod2": CALL_OF_DUTY_2,
    "cod4": COD4X,
    "codwaw": WORLD_AT_WAR,
}


@pytest.fixture(autouse=True)
def fast_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real limit is 500 ms (the simulator's default); tests keep the ratio, not the wait.
    monkeypatch.setattr(Quake3FamilyAdapter, "rcon_quiet_seconds", 0.05)
    for adapter_class in ADAPTERS.values():
        if adapter_class.rcon_interval_seconds:
            monkeypatch.setattr(adapter_class, "rcon_interval_seconds", 0.06)


async def simulator(flavour: Flavour, **options: object) -> Quake3Simulator:
    server = Quake3Simulator(flavour, password=PASSWORD, **options)  # type: ignore[arg-type]
    server.rate_limit_seconds = 0.05
    await server.start()
    return server


@pytest.fixture(params=sorted(ADAPTERS))
async def game(request: pytest.FixtureRequest) -> AsyncIterator[tuple[str, Quake3Simulator]]:
    server = await simulator(FLAVOURS[request.param])
    yield request.param, server
    server.stop()


@pytest.fixture
async def q3() -> AsyncIterator[Quake3Simulator]:
    server = await simulator(QUAKE3)
    yield server
    server.stop()


def adapter(
    adapter_id: str, server: Quake3Simulator, password: str = PASSWORD
) -> Quake3FamilyAdapter:
    adapter_class = ADAPTERS[adapter_id]
    schema = adapter_class.connection_schema
    assert issubclass(schema, Quake3Connection)
    connection = schema(host="127.0.0.1", port=server.port, rcon_password=SecretStr(password))
    return adapter_class(connection)


def rcon(server: Quake3Simulator, password: str = PASSWORD, interval: float = 0.06) -> Quake3Rcon:
    return Quake3Rcon(
        "127.0.0.1",
        server.port,
        password,
        first_answer_seconds=0.5,
        quiet_seconds=0.05,
        min_interval_seconds=interval,
    )


# --- packets, with shapes captured from real servers ----------------------------------------


def test_status_response_of_quake3_is_parsed() -> None:
    datagram = (
        b"\xff\xff\xff\xffstatusResponse\n\\sv_punkbuster\\0\\version\\Q3 1.32c linux-i386 May  8"
        b" 2006\\g_gametype\\0\\mapname\\q3dm17\\sv_hostname\\^1Lan^7Party\\sv_maxclients\\16"
        b'\\g_needpass\\0\n0 0 "Sarge"\n12 48 "^4A d a"\n-3 999 "q\\"x"\n'
    )
    status = parse_status_response(datagram)

    assert status.info["mapname"] == "q3dm17"
    assert status.info["sv_hostname"] == "^1Lan^7Party"
    assert [(p.score, p.ping, p.name) for p in status.players] == [
        (0, 0, "Sarge"),
        (12, 48, "A d a"),
    ]


def test_info_strings_are_bounded_and_keys_lower_case() -> None:
    info = parse_info_string("\\SV_HostName\\x\\a\\1\\a\\2\\dangling")
    assert info == {"sv_hostname": "x", "a": "1"}
    many = "".join(f"\\k{n}\\v" for n in range(1000))
    assert len(parse_info_string(many)) == 256


def test_other_packets_are_not_status() -> None:
    with pytest.raises(ProtocolError):
        parse_status_response(b"\xff\xff\xff\xffprint\nhello")


def test_print_packets_are_joined_and_colours_stripped() -> None:
    packets = [b"\xff\xff\xff\xffprint\nmap: q3dm1\n", b"junk", b"\xff\xff\xff\xffprint\nend\n"]
    assert printed_text(packets) == "map: q3dm1\nend\n"
    assert strip_colors("^1Red^7 and ^^7caret") == "Red and ^caret"  # as Q_CleanStr does


STATUS_Q3 = (
    "map: q3dm1\n"
    "num score ping name            lastmsg address               qport rate\n"
    "--- ----- ---- --------------- ------- --------------------- ----- -----\n"
    "  0     0    0 Sarge^7               100 bot                       0 16384\n"
    "  1    12   24 ^1Ada^7                 0 10.0.0.5:27960        12345 25000\n"
    "  2    -1 CNCT Bob the Builder^7      50 10.0.0.7:27960          999 25000\n"
    "  3     5   40 7 7 7^7                 0 localhost                 0 25000\n"
)
STATUS_COD4X = (
    "hostname: LinuxGSM\nmap     : mp_backlot\n\n"
    "num score ping playerid            steamid           name                             "
    "lastmsg address                                              qport rate\n"
    "--- ----- ---- ------------------- ----------------- -------------------------------- "
    "------- ---------------------------------------------------- ----- -----\n"
    "  2     0    0 0                   0                 bot2^7                           "
    "        0 bot                                                  65196 100000\n"
    " 11    25   31 1100001000000000    0                 [LAN] Eve^7                      "
    "        0 10.0.0.9:28960                                       65196 100000\n"
)


def test_status_rows_are_parsed_by_header() -> None:
    rows = parse_status(STATUS_Q3)
    assert [(r.slot, r.name, r.score, r.ping, r.address) for r in rows] == [
        ("0", "Sarge", 0, 0, "bot"),
        ("1", "Ada", 12, 24, "10.0.0.5:27960"),
        ("2", "Bob the Builder", -1, None, "10.0.0.7:27960"),
        ("3", "7 7 7", 5, 40, "localhost"),
    ]
    cod4 = parse_status(STATUS_COD4X)
    assert [(r.slot, r.name, r.address) for r in cod4] == [
        ("2", "bot2", "bot"),
        ("11", "[LAN] Eve", "10.0.0.9:28960"),
    ]
    assert parse_status("no header\n  1 2 3 x 4 5 6 7\n") == []
    wide = (
        "num score ping " + "x " * 500 + "name a b c d\n" + "  1 2 3 " + "y " * 500 + "n a b c d\n"
    )
    assert parse_status(wide) == []


def test_cvars_rotation_and_directories() -> None:
    assert cvar_value('"g_gametype" is:"sd^7" default:"dm^7"\n', "g_gametype") == "sd"
    cod4x = '"sv_mapRotation" is: "gametype war map mp_crash map evil;quit^7" default: "^7"'
    assert cvar_value(cod4x, "sv_maprotation") == "gametype war map mp_crash map evil;quit"
    assert cvar_value("unknown", "x") is None
    assert rotation_maps("gametype war map mp_crash map mp_bog map evil;quit gametype sd map") == [
        "mp_crash",
        "mp_bog",
    ]
    listing = (
        "Directory of maps bsp\n---------------\nq3dm1.bsp\npro-q3dm6.bsp\nx y.bsp\nq3dm1.bsp\n"
    )
    assert directory_maps(listing) == ["q3dm1", "pro-q3dm6"]


@pytest.mark.parametrize(
    ("reason", "sent"),
    [
        ("afk", "afk"),
        ('a";quit\n%/\\', "aquit"),
        ("  two   words ", "two words"),
        ("x" * 80, "x" * 64),
    ],
)
def test_kick_reasons_cannot_change_the_command(reason: str, sent: str) -> None:
    assert safe_message(reason) == sent


# --- transport -------------------------------------------------------------------------------


async def test_execute_returns_the_printed_text(q3: Quake3Simulator) -> None:
    client = rcon(q3)
    assert await client.execute("echo hello") == "hello \n"
    assert await client.execute("nosuchcommand") == ""
    assert q3.commands == ["echo hello", "nosuchcommand"]


async def test_long_output_arrives_complete(q3: Quake3Simulator) -> None:
    q3.maps = [f"map_{index:04d}" for index in range(500)]
    assert directory_maps(await rcon(q3).execute("dir maps bsp")) == q3.maps


async def test_commands_keep_the_servers_rate_limit(q3: Quake3Simulator) -> None:
    q3.rate_limit_seconds = 0.2
    client = rcon(q3, interval=0.25)
    started = time.monotonic()
    for _ in range(3):
        assert await client.execute("echo hi") == "hi \n"
    assert time.monotonic() - started >= 0.5
    assert q3.dropped == 0


async def test_without_the_interval_quake3_drops_commands(q3: Quake3Simulator) -> None:
    q3.rate_limit_seconds = 0.5
    client = rcon(q3, interval=0.0)
    await client.execute("echo one")
    with pytest.raises(ServerUnreachableError):
        await client.execute("echo two")
    assert q3.dropped == 1


@pytest.mark.parametrize(
    "flavour",
    [QUAKE3, CALL_OF_DUTY_2, COD4X, WORLD_AT_WAR],
    ids=["quake3", "cod2", "cod4x", "world-at-war"],
)
async def test_wrong_password_is_sent_only_once(flavour: Flavour) -> None:
    server = await simulator(flavour)
    client = rcon(server, "wrong-password")
    for _ in range(3):
        with pytest.raises(AuthenticationError) as refused:
            await client.execute("status")
        assert "wrong-password" not in str(refused.value)
    server.stop()
    assert server.bad_passwords == 1
    assert server.commands == []


@pytest.mark.parametrize("password", ["", 'with"quote', "two words", "tab\there", "nul\x00"])
def test_passwords_the_protocol_cannot_carry_are_refused_locally(password: str) -> None:
    with pytest.raises(AuthenticationError):
        Quake3Rcon("127.0.0.1", 27960, password)


@pytest.mark.parametrize(
    "command", ["", "   ", "status\nquit", "status\rquit", "a\x00b", "x" * 1001]
)
async def test_commands_that_would_break_the_line_are_rejected(
    q3: Quake3Simulator, command: str
) -> None:
    with pytest.raises(CommandRejectedError):
        await rcon(q3).execute(command)
    assert q3.commands == []


async def test_silent_or_closed_server_is_unreachable(q3: Quake3Simulator) -> None:
    q3.silent = True
    with pytest.raises(ServerUnreachableError):
        await rcon(q3).execute("status")
    with pytest.raises(ServerUnreachableError):
        await get_status("127.0.0.1", q3.port, 0.2)
    q3.silent = False
    client = rcon(q3)
    q3.stop()
    with pytest.raises(ServerUnreachableError):
        await client.execute("status")


# --- adapters --------------------------------------------------------------------------------


async def test_adapters_fulfil_the_contract(game: tuple[str, Quake3Simulator]) -> None:
    adapter_id, server = game
    await check_adapter(adapter(adapter_id, server))


def test_adapters_are_registered_and_describe_their_form() -> None:
    registry = load_registry()
    for adapter_id, adapter_class in ADAPTERS.items():
        assert registry.get(adapter_id) is adapter_class
    fields = {field.name: field for field in describe_fields(Quake3Connection)}
    assert list(fields) == ["host", "port", "rcon_password"]
    assert fields["port"].default == 27960
    assert fields["rcon_password"].kind == "password"
    assert {f.name: f for f in describe_fields(CallOfDutyConnection)}["port"].default == 28960
    assert Capability.BAN in Quake3ArenaAdapter.capabilities
    assert Capability.BAN not in CallOfDutyAdapter.capabilities | CallOfDuty4Adapter.capabilities


async def test_status_is_read_without_password(game: tuple[str, Quake3Simulator]) -> None:
    adapter_id, server = game
    status = await adapter(adapter_id, server, "wrong-password").query()

    assert (status.name, status.players, status.max_players) == ("Simulated Server", 3, 16)
    assert status.map == MapRef(server.maps[0])
    assert status.game_type == ADAPTERS[adapter_id].game_types[server.flavour.game_types[0]]
    assert status.password_protected is False
    assert server.bad_passwords == 0


async def test_maps_are_listed_once_per_game_type(game: tuple[str, Quake3Simulator]) -> None:
    adapter_id, server = game
    server.rotation = f"gametype war map mp_custom_dust map {server.maps[0]}"
    maps = await adapter(adapter_id, server).list_maps()
    adapter_class = ADAPTERS[adapter_id]

    keys = list(dict.fromkeys(item.key for item in maps))
    if adapter_id in ("cod4", "codwaw"):
        assert keys == [*catalog.load().keys(adapter_id), "mp_custom_dust"]
        assert "sv_mapRotation" in server.commands
    else:
        assert keys == server.maps
    assert len(maps) == len(keys) * len(adapter_class.game_types)
    assert {(item.game_type, item.extra[GAME_TYPE_KEY]) for item in maps} == {
        (name, code) for code, name in adapter_class.game_types.items()
    }


async def test_map_change_sets_the_game_type(game: tuple[str, Quake3Simulator]) -> None:
    adapter_id, server = game
    code = server.flavour.game_types[-1]
    game_adapter = adapter(adapter_id, server)
    target = MapRef(server.maps[1], None, None, {GAME_TYPE_KEY: code})

    await game_adapter.change_map(target)

    status = await game_adapter.query()
    assert status.map == MapRef(server.maps[1])
    assert status.game_type == ADAPTERS[adapter_id].game_types[code]
    assert server.commands == [f"g_gametype {code}", f"map {server.maps[1]}"]


async def test_failed_change_does_not_leave_the_game_type_pending(q3: Quake3Simulator) -> None:
    game_adapter = adapter("q3a", q3)
    with pytest.raises(CommandRejectedError, match="does not have the map q3dm99"):
        await game_adapter.change_map(MapRef("q3dm99", None, None, {GAME_TYPE_KEY: "4"}))
    assert q3.pending_game_type is None
    assert q3.commands == ["g_gametype 4", "map q3dm99", "g_gametype 0"]


async def test_a_map_not_made_for_the_game_type_is_reported() -> None:
    server = await simulator(UNITED_OFFENSIVE, unsupported={("mp_sicily", "bas")})
    game_adapter = adapter("coduo", server)

    with pytest.raises(CommandRejectedError, match=r"not as Base Assault.*Deathmatch"):
        await game_adapter.change_map(MapRef("mp_sicily", None, None, {GAME_TYPE_KEY: "bas"}))
    await game_adapter.change_map(MapRef("mp_foy", None, None, {GAME_TYPE_KEY: "bas"}))
    server.stop()

    assert (server.current_map, server.game_type) == ("mp_foy", "bas")


async def test_a_slow_level_load_is_waited_for() -> None:
    server = await simulator(CALL_OF_DUTY_2, load_seconds=0.4)
    quick = rcon(server)  # waits 0.5 s for a first answer
    quick._first_answer_seconds = 0.2
    with pytest.raises(ServerUnreachableError):
        await quick.execute("map mp_toujane")
    await asyncio.sleep(0.5)
    assert "Shutdown" in await quick.execute("map mp_carentan", first_answer_seconds=1.0)
    await asyncio.sleep(0.5)

    await adapter("cod2", server).change_map(
        MapRef("mp_breakout", None, None, {GAME_TYPE_KEY: "sd"})
    )
    server.stop()
    assert (server.current_map, server.game_type) == ("mp_breakout", "sd")


@pytest.mark.parametrize(
    "target",
    [
        MapRef("q3dm1; quit"),
        MapRef("q3dm1\nquit"),
        MapRef('x" quit'),
        MapRef(""),
        MapRef("a" * 65),
        MapRef("q3dm1", None, None, {GAME_TYPE_KEY: "0; quit"}),
        MapRef("q3dm1", None, None, {GAME_TYPE_KEY: "2"}),  # single player
    ],
)
async def test_unsafe_targets_never_reach_the_server(q3: Quake3Simulator, target: MapRef) -> None:
    with pytest.raises(CommandRejectedError, match="cannot be sent"):
        await adapter("q3a", q3).change_map(target)
    assert q3.commands == []


@pytest.mark.parametrize(
    ("adapter_id", "soft", "hard"),
    [
        ("q3a", ["map_restart"], ["map q3dm1"]),
        ("cod", ["map_restart"], ["map mp_carentan"]),
        ("coduo", ["map_restart"], ["map mp_cassino"]),
        ("cod2", ["fast_restart"], ["map_restart"]),
        ("cod4", ["fast_restart"], ["map_restart"]),
        ("codwaw", ["fast_restart"], ["map_restart"]),
    ],
)
async def test_restarts(adapter_id: str, soft: list[str], hard: list[str]) -> None:
    server = await simulator(FLAVOURS[adapter_id])
    game_adapter = adapter(adapter_id, server)
    await game_adapter.restart(RestartMode.SOFT)
    await game_adapter.restart(RestartMode.HARD)
    server.stop()
    assert server.commands == soft + hard
    assert server.restarts == ["soft", "hard"]


async def test_players_and_kick(game: tuple[str, Quake3Simulator]) -> None:
    adapter_id, server = game
    game_adapter = adapter(adapter_id, server)
    players = await game_adapter.list_players()
    can_ban = adapter_id == "q3a"
    assert [(p.id, p.name, p.score, p.ping, p.bot, p.can_kick, p.can_ban) for p in players] == [
        ("0", "Ada", 12, 24, False, True, can_ban),
        ("1", "Sarge", 3, None, True, True, False),
        ("2", "Bob the Builder", -1, 48, False, True, can_ban),
    ]

    await game_adapter.kick(players[1], "bye; quit")

    reason = "bye quit" if adapter_id == "cod4" else ""
    assert server.kicks == [(1, reason)]
    assert server.commands[-1] == f"clientkick 1 {reason}".rstrip()
    assert await game_adapter.console("echo hello") == "hello \n"


async def test_a_reused_slot_is_not_kicked(q3: Quake3Simulator) -> None:
    game_adapter = adapter("q3a", q3)
    players = await game_adapter.list_players()
    q3.clients[0].name = "Someone Else"  # Ada left, another player took slot 0

    with pytest.raises(CommandRejectedError, match="no longer on the server"):
        await game_adapter.kick(players[0])
    with pytest.raises(CommandRejectedError, match="cannot be named"):
        await game_adapter.kick(Player("0; quit", "Ada"))
    assert q3.kicks == []


async def test_quake3_bans_by_address(q3: Quake3Simulator) -> None:
    game_adapter = adapter("q3a", q3)
    ada, sarge, _ = await game_adapter.list_players()

    await game_adapter.ban(ada, "cheating")
    with pytest.raises(CommandRejectedError, match="IP address"):
        await game_adapter.ban(sarge)

    assert q3.banned == ["10.0.0.5"]
    assert q3.kicks == [(0, "")]
    assert [c.name for c in q3.clients] == ["Sarge^7", "Bob the Builder"]


async def test_wrong_password_keeps_status_working(game: tuple[str, Quake3Simulator]) -> None:
    adapter_id, server = game
    game_adapter = adapter(adapter_id, server, "wrong-password")
    for _ in range(2):
        assert (await game_adapter.query()).map == MapRef(server.maps[0])
    for _ in range(2):
        with pytest.raises(AuthenticationError):
            await game_adapter.list_maps()
    assert server.bad_passwords == 1


async def test_unreachable_server(q3: Quake3Simulator) -> None:
    game_adapter = adapter("q3a", q3)
    q3.stop()
    with pytest.raises(ServerUnreachableError):
        await game_adapter.query()


# --- LAN scan --------------------------------------------------------------------------------


async def test_discovery_finds_each_game_once(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = [await simulator(flavour) for flavour in FLAVOURS.values()]
    monkeypatch.setattr(common, "SCAN_PORTS", tuple(s.port for s in servers))
    networks = [IPv4Network("127.0.0.1/32")]

    found = {
        adapter_id: await adapter_class.discover(networks, 0.3)
        for adapter_id, adapter_class in ADAPTERS.items()
    }
    for server in servers:
        server.stop()

    for (adapter_id, items), server in zip(found.items(), servers, strict=True):
        assert [(i.adapter_id, i.host, i.port, i.name) for i in items] == [
            (adapter_id, "127.0.0.1", server.port, "Simulated Server")
        ]
        assert items[0].game_type == ADAPTERS[adapter_id].game_types[server.game_type]


async def test_discovery_skips_other_games(monkeypatch: pytest.MonkeyPatch) -> None:
    server = await simulator(QUAKE3)
    server.flavour = dataclasses.replace(QUAKE3, version="ET 2.60b", gamename="etmain")
    monkeypatch.setattr(common, "SCAN_PORTS", (server.port,))
    found = await Quake3ArenaAdapter.discover([IPv4Network("127.0.0.1/32")], 0.3)
    server.stop()
    assert found == []


# --- through the web interface ---------------------------------------------------------------


@pytest.fixture
def background_server() -> Iterator[Quake3Simulator]:
    """A simulator in its own thread: the test client blocks the test's event loop."""
    server = Quake3Simulator(QUAKE3, password=PASSWORD)
    server.rate_limit_seconds = 0.05
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        ready.set()
        loop.run_forever()
        server.stop()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5)
    yield server
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


def test_quake3_server_in_the_web_interface(
    background_server: Quake3Simulator,
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

    form = client.get("/admin/servers/new?adapter=q3a").text
    assert "RCON password" in form and 'name="c_port" type="number" value="27960"' in form
    status, page = post(
        "/admin/servers/new",
        adapter="q3a",
        name="Q3 Arena",
        enabled="1",
        c_host="127.0.0.1",
        c_port=str(background_server.port),
        c_rcon_password=PASSWORD,
    )
    assert status == 303, page
    [(server_id,)] = query("SELECT id FROM servers")
    assert query("SELECT count(*) FROM server_maps") == [(5 * 4,)]

    card = client.get(f"/servers/{server_id}/card").text
    assert "Quake III Arena" in card and "Arena Gate" in card and "Free For All" in card
    page = client.get(f"/servers/{server_id}").text
    assert '<optgroup label="Capture the Flag">' in page and "The Longest Yard" in page

    [(ctf,)] = query(
        "SELECT id FROM server_maps WHERE key = 'q3ctf1' AND game_type = 'Capture the Flag'"
    )
    assert post(f"/servers/{server_id}/map", map_id=str(ctf))[0] == 303
    assert (background_server.current_map, background_server.game_type) == ("q3ctf1", "4")
    assert post(f"/servers/{server_id}/restart", mode="soft")[0] == 303
    assert background_server.restarts == ["soft"]
    for path in (f"/servers/{server_id}", "/admin/servers", "/admin/audit"):
        assert PASSWORD not in client.get(path).text
