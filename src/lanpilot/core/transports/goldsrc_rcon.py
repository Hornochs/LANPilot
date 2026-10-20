"""RCON for GoldSrc servers (Half-Life Dedicated Server: Counter-Strike 1.6 and relatives).

The protocol, as measured against HLDS build 10211 (see docs/games/counter-strike.md):

* Everything is UDP on the game port. Each datagram starts with ``FF FF FF FF``.
* ``challenge rcon`` is answered with ``challenge rcon <number>``.
* A command is sent as ``rcon <number> "<password>" <command>``. The password travels in
  clear text with every command.
* Output comes back as print packets: ``FF FF FF FF 6C`` + text + ``00 00``. Long output
  arrives as several print packets. A single print packet above 1400 bytes is cut into
  fragments ``FE FF FF FF`` + id (4 bytes) + one byte holding fragment index and count.
* There is no end marker. The answer is complete when nothing more arrives for a moment.
* A wrong password is answered with the text ``Bad rcon_password.``; repeated failures
  make the server ban the client's address.
"""

from __future__ import annotations

import asyncio
import re

from lanpilot.core.adapters import (
    AuthenticationError,
    CommandRejectedError,
    ProtocolError,
    ServerUnreachableError,
)
from lanpilot.i18n import M

HEADER = b"\xff\xff\xff\xff"
SPLIT_HEADER = b"\xfe\xff\xff\xff"
PRINT = 0x6C  # "l"

MAX_DATAGRAMS = 512
MAX_RESPONSE_BYTES = 1024 * 1024
REFUSED_TEXT = "Bad rcon_password."
BAD_CHALLENGE = "Bad challenge."
CHALLENGE_PATTERN = re.compile(rb"challenge rcon (\d{1,20})")


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


def assemble(datagrams: list[bytes]) -> list[bytes]:
    """Turn received datagrams into whole packets, joining fragments. Order is kept."""
    packets: list[bytes] = []
    fragments: dict[bytes, dict[int, bytes]] = {}
    positions: dict[bytes, int] = {}
    for datagram in datagrams:
        if datagram.startswith(HEADER):
            packets.append(datagram)
        elif datagram.startswith(SPLIT_HEADER) and len(datagram) > 9:
            packet_id, numbering = datagram[4:8], datagram[8]
            index, total = numbering >> 4, numbering & 0x0F
            parts = fragments.setdefault(packet_id, {})
            if not parts:
                positions[packet_id] = len(packets)
                packets.append(b"")  # placeholder, filled when all fragments are there
            parts[index] = datagram[9:]
            if total and len(parts) == total and set(parts) == set(range(total)):
                packets[positions[packet_id]] = b"".join(parts[i] for i in range(total))
    return [packet for packet in packets if packet]


def printed_text(packets: list[bytes]) -> str:
    """The text of all print packets, in order."""
    chunks = [
        packet[5:].rstrip(b"\x00")
        for packet in packets
        if len(packet) >= 5 and packet.startswith(HEADER) and packet[4] == PRINT
    ]
    return b"".join(chunks).decode("utf-8", errors="replace")


class GoldSrcRcon:
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
    ) -> None:
        if not password or any(ch in password for ch in '"\r\n\x00'):
            # Checked here rather than by the server: such a password cannot be quoted.
            raise AuthenticationError(
                M(
                    "The RCON password is empty or contains a quote or line break, "
                    "which the protocol cannot carry."
                )
            )
        self._address = (host, port)
        self._password = password
        self._first_answer_seconds = first_answer_seconds
        self._quiet_seconds = quiet_seconds
        self._challenge: str | None = None
        self._refused = False

    async def _exchange(self, payload: bytes) -> list[bytes]:
        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                _Collector, remote_addr=self._address
            )
        except OSError as exc:
            raise ServerUnreachableError(M("The server's address cannot be reached.")) from exc
        try:
            transport.sendto(HEADER + payload)
            datagrams: list[bytes] = []
            size = 0
            wait = self._first_answer_seconds
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
                wait = self._quiet_seconds
        finally:
            transport.close()
        if not datagrams:
            raise ServerUnreachableError(M("The server did not answer."))
        return assemble(datagrams)

    async def _fetch_challenge(self) -> str:
        packets = await self._exchange(b"challenge rcon\n")
        for packet in packets:
            match = CHALLENGE_PATTERN.search(packet)
            if match:
                return match.group(1).decode()
        raise ProtocolError(M("The server did not send an RCON challenge."))

    async def execute(self, command: str) -> str:
        """Run a console command and return what the server printed.

        ``command`` is sent as one line. Callers that build it from parts must validate the
        parts; this method only rejects what would break the line itself.
        """
        if not command.strip() or any(ch in command for ch in "\r\n\x00"):
            raise CommandRejectedError(M("The command is empty or contains a line break."))
        if self._refused:
            # Do not try again: the server bans addresses that keep sending wrong passwords.
            raise AuthenticationError(
                M("The server refused the RCON password. Edit the server to enter another one.")
            )

        for attempt in range(2):
            if self._challenge is None:
                self._challenge = await self._fetch_challenge()
            line = f'rcon {self._challenge} "{self._password}" {command}\n'
            text = printed_text(await self._exchange(line.encode()))
            if text.startswith(BAD_CHALLENGE) and attempt == 0:
                self._challenge = None  # challenges expire; get a new one and retry once
                continue
            if text.startswith(REFUSED_TEXT):
                self._refused = True
                raise AuthenticationError(M("The server refused the RCON password."))
            return text
        raise ProtocolError(M("The server keeps rejecting the RCON challenge."))
