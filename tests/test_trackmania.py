"""GBXRemote transport and the TrackMania Forever adapter, against the simulator."""

import asyncio
import struct
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

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
from lanpilot.core.transports import gbxremote
from lanpilot.core.transports.gbxremote import GbxFaultError, GbxRemote
from lanpilot.games.trackmania import common
from lanpilot.games.trackmania.common import ChallengeConnection, password_flag
from lanpilot.games.trackmania.forever import (
    TrackmaniaForeverAdapter,
    TrackmaniaForeverConnection,
    plain,
    valid_filename,
)
from lanpilot.games.trackmania.sunrise import TrackmaniaSunriseAdapter
from lanpilot.testing.contract import check_adapter
from lanpilot.testing.gbxremote import GbxRemoteSimulator, track
from tests.conftest import ClientFactory, Query, UserFactory, csrf_token, sign_in

PASSWORD = "tm-s3cret"


@pytest.fixture
async def server() -> AsyncIterator[GbxRemoteSimulator]:
    async with GbxRemoteSimulator(passwords={"Admin": PASSWORD}) as simulator:
        yield simulator


def client(server: GbxRemoteSimulator, password: str = PASSWORD) -> GbxRemote:
    return GbxRemote("127.0.0.1", server.port, "Admin", password, timeout_seconds=0.5)


def adapter(server: GbxRemoteSimulator, password: str = PASSWORD) -> TrackmaniaForeverAdapter:
    connection = TrackmaniaForeverConnection(
        host="127.0.0.1", port=server.port, password=SecretStr(password)
    )
    return TrackmaniaForeverAdapter(connection)


