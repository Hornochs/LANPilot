"""GoldSrc RCON transport and the Counter-Strike 1.6 adapter, against the simulator."""

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from lanpilot.auth.roles import Role
from lanpilot.core.adapters import (
    AuthenticationError,
    CommandRejectedError,
    MapRef,
    ProtocolError,
    RestartMode,
    ServerUnreachableError,
)
from lanpilot.core.adapters.registry import load_registry
from lanpilot.core.adapters.schema import describe_fields
from lanpilot.core.transports.goldsrc_rcon import GoldSrcRcon, assemble, printed_text
from lanpilot.games.counterstrike.goldsrc import (
    CounterStrike16Adapter,
    CounterStrike16Connection,
    parse_maps,
    parse_stats,
    parse_status,
)
from lanpilot.games.valve.common import safe_message
from lanpilot.testing.contract import check_adapter
from lanpilot.testing.goldsrc import GoldSrcSimulator
from tests.conftest import ClientFactory, Query, UserFactory, csrf_token, sign_in

PASSWORD = "rcon-s3cret"


@pytest.fixture
async def server() -> AsyncIterator[GoldSrcSimulator]:
    async with GoldSrcSimulator(password=PASSWORD) as simulator:
        yield simulator


def rcon(server: GoldSrcSimulator, password: str = PASSWORD) -> GoldSrcRcon:
    return GoldSrcRcon(
        "127.0.0.1", server.port, password, first_answer_seconds=0.5, quiet_seconds=0.05
    )


def adapter(server: GoldSrcSimulator, password: str = PASSWORD) -> CounterStrike16Adapter:
    connection = CounterStrike16Connection(
        host="127.0.0.1", port=server.port, rcon_password=SecretStr(password)
    )
    return CounterStrike16Adapter(connection)


# --- packet handling, with shapes captured from a real server ------------------------------


def test_print_packets_are_joined_in_order() -> None:
    packets = [
        b"\xff\xff\xff\xfflCommand List\n----\n",
        b"\xff\xff\xff\xfflstartdemos\nwriteip\n\x00\x00",
    ]

    assert printed_text(assemble(packets)) == "Command List\n----\nstartdemos\nwriteip\n"


def test_fragments_are_reassembled_even_out_of_order() -> None:
    whole = b"\xff\xff\xff\xfflCVar List\n" + b"x" * 3000 + b"\nend\n\x00\x00"
    parts = [whole[0:1391], whole[1391:2782], whole[2782:]]
    fragments = [
        b"\xfe\xff\xff\xff\x02\x00\x00\x00" + bytes([(index << 4) | 3]) + part
        for index, part in enumerate(parts)
    ]
    trailing = b"\xff\xff\xff\xfflmore\n\x00\x00"

    packets = assemble([fragments[1], fragments[0], fragments[2], trailing])

    assert len(packets) == 2
    text = printed_text(packets)
    assert text.startswith("CVar List\n") and text.endswith("\nend\nmore\n")
    assert len(text) == len("CVar List\n") + 3000 + len("\nend\nmore\n")


def test_incomplete_fragments_and_foreign_packets_are_dropped() -> None:
    half = b"\xfe\xff\xff\xff\x07\x00\x00\x00" + bytes([0x02]) + b"\xff\xff\xff\xfflpart one"
    other_type = b"\xff\xff\xff\xffAchallenge"
    garbage = b"hello"

    assert printed_text(assemble([half, other_type, garbage])) == ""


# --- transport -----------------------------------------------------------------------------


async def test_execute_returns_the_printed_text(server: GoldSrcSimulator) -> None:
    assert await rcon(server).execute("echo hello") == "hello \n"
    assert await rcon(server).execute("nosuchcommand") == ""
    assert server.commands == ["echo hello", "nosuchcommand"]


@pytest.mark.parametrize("fragmented", [False, True])
async def test_long_output_arrives_complete(server: GoldSrcSimulator, fragmented: bool) -> None:
    server.fragment_long_output = fragmented
    server.maps = [f"de_map_{index:04d}" for index in range(400)]

    listing = await rcon(server).execute("maps *")

    assert len(listing) > 10_000
    assert parse_maps(listing) == server.maps


async def test_wrong_password_is_sent_only_once(server: GoldSrcSimulator) -> None:
    client = rcon(server, "wrong-password")

    for _ in range(5):
        with pytest.raises(AuthenticationError) as refused:
            await client.execute("status")
        assert "wrong-password" not in str(refused.value)

    # A real server bans an address after a few failures; LANPilot must not cause that.
    assert server.bad_passwords == 1
    assert server.commands == []


