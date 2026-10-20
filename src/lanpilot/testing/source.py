"""Simulator of a Source or Source 2 dedicated server (srcds, CS2) for tests.

Speaks the status query (A2S_INFO, UDP) and Source RCON (TCP) on the same port number and
behaves like the measured servers (docs/games/counter-strike.md), selected with ``game``:

* ``"cs2"``: all packets that arrive together are executed together and every answer
  carries the id of the last of them; ``maps *`` lists bare resource names, ``stats``
  prints nothing, ``restart`` is refused as a cheat.
* ``"css"``: correct ids, an empty packet before the login answer, ``maps *`` lines like
  ``PENDING:   (fs) de_dust2``, long output split into packets of about 4 KB.
* ``"csgo"``: like ``"css"``, but ``maps *`` names end in ``.bsp``, every answer (the one
  to the end marker included) carries a line of the server log, and a map change closes
  the RCON connection.

    async with SourceSimulator(password="secret", game="cs2") as server:
        adapter = CounterStrike2Adapter(
            SourceConnection(host="127.0.0.1", port=server.port, rcon_password="secret")
        )
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
from types import TracebackType
from typing import Literal, cast

from lanpilot.testing.rcon_players import PlayerCommands, default_roster

HEADER = b"\xff\xff\xff\xff"
AUTH = 3
AUTH_RESPONSE = 2
EXECCOMMAND = 2
RESPONSE_VALUE = 0
SPLIT_SIZE = 4096

DEFAULT_MAPS = ("de_dust2", "de_inferno", "de_nuke", "cs_office", "cs_italy", "ar_baggage")
# What CS2 lists under "maps *" besides the playable maps (a sample of the real listing).
CS2_RESOURCES = (
    "de_dust2_vanity",
    "error",
    "graphics_settings",
    "lobby_mapveto",
    "prefabs/misc/team_select",
    "ui/buy_menu",
    "workshop_preview_dust2",
)


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self, server: SourceSimulator) -> None:
        self.server = server
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        # Python 3.11's selector transport does not derive from DatagramTransport.
        self.transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.server.silent or self.transport is None:
            return
        if data.startswith(HEADER + b"TSource Engine Query"):
            self.transport.sendto(self.server.info(), addr)


def packet(packet_id: int, packet_type: int, body: bytes) -> bytes:
    payload = struct.pack("<ii", packet_id, packet_type) + body + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


class SourceSimulator:
    def __init__(
        self,
        *,
        password: str = "secret",  # noqa: S107 - default of a test double
        game: Literal["cs2", "csgo", "css"] = "cs2",
        maps: tuple[str, ...] = DEFAULT_MAPS,
        workshop_maps: tuple[str, ...] = (),
        hostname: str = "Simulated srcds",
        players: int = 2,
        max_players: int = 16,
    ) -> None:
        self.password = password
        self.game = game
        self.cs2 = game == "cs2"
        self.maps = list(maps)
        self.workshop_maps = list(workshop_maps)
        self.current_map = self.maps[0]
        self.hostname = hostname
        #: Players, kicks and bans (``status``, ``kickid``, ``addip``, ``writeip``).
        self.player_commands = PlayerCommands(default_roster(players))
        self.max_players = max_players
        #: Commands that passed the password check, in order (end markers excluded).
        self.commands: list[str] = []
        #: How often a wrong password arrived.
        self.bad_passwords = 0
        #: How many RCON connections were opened.
        self.connections = 0
        #: Answer nothing at all, like a server that hangs.
        self.silent = False
        #: Close every RCON connection right after the login, like a server that drops idle ones.
        self.drop_after_login = False
        self.restarts: list[str] = []
        #: Every map that was loaded, in order (reloads of the current map included).
        self.map_changes: list[str] = []
        #: Refuse map changes like CS2 does while it is still loading a map.
        self.loading = False
        self.port = 0
        self._udp: asyncio.DatagramTransport | None = None
        self._tcp: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    @property
    def players(self) -> int:
        return len(self.player_commands.roster)

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        # TCP and UDP share the port number, as on a real server.
        for _ in range(20):
            tcp = await asyncio.start_server(self._serve, "127.0.0.1", 0)
            port = tcp.sockets[0].getsockname()[1]
            try:
                udp, _ = await loop.create_datagram_endpoint(
                    lambda: _Datagrams(self), local_addr=("127.0.0.1", port), family=socket.AF_INET
                )
            except OSError:
                tcp.close()
                continue
            self._tcp, self._udp, self.port = tcp, udp, port
            return
        raise RuntimeError("no free port pair for the simulator")

    def stop(self) -> None:
        if self._udp is not None:
            self._udp.close()
            self._udp = None
        if self._tcp is not None:
            self._tcp.close()
            self._tcp = None
        for writer in list(self._writers):
            writer.close()

    async def __aenter__(self) -> SourceSimulator:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    # --- status query ------------------------------------------------------------------

    def info(self) -> bytes:
        def text(value: str) -> bytes:
            return value.encode() + b"\x00"

        game = {
            "cs2": "Counter-Strike 2",
            "csgo": "Counter-Strike: Global Offensive",
            "css": "Counter-Strike: Source",
        }[self.game]
        return (
            HEADER
            + b"I"
            + bytes([17])
            + text(self.hostname)
            + text(self.current_map)
            + text("cstrike" if self.game == "css" else "csgo")
            + text(game)
            # CS2 kept CS:GO's app id; the version tells them apart.
            + struct.pack("<H", 240 if self.game == "css" else 730)
            + bytes([self.players, self.max_players, 0])
            + b"dl"
            + bytes([0, 0])
            + text({"cs2": "1.41.9.0", "csgo": "1.38.8.1", "css": "11003710"}[self.game])
            + bytes([0])
        )

    # --- RCON --------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.add(writer)
        buffer = b""
        authenticated = False
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                if self.silent:
                    continue
                buffer += data
                batch: list[tuple[int, int, bytes]] = []
                while len(buffer) >= 4:
                    (size,) = struct.unpack_from("<i", buffer)
                    if len(buffer) < 4 + size:
                        break
                    packet_id, packet_type = struct.unpack_from("<ii", buffer, 4)
                    batch.append((packet_id, packet_type, buffer[12 : 4 + size].rstrip(b"\x00")))
                    buffer = buffer[4 + size :]
                for packet_id, packet_type, body in batch:
                    if packet_type == AUTH:
                        authenticated = self._login(writer, packet_id, body)
                        if not authenticated or self.drop_after_login:
                            await writer.drain()
                            return
                if not authenticated:
                    continue
                commands = [(i, b) for i, t, b in batch if t == EXECCOMMAND]
                last_id = batch[-1][0]
                changes = len(self.map_changes)
                for packet_id, body in commands:
                    command = body.decode(errors="replace")
                    answer = self._run(command)
                    if self.game == "csgo":
                        answer += (
                            "L 10/14/2026 - 14:18:30: rcon from "
                            f'"127.0.0.1:50000": command "{command}"\n'
                        )
                    # CS2 labels every answer with the id of the last packet it received.
                    self._answer(writer, last_id if self.cs2 else packet_id, answer.encode())
                await writer.drain()
                if self.game == "csgo" and len(self.map_changes) > changes:
                    return  # CS:GO closes RCON connections when it loads a map
        except ConnectionError:
            return
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def _login(self, writer: asyncio.StreamWriter, packet_id: int, body: bytes) -> bool:
        if not self.cs2:
            writer.write(packet(packet_id, RESPONSE_VALUE, b""))
        if body.decode(errors="replace") != self.password:
            self.bad_passwords += 1
            writer.write(packet(-1, AUTH_RESPONSE, b""))
            return False
        writer.write(packet(packet_id, AUTH_RESPONSE, b""))
        return True

    def _answer(self, writer: asyncio.StreamWriter, packet_id: int, answer: bytes) -> None:
        if self.cs2 or len(answer) <= SPLIT_SIZE:
            writer.write(packet(packet_id, RESPONSE_VALUE, answer))
            return
        for start in range(0, len(answer), SPLIT_SIZE):
            writer.write(packet(packet_id, RESPONSE_VALUE, answer[start : start + SPLIT_SIZE]))

    def _run(self, command: str) -> str:
        if not command:
            return ""  # the end marker
        self.commands.append(command)
        name, _, argument = command.partition(" ")
        argument = argument.strip()
        if name == "maps":
            if self.cs2:
                # Downloaded workshop maps are listed here as well.
                listing = sorted([*self.maps, *CS2_RESOURCES, *self.workshop_maps])
                return "".join(f"\t{item}\n" for item in listing)
            if self.game == "csgo":
                listing = [*self.maps, "lobby_mapveto"]
                return "-------------\n" + "".join(f"PENDING:   (fs) {i}.bsp\n" for i in listing)
            return "-------------\n" + "".join(f"PENDING:   (fs) {item}\n" for item in self.maps)
        if name == "ds_workshop_listmaps" and self.cs2:
            return "".join(f"{item}\n" for item in self.workshop_maps)
        if name == "stats":
            if self.cs2:
                return ""
            if self.game == "csgo":
                return (
                    "  CPU   NetIn   NetOut    Uptime  Maps   FPS   Players  Svms    +-ms   ~tick\n"
                    f"  10.0      0.0      0.0       1     0   25.17       {self.players}"
                    "   39.01    6.01    3.28\n"
                )
            return (
                "CPU    In_(KB/s)  Out_(KB/s)  Uptime  Map_changes  FPS      Players  Connects\n"
                f"2.50   0.00       0.00        12      1            66.67    {self.players}  4\n"
            )
        if name in ("changelevel", "ds_workshop_changelevel") and self.loading:
            return "Can't changelevel, server not in a map\n"
        if name == "changelevel":
            if argument not in self.maps:
                if self.cs2:
                    return f"changelevel {argument}:  invalid map name\n"
                if self.game == "csgo":
                    return (
                        f"CModelLoader::Map_IsValid:  No such map 'maps/{argument}.bsp'\n"
                        f"changelevel failed: {argument} not found\n"
                    )
                return f"Failed to find map {argument}\n"
            return self._change(argument)
        if name == "ds_workshop_changelevel" and self.cs2:
            if argument not in self.workshop_maps:
                return ""  # CS2 prints nothing for an unknown workshop map
            return self._change(argument)
        if name == "mp_restartgame":
            self.restarts.append("soft")
            return ""
        if name == "restart":
            if self.cs2:
                return "SV: Cheat command 'restart' ignored.\n"
            return self._change(self.current_map)
        if name == "echo":
            return f"{argument}\n" if self.cs2 else f"{argument} \n"
        if name == "status" and not self.cs2:
            return self._status()
        if name == "kickid" and not self.cs2:
            if self.player_commands.kick(argument):
                return ""
            return f'userid "{argument.split()[0] if argument else ""}" not found\n'
        if name == "addip" and not self.cs2:
            self.player_commands.add_ip(argument)
            return ""
        if name == "writeip" and not self.cs2:
            self.player_commands.saved += 1
            return ""
        if self.cs2:
            return f"Unknown command '{name}'!\n"
        return f'Unknown command "{name}"\n'

    def _status(self) -> str:
        """The Source 1 answer to ``status``; CS:GO adds the slot and the rate."""
        csgo = self.game == "csgo"
        lines = [
            f"hostname: {self.hostname}",
            f"map     : {self.current_map} at: 0 x, 0 y, 0 z",
            f"players : {self.players} humans, 0 bots ({self.max_players}/0 max)",
            "",
            "# userid name uniqueid connected ping loss state rate adr"
            if csgo
            else "# userid name                uniqueid            connected ping loss state  adr",
        ]
        for slot, player in enumerate(self.player_commands.roster, 1):
            head = f"#{player.userid:3d} {slot}" if csgo else f"#{player.userid:7d}"
            if player.bot:
                lines.append(f'{head} "{player.name}" BOT active' + (" 64" if csgo else ""))
                continue
            rate = " 196608" if csgo else ""
            lines.append(
                f'{head} "{player.name}" {player.uniqueid} 01:23 {player.ping} 0 active'
                f"{rate} {player.address}"
            )
        if csgo:
            lines.append("#end")
        return "\n".join(lines) + "\n"

    def _change(self, name: str) -> str:
        if name == self.current_map:
            self.restarts.append("hard")
        self.current_map = name
        self.map_changes.append(name)
        if not self.cs2:
            return ""  # CS:Source prints nothing for a successful change
        return (
            f"Changelevel to {name}\n"
            f'changelevel "{name}"\n'
            f"CHostStateMgr::QueueNewRequest( Changelevel ({name}), 3 )\n"
        )
