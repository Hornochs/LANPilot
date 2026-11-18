"""The out-of-band UDP protocol of the Quake 3 engine and its descendants (Quake 3 Arena,
Call of Duty 1, 4 and World at War): the status query and RCON.

As measured against Quake 3 1.32c, Call of Duty 1.5, CoD4X 1.8 and World at War 1.7
(docs/games/quake3.md):

* Everything is UDP on the game port. Each datagram starts with ``FF FF FF FF``.
* ``getstatus`` is answered without a password by ``statusResponse\\n``, the server's info
  string (``\\key\\value`` pairs) and one line per player: ``<score> <ping> "<name>"``.
* A command is sent as ``rcon "<password>" <command>``. The server runs exactly one command
  per packet: neither ``;`` nor a line break starts a second one. The password travels in
  clear text with every command; there is no challenge.
* Output comes back as ``print\\n`` packets of up to about 1000 bytes each, with no end
  marker. The answer is complete when nothing more arrives for a moment.
* Quake 3 1.32c, Call of Duty 1 and World at War drop every RCON packet that arrives less
  than 500 ms after the previous one, from anyone; such a command gets no answer at all.
  CoD4X has no such limit. The transport therefore keeps a minimum interval between its commands.
* A wrong password is answered with ``Bad rconpassword.`` (Quake 3, Call of Duty 1),
  ``Bad rcon`` (CoD4X) or ``Invalid password.`` (World at War); a server without a
  password says so (``The server must set 'rcon_password' …`` on World at War).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from lanpilot.core.adapters import (
    AuthenticationError,
    CommandRejectedError,
    ProtocolError,
    ServerUnreachableError,
)
from lanpilot.i18n import M

HEADER = b"\xff\xff\xff\xff"
PRINT = b"print\n"
STATUS_RESPONSE = b"statusResponse\n"
INFO_RESPONSE = b"infoResponse\n"

MAX_DATAGRAMS = 512
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_INFO_KEYS = 256
MAX_STATUS_PLAYERS = 256
MAX_COMMAND_LENGTH = 1000  # the engines read about 1 KB of a packet's command
REFUSED_PREFIXES = (
    "Bad rcon",  # Quake 3, CoD 1: "Bad rconpassword."; CoD4X: "Bad rcon"
    "Invalid password",  # World at War
    "No rconpassword set",
    "No rcon password set",
    "The server must set 'rcon_password'",  # World at War without a password
)
# A player line of getstatus: score, ping, quoted name (which may contain anything but ").
STATUS_PLAYER = re.compile(r'(-?\d{1,9}) (-?\d{1,6}) "([^"]*)"')
COLOR_CODE = re.compile(r"\^[^^]")


def strip_colors(text: str) -> str:
    """Remove the engine's colour codes (``^1Red``) from a name."""
    return COLOR_CODE.sub("", text)


@dataclass(frozen=True)
class StatusPlayer:
    score: int
    ping: int
    name: str  # colour codes removed


@dataclass(frozen=True)
class StatusResponse:
    info: dict[str, str] = field(default_factory=dict)  # keys in lower case
    players: list[StatusPlayer] = field(default_factory=list)


def parse_info_string(text: str) -> dict[str, str]:
    """``\\key\\value\\key\\value`` as a dict with lower-case keys (the engines treat keys
    without regard to case). Bounded; malformed pairs are dropped."""
    parts = text.split("\\")
    if parts and parts[0] == "":
        parts = parts[1:]
    info: dict[str, str] = {}
    for index in range(0, min(len(parts) - 1, 2 * MAX_INFO_KEYS), 2):
        key = parts[index].strip().lower()
        if key and key.isprintable():
            info.setdefault(key, parts[index + 1])
    return info


def parse_status_response(datagram: bytes) -> StatusResponse:
    if not datagram.startswith(HEADER + STATUS_RESPONSE):
        raise ProtocolError(M("The server's status answer could not be read."))
    lines = datagram[len(HEADER + STATUS_RESPONSE) :].decode("utf-8", "replace").split("\n")
    players = []
    for line in lines[1 : MAX_STATUS_PLAYERS + 1]:
        match = STATUS_PLAYER.fullmatch(line.strip())
        if match:
            players.append(StatusPlayer(int(match[1]), int(match[2]), strip_colors(match[3])))
    return StatusResponse(parse_info_string(lines[0]), players)


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.datagrams: asyncio.Queue[bytes] = asyncio.Queue()
        self.error: OSError | None = None

    def datagram_received(self, data: bytes, addr: object) -> None:
        self.datagrams.put_nowait(data)

    def error_received(self, exc: Exception) -> None:
        # E.g. "connection refused" when nothing listens on the port.
        self.error = exc if isinstance(exc, OSError) else OSError(str(exc))
        self.datagrams.put_nowait(b"")


