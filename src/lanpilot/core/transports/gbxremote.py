"""GBXRemote 2: XML-RPC over TCP, as spoken by Trackmania dedicated servers.

The protocol, as measured against a TrackMania Nations Forever server (build 2011-02-21,
see docs/games/trackmania.md):

* TCP, port 5000 by default; the server only accepts remote connections when
  ``xmlrpc_allowremote`` is set in its configuration.
* On connect the server sends a 4-byte length (little-endian) and the protocol name,
  ``GBXRemote 2``.
* Every call and every answer is a 4-byte length, a 4-byte handle and an XML-RPC document.
  The client picks the handle; calls start at ``0x80000001`` and the answer carries the same
  handle. Callbacks (only sent after ``EnableCallbacks``, which LANPilot never calls) would
  carry handles below ``0x80000000``.
* On TrackMania Forever nothing works before ``Authenticate(login, password)``: every
  method, even ``GetVersion``, is answered with the fault "Permission denied.". TrackMania
  Sunrise answers the status methods without a login. A wrong password gives the fault
  "Password incorrect." and leaves the connection open.
* ``system.multicall`` runs several methods in one round trip; every call took about
  45 ms, so status queries are batched.

XML from the server is untrusted. Answers are size-limited before they are parsed, and
``xmlrpc.client`` neither resolves external entities nor expands entity bombs with the
expat versions Python ships with.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import xmlrpc.client
from collections.abc import Sequence
from typing import Any

from lanpilot.core.adapters import (
    AuthenticationError,
    CommandRejectedError,
    ProtocolError,
    ServerUnreachableError,
)
from lanpilot.i18n import M

PROTOCOL = b"GBXRemote 2"
FIRST_HANDLE = 0x80000000
MAX_GREETING = 64
MAX_ANSWER_BYTES = 8 * 1024 * 1024
MAX_SKIPPED_FRAMES = 100
MAX_FAULT_TEXT = 200
PASSWORD_INCORRECT = "Password incorrect."  # noqa: S105 - the server's fault text
PERMISSION_DENIED = "Permission denied."


class GbxFaultError(CommandRejectedError):
    """The server answered a call with an XML-RPC fault."""

    def __init__(self, code: int, text: str) -> None:
        # Server text is shown to operators: keep it short and printable.
        clean = "".join(ch for ch in text if ch.isprintable())[:MAX_FAULT_TEXT]
        super().__init__(clean or "The server refused the request.")
        self.code = code
        self.text = clean


class GbxRemote:
    """One connection to a server, logged in with ``login`` unless that is ``None``. Not safe
    for concurrent use; LANPilot calls an adapter from one task at a time."""

    def __init__(
        self,
        host: str,
        port: int,
        login: str | None,
        password: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._address = (host, port)
        self._login = login
        self._password = password
        self._timeout = timeout_seconds
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._handle = FIRST_HANDLE
        self._refused = False
        self._answered = False  # whether the current call received anything yet

    async def close(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def call(self, method: str, *params: Any) -> Any:
        """Call one method and return its result. Faults raise ``GbxFaultError``."""
        if self._refused:
            # Same rule as for RCON: a refused password is not sent again.
            raise AuthenticationError(
                M("The server refused the password. Edit the server to enter another one.")
            )
        try:
            if self._writer is None:
                await self._connect()
                return await self._call(method, params)
            try:
                return await self._call(method, params)
            except (OSError, asyncio.IncompleteReadError):
                if self._answered:
                    raise
                # The kept connection was closed in the meantime: try a fresh one.
                await self.close()
                await self._connect()
                return await self._call(method, params)
        except TimeoutError as exc:
            await self.close()
            raise ServerUnreachableError(M("The server stopped answering.")) from exc
        except (OSError, asyncio.IncompleteReadError) as exc:
            await self.close()
            raise ServerUnreachableError(M("The connection was closed by the server.")) from exc
        except GbxFaultError:
            raise  # a fault is a complete answer; the connection is fine
        except BaseException:
            await self.close()
            raise

    async def multicall(self, calls: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
        """Run several methods in one round trip. Each entry of the result is the method's
        result or a ``GbxFaultError`` instance (not raised)."""
        answer = await self.call(
            "system.multicall",
            [{"methodName": method, "params": list(params)} for method, params in calls],
        )
        if not isinstance(answer, list) or len(answer) != len(calls):
            raise ProtocolError(M("The server's answer to a batch of calls is malformed."))
        results: list[Any] = []
        for item in answer:
            if isinstance(item, list) and len(item) == 1:
                results.append(item[0])
            elif isinstance(item, dict) and "faultCode" in item:
                results.append(_fault(item.get("faultCode"), item.get("faultString")))
            else:
                raise ProtocolError(M("The server's answer to a batch of calls is malformed."))
        return results

    async def _connect(self) -> None:
        try:
            async with asyncio.timeout(self._timeout):
                self._reader, self._writer = await asyncio.open_connection(*self._address)
                (size,) = struct.unpack("<I", await self._reader.readexactly(4))
                if size > MAX_GREETING:
                    raise ProtocolError(M("The server does not speak GBXRemote."))
                greeting = await self._reader.readexactly(size)
        except (TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
            await self.close()
            raise ServerUnreachableError(M("Nothing answers with GBXRemote on that port.")) from exc
        except ProtocolError:
            await self.close()
            raise
        if greeting != PROTOCOL:
            await self.close()
            raise ProtocolError(M("The server speaks an unsupported protocol version."))
        if self._login is None:
            return  # a connection for what the server answers without a login
        try:
            await self._call("Authenticate", (self._login, self._password))
        except GbxFaultError as exc:
            await self.close()
            if exc.text == PASSWORD_INCORRECT:
                self._refused = True
                raise AuthenticationError(M("The server refused the login or password.")) from exc
            raise

    def _next_handle(self) -> int:
        self._handle = self._handle + 1 if self._handle < 0xFFFFFFFF else FIRST_HANDLE + 1
        return self._handle

    async def _call(self, method: str, params: Sequence[Any]) -> Any:
        assert self._reader is not None and self._writer is not None  # noqa: S101 - connected
        handle = self._next_handle()
        body = xmlrpc.client.dumps(tuple(params), method, encoding="utf-8").encode()
        self._answered = False
        self._writer.write(struct.pack("<II", len(body), handle) + body)
        await self._writer.drain()
        async with asyncio.timeout(self._timeout):
            for _ in range(MAX_SKIPPED_FRAMES):
                size, answer_handle = struct.unpack("<II", await self._reader.readexactly(8))
                self._answered = True
                if size > MAX_ANSWER_BYTES:
                    raise ProtocolError(M("The server's answer is too long."))
                data = await self._reader.readexactly(size)
                if answer_handle == handle:
                    return _parse(data)
                # A callback or the answer to an abandoned call: skip it.
        raise ProtocolError(M("The server sent too many unexpected messages."))


def _parse(data: bytes) -> Any:
    try:
        result, _ = xmlrpc.client.loads(data, use_builtin_types=True)
    except xmlrpc.client.Fault as fault:
        raise _fault(fault.faultCode, fault.faultString) from None
    except Exception as exc:
        raise ProtocolError(M("The server's answer is not valid XML-RPC.")) from exc
    if len(result) != 1:
        raise ProtocolError(M("The server's answer is not valid XML-RPC."))
    return result[0]


def _fault(code: object, text: object) -> GbxFaultError:
    return GbxFaultError(
        code if isinstance(code, int) else 0, str(text) if text is not None else ""
    )
