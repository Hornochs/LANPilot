"""Simulator of Quake 3 engine servers for tests: Quake 3 Arena 1.32c, Call of Duty 1.5,
United Offensive 1.51, Call of Duty 2 1.3, CoD4X 1.8 and Call of Duty: World at War 1.7.

Speaks what LANPilot uses: ``getstatus`` and RCON. Its answers follow what the real servers
sent when they were measured (docs/games/quake3.md), including the differences between
them: the columns of ``status``, the text for a wrong password, ``dir`` (not on CoD4X and WaW),
``fast_restart`` (CoD4X and WaW), the rate limit of Quake 3, Call of Duty 1 and WaW (one
RCON command per 500 ms, the rest dropped without an answer) and how a pending
``g_gametype`` is cleared (not on CoD4X).

    async with Quake3Simulator(QUAKE3, password="secret") as server:
        adapter = Quake3ArenaAdapter(
            Quake3Connection(host="127.0.0.1", port=server.port, rcon_password="secret")
        )
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from types import TracebackType

from lanpilot.core import catalog

HEADER = b"\xff\xff\xff\xff"
PRINT_LIMIT = 1000  # the real servers flush their output buffer at about this size
RCON_PATTERN = re.compile(r'rcon (?:"([^"]*)"|(\S+)) ?(.*)', re.S)


@dataclass(frozen=True)
class Flavour:
    gamename: str
    version: str
    #: Columns of ``status`` around the name, as printed in the header.
    before_name: tuple[str, ...]
    refused: str
    game_types: tuple[str, ...]
    map_folder: str | None  # the folder ``dir <folder> bsp`` lists; None: no ``dir``
    fast_restart: bool
    rate_limited: bool
    clears_pending: bool  # setting the running g_gametype clears a pending one
    maps: tuple[str, ...]
    status_banner: str = ""
    cvar_format: str = '"{name}" is:"{value}^7" default:"{default}^7"\n'
    map_extension: str = "bsp"
    #: ``clientkick <slot> <reason>`` prints the usage and kicks nobody (CoD2, WaW).
    kick_refuses_reason: bool = False
    needpass_key: str | None = "pswrd"  # the getstatus key of the join password flag


QUAKE3 = Flavour(
    gamename="baseq3",
    version="Q3 1.32c linux-i386 May  8 2006",
    before_name=("num", "score", "ping"),
    refused="Bad rconpassword.\n",
    game_types=("0", "1", "3", "4"),
    map_folder="maps",
    fast_restart=False,
    rate_limited=True,
    clears_pending=True,
    maps=("q3dm1", "q3dm17", "q3tourney2", "q3ctf1", "pro-q3dm6"),
    needpass_key="g_needpass",
)
CALL_OF_DUTY = Flavour(
    gamename="Call of Duty",
    version="",
    before_name=("num", "score", "ping", "guid"),
    refused="Bad rconpassword.\n",
    game_types=("dm", "tdm", "sd", "re", "bel", "hq"),
    map_folder="maps/mp",
    fast_restart=False,
    rate_limited=True,
    clears_pending=True,
    maps=("mp_carentan", "mp_harbor", "mp_brecourt", "mp_powcamp"),
)
UNITED_OFFENSIVE = Flavour(
    gamename="CoD:United Offensive",
    version="",
    before_name=("num", "score", "ping", "guid"),
    refused="Bad rconpassword.\n",
    game_types=("dm", "tdm", "sd", "re", "bel", "hq", "bas", "ctf", "dom"),
    map_folder="maps/mp",
    fast_restart=False,
    rate_limited=True,
    clears_pending=True,
    maps=("mp_cassino", "mp_foy", "mp_sicily", "mp_carentan"),
)
CALL_OF_DUTY_2 = Flavour(
    gamename="Call of Duty 2",
    version="",
    before_name=("num", "score", "ping", "guid"),
    refused="Invalid password.\n",
    game_types=("dm", "tdm", "sd", "hq", "ctf"),
    map_folder="maps/mp",
    fast_restart=True,
    rate_limited=True,
    clears_pending=True,
    maps=("mp_leningrad", "mp_toujane", "mp_carentan", "mp_breakout"),
    cvar_format='"{name}" is: "{value}^7" default: "{default}^7"\n  Domain is any text\n',
    map_extension="d3dbsp",
    kick_refuses_reason=True,
)
COD4X = Flavour(
    gamename="Call of Duty 4",
    version="CoD4 X - linux-i386 build 1154 May  1 2022",
    before_name=("num", "score", "ping", "playerid", "steamid"),
    refused="Bad rcon",
    game_types=("dm", "war", "sd", "sab", "dom", "koth"),
    map_folder=None,
    fast_restart=True,
    rate_limited=False,
    clears_pending=False,
    # The stock maps (LANPilot's catalogue lists them, CoD4 cannot) and a custom map.
    maps=(*catalog.load().keys("cod4"), "mp_custom_dust"),
    needpass_key=None,  # CoD4X sends pswrd only when a password is set
    status_banner=(
        "hostname: {hostname}\nversion : CoD4 X - linux-i386 build 1154 May  1 2022\n"
        "udp/ip  : 0.0.0.0:28960\nos      : linux\ntype    : dedicated server\n"
        "map     : {map}\n\n"
    ),
    cvar_format='"{name}" is: "{value}^7" default: "{default}^7" info: "{info}^7"\n',
)

WORLD_AT_WAR = Flavour(
    gamename="Call of Duty: World at War",
    version="",
    before_name=("num", "score", "ping", "guid"),
    refused="Invalid password.\n",
    game_types=("dm", "tdm", "sd", "sab", "dom", "koth", "ctf", "twar"),
    map_folder=None,
    fast_restart=True,
    rate_limited=True,
    clears_pending=True,
    # The stock maps (LANPilot's catalogue lists them, WaW cannot) and a custom map.
    maps=(*catalog.load().keys("codwaw"), "mp_custom_tank"),
    cvar_format='"{name}" is: "{value}^7" default: "{default}^7"\n  Domain is any text\n',
    kick_refuses_reason=True,
)


@dataclass
class SimulatedClient:
    slot: int
    name: str  # may carry colour codes
    score: int = 0
    ping: int = 30
    address: str = "10.0.0.5:27960"

    @property
    def bot(self) -> bool:
        return self.address == "bot"


def default_clients() -> list[SimulatedClient]:
    return [
        SimulatedClient(0, "^1Ada^7", score=12, ping=24, address="10.0.0.5:27960"),
        SimulatedClient(1, "Sarge^7", score=3, ping=0, address="bot"),
        SimulatedClient(2, "Bob the Builder", score=-1, ping=48, address="10.0.0.7:27960"),
    ]


@dataclass
class Quake3Simulator(asyncio.DatagramProtocol):
    flavour: Flavour
    password: str = "secret"  # noqa: S105 - default of a test double
    hostname: str = "^2Simulated ^7Server"
    max_clients: int = 16
    clients: list[SimulatedClient] = field(default_factory=default_clients)
    #: Map rotation (sv_mapRotation); custom maps of CoD4 are named here.
    rotation: str = ""
    port: int = 0
    #: Commands that passed the password check, in order.
    commands: list[str] = field(default_factory=list)
    #: RCON packets that arrived too soon after the previous one and were dropped.
    dropped: int = 0
    bad_passwords: int = 0
    #: Restarts as "soft" (map_restart, fast_restart) or "hard" (map of the current map).
    restarts: list[str] = field(default_factory=list)
    kicks: list[tuple[int, str]] = field(default_factory=list)
    banned: list[str] = field(default_factory=list)
    #: Answer nothing at all, like a server that is down.
    silent: bool = False
    #: The rate limit of rate-limited flavours (measured: 500 ms); tests may shorten it.
    rate_limit_seconds: float = 0.5
    #: How long ``map`` takes before it answers (measured on UO and CoD2: 2-3 s).
    load_seconds: float = 0.0
    #: (map, game type) pairs the map is not made for: the server falls back to the first
    #: game type, as United Offensive did with mp_sicily and Base Assault.
    unsupported: set[tuple[str, str]] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.maps = list(self.flavour.maps)
        self.current_map = self.maps[0]
        self.game_type = self.flavour.game_types[0]
        self.pending_game_type: str | None = None
        self._last_rcon = -1.0
        self._transport: asyncio.DatagramTransport | None = None

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=("127.0.0.1", self.port)
        )
        self._transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def __aenter__(self) -> Quake3Simulator:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    # --- protocol ----------------------------------------------------------------------

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.silent or not data.startswith(HEADER) or self._transport is None:
            return
        body = data[4:].decode("latin-1")
        if body.startswith("getstatus"):
            self._transport.sendto(self._status_response(), addr)
        elif body.startswith("rcon "):
            self._rcon(body.rstrip("\n"), addr)

    def _status_response(self) -> bytes:
        info = {
            "sv_hostname": self.hostname,
            "sv_maxclients": str(self.max_clients),
            "mapname": self.current_map,
            "g_gametype": self.game_type,
            "gamename": self.flavour.gamename,
            "protocol": "68",
        }
        if self.flavour.version:
            info["version"] = self.flavour.version
        if self.flavour.needpass_key:
            info[self.flavour.needpass_key] = "0"
        text = "".join(f"\\{key}\\{value}" for key, value in info.items()) + "\n"
        text += "".join(f'{c.score} {c.ping} "{c.name}"\n' for c in self.clients)
        return HEADER + b"statusResponse\n" + text.encode()

    def _rcon(self, body: str, addr: tuple[str, int]) -> None:
        now = time.monotonic()
        if self.flavour.rate_limited and now - self._last_rcon < self.rate_limit_seconds:
            self.dropped += 1
            return  # dropped without an answer, also with a wrong password
        self._last_rcon = now
        match = RCON_PATTERN.fullmatch(body)
        password = ""
        if match is not None:
            password = match.group(1) if match.group(1) is not None else match.group(2)
        if match is None or password != self.password:
            self.bad_passwords += 1
            self._print(self.flavour.refused, addr)
            return
        command = match.group(3).strip()
        self.commands.append(command)
        answer = self._run(command)
        if self.load_seconds and command.lower().startswith("map "):
            # The level is loaded before the answer; the state already changed above.
            asyncio.get_running_loop().call_later(self.load_seconds, self._print, answer, addr)
        else:
            self._print(answer, addr)

    def _print(self, text: str, addr: tuple[str, int]) -> None:
        if self._transport is None:
            return
        data = text.encode()
        chunks = [data[i : i + PRINT_LIMIT] for i in range(0, len(data), PRINT_LIMIT)] or [b""]
        for chunk in chunks:
            self._transport.sendto(HEADER + b"print\n" + chunk, addr)

    # --- console -----------------------------------------------------------------------

    def _run(self, command: str) -> str:
        name, _, argument = command.partition(" ")
        argument = argument.strip()
        name = name.lower()
        if name == "status":
            return self._status()
        if name == "dir" and self.flavour.map_folder is not None:
            folder, _, extension = argument.partition(" ")
            ext = self.flavour.map_extension
            mine = folder == self.flavour.map_folder and extension.strip(".") == ext
            listing = "".join(f"{m}.{ext}\n" for m in self.maps) if mine else ""
            return f"Directory of {argument}\n---------------\n{listing}\n"
        if name == "g_gametype":
            return self._set_game_type(argument)
        if name == "sv_maprotation" and not argument:
            return self.flavour.cvar_format.format(
                name="sv_mapRotation", value=self.rotation, default="", info="Map rotation"
            )
        if name == "map":
            return self._map(argument)
        if name == "map_restart":
            self._apply_pending()
            self.restarts.append("hard" if self.flavour.fast_restart else "soft")
            return "==== ShutdownGame ====\n------- Game Initialization -------\n"
        if name == "fast_restart" and self.flavour.fast_restart:
            self.restarts.append("soft")
            return "==== ShutdownGame (0) ====\n"
        if name == "clientkick":
            return self._kick(argument)
        if name == "addip" and self.flavour is QUAKE3:
            self.banned.append(argument)
            return ""
        if name == "echo":
            return f"{argument} \n"
        return ""  # unknown commands print nothing

    def _status(self) -> str:
        columns = (*self.flavour.before_name, "name", "lastmsg", "address", "qport", "rate")
        lines = [" ".join(columns), " ".join("-" * len(column) for column in columns)]
        for c in self.clients:
            extra = {"guid": "0", "playerid": "0", "steamid": "0"}
            head = [f"{c.slot:3d}", f"{c.score:5d}", f"{c.ping:4d}"]
            head += [extra[column] for column in self.flavour.before_name[3:]]
            lines.append(
                " ".join(head) + f" {c.name:<15} {50:7d} {c.address:<21} {1234:5d} {25000:5d}"
            )
        banner = self.flavour.status_banner.format(hostname=self.hostname, map=self.current_map)
        if not banner:
            banner = f"map: {self.current_map}\n"
        return banner + "\n".join(lines) + "\n\n"

    def _set_game_type(self, value: str) -> str:
        if not value:
            pending = f'latched: "{self.pending_game_type}"\n' if self.pending_game_type else ""
            return (
                self.flavour.cvar_format.format(
                    name="g_gametype",
                    value=self.game_type,
                    default=self.flavour.game_types[0],
                    info="Current game type",
                )
                + pending
            )
        if value == self.game_type:
            if self.flavour.clears_pending:
                self.pending_game_type = None
            return ""
        self.pending_game_type = value
        return "g_gametype will be changed upon restarting.\n"

    def _apply_pending(self) -> None:
        if self.pending_game_type is not None:
            self.game_type = self.pending_game_type
            self.pending_game_type = None

    def _map(self, argument: str) -> str:
        if argument not in self.maps:
            folder = self.flavour.map_folder or "maps/mp"
            if self.flavour is WORLD_AT_WAR:
                return (
                    f'^1Error: Can\'t find map "{argument}".\nA mod is required for custom maps\n'
                )
            if self.flavour is COD4X:
                return (
                    f"^1Error: Can't find map {argument}\n"
                    "^1Error: A mod is required to run custom maps\n"
                )
            return f"Can't find map {folder}/{argument}.{self.flavour.map_extension}\n"
        if argument == self.current_map:
            self.restarts.append("hard")
        self.current_map = argument
        self._apply_pending()
        if (argument, self.game_type) in self.unsupported:
            self.game_type = self.flavour.game_types[0]
        return "==== ShutdownGame ====\n------ Server Initialization ------\n" + "x" * 2500 + "\n"

    def _kick(self, argument: str) -> str:
        slot, _, reason = argument.partition(" ")
        if reason and self.flavour.kick_refuses_reason:
            return "Usage: clientkick <client number>\n"  # and nobody is kicked
        client = next((c for c in self.clients if str(c.slot) == slot), None)
        if client is None:
            return f"Client {slot} is not active\n"
        self.clients.remove(client)
        self.kicks.append((client.slot, reason.strip()))
        return f'broadcast: print "{client.name} was kicked\\n"\nClientDisconnect: {client.slot}\n'