async def raw_server(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> tuple[asyncio.Server, int]:
    listener = await asyncio.start_server(handler, "127.0.0.1", 0)
    return listener, listener.sockets[0].getsockname()[1]


def frame(handle: int, body: bytes) -> bytes:
    return struct.pack("<II", len(body), handle) + body


# --- transport -----------------------------------------------------------------------------


async def test_calls_on_one_authenticated_connection(server: GbxRemoteSimulator) -> None:
    connection = client(server)

    assert await connection.call("GetServerName") == "$o$f00Simulated$z TMNF"
    assert await connection.call("GetMaxPlayers") == {"CurrentValue": 32}
    await connection.close()

    assert server.calls == ["GetServerName", "GetMaxPlayers"]
    assert server.connections == 1


async def test_callbacks_and_stale_answers_are_skipped(server: GbxRemoteSimulator) -> None:
    server.callbacks_before_answer = 3
    connection = client(server)

    assert await connection.call("GetGameMode") == 1
    await connection.close()


async def test_multicall_returns_results_and_faults(server: GbxRemoteSimulator) -> None:
    connection = client(server)

    name, missing, mode = await connection.multicall(
        [("GetServerName", []), ("NoSuchMethod", []), ("GetGameMode", [])]
    )
    await connection.close()

    assert name == "$o$f00Simulated$z TMNF" and mode == 1
    assert isinstance(missing, GbxFaultError) and missing.code == -506


async def test_faults_leave_the_connection_usable(server: GbxRemoteSimulator) -> None:
    connection = client(server)

    with pytest.raises(GbxFaultError, match="Track unknown"):
        await connection.call("ChooseNextChallenge", "nope.Challenge.Gbx")
    assert await connection.call("GetGameMode") == 1
    await connection.close()
    assert server.connections == 1


async def test_wrong_password_is_sent_only_once(server: GbxRemoteSimulator) -> None:
    connection = client(server, "wrong-password")

    for _ in range(4):
        with pytest.raises(AuthenticationError) as refused:
            await connection.call("GetServerName")
        assert "wrong-password" not in str(refused.value)

    assert server.bad_passwords == 1
    assert server.calls == []


async def test_dropped_connection_is_opened_again(server: GbxRemoteSimulator) -> None:
    connection = client(server)
    await connection.call("GetGameMode")
    server.drop_connections()
    await asyncio.sleep(0.05)

    assert await connection.call("GetGameMode") == 1
    await connection.close()
    assert server.connections == 2


@pytest.mark.parametrize("greeting", [b"GBXRemote 1", b"HTTP/1.0 200 OK", b"x" * 65])
async def test_other_protocols_are_refused(server: GbxRemoteSimulator, greeting: bytes) -> None:
    server.greeting = greeting

    with pytest.raises(ProtocolError):
        await client(server).call("GetVersion")


async def test_silent_and_missing_servers_are_unreachable(server: GbxRemoteSimulator) -> None:
    server.silent = True
    with pytest.raises(ServerUnreachableError):
        await client(server).call("GetVersion")

    port = server.port
    server.stop()
    with pytest.raises(ServerUnreachableError):
        await GbxRemote("127.0.0.1", port, "Admin", PASSWORD, timeout_seconds=0.5).call("X")


OK = (
    b"<?xml version='1.0'?><methodResponse><params><param><value><boolean>1</boolean>"
    b"</value></param></params></methodResponse>"
)


@pytest.mark.parametrize(
    "answer",
    [
        b"<not xml",
        b"<?xml version='1.0'?><methodResponse><params></params></methodResponse>",
        b"<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><methodResponse>&e;</methodResponse>",
    ],
)
async def test_malformed_answers_are_protocol_errors(answer: bytes) -> None:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(struct.pack("<I", 11) + b"GBXRemote 2")
        for reply in (OK, answer):  # the login, then the call
            size, handle = struct.unpack("<II", await reader.readexactly(8))
            await reader.readexactly(size)
            writer.write(frame(handle, reply))
        await reader.read()

    listener, port = await raw_server(serve)
    try:
        connection = GbxRemote("127.0.0.1", port, "Admin", "pw", timeout_seconds=0.5)
        with pytest.raises(ProtocolError):
            await connection.call("GetVersion")
        await connection.close()
    finally:
        listener.close()


async def test_oversized_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gbxremote, "MAX_ANSWER_BYTES", 1000)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(struct.pack("<I", 11) + b"GBXRemote 2")
        _, handle = struct.unpack("<II", await reader.readexactly(8))
        writer.write(struct.pack("<II", 10_000_000, handle))
        await reader.read()

    listener, port = await raw_server(serve)
    try:
        with pytest.raises(ProtocolError, match="too long"):
            await GbxRemote("127.0.0.1", port, "Admin", "pw", timeout_seconds=0.5).call("X")
    finally:
        listener.close()


# --- parsing -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("$o$f00Red$z Server", "Red Server"),
        ("100$$ fun", "100$ fun"),
        ("$l[http://example.org]Link$l text", "Link text"),
        ("$fffB05$o-Race", "B05-Race"),
        ("trailing$", "trailing"),
        ("plain", "plain"),
    ],
)
def test_formatting_codes_are_removed(raw: str, shown: str) -> None:
    assert plain(raw) == shown


@pytest.mark.parametrize(
    ("filename", "valid"),
    [
        ("Campaigns/Nations/Green/B11-Race.Challenge.Gbx", True),
        ("Challenges\\My Tracks\\Fun (v2).Challenge.Gbx", True),
        ("../../etc/passwd.gbx", False),
        ("Challenges/..\\x.Challenge.Gbx", False),
        ("Challenges/notes.txt", False),
        ("line\nbreak.gbx", False),
        ("", False),
        ("x" * 252 + ".gbx", False),
    ],
)
def test_track_file_names_are_checked(filename: str, valid: bool) -> None:
    assert valid_filename(filename) is valid


# --- adapter -------------------------------------------------------------------------------


async def test_adapter_fulfils_the_contract(server: GbxRemoteSimulator) -> None:
    await check_adapter(adapter(server))


def test_adapter_is_registered_and_describes_its_form() -> None:
    assert load_registry().get("tmforever") is TrackmaniaForeverAdapter

    fields = {field.name: field for field in describe_fields(TrackmaniaForeverConnection)}
    assert list(fields) == ["host", "port", "login", "password"]
    assert fields["port"].default == 5000
    assert fields["login"].kind == "select" and fields["login"].default == "Admin"
    assert fields["password"].kind == "password"


