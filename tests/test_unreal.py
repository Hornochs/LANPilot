"""UWeb transport and the UT2004 and UT99 adapters, against the simulator."""

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator

import pytest
from aiohttp import web
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
    UnsupportedError,
)
from lanpilot.core.adapters.registry import load_registry
from lanpilot.core.adapters.schema import describe_fields
from lanpilot.core.transports import uweb
from lanpilot.core.transports.uweb import UWebAdmin, parse
from lanpilot.games.unreal.common import split_options, valid_options
from lanpilot.games.unreal.ut3 import UT3Adapter, UT3Connection
from lanpilot.games.unreal.ut99 import UT99Adapter, UT99Connection
from lanpilot.games.unreal.ut2004 import UT2004Adapter, UT2004Connection
from lanpilot.testing.contract import check_adapter
from lanpilot.testing.uweb import SimulatedPlayer, UnrealSimulator, UT3Simulator
from tests.conftest import ClientFactory, Query, UserFactory, csrf_token, sign_in

PASSWORD = "ut-s3cret"


@pytest.fixture
async def server() -> AsyncIterator[UnrealSimulator]:
    async with UnrealSimulator(password=PASSWORD) as simulator:
        yield simulator


def adapter(server: UnrealSimulator, password: str = PASSWORD) -> UT2004Adapter:
    connection = UT2004Connection(
        host="127.0.0.1",
        port=server.web_port,
        query_port=server.query_port,
        password=SecretStr(password),
    )
    return UT2004Adapter(connection)


# --- transport and parsing -----------------------------------------------------------------


def test_forms_are_parsed_tolerantly() -> None:
    page = parse(
        "<form><select class='mini' name='GameTypeSelect'>"
        '<option value="XGame.xDeathMatch" selected>DeathMatch'
        "<option value='XGame.xCTFGame'>Capture &amp; the Flag</option>"
        "<option>No value</option></select>"
        '<input type="hidden" name="MapName" value="DM-Deck17">'
        "<select name=Empty></select><input value=x></form><script>alert(1)</script>"
    )

    assert [(o.value, o.label, o.selected) for o in page.options("GameTypeSelect")] == [
        ("XGame.xDeathMatch", "DeathMatch", True),
        ("XGame.xCTFGame", "Capture & the Flag", False),
        ("No value", "No value", False),
    ]
    assert page.inputs == {"MapName": "DM-Deck17"}
    assert page.options("Empty") == [] and page.options("Missing") == []


async def test_wrong_credentials_are_sent_only_once(server: UnrealSimulator) -> None:
    client = UWebAdmin("127.0.0.1", server.web_port, "admin", "wrong-password")

    for _ in range(3):
        with pytest.raises(AuthenticationError) as refused:
            await client.get("/ServerAdmin/current_game")
        assert "wrong-password" not in str(refused.value)

    assert server.refused == 1


async def test_unreachable_webadmin(server: UnrealSimulator) -> None:
    port = server.web_port
    await server.stop()

    with pytest.raises(ServerUnreachableError):
        await UWebAdmin("127.0.0.1", port, "admin", PASSWORD).get("/ServerAdmin/")


async def test_oversized_and_failing_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uweb, "MAX_PAGE_BYTES", 1000)

    async def big(request: web.Request) -> web.Response:
        return web.Response(body=b"x" * 5000)

    async def broken(request: web.Request) -> web.Response:
        return web.Response(status=500)

    app = web.Application()
    app.router.add_get("/big", big)
    app.router.add_get("/broken", broken)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        client = UWebAdmin("127.0.0.1", port, "admin", "pw")
        with pytest.raises(ProtocolError, match="too large"):
            await client.get("/big")
        with pytest.raises(ProtocolError, match="HTTP 500"):
            await client.get("/broken")
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    ("entry", "valid"),
    [
        ("ONS-Torlan?LinkSetup=Default", True),
        ("DM-Deck17", True),
        ("ONS-Torlan?LinkSetup=Default?AdminPassword=x", False),
        ("ONS-Torlan?Game=XGame.xDeathMatch", False),
        ("ONS-Torlan?LinkSetup=a b", False),
    ],
)
def test_map_options_are_restricted(entry: str, valid: bool) -> None:
    assert valid_options(split_options(entry)[1]) is valid


