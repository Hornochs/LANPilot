"""Live side of the configured servers: adapter instances, status polling, serialised calls.

``ServerManager`` owns one ``ServerRuntime`` per configured server. A runtime holds the
adapter instance and a lock; the poll loop and every action go through that lock, so a
server never sees two conversations at once. State lives in memory only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lanpilot import i18n
from lanpilot.core import catalog, maps, secrets, servers
from lanpilot.core.adapters import (
    AdapterError,
    AuthenticationError,
    Capability,
    GameAdapter,
    MapRef,
    ServerStatus,
)
from lanpilot.core.adapters.registry import AdapterRegistry
from lanpilot.core.events import SERVER_CHANGED, SERVERS_CHANGED, Event, EventBus
from lanpilot.core.secrets import SecretBox, SecretError
from lanpilot.db.base import utcnow
from lanpilot.db.models import Server
from lanpilot.i18n import M

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_BACKOFF_FACTOR = 6
FIRST_POLL_WAIT_SECONDS = 2.0
INTERNAL_ERROR = M("Internal error while talking to the server; see the LANPilot log.")


@dataclass(frozen=True)
class ServerState:
    """Snapshot of what LANPilot knows about a server right now. Safe to show to any
    signed-in user: it never contains credentials."""

    server_id: int
    name: str
    adapter_id: str
    adapter_name: str
    host: str
    port: int
    enabled: bool
    capabilities: frozenset[Capability] = frozenset()
    # None until the first poll finished; then whether the last poll got an answer.
    online: bool | None = None
    status: ServerStatus | None = None
    # Why the server is offline or cannot be used, phrased for operators.
    error: str | None = None
    # Why remote control does not work although the status query does (e.g. wrong password).
    control_error: str | None = None
    checked_at: datetime | None = None
    last_seen_at: datetime | None = None

    @property
    def usable(self) -> bool:
        """Whether actions can be sent: configured correctly and enabled."""
        return self.enabled and bool(self.capabilities)

    def can(self, capability: Capability) -> bool:
        return capability in self.capabilities


@dataclass
class ServerRuntime:
    state: ServerState
    adapter: GameAdapter | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    first_poll_done: asyncio.Event = field(default_factory=asyncio.Event)
    polled: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    failures: int = 0
    maps_loaded: bool = False
    #: The current map that was last checked for an image lookup.
    announced_map: str | None = None


class ServerManager:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        registry: AdapterRegistry,
        box: SecretBox,
        bus: EventBus,
        interval: float,
        timeout: float,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._registry = registry
        self._box = box
        self._bus = bus
        self._interval = interval
        self._timeout = timeout
        self._runtimes: dict[int, ServerRuntime] = {}
        #: Called with (adapter id, map) for the current map after every successful poll.
        self.on_map: Callable[[str, MapRef], object] | None = None

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        async with self._sessionmaker() as db:
            for server in await servers.list_servers(db):
                self._add(server)

    async def stop(self) -> None:
        for server_id in list(self._runtimes):
            await self._remove(server_id)

    async def reload(self, server_id: int) -> None:
        """Pick up a server that was added, edited or deleted. Waits briefly for the first
        status, so the page shown right after saving already has it."""
        await self._remove(server_id)
        async with self._sessionmaker() as db:
            server = await servers.get_server(db, server_id)
            runtime = self._add(server) if server is not None else None
        if runtime is not None and runtime.task is not None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(FIRST_POLL_WAIT_SECONDS):
                    await runtime.first_poll_done.wait()
        self._bus.publish(Event(SERVERS_CHANGED))

    def _add(self, server: Server) -> ServerRuntime:
        adapter_class = self._registry.get(server.adapter_id)
        state = ServerState(
            server_id=server.id,
            name=server.name,
            adapter_id=server.adapter_id,
            adapter_name=adapter_class.name if adapter_class else server.adapter_id,
            host=server.host,
            port=server.port,
            enabled=server.enabled,
        )
        runtime = ServerRuntime(state=state)
        self._runtimes[server.id] = runtime

        if adapter_class is None:
            runtime.state = replace(state, error=M("The adapter for this game is not installed."))
            return runtime
        try:
            connection = servers.load_connection(server, adapter_class, self._box)
        except SecretError as exc:
            runtime.state = replace(state, error=secrets.describe(exc, reenter=True))
            return runtime
        except servers.ServerError as exc:
            runtime.state = replace(state, error=i18n.message_of(exc))
            return runtime

        runtime.adapter = adapter_class(connection)
        runtime.state = replace(state, capabilities=adapter_class.capabilities)
        if server.enabled:
            runtime.task = asyncio.create_task(
                self._poll_loop(runtime), name=f"poll-server-{server.id}"
            )
        return runtime

    async def _remove(self, server_id: int) -> None:
        runtime = self._runtimes.pop(server_id, None)
        if runtime is None:
            return
        if runtime.task is not None:
            runtime.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runtime.task
        if runtime.adapter is not None:
            try:
                await runtime.adapter.close()
            except Exception:
                logger.exception("closing the adapter of server %s failed", server_id)

    # --- reading -----------------------------------------------------------------------

    def get(self, server_id: int) -> ServerRuntime | None:
        return self._runtimes.get(server_id)

    def state(self, server_id: int) -> ServerState | None:
        runtime = self._runtimes.get(server_id)
        return runtime.state if runtime else None

    def states(self) -> list[ServerState]:
        return sorted(
            (runtime.state for runtime in self._runtimes.values()),
            key=lambda state: state.name.lower(),
        )

    # --- calling the server ------------------------------------------------------------

    async def call(self, server_id: int, operation: Callable[[GameAdapter], Awaitable[T]]) -> T:
        """Run ``operation`` against the server's adapter, serialised with the poll loop.

        Raises ``LookupError`` if the server has no usable adapter, and whatever
        ``AdapterError`` the adapter raises. Callers bound the wait with ``asyncio.timeout``.
        """
        runtime = self._runtimes.get(server_id)
        if runtime is None or runtime.adapter is None:
            raise LookupError(server_id)
        async with runtime.lock:
            return await operation(runtime.adapter)

    def poll_soon(self, server_id: int) -> None:
        """Ask for a fresh status right away, e.g. after an action changed something."""
        runtime = self._runtimes.get(server_id)
        if runtime is not None:
            runtime.wake.set()

    async def poll_now(self, server_id: int, *, wait_seconds: float = 1.5) -> None:
        """Trigger a poll and wait briefly for it, so the next page already shows the result."""
        runtime = self._runtimes.get(server_id)
        if runtime is None or runtime.task is None:
            return
        runtime.polled.clear()
        runtime.wake.set()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(wait_seconds):
                await runtime.polled.wait()

    def announce(self, server_id: int | None = None) -> None:
        """Tell open pages to refetch, although no server's state changed (e.g. a new map
        image). Without an id every page reloads all of its servers."""
        if server_id is None:
            self._bus.publish(Event(SERVERS_CHANGED))
        else:
            self._bus.publish(Event(SERVER_CHANGED, server_id))

    def set_control_error(self, server_id: int, message: str | None) -> None:
        runtime = self._runtimes.get(server_id)
        if runtime is not None and runtime.state.control_error != message:
            runtime.state = replace(runtime.state, control_error=message)
            self._bus.publish(Event(SERVER_CHANGED, server_id))

    # --- polling -----------------------------------------------------------------------

    async def _poll_loop(self, runtime: ServerRuntime) -> None:
        while True:
            await self._poll_once(runtime)
            runtime.first_poll_done.set()
            runtime.polled.set()
            # Back off while a server is down, and spread the polls of many servers out.
            factor = min(2**runtime.failures, MAX_BACKOFF_FACTOR) if runtime.failures else 1
            delay = self._interval * factor * random.uniform(0.9, 1.1)  # noqa: S311 - jitter
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(delay):
                    await runtime.wake.wait()
            runtime.wake.clear()

    async def _poll_once(self, runtime: ServerRuntime) -> None:
        adapter = runtime.adapter
        if adapter is None:  # pragma: no cover - loops only run for runtimes with an adapter
            return
        previous = runtime.state
        now = utcnow()
        error: str
        try:
            async with runtime.lock, asyncio.timeout(self._timeout):
                status = await adapter.query()
        except TimeoutError:
            error = M("No answer within {seconds} seconds.", seconds=f"{self._timeout:.0f}")
        except AdapterError as exc:
            error = i18n.message_of(exc) or type(exc).__name__
        except asyncio.CancelledError:
            raise
        except Exception:
            # A bug in an adapter must not end the polling of its server.
            logger.exception("status query of server %s failed unexpectedly", previous.server_id)
            error = INTERNAL_ERROR
        else:
            runtime.failures = 0
            if status.map is not None:
                status = replace(status, map=catalog.load().named(previous.adapter_id, status.map))
            runtime.state = replace(
                previous, online=True, status=status, error=None, checked_at=now, last_seen_at=now
            )
            if not runtime.maps_loaded:
                await self._load_maps(runtime)
            if status.map is not None:
                await self._announce_map(runtime, status.map)
            self._publish_if_changed(previous, runtime.state)
            return

        runtime.failures += 1
        runtime.state = replace(previous, online=False, status=None, error=error, checked_at=now)
        self._publish_if_changed(previous, runtime.state)

    async def _announce_map(self, runtime: ServerRuntime, current: MapRef) -> None:
        """Report the current map for an image lookup, once per map change, and only a map
        the catalogue knows or the server lists: the name comes from the server's status,
        and a server that keeps reporting new names must not cause a lookup for each."""
        if self.on_map is None or current.key == runtime.announced_map:
            return
        runtime.announced_map = current.key
        adapter_id = runtime.state.adapter_id
        if catalog.load().name(adapter_id, current.key) is None:
            async with self._sessionmaker() as db:
                if await maps.find_map(db, runtime.state.server_id, current.key) is None:
                    return
        self.on_map(adapter_id, current)

    async def _load_maps(self, runtime: ServerRuntime) -> None:
        """Fetch the map list once after a server first answers. This also reveals whether
        the stored password works, which the status query cannot tell."""
        adapter = runtime.adapter
        runtime.maps_loaded = True
        if adapter is None or not adapter.supports(Capability.MAP_LIST):
            return
        server_id = runtime.state.server_id
        try:
            async with runtime.lock, asyncio.timeout(self._timeout):
                reported = await adapter.list_maps()
            async with self._sessionmaker() as db:
                await maps.replace_reported(db, server_id, reported, runtime.state.adapter_id)
                await db.commit()
        except AuthenticationError as exc:
            runtime.state = replace(runtime.state, control_error=i18n.message_of(exc))
        except TimeoutError:
            runtime.maps_loaded = False  # try again with the next successful poll
        except AdapterError as exc:
            runtime.state = replace(runtime.state, control_error=i18n.message_of(exc))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("loading the map list of server %s failed unexpectedly", server_id)
            runtime.state = replace(runtime.state, control_error=INTERNAL_ERROR)
        else:
            runtime.state = replace(runtime.state, control_error=None)
            runtime.announced_map = None  # the list changed: check the current map again
            # Adapters may learn from the map list (e.g. the names of game types); ask for
            # the status again right away, so pages do not wait a whole interval for that.
            runtime.wake.set()

    def _publish_if_changed(self, before: ServerState, after: ServerState) -> None:
        # checked_at changes with every poll; it alone is no news.
        if replace(before, checked_at=None, last_seen_at=None) != replace(
            after, checked_at=None, last_seen_at=None
        ):
            self._bus.publish(Event(SERVER_CHANGED, after.server_id))