async def exchange(
    host: str,
    port: int,
    payload: bytes,
    *,
    first_answer_seconds: float,
    quiet_seconds: float,
    single: bool = False,
) -> list[bytes]:
    """Send one out-of-band packet and collect the answers: until ``quiet_seconds`` pass
    without another datagram, or after the first one with ``single``. Bounded."""
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _Collector, remote_addr=(host, port)
        )
    except OSError as exc:
        raise ServerUnreachableError(M("The server's address cannot be reached.")) from exc
    try:
        transport.sendto(HEADER + payload)
        datagrams: list[bytes] = []
        size = 0
        wait = first_answer_seconds
        while len(datagrams) < MAX_DATAGRAMS and size < MAX_RESPONSE_BYTES:
            try:
                async with asyncio.timeout(wait):
                    datagram = await protocol.datagrams.get()
            except TimeoutError:
                break
            if protocol.error is not None:
                raise ServerUnreachableError(M("Nothing answers on the server's port."))
            datagrams.append(datagram)
            size += len(datagram)
            if single:
                break
            wait = quiet_seconds
    finally:
        transport.close()
    if not datagrams:
        raise ServerUnreachableError(M("The server did not answer."))
    return datagrams


async def get_status(host: str, port: int, seconds: float = 3.0) -> StatusResponse:
    """The server's ``getstatus`` answer. Needs no password."""
    datagrams = await exchange(
        host, port, b"getstatus\n", first_answer_seconds=seconds, quiet_seconds=0, single=True
    )
    return parse_status_response(datagrams[0])


def printed_text(datagrams: list[bytes]) -> str:
    """The text of all print packets, in the order they arrived."""
    prefix = HEADER + PRINT
    chunks = [data[len(prefix) :] for data in datagrams if data.startswith(prefix)]
    return b"".join(chunks).decode("utf-8", errors="replace")


class Quake3Rcon:
    """Sends console commands to one server. Not safe for concurrent use; LANPilot calls an
    adapter from one task at a time."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        first_answer_seconds: float = 3.0,
        quiet_seconds: float = 0.3,
        min_interval_seconds: float = 0.0,
    ) -> None:
        if not password or any(ch.isspace() or ch in '"\x00' for ch in password):
            # Checked here rather than by the server: the engines take the password as one
            # word and cannot quote a quote.
            raise AuthenticationError(
                M(
                    "The RCON password is empty or contains a space or a quote, "
                    "which the protocol cannot carry."
                )
            )
        self._host = host
        self._port = port
        self._password = password
        self._first_answer_seconds = first_answer_seconds
        self._quiet_seconds = quiet_seconds
        self._min_interval_seconds = min_interval_seconds
        self._last_sent: float | None = None
        self._refused = False

    async def execute(self, command: str, *, first_answer_seconds: float | None = None) -> str:
        """Run a console command and return what the server printed.

        ``first_answer_seconds`` overrides how long to wait for the first packet: a server
        prints the answer to ``map`` only once the level is loaded.

        ``command`` is sent as one line. Callers that build it from parts must validate the
        parts; this method only rejects what would break the line itself.
        """
        if (
            not command.strip()
            or len(command) > MAX_COMMAND_LENGTH
            or any(ch in command for ch in "\r\n\x00")
        ):
            raise CommandRejectedError(
                M("The command is empty, too long or contains a line break.")
            )
        if self._refused:
            # Do not try again: a wrong password should not be repeated with every poll.
            raise AuthenticationError(
                M("The server refused the RCON password. Edit the server to enter another one.")
            )
        line = f'rcon "{self._password}" {command}\n'
        if self._last_sent is not None:
            wait = self._last_sent + self._min_interval_seconds - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_sent = time.monotonic()
        text = printed_text(
            await exchange(
                self._host,
                self._port,
                line.encode(),
                first_answer_seconds=first_answer_seconds or self._first_answer_seconds,
                quiet_seconds=self._quiet_seconds,
            )
        )
        if text.lstrip().startswith(REFUSED_PREFIXES):
            self._refused = True
            raise AuthenticationError(M("The server refused the RCON password."))
        return text
