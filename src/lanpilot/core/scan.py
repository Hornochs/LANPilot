"""The on-demand LAN scan: ask every adapter that can discover servers, once, on request.

There is no background scanning. One scan runs at a time; a game whose discovery fails or
takes too long is reported as such and never fails the scan.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Network

from lanpilot.config import ScanSettings
from lanpilot.core.adapters import Capability, DiscoveredServer, GameAdapter
from lanpilot.core.adapters.registry import AdapterRegistry
from lanpilot.core.discovery import inside, parse_networks

logger = logging.getLogger(__name__)

# Time a game's discovery gets beyond the broadcast wait, for its follow-up queries.
FOLLOW_UP_SECONDS = 15.0
MAX_FOUND = 512


class ScanBusyError(Exception):
    """Another scan is still running."""


@dataclass(frozen=True)
class Found:
    server: DiscoveredServer
    game: str  # the adapter's display name
    configured: bool  # a server of this game with this address exists already


@dataclass(frozen=True)
class ScanResult:
    networks: tuple[str, ...]
    found: tuple[Found, ...]
    failed: tuple[str, ...]  # games whose discovery failed or timed out
    seconds: float


class Scanner:
    def __init__(self, registry: AdapterRegistry, settings: ScanSettings) -> None:
        self._registry = registry
        self._settings = settings
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._settings.networks)

    @property
    def networks(self) -> tuple[str, ...]:
        return tuple(self._settings.networks)

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def adapters(self) -> list[type[GameAdapter]]:
        return [
            adapter
            for adapter in self._registry.all(include_demo=False)
            if Capability.DISCOVER in adapter.capabilities
        ]

    async def run(self, configured: Iterable[tuple[str, str]] = ()) -> ScanResult:
        """Scan the configured networks. ``configured`` holds (adapter id, host) of the
        servers that exist, so finds can be marked."""
        if self._lock.locked():
            raise ScanBusyError
        async with self._lock:
            started = time.monotonic()
            networks = parse_networks(self._settings.networks)
            adapters = self.adapters()
            outcomes = await asyncio.gather(
                *(self._discover(adapter, networks) for adapter in adapters)
            )
            known = {(adapter_id, host.lower()) for adapter_id, host in configured}
            found: dict[tuple[str, str, int | None], Found] = {}
            failed: list[str] = []
            for adapter, servers in zip(adapters, outcomes, strict=True):
                if servers is None:
                    failed.append(adapter.name)
                    continue
                for server in servers:
                    if server.adapter_id != adapter.id or not inside(server.host, networks):
                        continue  # an adapter may only report its own game, inside the scan
                    key = (server.adapter_id, server.host, server.port)
                    if key not in found and len(found) < MAX_FOUND:
                        found[key] = Found(
                            server, adapter.name, (adapter.id, server.host.lower()) in known
                        )
            ordered = sorted(
                found.values(),
                key=lambda item: (
                    item.game.lower(),
                    _address_key(item.server.host),
                    item.server.port or 0,
                ),
            )
            return ScanResult(
                networks=self.networks,
                found=tuple(ordered),
                failed=tuple(failed),
                seconds=time.monotonic() - started,
            )

    async def _discover(
        self, adapter: type[GameAdapter], networks: Sequence[IPv4Network]
    ) -> Sequence[DiscoveredServer] | None:
        limit = self._settings.timeout_seconds + FOLLOW_UP_SECONDS
        try:
            async with asyncio.timeout(limit):
                return await adapter.discover(networks, self._settings.timeout_seconds)
        except TimeoutError:
            logger.warning("discovery of %s took longer than %.0f s", adapter.id, limit)
        except Exception:
            logger.exception("discovery of %s failed", adapter.id)
        return None


def _address_key(host: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in host.split("."))
    except ValueError:
        return ()