# --- adapter -------------------------------------------------------------------------------


async def test_adapter_fulfils_the_contract(server: UnrealSimulator) -> None:
    await check_adapter(adapter(server))


def test_adapter_is_registered_and_describes_its_form() -> None:
    assert load_registry().get("ut2004") is UT2004Adapter

    fields = {field.name: field for field in describe_fields(UT2004Connection)}
    assert list(fields) == ["host", "port", "query_port", "username", "password"]
    assert (fields["port"].default, fields["query_port"].default) == (8075, 7787)
    assert fields["username"].default == "admin" and fields["password"].kind == "password"


async def test_status_comes_without_password(server: UnrealSimulator) -> None:
    server.join_password = "lan"
    game = adapter(server, "wrong-password")

    status = await game.query()

    assert (status.name, status.players, status.max_players) == ("Simulated UT", 2, 16)
    assert status.map == MapRef("DM-Deck17", "Deck 17")
    assert status.game_type == "xDeathMatch"  # names are known after the map list was read
    assert status.password_protected is True
    assert server.refused == 0


async def test_map_list_covers_every_game_type(server: UnrealSimulator) -> None:
    game = adapter(server)

    maps = await game.list_maps()

    assert [(item.key, item.game_type) for item in maps] == [
        ("DM-Deck17", "DeathMatch"),
        ("DM-Antalus", "DeathMatch"),
        ("DM-RANKIN", "DeathMatch"),  # "DM-Rankin" is the same map
        ("DM-Gael", "DeathMatch"),
        ("CTF-Face3", "Capture the Flag"),
        ("CTF-FaceClassic", "Capture the Flag"),  # "CTF-Evil;quit" is dropped
        ("ONS-TORLAN", "Onslaught"),  # the entry smuggling ?AdminPassword= is dropped
    ]
    assert maps[-1].extra == {
        "game_class": "Onslaught.ONSOnslaughtGame",
        "options": "?LinkSetup=Default",
    }
    assert (await game.query()).game_type == "DeathMatch"


async def test_change_map_with_game_type_and_restart(server: UnrealSimulator) -> None:
    game = adapter(server)
    maps = {(item.key, item.game_type): item for item in await game.list_maps()}

    await game.change_map(maps[("CTF-Face3", "Capture the Flag")])
    status = await game.query()
    assert (status.map and status.map.key, status.game_type) == ("CTF-Face3", "Capture the Flag")
    await game.change_map(maps[("ONS-TORLAN", "Onslaught")])
    await game.restart(RestartMode.HARD)
    with pytest.raises(UnsupportedError):
        await game.restart(RestartMode.SOFT)

    assert server.travels == [
        "CTF-Face3?Game=XGame.xCTFGame",
        "ONS-TORLAN?LinkSetup=Default?Game=Onslaught.ONSOnslaughtGame",
        "ONS-TORLAN?Game=Onslaught.ONSOnslaughtGame",
    ]


async def test_map_without_game_type_keeps_the_current_one(server: UnrealSimulator) -> None:
    await adapter(server).change_map(MapRef("DM-Antalus"))

    assert server.travels == ["DM-Antalus?Game=XGame.xDeathMatch"]


@pytest.mark.parametrize(
    "target",
    [
        MapRef("DM-Deck17; quit"),
        MapRef("DM-Deck17?Game=Evil.Class"),
        MapRef("DM-Deck17", extra={"game_class": "XGame.xDeathMatch?AdminPassword=x"}),
        MapRef("ONS-Torlan", extra={"game_class": "A.B", "options": "?AdminPassword=x"}),
        MapRef(""),
    ],
)
async def test_unsafe_values_never_reach_the_server(
    server: UnrealSimulator, target: MapRef
) -> None:
    with pytest.raises(CommandRejectedError, match="cannot be sent"):
        await adapter(server).change_map(target)

    assert server.travels == []


