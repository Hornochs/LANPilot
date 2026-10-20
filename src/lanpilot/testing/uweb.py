"""Simulator of a UT2004 or UT99 server for tests: the WebAdmin (HTTP, Basic auth) and the
GameSpy status query (UDP), the way UT2004 build 3369 and UT99 v469 answered
(docs/games/unreal-tournament.md).

    async with UnrealSimulator(game="ut2004", password="secret") as server:
        adapter = UT2004Adapter(UT2004Connection(
            host="127.0.0.1", port=server.web_port, query_port=server.query_port,
            password="secret"))

UT2004: a change is a POST of the restart form (``server_restart``) with game type and map;
``current_restart`` is that form. UT99: map values carry ``.unr``; ``SwitchMap`` keeps the
running game type, ``SwitchGameTypeAndMap`` takes the chosen one; requesting
``current_restart`` restarts the map. Both: unknown maps are "switched to" without effect.

The console (``current_console``, answers in ``current_console_log``) answers as measured.
Players (``current_players``) are built after the games' WebAdmin code (XWebAdmin.u,
UTServerAdmin.u), not measured: bots join these servers only with a human player.
"""

from __future__ import annotations

import asyncio
import base64
import html
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, cast

from aiohttp import web
from aiohttp.typedefs import Handler

# Per game: game types (class, name), and per class (in the rotation, other installed maps).
GAMES = {
    "ut2004": (
        (
            ("XGame.xDeathMatch", "DeathMatch"),
            ("XGame.xCTFGame", "Capture the Flag"),
            ("Onslaught.ONSOnslaughtGame", "Onslaught"),
        ),
        {
            "XGame.xDeathMatch": (
                ["DM-Deck17", "DM-Antalus"],
                ["DM-RANKIN", "DM-Rankin", "DM-Gael"],
            ),
            "XGame.xCTFGame": (["CTF-Face3"], ["CTF-FaceClassic", "CTF-Evil;quit"]),
            "Onslaught.ONSOnslaughtGame": (
                ["ONS-TORLAN?LinkSetup=Default"],
                ["ONS-Dawn?LinkSetup=Default?AdminPassword=x"],
            ),
        },
    ),
    "ut99": (
        (
            ("Botpack.DeathMatchPlus", "Tournament DeathMatch"),
            ("Botpack.CTFGame", "Capture the Flag"),
            ("Botpack.TDarkMatch", "Tournament DarkMatch"),
        ),
        {
            "Botpack.DeathMatchPlus": (["DM-Deck16][.unr", "DM-Codex.unr"], ["DM-Agony.unr"]),
            "Botpack.CTFGame": (["CTF-Face.unr"], ["CTF-Coret.unr"]),
            "Botpack.TDarkMatch": ([""], [""]),  # the WebAdmin shows "*** None ***"
        },
    ),
}
TITLES = {"DM-Deck17": "Deck 17", "CTF-Face3": "Facing Worlds 3", "DM-Deck16][": "Deck #16 ]["}
PLEASE_WAIT = "<td class=ttitle>Please Wait</td>"
CONSOLE_LOG_LINES = 8  # the simulated message log keeps this many lines


@dataclass
class SimulatedPlayer:
    id: int
    name: str
    score: int = 0
    ping: int = 40
    team: int | None = None  # 0 red, 1 blue
    bot: bool = False
    ip: str = "10.0.0.5"


def default_roster() -> list[SimulatedPlayer]:
    return [SimulatedPlayer(3, "Ada", 12, 48, 0), SimulatedPlayer(4, "Loque", 5, 0, 1, bot=True)]


def _select(name: str, values: list[str], attributes: str = "") -> str:
    options = "".join(
        f'<option value="{html.escape(value)}">'
        f"{html.escape(value.split('?')[0].removesuffix('.unr') or '*** None ***')}</option>"
        for value in values
    )
    return f"<select class='mini' name='{name}'{attributes}>{options}</select>"


