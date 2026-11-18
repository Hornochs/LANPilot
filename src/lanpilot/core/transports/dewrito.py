"""ElDewrito (Halo Online): the HTTP status server and RCON over a WebSocket.

As measured against ElDewrito 0.7.1 (docs/games/eldewrito.md):

* ``GET /`` on ``Server.Port`` (default 11775, TCP) answers with a JSON object: name, map,
  game variant, state (``InLobby``, ``Loading``, ``InGame``), player count and, unless the
  server has a join password, the players. With a join password the players are only sent
  to requests that carry HTTP Basic credentials ``dorito:<join password>``; the others get
  ``"passworded": true`` instead. Without a join password the answer is cached for
  ``Server.Http.CacheTime`` seconds (default 5).
* RCON is a WebSocket on ``Game.RconPort`` (default 11776) with the subprotocol
  ``dew-rcon``; without it the handshake fails. The first text frame is the password, which
  the server answers with ``accept``, or with ``deny`` and a close (code 1008). Every further
  frame is one console command, answered by one frame (empty for commands that print
  nothing). ``;`` does not separate commands.
* With ``Server.SendChatToRconClients`` or ``Server.RconConsoleOutputEnabled`` switched on
  the server also pushes chat and console output unasked (both off by default). Such
  frames would be taken for answers, so LANPilot needs both off.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from typing import Any

import aiohttp

from lanpilot.core.adapters import (
    AuthenticationError,
    CommandRejectedError,
    ProtocolError,
    ServerUnreachableError,
)
from lanpilot.i18n import M

SUBPROTOCOL = "dew-rcon"
ACCEPTED = "accept"
DENIED = "deny"
# The user name the status server expects along with a join password.
JOIN_PASSWORD_USER = "dorito"  # noqa: S105 - a fixed user name, not a password
MAX_STATUS_BYTES = 256 * 1024
MAX_ANSWER_BYTES = 1024 * 1024  # "help" is about 30 KB
MAX_COMMAND_LENGTH = 1000
IDLE_RECONNECT_SECONDS = 60.0


async def get_info(
    host: str, port: int, *, join_password: str | None = None, seconds: float = 3.0
) -> dict[str, Any]:
    """The status server's JSON object. Needs no password; with ``join_password`` a server
    that has one also lists its players."""
    headers = {}
    if join_password:
        credentials = f"{JOIN_PASSWORD_USER}:{join_password}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode()
    try:
        async with (
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=seconds), trust_env=False
            ) as session,
            session.get(
                f"http://{host}:{port}/", headers=headers, allow_redirects=False
            ) as response,
        ):
            if response.status != 200:
                raise ProtocolError(
                    M("The status server answered with HTTP {status}.", status=response.status)
                )
            body = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                body += chunk
                if len(body) > MAX_STATUS_BYTES:
                    raise ProtocolError(M("The server's status answer could not be read."))
    except (aiohttp.ClientConnectionError, TimeoutError) as exc:
        raise ServerUnreachableError(M("The server's status port cannot be reached.")) from exc
    except aiohttp.ClientError as exc:
        raise ProtocolError(M("The server's status answer could not be read.")) from exc
    try:
        data = json.loads(bytes(body).decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise ProtocolError(M("The server's status answer could not be read.")) from exc
    if not isinstance(data, dict):
        raise ProtocolError(M("The server's status answer could not be read."))
    return data


class DewRcon:
    """One RCON connection to one server, opened on first use and kept. Not safe for
    concurrent use; LANPilot calls an adapter from one task at a time."""

    def __init__(self, host: str, port: int, password: str, *, seconds: float = 5.0) -> None:
        if not password:
            raise AuthenticationError(M("The server refused the RCON password."))
        self._url = f"ws://{host}:{port}/"
        self._password = password
        self._seconds = seconds
        self._session: aiohttp.ClientSession | None = None
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self._last_used = 0.0
        self._refused = False

    async def close(self) -> None:
        socket, session = self._socket, self._session
        self._socket = self._session = None
        if socket is not None:
            with contextlib.suppress(aiohttp.ClientError, OSError, TimeoutError):
                await socket.close()
        if session is not None:
            await session.close()

    async def _connect(self) -> None:
        self._session = aiohttp.ClientSession(trust_env=False)
        try:
            async with asyncio.timeout(self._seconds):
                self._socket = await self._session.ws_connect(
                    self._url,
                    protocols=(SUBPROTOCOL,),
                    max_msg_size=MAX_ANSWER_BYTES,
                    autoclose=False,
                    autoping=True,
                )
        except (aiohttp.ClientConnectionError, TimeoutError, OSError) as exc:
            await self.close()
            raise ServerUnreachableError(
                M("Nothing accepts RCON connections on that port.")
            ) from exc
        except aiohttp.WSServerHandshakeError as exc:
            await self.close()
            raise ProtocolError(
                M("The server's RCON port does not speak ElDewrito's RCON protocol.")
            ) from exc
        if self._socket.protocol != SUBPROTOCOL:
            await self.close()
            raise ProtocolError(
                M("The server's RCON port does not speak ElDewrito's RCON protocol.")
            )
        await self._socket.send_str(self._password)
        answer = await self._receive()
        if answer == DENIED:
            self._refused = True
            await self.close()
            raise AuthenticationError(M("The server refused the RCON password."))
        if answer != ACCEPTED:
            await self.close()
            raise ProtocolError(M("The server did not confirm the RCON login."))

    async def _receive(self) -> str:
        assert self._socket is not None  # noqa: S101 - only called while connected
        async with asyncio.timeout(self._seconds):
            message = await self._socket.receive()
        if message.type is aiohttp.WSMsgType.TEXT:
            return str(message.data)
        if message.type is aiohttp.WSMsgType.BINARY:
            return bytes(message.data).decode("utf-8", errors="replace")
        if message.type is aiohttp.WSMsgType.ERROR:
            raise ProtocolError(M("The server sent a malformed RCON answer."))
        raise ConnectionResetError("closed")  # CLOSE, CLOSING, CLOSED

    async def execute(self, command: str) -> str:
        """Run a console command and return the server's answer.

        ``command`` is sent as one frame. Callers that build it from parts must validate the
        parts (quotes group arguments); this method only rejects what cannot be one command.
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
            # Do not try again with every poll.
            raise AuthenticationError(
                M("The server refused the RCON password. Edit the server to enter another one.")
            )
        if self._socket is not None and (
            self._socket.closed or time.monotonic() - self._last_used > IDLE_RECONNECT_SECONDS
        ):
            await self.close()
        try:
            if self._socket is None:
                await self._connect()
                text = await self._exchange(command)
            else:
                try:
                    text = await self._exchange(command)
                except (ConnectionResetError, aiohttp.ClientConnectionError):
                    # The kept connection was closed in the meantime: try a fresh one.
                    await self.close()
                    await self._connect()
                    text = await self._exchange(command)
        except TimeoutError as exc:
            await self.close()
            raise ServerUnreachableError(M("The server stopped answering over RCON.")) from exc
        except (ConnectionResetError, aiohttp.ClientConnectionError, OSError) as exc:
            await self.close()
            raise ServerUnreachableError(
                M("The RCON connection was closed by the server.")
            ) from exc
        except BaseException:
            await self.close()
            raise
        self._last_used = time.monotonic()
        return text

    async def _exchange(self, command: str) -> str:
        assert self._socket is not None  # noqa: S101 - _connect() succeeded
        await self._socket.send_str(command)
        return await self._receive()