@pytest.mark.parametrize("password", ["", 'with"quote', "line\nbreak"])
def test_passwords_the_protocol_cannot_carry_are_refused_locally(password: str) -> None:
    with pytest.raises(AuthenticationError):
        GoldSrcRcon("127.0.0.1", 27015, password)


@pytest.mark.parametrize("command", ["", "   ", "status\nquit", "status\rquit", "a\x00b"])
async def test_commands_that_would_break_the_line_are_rejected(
    server: GoldSrcSimulator, command: str
) -> None:
    with pytest.raises(CommandRejectedError):
        await rcon(server).execute(command)

    assert server.commands == []


async def test_silent_server_is_unreachable(server: GoldSrcSimulator) -> None:
    server.silent = True

    with pytest.raises(ServerUnreachableError):
        await rcon(server).execute("status")


async def test_closed_port_is_unreachable(server: GoldSrcSimulator) -> None:
    client = rcon(server)
    server.stop()

    with pytest.raises(ServerUnreachableError):
        await client.execute("status")


async def test_missing_challenge_is_a_protocol_error(server: GoldSrcSimulator) -> None:
    server.challenge = "not-a-number"

    with pytest.raises(ProtocolError):
        await rcon(server).execute("status")


# --- parsing -------------------------------------------------------------------------------


def test_map_listing_is_filtered_and_deduplicated() -> None:
    listing = (
        "-------------\n"
        "de_dust2.bsp\ncs_office.bsp\nde_dust2.bsp\n"
        "readme.txt\nbroken name.bsp\nevil;quit.bsp\n$weird.bsp\n.bsp\n"
        "AS_Oilrig.BSP\n"
    )

    assert parse_maps(listing) == ["de_dust2", "cs_office", "AS_Oilrig"]


def test_stats_are_parsed_by_column_name() -> None:
    answer = (
        "CPU   In    Out   Uptime  Users   FPS    Players\n"
        " 6.40  0.00  0.00       0     0   99.17       0\n"
    )

    assert parse_stats(answer) == {"cpu": 6.4, "fps": 99.17}
    assert parse_stats("") == {}
    assert parse_stats("CPU FPS\nn/a 100\n") == {"fps": 100.0}


# --- adapter -------------------------------------------------------------------------------


async def test_adapter_fulfils_the_contract(server: GoldSrcSimulator) -> None:
    await check_adapter(adapter(server))


def test_adapter_is_registered_and_describes_its_form() -> None:
    assert load_registry().get("cs16") is CounterStrike16Adapter

    fields = {field.name: field for field in describe_fields(CounterStrike16Connection)}
    assert list(fields) == ["host", "port", "rcon_password"]
    assert fields["port"].default == 27015
    assert fields["rcon_password"].kind == "password"


async def test_status_map_change_and_restarts(server: GoldSrcSimulator) -> None:
    game = adapter(server)

    status = await game.query()
    assert (status.name, status.players, status.max_players) == ("Simulated HLDS", 3, 16)
    assert status.map == MapRef("de_dust2")
    assert status.password_protected is False
    assert status.load == {"cpu": 6.4, "fps": 99.17}

    await game.change_map(MapRef("cs_office"))
    assert (await game.query()).map == MapRef("cs_office")

    await game.restart(RestartMode.SOFT)
    await game.restart(RestartMode.HARD)
    assert server.restarts == ["soft", "hard"]
    assert server.commands == ["stats", "changelevel cs_office", "sv_restart 1", "restart"]


async def test_unknown_map_is_reported(server: GoldSrcSimulator) -> None:
    with pytest.raises(CommandRejectedError, match="does not have the map de_train"):
        await adapter(server).change_map(MapRef("de_train"))


@pytest.mark.parametrize("name", ["de_dust2; quit", "de_dust2\nquit", 'x" quit', "", "a" * 65])
async def test_unsafe_map_names_never_reach_the_server(server: GoldSrcSimulator, name: str) -> None:
    with pytest.raises(CommandRejectedError, match="cannot be sent"):
        await adapter(server).change_map(MapRef(name))

    assert server.commands == []


async def test_stats_are_not_asked_with_every_poll(server: GoldSrcSimulator) -> None:
    game = adapter(server)

    for _ in range(5):
        assert (await game.query()).load == {"cpu": 6.4, "fps": 99.17}

    assert server.commands == ["stats"]


async def test_wrong_password_keeps_status_working(server: GoldSrcSimulator) -> None:
    game = adapter(server, "wrong-password")

    for _ in range(3):
        status = await game.query()
        assert status.map == MapRef("de_dust2")
        assert status.load == {}
    with pytest.raises(AuthenticationError):
        await game.list_maps()

    assert server.bad_passwords == 1