class _Query(asyncio.DatagramProtocol):
    def __init__(self, server: UnrealSimulator) -> None:
        self.server = server
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # Python 3.11's selector transport does not derive from DatagramTransport.
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport is None or self.server.silent:
            return
        server = self.server
        ut99 = server.game == "ut99"
        # UT99 says True/False, UT2004 1/0.
        password = {(True, True): "True", (True, False): "1", (False, True): "False"}.get(
            (bool(server.join_password), ut99), "0"
        )
        fields = {
            "gamename": "ut" if ut99 else "ut2004",
            "gamever": "469" if ut99 else "3369",
            "hostname": server.hostname,
            "hostport": "7777",
            "maptitle": TITLES.get(server.current_map, server.current_map),
            "mapname": server.current_map,
            "gametype": server.game_class.split(".")[1],
            "numplayers": str(len(server.roster)),
            "maxplayers": "16",
            "password": password,
        }
        body = "".join(f"\\{key}\\{value}" for key, value in fields.items())
        self.transport.sendto(f"{body}\\final\\\\queryid\\1.1".encode("latin-1"), addr)


class UnrealSimulator:
    def __init__(
        self,
        *,
        game: Literal["ut2004", "ut99"] = "ut2004",
        username: str = "admin",
        password: str = "secret",  # noqa: S107 - default of a test double
        hostname: str = "Simulated UT",
    ) -> None:
        self.game = game
        self.game_types, self.map_lists = GAMES[game]
        self.username = username
        self.password = password
        self.hostname = hostname
        self.roster = default_roster()
        #: IP addresses the server banned.
        self.banned: list[str] = []
        #: Console commands the server ran, and its message log ("> " lines).
        self.commands: list[str] = []
        self.console_log = ["Ada joined the game.", "Ada: gl hf"]
        self.join_password = ""
        self.game_class = self.game_types[0][0]
        self.current_map = self.map_lists[self.game_class][0][0].removesuffix(".unr")
        #: Travels the server made, as "Map?Game=Class" URLs (without .unr).
        self.travels: list[str] = []
        #: Requests with wrong credentials.
        self.refused = 0
        self.silent = False
        self.web_port = 0
        self.query_port = 0
        self._runner: web.AppRunner | None = None
        self._udp: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        app = web.Application(middlewares=[self._auth])
        app.router.add_get("/ServerAdmin/current_game", self._current_game)
        app.router.add_post("/ServerAdmin/current_game", self._current_game)
        app.router.add_get("/ServerAdmin/defaults_maps", self._defaults_maps)
        app.router.add_get("/ServerAdmin/current_restart", self._current_restart)
        app.router.add_post("/ServerAdmin/server_restart", self._server_restart)
        app.router.add_get("/ServerAdmin/current_players", self._current_players)
        app.router.add_post("/ServerAdmin/current_players", self._current_players)
        app.router.add_post("/ServerAdmin/current_console", self._current_console)
        app.router.add_get("/ServerAdmin/current_console_log", self._current_console_log)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.web_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        loop = asyncio.get_running_loop()
        self._udp, _ = await loop.create_datagram_endpoint(
            lambda: _Query(self), local_addr=("127.0.0.1", 0)
        )
        self.query_port = self._udp.get_extra_info("sockname")[1]

    async def stop(self) -> None:
        if self._udp is not None:
            self._udp.close()
            self._udp = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def __aenter__(self) -> UnrealSimulator:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    # --- WebAdmin ----------------------------------------------------------------------

    @web.middleware
    async def _auth(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        expected = base64.b64encode(f"{self.username}:{self.password}".encode("latin-1"))
        if request.headers.get("Authorization", "") != f"Basic {expected.decode()}":
            self.refused += 1
            return web.Response(
                status=401,
                text="<TITLE>401 Unauthorized</TITLE><H1>401 Unauthorized</H1>",
                headers={"WWW-authenticate": 'basic realm="UT Remote Admin Server"'},
            )
        return await handler(request)

    @staticmethod
    def _page(body: str) -> web.Response:
        return web.Response(
            body=f"<html><body>{body}</body></html>".encode("latin-1"),
            content_type="text/html",
            charset="iso-8859-1",
        )

    def _travel(self, url: str, game_class: str) -> web.Response:
        name, _, options = url.partition("?")
        name = name.removesuffix(".unr")
        self.travels.append(f"{name}{'?' + options if options else ''}?Game={game_class}")
        known = any(
            name.lower() == entry.split("?")[0].removesuffix(".unr").lower()
            for included, excluded in self.map_lists.values()
            for entry in included + excluded
            if entry
        )
        if known:  # like the real servers, an unknown map is "switched to" without effect
            self.current_map, self.game_class = name, game_class
        return self._page(f"{PLEASE_WAIT}The server is now switching to map '{html.escape(url)}'.")

    async def _current_game(self, request: web.Request) -> web.Response:
        form = await request.post() if request.method == "POST" else {}
        if form.get("SwitchMap"):  # keeps the running game type, whatever is selected
            return self._travel(str(form.get("MapSelect", "")), self.game_class)
        if form.get("SwitchGameTypeAndMap") and self.game == "ut99":
            return self._travel(str(form.get("MapSelect", "")), str(form.get("GameTypeSelect")))
        types = "".join(
            f'<option value="{value}"{" selected" if value == self.game_class else ""}>'
            f"{label}</option>"
            for value, label in self.game_types
        )
        rotation = self.map_lists[self.game_class][0]
        return self._page(
            '<form method="post" action="current_game">'
            f"<select class='mini' name='GameTypeSelect'>{types}</select>"
            f"{_select('MapSelect', rotation)}</form>"
        )

    async def _defaults_maps(self, request: web.Request) -> web.Response:
        included, excluded = self.map_lists.get(request.query.get("GameType", ""), ([], []))
        return self._page(
            '<form method="post" action="defaults_maps">'
            f"{_select('ExcludeMapsSelect', excluded, ' size=9 multiple')}"
            f"{_select('IncludeMapsSelect', included, ' size=9 multiple')}"
            "</form>"
        )

    async def _current_restart(self, request: web.Request) -> web.Response:
        if self.game == "ut99":  # a link: requesting it restarts the map
            self.travels.append(f"{self.current_map}?Game={self.game_class}")
            return self._page(f"{PLEASE_WAIT}The server is now restarting the current map.")
        return self._page(
            'Restart Conflicts <form method="post" action="server_restart">'
            '<input type="checkbox" value="True" name="xDeathMatch.TimeLimit" checked>'
            '<input class="button" type="submit" name="Save" value="Accept">'
            f'<input type="hidden" name="GameType" value="{self.game_class}">'
            f'<input type="hidden" name="MapName" value="{self.current_map}"></form>'
        )

    async def _server_restart(self, request: web.Request) -> web.Response:
        form = await request.post()
        return self._travel(str(form.get("MapName", "")), str(form.get("GameType", "")))

    # --- players and console --------------------------------------------------------------

    async def _current_players(self, request: web.Request) -> web.Response:
        form = await request.post() if request.method == "POST" else {}
        ut99 = self.game == "ut99"
        kick, ban = ("KickPlayer", "BanPlayer") if ut99 else ("Kick", "Ban")
        for player in list(self.roster):
            if player.bot and (ut99 or not form.get(f"{kick}{player.id}")):
                continue  # UT99 cannot remove bots; UT2004 kicks them, never bans them
            if not player.bot and form.get(f"{ban}{player.id}"):
                self.banned.append(player.ip)
                self.roster.remove(player)
            elif form.get(f"{kick}{player.id}"):
                self.roster.remove(player)
        rows = self._ut99_players() if ut99 else self._ut2004_players()
        # Both pages put the list inside the page's layout table.
        return self._page(
            f'<table><tr><td><form method="post" action="current_players">{rows}'
            '<input type="submit" name="Kick" value="Kick / Ban"></form></td></tr></table>'
        )

    def _ut99_players(self) -> str:
        header = (
            '<tr><td width="1%">Kick</td><td width="1%">BanIP</td>'
            '<td><a href="current_players?Sort=Name" target="_self">Name</a></td>'
            '<td width="1%"><a href="current_players?Sort=Team" target="_self">Team</a></td>'
            '<td width="1%"><a href="current_players?Sort=Ping" target="_self">Ping</a></td>'
            '<td width="1%"><a href="current_players?Sort=Score" target="_self">Score</a></td>'
            '<td width="1%">IP</td></tr>'
        )
        rows = []
        for player in self.roster:
            if player.bot:
                boxes, suffix, ip = '<tr><td width="1%" colspan=2>&nbsp;</td>', "&nbsp;(Bot)", ""
            else:
                boxes = (
                    '<tr><td width="1%"><div align="center"><input type="checkbox" '
                    f'name="KickPlayer{player.id}" value="kick"></div></td><td width="1%">'
                    '<div align="center"><input type="checkbox" '
                    f'name="BanPlayer{player.id}" value="ban"></div></td>'
                )
                suffix, ip = "", player.ip
            team = {0: "Red", 1: "Blue"}.get(player.team, "") if player.team is not None else ""
            # UT99 puts the name into the page as it is, without escaping it.
            rows.append(
                f'{boxes}<td><div align="left">{player.name}{suffix}</div></td>'
                f'<td width="1%"><div align="center">{team}&nbsp;</div></td>'
                f'<td width="1%"><div align="center">{player.ping}</div></td>'
                f'<td width="1%"><div align="center">{player.score}</div></td>'
                f'<td width="1%"><div align="center">{ip}</div></td></tr>'
            )
        if not rows:
            rows.append('<tr align="center"><td colspan="5">** No Players Connected **</td></tr>')
        return f'<table width="400" border="0">{header}{"".join(rows)}</table>'

    def _ut2004_players(self) -> str:
        team_game = "CTF" in self.game_class

        def head(title: str, link: bool = False) -> str:
            if link:
                return (
                    f'<td><a href="current_players?Sort={title}?Last=Name" target="_self">'
                    f"&nbsp;{title}:&nbsp;</a></td>"
                )
            return f"<td>&nbsp;{title}&nbsp;</td>"

        header = (
            head("Kick")
            + head("Ban")
            + head("Name", True)
            + (head("Team", True) if team_game else "")
            + head("Ping", True)
            + head("Score", True)
            + head("IP")
        )
        rows = []
        for player in self.roster:
            cells = [
                f"<input class=\"checkbox\" type='checkbox' name='Kick{player.id}' value='True'>",
                ""
                if player.bot
                else f"<input class=\"checkbox\" type='checkbox' name='Ban{player.id}' "
                "value='True'>",
            ]
            name = html.escape(player.name + (" (Bot) " if player.bot else "")).replace(
                " ", "&nbsp;"
            )
            row = "".join(f'<td align="center">{cell}</td>' for cell in cells)
            row += f'<td align="left" nowrap>{name}</td>'
            if team_game:
                color, label = [("Red", "Red Team"), ("Blue", "Blue Team")][player.team or 0]
                row += (
                    f"<td align=\"center\" nowrap><span style='background-color: {color}'>"
                    f"&nbsp;&nbsp;</span>{label}</td>"
                )
            ip = "" if player.bot else player.ip
            row += "".join(
                f'<td align="center">{cell}</td>'
                for cell in (player.ping * 4, player.score, f"&nbsp;{ip}")
            )
            rows.append(f'<tr align="left">{row}</tr>')
        if not rows:
            rows.append(
                '<tr align="center"><td colspan="6" nowrap>** No Players Connected **</td></tr>'
            )
        return f'<table border="0"><tr>{header}</tr>{"".join(rows)}</table>'

    async def _current_console(self, request: web.Request) -> web.Response:
        command = str((await request.post()).get("SendText", ""))
        if command:
            self.commands.append(command)
            ut99 = self.game == "ut99"
            # As measured: UT2004 shows answers as said by "WebAdmin" and chat without a
            # name; UT99 the other way round. Unknown commands add nothing.
            if command.lower().startswith("say "):
                said = command[4:]
                self.console_log.append(f"(Event) Admin: {said}" if ut99 else said)
            elif command.lower().startswith("get "):
                self.console_log.append("16" if ut99 else "WebAdmin: 32")
            elif command.lower().startswith("echo "):  # stands for any answer
                self.console_log.append(command[5:])
            del self.console_log[:-CONSOLE_LOG_LINES]
        return self._page('<frameset rows="*,40"></frameset>')

    async def _current_console_log(self, request: web.Request) -> web.Response:
        lines = "".join(f"&gt; {html.escape(line)}<br>" for line in self.console_log)
        return self._page(f'<p class="title">Console</p>{lines}<a name="END"></a>')


# --- UT3 -----------------------------------------------------------------------------------

UT3_GAME_TYPES = (
    ("UTGame.UTDeathmatch", "Deathmatch"),
    ("UTGameContent.UTOnslaughtGame_Content", "Warfare"),
)
# Per game type: (value, label) of the maps, as the change page's data request lists them.
UT3_MAPS = {
    "UTGame.UTDeathmatch": [("DM-Deck", "DM-Deck (Deck)"), ("DM-Arsenal", "DM-Arsenal (Arsenal)")],
    "UTGameContent.UTOnslaughtGame_Content": [
        ("WAR-Avalanche", "WAR-Avalanche (Avalanche)"),
        ("WAR-Avalanche?LinkSetup=Alt", "WAR-Avalanche?LinkSetup=Alt (Avalanche Two Fronts)"),
        ("WAR-Evil;quit", "WAR-Evil;quit (Evil)"),
    ],
}
UT3_URL = "?bIsDedicated=true?bIsLanMatch=true?TimeLimit=15?GoalScore=0"


class UT3Simulator:
    """UT3's WebAdmin with HTTP authentication enabled (``bHttpAuth=true``), the way the
    measured server (UT3 patch 5, web server build 3808) answered. Wrong credentials get the
    login form, not 401; every request without the session cookie opens a new session.

    The scoreboard and the console were measured (with bots); the player page and its kick
    and ban actions follow the WebAdmin's templates and code (no human players to test)."""

    def __init__(
        self,
        *,
        username: str = "admin",
        password: str = "secret",  # noqa: S107 - default of a test double
    ) -> None:
        self.username = username
        self.password = password
        self.game_class = UT3_GAME_TYPES[0][0]
        self.current_map = "DM-Deck"
        self.layout = ""  # ?LinkSetup=... of the running map
        self.mutator = "UTGame.UTMutator_BigHead"  # a running mutator ("" for none)
        self.roster = [
            SimulatedPlayer(256, "Ada", 7, 48, 0),
            SimulatedPlayer(257, "<b>Bob</b>", -1, 60, 1, ip="10.0.0.6"),
            SimulatedPlayer(258, "Ada", 3, 52, 1, ip="10.0.0.7"),
            SimulatedPlayer(259, "Kragoth", 4, 0, 0, bot=True),
        ]
        self.banned: list[str] = []
        self.console_enabled = True
        self.commands: list[str] = []
        #: Changes as (map with options, game type, urlextra, mutator) tuples.
        self.changes: list[tuple[str, str, str, str]] = []
        self.refused = 0
        self.sessions = 0
        self.web_port = 0
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(middlewares=[self._auth])
        app.router.add_get("/ServerAdmin/current", self._current)
        app.router.add_get("/ServerAdmin/current/change", self._change_page)
        app.router.add_post("/ServerAdmin/current/change", self._change)
        app.router.add_post("/ServerAdmin/current/change/data", self._data)
        app.router.add_get("/ServerAdmin/current/players", self._players)
        app.router.add_post("/ServerAdmin/current/players", self._players)
        app.router.add_post("/ServerAdmin/console", self._console)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.web_port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def __aenter__(self) -> UT3Simulator:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    @web.middleware
    async def _auth(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        new_session = "sessionid" not in request.cookies
        if new_session:
            self.sessions += 1
        expected = base64.b64encode(f"{self.username}:{self.password}".encode("latin-1"))
        if request.headers.get("Authorization", "") != f"Basic {expected.decode()}":
            self.refused += 1
            response: web.StreamResponse = self._page(
                '<form method="post" action="/ServerAdmin/">'
                '<input type="hidden" name="token" value="9311E0E6" />'
                '<input type="text" id="username" name="username" value="" />'
                '<input type="password" id="password" name="password" value="" /></form>'
            )
        else:
            response = await handler(request)
        if new_session:
            response.set_cookie("sessionid", f"S{self.sessions:031d}", path="/ServerAdmin/")
        return response

    @staticmethod
    def _page(body: str) -> web.Response:
        return web.Response(
            body=f"<html><body>{body}</body></html>".encode("latin-1"),
            content_type="text/html",
            charset="iso-8859-1",
        )

    async def _current(self, request: web.Request) -> web.Response:
        label = dict(UT3_GAME_TYPES)[self.game_class]
        return self._page(
            '<dl id="currentGame"><dt>Server Name</dt><dd>Simulated UT3</dd>'
            f"<dt>Game type</dt><dd>{label}<br /><code>{self.game_class}</code></dd>"
            f"<dt>Map</dt><dd><code>{self.current_map}</code><br /></dd>"
            "<dt>Mutators</dt><dd></dd></dl>"
            '<dl id="currentRules"><dt>Time Limit</dt><dd>15 minutes</dd>'
            f"<dt>Players (Max)</dt><dd>{self._humans()} (32)</dd></dl>"
            '<h3>Players</h3><table id="players" class="grid"><thead><tr><th>&#160;</th>'
            '<th><a href="/ServerAdmin/current?sortby=name" class="sortable">Player name</a></th>'
            "<th>Ranking</th><th>Score</th><th>Deaths</th><th>Kills</th><th>Ping</th>"
            '<th>Bot</th><th>Admin</th><th title="Spectator">Spec.</th></tr></thead><tbody>'
            + "".join(
                f'<tr class="even"><td style="background: #f00;">{player.team}&#160;</td>'
                f'<td>{html.escape(player.name)}</td><td class="right">0</td>'
                f'<td class="right">{player.score}</td><td class="right">0</td>'
                f'<td class="right">0</td><td class="right" title="Packet loss: 0">'
                f'{player.ping}</td><td class="center">{player.bot}</td>'
                '<td class="center">False</td><td class="center">False</td></tr>'
                for player in self.roster
            )
            + (
                ""
                if self.roster
                else '<tr><td colspan="10" class="center"><em>There are no players</em></td></tr>'
            )
            + "</tbody></table>"
        )

    def _humans(self) -> int:
        return sum(1 for player in self.roster if not player.bot)

    @staticmethod
    def _key(player: SimulatedPlayer) -> str:
        return f"{player.id}_0x0000000000000000_{player.id / 10:.4f}"

    async def _players(self, request: web.Request) -> web.Response:
        form = await request.post() if request.method == "POST" else {}
        action, key = str(form.get("action", "")), str(form.get("playerkey", ""))
        target = next((p for p in self.roster if self._key(p) == key), None)
        if action and target is not None and not target.bot:
            if action == "banip":
                self.banned.append(target.ip)
            if action in ("kick", "banip", "banid", "banhash", "sessionban"):
                self.roster.remove(target)
        rows = (
            "".join(
                f'<tr class="even"><td style="background: #f00;">{player.team}&#160;</td>'
                f'<td>{html.escape(player.name)}</td><td class="right">{player.ping}</td>'
                f"<td>{player.ip}</td><td>0x0000000000000000</td><td></td>"
                '<td class="center">False</td><td class="center">False</td><td>'
                '<form method="post" action="/ServerAdmin/current/players"><div>'
                f'<input type="hidden" name="playerid" value="{player.id}" />'
                f'<input type="hidden" name="playerkey" value="{self._key(player)}" />'
                '<select name="action"><option value=""></option><option value="kick">Kick</option>'
                '<option value="banip">Ban IP</option></select>'
                '<button type="submit">go</button></div></form></td></tr>'
                for player in self.roster
                if not player.bot
            )
            or '<tr><td colspan="9" class="center"><em>There are no players</em></td></tr>'
        )
        return self._page(
            '<div id="messages"><p>No localization data. Please make sure the file '
            "Localization/INT/WebAdmin.int is up to date.</p></div>"
            '<table id="players" class="grid"><thead><tr><th>&#160;</th>'
            '<th><a href="/ServerAdmin/current/players?sortby=name">Player name</a></th>'
            "<th>Ping</th><th>IP</th><th>Unique ID</th><th>Client Hash</th><th>Admin</th>"
            f"<th>Spec.</th><th>Controls</th></tr></thead><tbody>{rows}</tbody></table>"
        )

    async def _console(self, request: web.Request) -> web.Response:
        if not self.console_enabled:
            return web.Response(status=404, text="Not found")
        command = str((await request.post()).get("command", ""))
        self.commands.append(command)
        result = (
            "0) UTDeathmatch DM-Deck.TheWorld:PersistentLevel.UTDeathmatch_2.GoalScore = 0\n"
            "1) <none>"
            if command.lower().startswith("getall ")
            else ""
        )
        return self._page(
            '<div id="content"><form action="/ServerAdmin/console" method="post">'
            '<input type="text" name="command" value="" size="80" /></form>'
            '<div id="consoleResults" style="">&gt; <span class="command">'
            f"{html.escape(command)}</span><br />\r\n{html.escape(result)}</div>\r\n</div>"
            '<div id="menu"><h2>Navigation</h2></div>'
        )

    def _map_select(self, game_class: str) -> str:
        options = "".join(
            f'<option value="{html.escape(value)}">{html.escape(label)}</option>'
            for value, label in UT3_MAPS.get(game_class, [])
        )
        return f'<select id="map" name="map">{options}</select>'

    async def _change_page(self, request: web.Request) -> web.Response:
        types = "".join(
            f'<option value="{value}"{" selected" if value == self.game_class else ""}>'
            f"{label}</option>"
            for value, label in UT3_GAME_TYPES
        )
        checked = ' checked="checked"' if self.mutator else ""
        return self._page(
            '<form method="post" action="/ServerAdmin/current/change">'
            f'<select id="gametype" name="gametype">{types}</select>'
            f"{self._map_select(self.game_class)}"
            f'<input type="checkbox" name="mutgroup7" value="UTGame.UTMutator_BigHead"{checked} />'
            '<input type="checkbox" name="mutgroup8" value="UTGame.UTMutator_LowGrav" />'
            '<input type="hidden" name="mutatorGroupCount" value="10" />'
            f'<input type="hidden" name="urlextra" value="{UT3_URL}{self.layout}" />'
            '<button type="submit" name="action" value="change">Change game</button></form>'
        )

    async def _data(self, request: web.Request) -> web.Response:
        form = await request.post()
        return web.Response(text=self._map_select(str(form.get("gametype", ""))))

    async def _change(self, request: web.Request) -> web.Response:
        form = await request.post()
        url, game_class = str(form.get("map", "")), str(form.get("gametype", ""))
        urlextra, mutator = str(form.get("urlextra", "")), str(form.get("mutgroup7", ""))
        self.changes.append((url, game_class, urlextra, mutator))
        known = [value for value, _ in UT3_MAPS.get(game_class, [])]
        if url in known:
            name, _, layout = url.partition("?")
            self.current_map, self.game_class = name, game_class
            self.layout = f"?{layout}" if layout else ""
        else:  # the real server falls back to some other map
            self.current_map = UT3_MAPS[self.game_class][0][0]
        self.mutator = mutator
        return self._page("Changing the current game with the following url:")
