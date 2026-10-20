"""RCON for Source and Source 2 servers (Counter-Strike: Source, CS:GO, CS2 and relatives).

The protocol, as measured against CS2, CS:GO and CS:Source (see docs/games/counter-strike.md):

* TCP on the game port. Every packet is ``size`` (int32, little-endian, counting what
  follows), ``id`` (int32), ``type`` (int32), a body and two null bytes.
* Login: an AUTH packet (type 3) with the password as body. The server answers with an
  AUTH_RESPONSE (type 2) carrying the same id, or id -1 if the password is wrong.
  CS:Source and CS:GO send an empty RESPONSE_VALUE (type 0) first, CS2 does not.
* A command is an EXECCOMMAND packet (type 2). Its output comes back as one or more
  RESPONSE_VALUE packets (type 0). There is no end marker, so LANPilot sends an empty
  EXECCOMMAND right after the command: the server answers it with an empty packet after the
  command's output, which marks the end.
* **CS2 labels its answers wrongly.** It executes all packets that arrived together and
  gives every answer the id of the *last* of them. The command's output therefore often
  carries the id of the empty end marker packet. The end is the empty answer with the
  marker's id that comes after at least one other answer.
* **CS:GO adds lines of its server log** (``L 10/14/2026 - 14:18:30: rcon from …``) to the
  answers, the answer to the end marker included. Such lines are not output: they are
  ignored when looking for the empty answer and removed from the text.

The connection is kept open between commands and opened again after a minute of idleness,
so a connection the server dropped silently is never relied on. **CS:GO closes RCON
connections on every map change.** When a connection that was kept open turns out to be
closed before any answer arrived, the server cannot have run the command, so it is sent
once more on a new connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import re
import struct
import time

from lanpilot.core.adapters import (
    AuthenticationError,
    CommandRejectedError,
    ProtocolError,
    ServerUnreachableError,
)
from lanpilot.i18n import M

AUTH = 3
AUTH_RESPONSE = 2
EXECCOMMAND = 2
RESPONSE_VALUE = 0

HEADER = struct.Struct("<iii")
MIN_PACKET_SIZE = 10  # id, type and the two null bytes
MAX_PACKET_SIZE = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
# Packets one exchange may read, whatever their id: a long answer on CS:Source comes in
# packets of about 4 KB, so this is far above what a real answer needs.
MAX_PACKETS = 4096
PACKET_OVERHEAD = HEADER.size + 2  # counted per packet, so empty ones add up too
MAX_COMMAND_LENGTH = 1000
MAX_AUTH_PACKETS = 4
IDLE_RECONNECT_SECONDS = 60.0
LOG_LINE = re.compile(rb"L \d{2}/\d{2}/\d{4} - \d{2}:\d{2}:\d{2}: ")


def without_log_lines(text: bytes) -> bytes:
    return b"".join(line for line in text.splitlines(keepends=True) if not LOG_LINE.match(line))


def encode(packet_id: int, packet_type: int, body: str) -> bytes:
    payload = HEADER.pack(0, packet_id, packet_type)[4:] + body.encode() + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


class SourceRcon:
    """Sends console commands to one server. Not safe for concurrent use; LANPilot calls an
    adapter from one task at a time."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        timeout_seconds: float = 5.0,
        quiet_seconds: float = 0.3,
    ) -> None:
        if not password or "\x00" in password:
            # Checked here rather than by the server: the body is a null-terminated string.
            raise AuthenticationError(
                M(
                    "The RCON password is empty or contains a null character, "
                    "which the protocol cannot carry."
                )
            )
        self._address = (host, port)
        self._password = password
        self._timeout = timeout_seconds
        self._quiet = quiet_seconds
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._last_used = 0.0
        self._ids = itertools.count(1)
        self._refused = False
        self._answered = False  # whether the current exchange received anything yet

    async def close(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def _next_id(self) -> int:
        packet_id = next(self._ids)
        if packet_id >= 2**30:
            self._ids = itertools.count(1)
            packet_id = next(self._ids)
        return packet_id

    async def _read_packet(self, wait: float) -> tuple[int, int, bytes]:
        assert self._reader is not None  # noqa: S101 - only called while connected
        async with asyncio.timeout(wait):
            (size,) = struct.unpack("<i", await self._reader.readexactly(4))
            if not MIN_PACKET_SIZE <= size <= MAX_PACKET_SIZE:
                raise ProtocolError(M("The server sent a malformed RCON packet."))
            data = await self._reader.readexactly(size)
        packet_id, packet_type = struct.unpack_from("<ii", data)
        return packet_id, packet_type, data[8:].rstrip(b"\x00")

    async def _connect(self) -> None:
        try:
            async with asyncio.timeout(self._timeout):
                self._reader, self._writer = await asyncio.open_connection(*self._address)
        except (TimeoutError, OSError) as exc:
            raise ServerUnreachableError(
                M("Nothing accepts RCON connections on that port.")
            ) from exc

        auth_id = self._next_id()
        self._writer.write(encode(auth_id, AUTH, self._password))
        for _ in range(MAX_AUTH_PACKETS):
            packet_id, packet_type, _body = await self._read_packet(self._timeout)
            if packet_type != AUTH_RESPONSE:
                continue  # older servers send an empty RESPONSE_VALUE first
            if packet_id == -1:
                self._refused = True
                raise AuthenticationError(M("The server refused the RCON password."))
            if packet_id == auth_id:
                return
            break
        raise ProtocolError(M("The server did not confirm the RCON login."))

    async def execute(self, command: str) -> str:
        """Run a console command and return what the server printed.

        ``command`` is sent as one line. Callers that build it from parts must validate the
        parts; this method only rejects what would break the packet or the line.
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
            # Do not try again: servers ban addresses that keep sending wrong passwords.
            raise AuthenticationError(
                M("The server refused the RCON password. Edit the server to enter another one.")
            )
        if self._writer is not None and time.monotonic() - self._last_used > IDLE_RECONNECT_SECONDS:
            await self.close()
        try:
            if self._writer is None:
                await self._connect()
                text = await self._exchange(command)
            else:
                try:
                    text = await self._exchange(command)
                except (OSError, asyncio.IncompleteReadError):
                    if self._answered:
                        raise
                    # The kept connection was closed in the meantime: try a fresh one.
                    await self.close()
                    await self._connect()
                    text = await self._exchange(command)
        except TimeoutError as exc:
            await self.close()
            raise ServerUnreachableError(M("The server stopped answering over RCON.")) from exc
        except (OSError, asyncio.IncompleteReadError) as exc:
            await self.close()
            raise ServerUnreachableError(
                M("The RCON connection was closed by the server.")
            ) from exc
        except BaseException:
            # Whatever went wrong, the stream may hold half an answer: start afresh next time.
            await self.close()
            raise
        self._last_used = time.monotonic()
        return text

    async def _exchange(self, command: str) -> str:
        assert self._writer is not None  # noqa: S101 - _connect() succeeded
        command_id, marker_id = self._next_id(), self._next_id()
        self._answered = False
        self._writer.write(encode(command_id, EXECCOMMAND, command))
        self._writer.write(encode(marker_id, EXECCOMMAND, ""))
        await self._writer.drain()

        chunks: list[bytes] = []
        answers = 0
        size = 0
        packets = 0
        timeout = self._timeout
        while True:
            # Every packet counts, skipped and empty ones too, so a server that keeps sending
            # cannot keep the exchange going.
            packets += 1
            if packets > MAX_PACKETS:
                raise ProtocolError(M("The server's answer is too long."))
            try:
                packet_id, packet_type, body = await self._read_packet(timeout)
                self._answered = True
            except TimeoutError:
                if timeout != self._quiet:
                    raise
                # Only the end marker came back and nothing followed it. The wait may have
                # ended inside a packet, so the stream is not trusted any further.
                await self.close()
                break
            if packet_id not in (command_id, marker_id) or packet_type != RESPONSE_VALUE:
                continue  # left over from an earlier exchange
            if packet_id == marker_id and not without_log_lines(body):
                if answers:
                    break
                # The command printed nothing and the server sent no packet for it. Wait a
                # moment in case its answer is still on the way.
                answers += 1
                timeout = self._quiet
                continue
            answers += 1
            timeout = self._timeout
            size += len(body) + PACKET_OVERHEAD
            if size > MAX_RESPONSE_BYTES:
                raise ProtocolError(M("The server's answer is too long."))
            chunks.append(body)
        return without_log_lines(b"".join(chunks)).decode("utf-8", errors="replace")
