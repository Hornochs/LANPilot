"""Simulator of an ElDewrito 0.7.1 dedicated server for tests: the HTTP status server and
RCON over a WebSocket with the ``dew-rcon`` subprotocol.

Its answers follow what the real server sent when it was measured (docs/games/eldewrito.md):
``accept``/``deny``, one answer frame per command, case-sensitive game variants, quotes that
group arguments, the join password hiding the players from the status server, and a lobby
that falls back to the default map when ``Game.Start`` finds no players. The player list and
kick answers follow ElDewrito's source, as no players were available to measure them.

    async with ElDewritoSimulator(password="secret") as server:
        adapter = ElDewritoAdapter(
            ElDewritoConnection(
                host="127.0.0.1",
                port=server.rcon_port,
                status_port=server.status_port,
                rcon_password="secret",
            )
        )
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from types import TracebackType

from aiohttp import WSMsgType, web

from lanpilot.core import catalog
from lanpilot.games.eldewrito import GAME_TYPES, SCAN_QUERY

DEFAULT_MAP = "guardian"
VERSION = "0.7.1"
# The start of a real answer to the server browser's query (160 bytes); the rest is padding.
BEACON = bytes.fromhex(
    "01626c616d0000000981ac9a00012dc30493dc05d995412d0001a0d50001a0d5540009afcc73aafd9e52"
).ljust(160, b"\x00")


class _Beacon(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if data == SCAN_QUERY and self.transport is not None:
            self.transport.sendto(BEACON, addr)


@dataclass
class SimulatedPlayer:
    name: str
    uid: str
    ip: str
    score: int = 0
    team: int = 0


@dataclass
class ElDewritoSimulator:
    password: str
    join_password: str = ""
    name: str = "HaloOnline Server"
    maps: tuple[str, ...] = field(default_factory=lambda: tuple(catalog.load().keys("eldewrito")))
    max_players: int = 16
    #: Game.Info calls after Game.Stop that still report the running map.
    lobby_delay: int = 1
    #: Slot -> player.
    players: dict[int, SimulatedPlayer] = field(default_factory=dict)
    map: str = DEFAULT_MAP
    variant: str | None = None  # loaded with Game.GameType
    running: str | None = None  # the variant of the game that runs, None in the lobby
    teams: bool = False
    banned: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    logins: int = 0
    _stopping: int = 0
    _runners: list[web.AppRunner] = field(default_factory=list)
    status_port: int = 0
    rcon_port: int = 0
    #: UDP port answering the LAN scan's query (the game port of a real server).
    beacon_port: int = 0
    _beacon: asyncio.DatagramTransport | None = None

    async def __aenter__(self) -> ElDewritoSimulator:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        status = web.Application()
        status.router.add_get("/", self._status)
        rcon = web.Application()
        rcon.router.add_get("/", self._rcon)
        self.status_port = await self._serve(status)
        self.rcon_port = await self._serve(rcon)
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            _Beacon, local_addr=("127.0.0.1", 0)
        )
        self._beacon = transport
        self.beacon_port = int(transport.get_extra_info("sockname")[1])

    async def _serve(self, app: web.Application) -> int:
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self._runners.append(runner)
        server = site._server
        assert server is not None
        return int(server.sockets[0].getsockname()[1])  # type: ignore[attr-defined]

    async def stop(self) -> None:
        if self._beacon is not None:
            self._beacon.close()
            self._beacon = None
        for runner in self._runners:
            await runner.cleanup()
        self._runners.clear()

    def add_player(
        self, name: str, uid: str, ip: str = "10.0.0.5", score: int = 0, team: int = 0
    ) -> int:
        slot = next(index for index in range(16) if index not in self.players)
        self.players[slot] = SimulatedPlayer(name, uid, ip, score, team)
        return slot

    # --- status server -----------------------------------------------------------------

    def _authorised(self, request: web.Request) -> bool:
        if not self.join_password:
            return True
        expected = base64.b64encode(f"dorito:{self.join_password}".encode()).decode()
        return request.headers.get("Authorization") == f"Basic {expected}"

    async def _status(self, request: web.Request) -> web.Response:
        names = catalog.load()
        data: dict[str, object] = {
            "name": self.name,
            "port": 11774,
            "hostPlayer": "",
            "teams": self.teams,
            "map": names.name("eldewrito", self.map) or self.map,
            "mapFile": self.map,
            "variant": self.running or "none",
            "variantType": "slayer" if self.running else "none",
            "status": "InGame" if self.running else "InLobby",
            "numPlayers": len(self.players),
            "maxPlayers": self.max_players,
        }
        if self._authorised(request):
            data["xnkid"] = "0" * 32
            data["players"] = [
                {
                    "name": player.name,
                    "serviceTag": "",
                    "team": player.team,
                    "uid": player.uid,
                    "primaryColor": "#000000",
                    "isAlive": True,
                    "score": player.score,
                    "kills": 0,
                    "assists": 0,
                    "deaths": 0,
                    "betrayals": 0,
                    "timeSpentAlive": 0,
                    "suicides": 0,
                    "bestStreak": 0,
                }
                for player in self.players.values()
            ]
        else:
            data["passworded"] = True
        data.update(isDedicated=True, gameVersion="1.106708_cert_ms23___release")
        data["eldewritoVersion"] = VERSION
        return web.json_response(data, headers={"Server": f"ElDewrito/{VERSION}"})

    # --- RCON --------------------------------------------------------------------------

    async def _rcon(self, request: web.Request) -> web.StreamResponse:
        offered = [
            item.strip() for item in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        ]
        if "dew-rcon" not in offered:
            return web.Response(status=400)
        socket = web.WebSocketResponse(protocols=("dew-rcon",))
        await socket.prepare(request)
        authenticated = False
        async for message in socket:
            if message.type is not WSMsgType.TEXT:
                continue
            if not authenticated:
                if message.data != self.password:
                    await socket.send_str("deny")
                    await socket.close(code=1008, message=b"deny")
                    break
                authenticated = True
                self.logins += 1
                await socket.send_str("accept")
                continue
            self.commands.append(message.data)
            await socket.send_str(self.run(message.data))
        return socket

    def run(self, line: str) -> str:
        words = split_arguments(line)
        if not words:
            return "Command/Variable not found"
        command, arguments = words[0].lower(), words[1:]
        handler = getattr(self, "_" + command.replace(".", "_"), None)
        if handler is None:
            return "Command/Variable not found"
        return str(handler(arguments))

    def _help(self, arguments: list[str]) -> str:
        return (
            "Game.Map - Loads a map or map variant\n"
            f"Server.Name {self.name} - The name of the server\n"
            f"Server.RconPassword {self.password} - Password for the remote console\n"
        )

    def _game_listmaps(self, arguments: list[str]) -> str:
        return ",".join(self.maps)

    def _game_map(self, arguments: list[str]) -> str:
        if not arguments:
            return "You must specify an internal map or Forge map name!"
        name = arguments[0]
        if name.lower() not in self.maps:
            return "Invalid map!"
        self.map = name.lower()
        return f"Loading built-in map maps\\{self.map}.map...\nMap variant loaded successfully!"

    def _game_gametype(self, arguments: list[str]) -> str:
        if not arguments:
            return "You must specify a built-in gametype or custom gametype name!"
        name = arguments[0]
        answer = f"Loading built-in game variant {name}...\n"
        if name not in GAME_TYPES:
            return answer + "Invalid game variant name!"
        self.variant = name
        return answer + "Game variant loaded successfully!"

    def _game_start(self, arguments: list[str]) -> str:
        if self.players:
            self.running = self.variant or "Slayer"
            self.teams = self.running.startswith("Team")
        else:
            self.map = DEFAULT_MAP  # measured: the failed start resets the lobby
        return "Starting game..."

    def _game_stop(self, arguments: list[str]) -> str:
        if self.running:
            self._stopping = self.lobby_delay
        return "Stopping game..."

    def _game_info(self, arguments: list[str]) -> str:
        if self.running and self._stopping:
            self._stopping -= 1
            if not self._stopping:
                self.running = None
                self.teams = False
        current = self.map if self.running else "mainmenu"
        return f"Command line args: -dedicated\nCurrent Map: {current}\nLoaded Game Type: 0x0\n"

    def _game_version(self, arguments: list[str]) -> str:
        return VERSION

    def _server_name(self, arguments: list[str]) -> str:
        if arguments:
            old, self.name = self.name, arguments[0]
            return f"{old} -> {self.name}"
        return self.name

    def _server_password(self, arguments: list[str]) -> str:
        if arguments:
            old, self.join_password = self.join_password, arguments[0]
            return f"{old} -> {self.join_password}"
        return self.join_password

    def _server_say(self, arguments: list[str]) -> str:
        return ""

    def _server_listplayers(self, arguments: list[str]) -> str:
        return "".join(
            f'[{slot}] "{player.name}" (uid: {player.uid}, ip: {player.ip}:11774)\n'
            for slot, player in sorted(self.players.items())
        )

    def _kick_index(self, arguments: list[str], *, ban: bool) -> str:
        if len(arguments) != 1:
            return "Invalid arguments"
        if not arguments[0].isdigit() or not 0 <= int(arguments[0]) <= 15:
            return "Invalid player index"
        slot = int(arguments[0])
        player = self.players.pop(slot, None)
        if player is None:
            return "Failed to kick player "
        prefix = ""
        if ban:
            self.banned.append(player.ip)
            prefix = f"Added IP {player.ip} to the ban list\n"
        return prefix + f"Issued kick request for player {player.name} ({slot})"

    def _server_kickindex(self, arguments: list[str]) -> str:
        return self._kick_index(arguments, ban=False)

    def _server_kickbanindex(self, arguments: list[str]) -> str:
        return self._kick_index(arguments, ban=True)


def split_arguments(line: str) -> list[str]:
    """Split a command as ElDewrito does: spaces separate, double quotes group, and text
    right after a closing quote belongs to the same argument."""
    words: list[str] = []
    current: list[str] = []
    quoted = started = False
    for char in line:
        if char == '"':
            quoted = not quoted
            started = True
        elif char == " " and not quoted:
            if started:
                words.append("".join(current))
            current, started = [], False
        else:
            current.append(char)
            started = True
    if started:
        words.append("".join(current))
    return words