async def test_wrong_password_only_affects_control(server: UnrealSimulator) -> None:
    game = adapter(server, "wrong-password")

    assert (await game.query()).map is not None
    for _ in range(2):
        with pytest.raises(AuthenticationError):
            await game.list_maps()

    assert server.refused == 1


# --- UT99 ----------------------------------------------------------------------------------


@pytest.fixture
async def ut99() -> AsyncIterator[UnrealSimulator]:
    async with UnrealSimulator(game="ut99", password=PASSWORD) as simulator:
        yield simulator


def ut99_adapter(server: UnrealSimulator, password: str = PASSWORD) -> UT99Adapter:
    connection = UT99Connection(
        host="127.0.0.1",
        port=server.web_port,
        query_port=server.query_port,
        password=SecretStr(password),
    )
    return UT99Adapter(connection)


async def test_ut99_fulfils_the_contract(ut99: UnrealSimulator) -> None:
    await check_adapter(ut99_adapter(ut99))


def test_ut99_is_registered_and_describes_its_form() -> None:
    assert load_registry().get("ut99") is UT99Adapter
    fields = {field.name: field for field in describe_fields(UT99Connection)}
    assert (fields["port"].default, fields["query_port"].default) == (8076, 7778)


async def test_ut99_status_and_map_list(ut99: UnrealSimulator) -> None:
    ut99.join_password = "lan"
    game = ut99_adapter(ut99)

    status = await game.query()
    assert status.map == MapRef("DM-Deck16][", "Deck #16 ][")
    assert status.password_protected is True  # UT99 says "True"
    maps = await game.list_maps()

    assert [(item.key, item.game_type) for item in maps] == [
        ("DM-Deck16][", "Tournament DeathMatch"),
        ("DM-Codex", "Tournament DeathMatch"),
        ("DM-Agony", "Tournament DeathMatch"),
        ("CTF-Face", "Capture the Flag"),
        ("CTF-Coret", "Capture the Flag"),
    ]  # ".unr" removed; DarkMatch has no maps ("*** None ***")
    assert (await game.query()).game_type == "Tournament DeathMatch"


async def test_ut99_changes_map_and_game_type_and_restarts(ut99: UnrealSimulator) -> None:
    game = ut99_adapter(ut99)
    maps = {(item.key, item.game_type): item for item in await game.list_maps()}

    await game.change_map(maps[("CTF-Face", "Capture the Flag")])
    assert (ut99.current_map, ut99.game_class) == ("CTF-Face", "Botpack.CTFGame")
    await game.restart(RestartMode.HARD)
    await game.change_map(MapRef("DM-Codex"))  # no game type: the running one
    with pytest.raises(UnsupportedError):
        await game.restart(RestartMode.SOFT)

    assert ut99.travels == [
        "CTF-Face?Game=Botpack.CTFGame",
        "CTF-Face?Game=Botpack.CTFGame",
        "DM-Codex?Game=Botpack.CTFGame",
    ]


async def test_ut99_wrong_password_only_affects_control(ut99: UnrealSimulator) -> None:
    game = ut99_adapter(ut99, "wrong-password")

    assert (await game.query()).map is not None
    for _ in range(2):
        with pytest.raises(AuthenticationError):
            await game.restart(RestartMode.HARD)
    assert ut99.refused == 1


# --- UT3 -----------------------------------------------------------------------------------


@pytest.fixture
async def ut3() -> AsyncIterator[UT3Simulator]:
    async with UT3Simulator(password=PASSWORD) as simulator:
        yield simulator


def ut3_adapter(server: UT3Simulator, password: str = PASSWORD) -> UT3Adapter:
    return UT3Adapter(
        UT3Connection(host="127.0.0.1", port=server.web_port, password=SecretStr(password))
    )


async def test_ut3_fulfils_the_contract(ut3: UT3Simulator) -> None:
    await check_adapter(ut3_adapter(ut3))


def test_ut3_is_registered_and_describes_its_form() -> None:
    assert load_registry().get("ut3") is UT3Adapter
    fields = {field.name: field for field in describe_fields(UT3Connection)}
    assert list(fields) == ["host", "port", "username", "password"]