async def test_unreachable_server(server: GoldSrcSimulator) -> None:
    game = adapter(server)
    server.stop()

    with pytest.raises(ServerUnreachableError):
        await game.query()


# --- players and console -------------------------------------------------------------------


def test_status_players_are_parsed() -> None:
    answer = (
        "hostname:  LAN\n"
        "#      name userid uniqueid frag time ping loss adr\n"
        '# 1    "Ada" 12 STEAM_0:1:23   5 01:23   25    0 10.0.0.5:27005\n'
        '# 2 "say "x" 13 BOT" 13 STEAM_ID_LAN  -2 1:01:23   40    1 10.0.0.6:27005\n'
        '# 3 "Bot01" 14 BOT   0 00:10    0    0\n'
        '# 4 "HLTV" 15 HLTV hltv:2/128 delay:30 00:10 10.0.0.9:27020\n'
        "3 users\n"
    )

    players = parse_status(answer)

    assert [(p.id, p.name, p.score, p.ping, p.bot, p.can_ban) for p, _ in players] == [
        ("12", "Ada", 5, 25, False, True),
        ("13", 'say "x" 13 BOT', -2, 40, False, True),
        ("14", "Bot01", 0, 0, True, False),
    ]
    assert [address for _, address in players] == ["10.0.0.5", "10.0.0.6", None]


@pytest.mark.parametrize(
    ("reason", "sent"),
    [("afk", "afk"), ('a";quit\n', "aquit"), ("  two   words ", "two words"), ("x" * 80, "x" * 64)],
)
def test_kick_messages_cannot_change_the_command(reason: str, sent: str) -> None:
    assert safe_message(reason) == sent


async def test_players_kick_ban_and_console(server: GoldSrcSimulator) -> None:
    game = adapter(server)

    players = await game.list_players()
    assert [(p.id, p.name, p.score, p.bot) for p in players] == [
        ("2", "Ada", 7, False),
        ("3", "Bot01", 2, True),
        ("4", "Player 3", 0, False),
    ]
    await game.kick(players[1], "bye; quit")
    await game.ban(players[0], "cheating")
    with pytest.raises(CommandRejectedError, match="IP address"):
        await game.ban(players[1])  # gone, and bots have no address anyway

    commands = server.player_commands
    assert commands.kicks == [(3, "bye quit"), (2, "cheating")]
    assert (commands.banned, commands.saved) == (["10.0.0.5"], 1)
    assert [p.name for p in commands.roster] == ["Player 3"]
    assert "kick #3 bye quit" in server.commands
    assert await game.console("echo hello") == "hello \n"


# --- through the web interface -------------------------------------------------------------


@pytest.fixture
def background_server() -> Iterator[GoldSrcSimulator]:
    """A simulator in its own thread: the test client blocks the test's event loop."""
    simulator = GoldSrcSimulator(password=PASSWORD)
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(simulator.start())
        ready.set()
        loop.run_forever()
        simulator.stop()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5)
    yield simulator
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


def test_counter_strike_server_in_the_web_interface(
    background_server: GoldSrcSimulator,
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

    assert 'href="/admin/servers/new?adapter=cs16"' in client.get("/admin/servers/new").text
    form = client.get("/admin/servers/new?adapter=cs16").text
    assert "RCON password" in form and 'name="c_port" type="number" value="27015"' in form

    status, page = post(
        "/admin/servers/new",
        adapter="cs16",
        name="CS Arena",
        enabled="1",
        c_host="127.0.0.1",
        c_port=str(background_server.port),
        c_rcon_password=PASSWORD,
    )
    assert status == 303, page
    [(server_id,)] = query("SELECT id FROM servers")
    assert query("SELECT count(*) FROM server_maps") == [(6,)]

    card = client.get(f"/servers/{server_id}/card").text
    assert "Counter-Strike 1.6" in card and "Dust II" in card and "online" in card  # catalogue name

    [(office,)] = query("SELECT id FROM server_maps WHERE key = 'cs_office'")
    assert post(f"/servers/{server_id}/map", map_id=str(office))[0] == 303
    assert ">Office<" in client.get(f"/servers/{server_id}/card").text
    assert background_server.current_map == "cs_office"

    assert post(f"/servers/{server_id}/restart", mode="soft")[0] == 303
    assert background_server.restarts == ["soft"]
    for path in (f"/servers/{server_id}", "/admin/servers", "/admin/audit"):
        assert PASSWORD not in client.get(path).text