async def test_status(server: GbxRemoteSimulator) -> None:
    game = adapter(server)

    status = await game.query()
    await game.close()

    assert (status.name, status.players, status.max_players) == ("Simulated TMNF", 3, 32)
    assert status.game_type == "Time Attack"
    assert status.password_protected is False
    assert status.map is not None
    assert status.map.key == "Campaigns/Nations/Green/B11-Race.Challenge.Gbx"
    assert status.map.name == "B11-Race"
    assert status.map.extra == {"uid": server.tracks[0]["UId"], "environment": "Stadium"}
    assert server.connections == 1


async def test_join_password_is_reported_but_not_kept(server: GbxRemoteSimulator) -> None:
    server.join_password = "lan-party"
    game = adapter(server)

    status = await game.query()
    await game.close()

    assert status.password_protected is True
    assert "lan-party" not in repr(status)


async def test_track_list_is_read_page_by_page(
    server: GbxRemoteSimulator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(common, "PAGE_SIZE", 2)
    server.tracks.append({"Name": "broken"})  # no file name: left out
    game = adapter(server)

    maps = await game.list_maps()
    await game.close()

    assert [item.name for item in maps] == [
        "B11-Race",
        "B12-Race",
        "B03-Race",
        "B05-Race",
        "A01-Race",
    ]
    assert server.calls.count("GetChallengeList") == 4  # 2 + 2 + 2 + an empty page


async def test_change_map_and_restarts(server: GbxRemoteSimulator) -> None:
    game = adapter(server)
    target = MapRef(track("A01-Race")["FileName"])

    await game.change_map(target)
    assert (await game.query()).map == MapRef(
        target.key, "A01-Race", None, {"uid": track("A01-Race")["UId"], "environment": "Stadium"}
    )
    # Like the real server: the chosen track now follows the previous one.
    assert [item["Name"] for item in server.tracks][:2] == ["B11-Race", "A01-Race"]

    await game.restart(RestartMode.SOFT)
    await game.restart(RestartMode.HARD)
    await game.close()
    assert server.restarts == ["soft", "hard"]
    assert server.current_track["Name"] == "A01-Race"


async def test_unknown_track_and_loading_server_are_reported(server: GbxRemoteSimulator) -> None:
    game = adapter(server)

    with pytest.raises(CommandRejectedError, match="does not have this track"):
        await game.change_map(MapRef("Campaigns/Nope.Challenge.Gbx"))
    server.loading = True
    with pytest.raises(CommandRejectedError, match="still loading"):
        await game.restart(RestartMode.SOFT)
    with pytest.raises(CommandRejectedError, match="still loading"):
        await game.change_map(MapRef(track("A01-Race")["FileName"]))
    await game.close()


@pytest.mark.parametrize("key", ["../../x.Challenge.Gbx", "x.txt", "a\nb.gbx", ""])
async def test_unsafe_track_names_never_reach_the_server(
    server: GbxRemoteSimulator, key: str
) -> None:
    game = adapter(server)

    with pytest.raises(CommandRejectedError, match="cannot be sent"):
        await game.change_map(MapRef(key))
    await game.close()

    assert "ChooseNextChallenge" not in server.calls


async def test_wrong_password_makes_the_status_fail_once(server: GbxRemoteSimulator) -> None:
    game = adapter(server, "wrong-password")

    for _ in range(3):
        with pytest.raises(AuthenticationError):
            await game.query()
    await game.close()

    assert server.bad_passwords == 1


E01 = track("E01-Obstacle")
E01["FileName"] = "Campaigns/Nations/Black/E01-Obstacle.Challenge.Gbx"


async def test_tracks_outside_the_playlist_are_looked_up(server: GbxRemoteSimulator) -> None:
    server.disk_tracks = [dict(E01)]
    game = adapter(server)

    found = await game.look_up_map(E01["FileName"])
    assert (found.key, found.name, found.extra["uid"]) == (
        E01["FileName"],
        "E01-Obstacle",
        E01["UId"],
    )
    with pytest.raises(CommandRejectedError, match="does not have this track"):
        await game.look_up_map("Campaigns/Nations/Black/Nope.Challenge.Gbx")
    with pytest.raises(CommandRejectedError, match="file name relative"):
        await game.look_up_map("../../GameData/Config/dedicated_cfg.txt")
    # Known to the server, but of an environment its pack mask does not allow.
    coast = track("CoastE", environment="Coast")
    server.disk_tracks.append(coast)
    with pytest.raises(CommandRejectedError, match=r"cannot run this track: .*packmask"):
        await game.look_up_map(coast["FileName"])
    await game.close()

    assert server.calls == [
        "CheckChallengeForCurrentServerParams",
        "GetChallengeInfo",
        "CheckChallengeForCurrentServerParams",
        "CheckChallengeForCurrentServerParams",
    ]


async def test_changing_to_a_track_outside_the_playlist_inserts_it(
    server: GbxRemoteSimulator,
) -> None:
    server.disk_tracks = [dict(E01)]
    game = adapter(server)

    await game.change_map(MapRef(E01["FileName"]))
    await game.close()

    assert server.current_track["Name"] == "E01-Obstacle"
    assert len(server.tracks) == 6  # it is in the playlist now
    assert server.calls[-3:] == ["InsertChallenge", "ChooseNextChallenge", "NextChallenge"]


def test_permission_faults_are_explained() -> None:
    error = common._translate(GbxFaultError(-1000, "Permission denied."))
    assert isinstance(error, AuthenticationError) and "Admin" in str(error)
    other = GbxFaultError(-1000, "Something\x00 odd\n" + "x" * 500)
    assert common._translate(other) is other and len(str(other)) <= 200
    assert "\x00" not in str(other)


# --- TrackMania Sunrise ---------------------------------------------------------------------


@pytest.fixture
async def sunrise() -> AsyncIterator[GbxRemoteSimulator]:
    async with GbxRemoteSimulator(passwords={"Admin": PASSWORD}, game="sunrise") as simulator:
        yield simulator


def sunrise_adapter(
    server: GbxRemoteSimulator, password: str = PASSWORD
) -> TrackmaniaSunriseAdapter:
    connection = ChallengeConnection(
        host="127.0.0.1", port=server.port, password=SecretStr(password)
    )
    return TrackmaniaSunriseAdapter(connection)


async def test_sunrise_fulfils_the_contract(sunrise: GbxRemoteSimulator) -> None:
    await check_adapter(sunrise_adapter(sunrise))


def test_sunrise_is_registered() -> None:
    assert load_registry().get("tmsunrise") is TrackmaniaSunriseAdapter
    fields = [field.name for field in describe_fields(TrackmaniaSunriseAdapter.connection_schema)]
    assert fields == ["host", "port", "login", "password"]


async def test_sunrise_status_needs_no_password(sunrise: GbxRemoteSimulator) -> None:
    game = sunrise_adapter(sunrise, "wrong-password")

    for _ in range(3):
        status = await game.query()
        assert status.map is not None and status.map.name == "B11-Race"
        assert status.password_protected is False
    with pytest.raises(AuthenticationError):
        await game.restart(RestartMode.SOFT)
    with pytest.raises(AuthenticationError):
        await game.restart(RestartMode.SOFT)
    await game.close()

    assert sunrise.bad_passwords == 1


async def test_sunrise_tells_whether_a_join_password_is_set(sunrise: GbxRemoteSimulator) -> None:
    sunrise.join_password = "lan-party"
    game = sunrise_adapter(sunrise)

    status = await game.query()
    await game.close()

    assert status.password_protected is True
    assert "lan-party" not in repr(status)


async def test_sunrise_restarts_and_changes(sunrise: GbxRemoteSimulator) -> None:
    sunrise.disk_tracks = [dict(E01)]
    game = sunrise_adapter(sunrise)

    await game.restart(RestartMode.SOFT)
    await game.change_map(MapRef(track("A01-Race")["FileName"]))
    await game.restart(RestartMode.HARD)
    found = await game.look_up_map(E01["FileName"])  # no check method on Sunrise
    await game.change_map(found)
    with pytest.raises(CommandRejectedError, match="does not have this track"):
        await game.change_map(MapRef("Challenges/Nope.Challenge.Gbx"))
    await game.close()

    assert "ChallengeRestart" in sunrise.calls and "RestartChallenge" not in sunrise.calls
    assert "CheckChallengeForCurrentServerParams" not in sunrise.calls
    assert sunrise.restarts == ["soft", "hard"]
    assert sunrise.current_track["Name"] == "E01-Obstacle"


async def test_sunrise_status_while_a_track_loads(sunrise: GbxRemoteSimulator) -> None:
    sunrise.loading = True
    game = sunrise_adapter(sunrise)

    status = await game.query()
    await game.close()

    assert status.map is None and status.name == "Simulated TMNF"


@pytest.mark.parametrize(
    ("answer", "flag"),
    [("No password", False), ("Password required", True), ("", False), ("secret", True), (3, None)],
)
def test_password_flag(answer: object, flag: bool | None) -> None:
    assert password_flag(answer) is flag


# --- players -------------------------------------------------------------------------------


async def test_players_are_listed_with_scores_and_teams(server: GbxRemoteSimulator) -> None:
    server.roster[0].update(NickName="$o$f00Ada$z", TeamId=1)
    server.roster[1].update(Login="lan/10.0.0.5:2350", NickName="$$$")
    server.roster.append({"Login": "bad login", "NickName": "x", "TeamId": 0, "Score": 0})

    players = await adapter(server).list_players()

    assert [(p.id, p.name, p.score, p.team) for p in players] == [
        ("player0", "Ada", 0, "Red"),
        ("lan/10.0.0.5:2350", "$", 10, None),
        ("player2", "Player 2", 20, None),
    ]


async def test_kick_and_ban_by_login(server: GbxRemoteSimulator) -> None:
    game = adapter(server)
    players = await game.list_players()

    await game.kick(players[0], "afk")
    await game.ban(players[1])
    with pytest.raises(CommandRejectedError, match="no longer on the server"):
        await game.kick(players[0])

    assert server.removed == [("Kick", "player0", "afk"), ("Ban", "player1", "")]


async def test_sunrise_players_without_struct_version_or_message(
    sunrise: GbxRemoteSimulator,
) -> None:
    game = sunrise_adapter(sunrise)
    players = await game.list_players()

    await game.ban(players[2], "cheating")  # Sunrise cannot show a message

    assert [p.id for p in players] == ["player0", "player1", "player2"]
    assert sunrise.removed == [("Ban", "player2", "")]


# --- through the web interface -------------------------------------------------------------


@pytest.fixture
def background_server() -> Iterator[GbxRemoteSimulator]:
    """A simulator in its own thread: the test client blocks the test's event loop."""
    simulator = GbxRemoteSimulator(passwords={"Admin": PASSWORD}, disk_tracks=(dict(E01),))
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


def test_trackmania_server_in_the_web_interface(
    background_server: GbxRemoteSimulator,
    make_client: ClientFactory,
    make_user: UserFactory,
    query: Query,
) -> None:
    make_user("root", Role.ADMIN)
    web: TestClient = make_client()
    sign_in(web, "root")

    def post(path: str, **data: str) -> tuple[int, str]:
        response = web.post(path, data={**data, "csrf_token": csrf_token(web)})
        return response.status_code, response.text

    form = web.get("/admin/servers/new?adapter=tmforever").text
    assert 'name="c_login"' in form and 'name="c_port" type="number" value="5000"' in form

    status, page = post(
        "/admin/servers/new",
        adapter="tmforever",
        name="TM Arena",
        enabled="1",
        c_host="127.0.0.1",
        c_port=str(background_server.port),
        c_login="Admin",
        c_password=PASSWORD,
    )
    assert status == 303, page
    [(server_id,)] = query("SELECT id FROM servers")
    assert query("SELECT count(*) FROM server_maps") == [(5,)]

    card = web.get(f"/servers/{server_id}/card").text
    assert "B11-Race" in card and "Time Attack" in card and "online" in card

    page = web.get(f"/servers/{server_id}").text
    assert ">A01-Race</option>" in page  # the file name is not repeated in the picker
    [(a01,)] = query("SELECT id FROM server_maps WHERE name = 'A01-Race'")
    assert post(f"/servers/{server_id}/map", map_id=str(a01))[0] == 303
    assert "A01-Race" in web.get(f"/servers/{server_id}/card").text
    assert post(f"/servers/{server_id}/restart", mode="soft")[0] == 303
    assert background_server.restarts == ["soft"]
    for path in (f"/servers/{server_id}", "/admin/servers", "/admin/audit"):
        assert PASSWORD not in web.get(path).text


def test_maps_added_by_hand_in_the_web_interface(
    background_server: GbxRemoteSimulator,
    make_client: ClientFactory,
    make_user: UserFactory,
    query: Query,
) -> None:
    make_user("root", Role.ADMIN)
    make_user("operator", Role.OPERATOR)
    web: TestClient = make_client()
    sign_in(web, "root")

    def post(path: str, **data: str) -> tuple[int, str]:
        response = web.post(path, data={**data, "csrf_token": csrf_token(web)})
        return response.status_code, response.text

    post(
        "/admin/servers/new",
        adapter="tmforever",
        name="TM Arena",
        enabled="1",
        c_host="127.0.0.1",
        c_port=str(background_server.port),
        c_login="Admin",
        c_password=PASSWORD,
    )
    [(server_id,)] = query("SELECT id FROM servers")
    maps_page = f"/admin/servers/{server_id}/maps"
    assert "Add a map the server does not list" in web.get(maps_page).text

    status, page = post(f"{maps_page}/manual", key="Campaigns/Nations/Black/Nope.Challenge.Gbx")
    assert status == 502 and "does not have this track" in page
    status, page = post(f"{maps_page}/manual", key="../secret.txt")
    assert status == 502 and "file name relative" in page

    assert post(f"{maps_page}/manual", key=E01["FileName"])[0] == 303
    assert query("SELECT name, source FROM server_maps WHERE source = 'manual'") == [
        ("E01-Obstacle", "manual")
    ]
    assert "added by hand" in web.get(maps_page).text
    status, page = post(f"{maps_page}/manual", key=E01["FileName"])
    assert status == 409 and "already in the list" in page

    # Operators change to it like to any other map; it lands in the playlist ...
    [(e01_id,)] = query("SELECT id FROM server_maps WHERE source = 'manual'")
    assert post(f"/servers/{server_id}/map", map_id=str(e01_id))[0] == 303
    assert background_server.current_track["Name"] == "E01-Obstacle"
    # ... and stays a single entry added by hand when the list is read again.
    assert post(f"/servers/{server_id}/maps/refresh")[0] == 303
    assert query("SELECT count(*), min(source) FROM server_maps WHERE key LIKE '%E01%'") == [
        (1, "manual")
    ]

    # Only entries added by hand can be removed; operators cannot manage the list.
    [(protocol_id,)] = query("SELECT id FROM server_maps WHERE name = 'B11-Race'")
    assert post(f"{maps_page}/{protocol_id}/manual/delete")[0] == 404
    sign_in(web, "operator")
    assert post(f"{maps_page}/manual", key=E01["FileName"])[0] == 403
    sign_in(web, "root")
    assert post(f"{maps_page}/{e01_id}/manual/delete")[0] == 303
    assert query("SELECT count(*) FROM server_maps WHERE source = 'manual'") == [(0,)]
    actions = [row[0] for row in query("SELECT action FROM audit_log WHERE action LIKE 'server.%'")]
    assert actions.count("server.add_map") == 3  # two refused, one added
    assert "server.remove_map" in actions