async def test_ut3_status_and_map_list(ut3: UT3Simulator) -> None:
    game = ut3_adapter(ut3)

    status = await game.query()
    assert (status.name, status.players, status.max_players) == ("Simulated UT3", 3, 32)
    assert (status.map, status.game_type) == (MapRef("DM-Deck"), "Deathmatch")
    maps = await game.list_maps()
    assert [(item.key, item.name, item.game_type) for item in maps] == [
        ("DM-Deck", "Deck", "Deathmatch"),
        ("DM-Arsenal", "Arsenal", "Deathmatch"),
        ("WAR-Avalanche", "Avalanche", "Warfare"),
        ("WAR-Avalanche?LinkSetup=Alt", "Avalanche Two Fronts", "Warfare"),
    ]  # "WAR-Evil;quit" is dropped
    assert (await game.query()).map == MapRef("DM-Deck", "Deck")
    await game.close()
    assert ut3.sessions == 1  # the session cookie is kept


async def test_ut3_layouts_and_mutators(ut3: UT3Simulator) -> None:
    game = ut3_adapter(ut3)
    maps = {item.key: item for item in await game.list_maps()}

    await game.change_map(maps["WAR-Avalanche?LinkSetup=Alt"])
    await game.restart(RestartMode.HARD)  # keeps the layout
    await game.change_map(maps["DM-Arsenal"])  # drops it
    with pytest.raises(UnsupportedError):
        await game.restart(RestartMode.SOFT)

    war, dm_class = "UTGameContent.UTOnslaughtGame_Content", "UTGame.UTDeathmatch"
    base = "?bIsDedicated=true?bIsLanMatch=true?TimeLimit=15?GoalScore=0"
    assert ut3.changes == [
        ("WAR-Avalanche?LinkSetup=Alt", war, base, "UTGame.UTMutator_BigHead"),
        ("WAR-Avalanche?LinkSetup=Alt", war, base, "UTGame.UTMutator_BigHead"),
        ("DM-Arsenal", dm_class, base, "UTGame.UTMutator_BigHead"),
    ]  # the running mutator stays on


@pytest.mark.parametrize(
    "target",
    [
        MapRef("DM-Deck?Game=Evil.Class", extra={"game_class": "UTGame.UTDeathmatch"}),
        MapRef("DM-Deck", extra={"game_class": "UTGame.UTDeathmatch?x=1"}),
        MapRef("DM Deck"),
    ],
)
async def test_ut3_unsafe_values_never_reach_the_server(ut3: UT3Simulator, target: MapRef) -> None:
    with pytest.raises(CommandRejectedError, match="cannot be sent"):
        await ut3_adapter(ut3).change_map(target)

    assert ut3.changes == []


async def test_ut3_login_page_means_wrong_password(ut3: UT3Simulator) -> None:
    game = ut3_adapter(ut3, "wrong-password")

    for _ in range(3):
        with pytest.raises(AuthenticationError):
            await game.query()

    assert ut3.refused == 1


# --- through the web interface -------------------------------------------------------------


@pytest.fixture
def background_server() -> Iterator[UnrealSimulator]:
    """A simulator in its own thread: the test client blocks the test's event loop."""
    simulator = UnrealSimulator(password=PASSWORD)
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(simulator.start())
        ready.set()
        loop.run_forever()
        loop.run_until_complete(simulator.stop())
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5)
    yield simulator
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


