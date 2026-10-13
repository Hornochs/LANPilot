"""Simulator of a GoldSrc dedicated server (HLDS) for tests.

Speaks the two things LANPilot uses: the status query (A2S_INFO) and RCON. Its answers,
including the way long output is cut into packets, follow what a real Counter-Strike 1.6
server sent when it was measured (docs/games/counter-strike.md).

    async with GoldSrcSimulator(password="secret") as server:
        adapter = CounterStrike16Adapter(
            CounterStrike16Connection(host="127.0.0.1", port=server.port, rcon_password="secret")
        )
"""

from __future__ import annotations

import asyncio
import re
import struct
from types import TracebackType

HEADER = b"\xff\xff\xff\xff"
PRINT_LIMIT = 1395  # the real server flushes its print buffer at about this size
FRAGMENT_SIZE = 1391
RCON_PATTERN = re.compile(rb'rcon (\S+) "([^"]*)" (.*)', re.S)

DEFAULT_MAPS = ("de_dust2", "de_inferno", "de_nuke", "cs_office", "cs_italy", "as_oilrig")


class GoldSrcSimulator(asyncio.DatagramProtocol):
    def __init__(
        self,
        *,
        password: str = "secret",  # noqa: S107 - default of a test double
        maps: tuple[str, ...] = DEFAULT_MAPS,
        hostname: str = "Simulated HLDS",
        players: int = 3,
        max_players: int = 16,
    ) -> None:
        self.password = password
        self.maps = list(maps)
        self.current_map = self.maps[0]
        self.hostname = hostname
        self.players = players
        self.max_players = max_players
        self.challenge = "283211007"
        #: Commands that passed the password check, in order.
        self.commands: list[str] = []
        #: How often a wrong password arrived. A real server bans after a few.
        self.bad_passwords = 0
        #: Send long output as one fragmented packet instead of several print packets.
        self.fragment_long_output = False
        #: Answer nothing at all, like a server that is down.
        self.silent = False
        self.restarts: list[str] = []
        self.port = 0
        self._transport: asyncio.DatagramTransport | None = None

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=("127.0.0.1", 0)
        )
        self._transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def __aenter__(self) -> GoldSrcSimulator:
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
        body = data[4:]
        if body.startswith(b"TSource Engine Query"):
            self._transport.sendto(self._info(), addr)
        elif body.startswith(b"challenge rcon"):
            reply = f"challenge rcon {self.challenge}\n".encode() + b"\x00"
            self._transport.sendto(HEADER + reply, addr)
        elif body.startswith(b"rcon "):
            self._rcon(body, addr)

    def _info(self) -> bytes:
        def text(value: str) -> bytes:
            return value.encode() + b"\x00"

        return (
            HEADER
            + b"I"
            + bytes([48])
            + text(self.hostname)
            + text(self.current_map)
            + text("cstrike")
            + text("Counter-Strike")
            + struct.pack("<H", 10)
            + bytes([self.players, self.max_players, 0])
            + b"dl"
            + bytes([0, 1])
            + text("1.1.2.7/Stdio")
            + bytes([0])
        )

    def _rcon(self, body: bytes, addr: tuple[str, int]) -> None:
        match = RCON_PATTERN.match(body.rstrip(b"\n\x00"))
        if match is None:
            return
        _challenge, password, command = (part.decode(errors="replace") for part in match.groups())
        # Like the measured server, the challenge value itself is not verified.
        if password != self.password:
            self.bad_passwords += 1
            self._print("Bad rcon_password.\n", addr)
            return
        self.commands.append(command)
        self._print(self._run(command), addr)

    def _run(self, command: str) -> str:
        name, _, argument = command.partition(" ")
        argument = argument.strip()
        if name == "status":
            return (
                f"hostname:  {self.hostname}\n"
                "version :  48/1.1.2.7/Stdio 10211 secure  (10)\n"
                "tcp/ip  :  0.0.0.0:27015\n"
                f"map     :  {self.current_map} at: 0 x, 0 y, 0 z\n"
                f"players :  {self.players} active ({self.max_players} max)\n\n"
                "#      name userid uniqueid frag time ping loss adr\n"
                f"{self.players} users\n"
            )
        if name == "stats":
            return (
                "CPU   In    Out   Uptime  Users   FPS    Players\n"
                f" 6.40  0.00  0.00       0     0   99.17       {self.players}\n"
            )
        if name == "maps":
            # The real server lists every map twice, once per search path.
            listing = "".join(f"{item}.bsp\n" for item in self.maps)
            return "-------------\n" + listing + listing
        if name == "changelevel":
            if not argument:
                return "changelevel <levelname> : continue game on a new level\n"
            if argument not in self.maps:
                return f"changelevel failed: '{argument}' not found on server.\n"
            self.current_map = argument
            return self._loading_log()
        if name == "restart":
            self.restarts.append("hard")
            return self._loading_log()
        if name in ("sv_restart", "sv_restartround"):
            self.restarts.append("soft")
            return f'L 10/13/2026 - 22:59:36: Server cvar "{name}" = "{argument}"\n'
        if name == "echo":
            return f"{argument} \n"
        return ""  # unknown commands print nothing

    def _loading_log(self) -> str:
        lines = [f'L 10/13/2026 - 22:59:38: Loading map "{self.current_map}"\n']
        lines += [f'L 10/13/2026 - 22:59:38: Server cvar "cvar_{n}" = "{n}"\n' for n in range(90)]
        return "".join(lines)

    def _print(self, text: str, addr: tuple[str, int]) -> None:
        assert self._transport is not None
        data = text.encode()
        if self.fragment_long_output and len(data) > PRINT_LIMIT:
            packet = HEADER + b"l" + data + b"\x00\x00"
            parts = [packet[i : i + FRAGMENT_SIZE] for i in range(0, len(packet), FRAGMENT_SIZE)]
            for index, part in enumerate(parts):
                numbering = bytes([(index << 4) | len(parts)])
                self._transport.sendto(
                    b"\xfe\xff\xff\xff" + b"\x02\x00\x00\x00" + numbering + part, addr
                )
            return
        chunks = [data[i : i + PRINT_LIMIT] for i in range(0, len(data), PRINT_LIMIT)] or [b""]
        for chunk in chunks:
            self._transport.sendto(HEADER + b"l" + chunk + b"\x00\x00", addr)
