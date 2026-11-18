"""Simulator of a Factorio headless server's RCON for tests.

Behaves like the measured Factorio 2.1.24 (docs/games/factorio.md): Source RCON framing and
login, **exactly one** RESPONSE_VALUE packet per command (empty body if there is no
output), and **no answer at all to an empty command**, so the end-marker trick used for
Counter-Strike does not work.

    async with FactorioRconSimulator(password="secret") as server:
        adapter = FactorioAdapter(
            FactorioConnection(host="127.0.0.1", port=server.port, password=SecretStr("secret"))
        )
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from types import TracebackType

AUTH = 3
AUTH_RESPONSE = 2
EXECCOMMAND = 2
RESPONSE_VALUE = 0

SPACE_AGE_PLANETS = ("Vulcanus", "Fulgora", "Gleba", "Aquilo")


def packet(packet_id: int, packet_type: int, body: bytes) -> bytes:
    data = struct.pack("<ii", packet_id, packet_type) + body + b"\x00\x00"
    return struct.pack("<i", len(data)) + data


class FactorioRconSimulator:
    def __init__(
        self,
        *,
        password: str = "secret",  # noqa: S107 - default of a test double
        server_name: str = "Simulated Factorio",
        max_players: int = 0,
        version: str = "2.1.24",
        space_age: bool = True,
        game_minutes: int = 144833,
        online: tuple[str, ...] = ("Engineer",),
        seed: str = "3490025918",
        offline: tuple[str, ...] = ("Alice", "Bob"),
        extra_surfaces: tuple[str, ...] = (),
        autosave_minutes: int = 10,
        quit_closes: bool = False,
        save_answer: str = "",
        drops: dict[str, int] | None = None,
        save_delay: float = 0.0,
    ) -> None:
        self.password = password
        self.server_name = server_name
        self.max_players = max_players
        self.version = version
        self.space_age = space_age
        self.game_minutes = game_minutes
        self.online = list(online)
        self.seed = seed
        #: Players who joined before but are not online.
        self.offline = list(offline)
        #: Surfaces mods create, listed by /evolution without a [planet=…] tag.
        self.extra_surfaces = extra_surfaces
        self.autosave_minutes = autosave_minutes
        self.banned: list[str] = []
        #: Non-empty commands that passed the password check, in order.
        self.commands: list[str] = []
        #: How many empty commands arrived (Factorio does not answer them).
        self.empty_commands = 0
        self.connections = 0
        self.quit_received = False
        #: Close the connection on /quit without answering (not yet measured on a real server).
        self.quit_closes = quit_closes
        #: Seconds /server-save takes before it answers.
        self.save_delay = save_delay
        #: What /server-save answers (not yet measured on a real server).
        self.save_answer = save_answer
        #: Commands that close the connection without an answer, and how many times.
        self.drops = dict(drops or {})
        self.port = 0
        self._tcp: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        self._tcp = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._tcp.sockets[0].getsockname()[1]

    def stop(self) -> None:
        if self._tcp is not None:
            self._tcp.close()
            self._tcp = None
        for writer in list(self._writers):
            writer.close()

    async def __aenter__(self) -> FactorioRconSimulator:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    # --- RCON --------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.add(writer)
        authenticated = False
        try:
            while True:
                (size,) = struct.unpack("<i", await reader.readexactly(4))
                data = await reader.readexactly(size)
                packet_id, packet_type = struct.unpack_from("<ii", data)
                body = data[8:].rstrip(b"\x00").decode(errors="replace")
                if packet_type == AUTH:
                    authenticated = body == self.password
                    writer.write(packet(packet_id if authenticated else -1, AUTH_RESPONSE, b""))
                    await writer.drain()
                    if not authenticated:
                        return
                    continue
                if not authenticated or packet_type != EXECCOMMAND:
                    continue
                if not body:
                    self.empty_commands += 1
                    continue
                self.commands.append(body)
                if self.drops.get(body, 0) > 0:
                    self.drops[body] -= 1
                    return
                if body == "/quit" and self.quit_closes:
                    self.quit_received = True
                    return
                if body == "/server-save" and self.save_delay:
                    await asyncio.sleep(self.save_delay)
                writer.write(packet(packet_id, RESPONSE_VALUE, self._run(body).encode()))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def _run(self, command: str) -> str:
        words = command.split(" ", 2)
        name = words[1] if len(words) > 1 else ""
        match words[0]:
            case "/config" if command == "/config get name":
                return f"Server name: {self.server_name}\n"
            case "/config" if command == "/config get max-players":
                return f"{self.max_players}\n"
            case "/seed":
                return f"{self.seed}\n"
            case "/config" if command == "/config get autosave-interval":
                if not self.autosave_minutes:
                    return "Autosave is disabled.\n"
                return f"Autosave every {self.autosave_minutes} minutes.\n"
            case "/version":
                return f"{self.version}\n"
            case "/players" if command == "/players online":
                lines = [f"Online players ({len(self.online)}):"]
                lines += [f"  {player} (online)" for player in self.online]
                return "\n".join(lines) + "\n"
            case "/players" if command == "/players count":
                return f"Players ({len(self.online) + len(self.offline)}):\n"
            case "/players":
                lines = [f"Players ({len(self.online)}):"]
                lines += [f"  {player} (online)" for player in self.online]
                return "\n".join(lines) + "\n"
            case "/time":
                hours, minutes = divmod(self.game_minutes, 60)
                return f"{hours} hours and {minutes} minutes\n"
            case "/evolution":
                planets = ("Nauvis", *SPACE_AGE_PLANETS) if self.space_age else ("Nauvis",)
                factor = " - Evolution factor: 0.0000. (Time 0%) (Pollution 0%) (Spawner kills 0%)"
                lines = [f"[planet={planet.lower()}] {planet}{factor}" for planet in planets]
                lines += [f"{surface}{factor}" for surface in self.extra_surfaces]
                return "\n".join(lines) + "\n"
            case "/kick" | "/ban":
                if name not in self.online:
                    return f"Player {name} doesn't exist.\n"
                self.online.remove(name)
                if words[0] == "/ban":
                    self.banned.append(name)
                return ""
            case "/server-save":
                return self.save_answer
            case "/quit":
                self.quit_received = True
                return ""
        return f'Unknown command "{command[1:]}". Type /h or /help for more help about commands.\n'