def test_ut2004_server_in_the_web_interface(
    background_server: UnrealSimulator,
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

    status, page = post(
        "/admin/servers/new",
        adapter="ut2004",
        name="UT Arena",
        enabled="1",
        c_host="127.0.0.1",
        c_port=str(background_server.web_port),
        c_query_port=str(background_server.query_port),
        c_username="admin",
        c_password=PASSWORD,
    )
    assert status == 303, page
    [(server_id,)] = query("SELECT id FROM servers")
    assert query("SELECT count(*) FROM server_maps") == [(7,)]

    card = client.get(f"/servers/{server_id}/card").text
    assert "Deck 17" in card and "DeathMatch" in card and "online" in card
    server_page = client.get(f"/servers/{server_id}").text
    assert '<optgroup label="Capture the Flag">' in server_page

    [(ctf,)] = query("SELECT id FROM server_maps WHERE key = 'CTF-Face3'")
    assert post(f"/servers/{server_id}/map", map_id=str(ctf))[0] == 303
    assert background_server.travels == ["CTF-Face3?Game=XGame.xCTFGame"]
    assert "Facing Worlds 3" in client.get(f"/servers/{server_id}/card").text
    assert post(f"/servers/{server_id}/restart", mode="hard")[0] == 303
    assert post(f"/servers/{server_id}/restart", mode="soft")[0] == 400  # not offered
    for path in (f"/servers/{server_id}", "/admin/servers", "/admin/audit"):
        assert PASSWORD not in client.get(path).text


# --- players and console -------------------------------------------------------------------


def test_tables_line_up_with_their_header_row() -> None:
    page = parse(
        "<table><tr><td><table><tr><td>Kick</td><td>BanIP</td><td>Name</td><td>Ping</td></tr>"
        '<tr><td><input type="checkbox" name="KickPlayer3" value="kick"></td>'
        '<td><input type="checkbox" name="BanPlayer3"></td><td>Ada&nbsp;</td><td>40</td></tr>'
        "<tr><td colspan=2>&nbsp;</td><td>Loque&nbsp;(Bot)</td><td>0</td>"
        '<tr><td colspan="4">** No Players Connected **</td></tr>'
        "</table></td></tr></table>"
    )

    columns, rows = uweb.table(page, "Name", "ping")

    assert columns == {"kick": 0, "banip": 1, "name": 2, "ping": 3}
    assert [[cell.text for cell in row] for row in rows] == [
        ["", "", "Ada", "40"],
        ["", "", "Loque (Bot)", "0"],
        ["** No Players Connected **", "", "", ""],
    ]
    assert rows[0][0].inputs == {"KickPlayer3": "kick"}
    assert uweb.table(page, "Score") == ({}, [])


def test_text_lines_of_a_page_or_element() -> None:
    page = (
        '<div id="content"><div id="consoleResults">&gt; <span>getall x</span><br />\r\n'
        "0) a = 1\r\n1) b&nbsp;=&nbsp;2<div>inner</div></div>after</div>"
    )

    assert uweb.text_lines(page, "consoleResults") == [
        "> getall x",
        "0) a = 1",
        "1) b = 2",
        "inner",
    ]
    assert uweb.text_lines(page, "missing") is None
    assert uweb.text_lines("<h1>Log</h1>&gt; one<br>&gt; two<br>") == ["Log", "> one", "> two"]


async def test_ut2004_lists_kicks_and_bans_players(server: UnrealSimulator) -> None:
    game = adapter(server)

    players = await game.list_players()
    assert [(p.id, p.name, p.score, p.ping, p.team, p.bot, p.can_ban) for p in players] == [
        ("3", "Ada", 12, 192, None, False, True),
        ("4", "Loque", 5, 0, None, True, False),  # bots: kick yes, ban no
    ]
    server.game_class = "XGame.xCTFGame"
    assert [p.team for p in await game.list_players()] == ["Red Team", "Blue Team"]

    await game.kick(players[1])
    await game.ban(players[0])

    assert server.roster == [] and server.banned == ["10.0.0.5"]


async def test_ut99_cannot_remove_bots_and_ignores_forged_boxes(ut99: UnrealSimulator) -> None:
    game = ut99_adapter(ut99)
    # UT99 does not escape names: a name that brings another player's kick box.
    ut99.roster[0].name = 'Ada<input type="checkbox" name="KickPlayer4">'

    players = await game.list_players()

    assert [(p.id, p.name, p.bot, p.can_kick, p.can_ban) for p in players] == [
        ("3", "Ada", False, True, True),  # the box after the name is not taken
        ("bot-1", "Loque", True, False, False),
    ]
    with pytest.raises(CommandRejectedError):
        await game.kick(players[1])
    await game.kick(players[0])
    assert [player.name for player in ut99.roster] == ["Loque"] and ut99.banned == []

    # A name that adds a cell takes its row out of the list, not another player's place.
    ut99.roster.insert(0, SimulatedPlayer(5, "Eve</div></td><td>"))
    assert [p.name for p in await game.list_players()] == ["Loque"]


async def test_webadmin_console_returns_what_the_log_gained(server: UnrealSimulator) -> None:
    game = adapter(server)

    assert await game.console("get ini:Engine.Engine.GameEngine CacheSizeMegs") == "WebAdmin: 32"
    assert await game.console("nosuchcommand") == ""
    # The log is a window over the last lines: the answer is still found when it scrolled.
    for number in range(10):
        assert await game.console(f"say {number}") == str(number)
    assert await game.console("say 9") == "9"  # the same line again
    assert await game.console("echo <b>&nbsp;") == "<b>&nbsp;"
    assert server.commands[0] == "get ini:Engine.Engine.GameEngine CacheSizeMegs"


async def test_ut99_console(ut99: UnrealSimulator) -> None:
    game = ut99_adapter(ut99)

    assert await game.console("get ini:Engine.Engine.GameEngine CacheSizeMegs") == "16"
    assert await game.console("say hi") == "(Event) Admin: hi"


async def test_ut3_lists_humans_and_bots_and_removes_humans(ut3: UT3Simulator) -> None:
    game = ut3_adapter(ut3)

    players = await game.list_players()

    key = "256_0x0000000000000000_25.6000"
    assert [(p.id, p.name, p.score, p.ping, p.team, p.bot) for p in players] == [
        (key, "Ada", None, 48, "Red", False),  # two players called Ada: no score
        ("257_0x0000000000000000_25.7000", "<b>Bob</b>", -1, 60, "Blue", False),
        ("258_0x0000000000000000_25.8000", "Ada", None, 52, "Blue", False),
        ("bot-3", "Kragoth", 4, None, None, True),
    ]
    assert not players[3].can_kick and not players[3].can_ban

    await game.ban(players[1])
    await game.kick(players[0])

    assert [p.id for p in ut3.roster] == [258, 259] and ut3.banned == ["10.0.0.6"]
    with pytest.raises(CommandRejectedError):
        await game.kick(players[3])  # a bot


async def test_ut3_console(ut3: UT3Simulator) -> None:
    game = ut3_adapter(ut3)

    answer = await game.console("getall UTGame GoalScore")
    assert answer == (
        "0) UTDeathmatch DM-Deck.TheWorld:PersistentLevel.UTDeathmatch_2.GoalScore = 0\n1) <none>"
    )
    assert await game.console("say hi") == ""
    ut3.console_enabled = False
    with pytest.raises(UnsupportedError, match="bConsoleEnabled"):
        await game.console("say hi")


FORGED_ROW = (
    "Mallory</div></td><td>&nbsp;</td><td>1</td><td>1</td><td>1</td></tr>"
    '<tr><td><input type="checkbox" name="KickPlayer7" value="kick"></td>'
    '<td><input type="checkbox" name="BanPlayer7" value="ban"></td><td><div>Lookalike'
)


@pytest.mark.parametrize(
    "name",
    [
        FORGED_ROW,  # a row of its own with the victim's id, before the victim's real row
        "Mallory</div></td></tr></table><table><tr><td>",  # hides the rows after it
    ],
)
async def test_ut99_refuses_a_player_page_with_forged_rows(
    ut99: UnrealSimulator, name: str
) -> None:
    game = ut99_adapter(ut99)
    ut99.roster.insert(0, SimulatedPlayer(9, name))
    ut99.roster.append(SimulatedPlayer(7, "Victim"))

    with pytest.raises(ProtocolError, match="forged"):
        await game.list_players()


def test_webadmin_pages_that_explode_are_refused_quickly() -> None:
    import time

    for html in (
        "<table><tr>" + "<td colspan=64>" * 20_000,  # 1.3 million padding cells
        "<table>" * 100,  # deep nesting
        "<b>" * (uweb.MAX_TAGS + 1),
    ):
        started = time.monotonic()
        with pytest.raises(ProtocolError, match="too large"):
            uweb.parse(html)
        assert time.monotonic() - started < 2
    with pytest.raises(ProtocolError, match="too large"):
        uweb.text_lines("<p>" * (uweb.MAX_TAGS + 1))
