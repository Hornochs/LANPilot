"""Helpers for ``GameAdapter.discover()``: finding servers on the LAN without passwords.

Nothing here knows a game. Adapters build their query packets and parse the answers;
these functions only send, collect and bound what comes back:

* :func:`broadcast` sends datagrams to the directed broadcast address of every configured
  network (or the address itself for a single host) and collects the answers for a while;
* :func:`gather_found` runs the follow-up queries to the servers that answered.

Only answers from addresses inside the configured networks are kept, so a scan never reports
or follows up on anything outside them. Everything that arrives is untrusted.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Iterable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Upper bounds for one broadcast: a LAN party has a few dozen servers, not thousands.
MAX_REPLIES = 1024
# Answers kept per host: one host sending many must not crowd out the others.
MAX_REPLIES_PER_HOST = 8
# Follow-up queries to the servers that answered: how many at once, and how long each.
FOLLOW_UP_CONCURRENCY = 32
FOLLOW_UP_SECONDS = 5.0
MAX_TEXT_LENGTH = 64


@dataclass(frozen=True)
class Reply:
    data: bytes
    host: str
    port: int


def targets(networks: Sequence[IPv4Network]) -> list[str]:
    """Where to send a broadcast: the broadcast address of each network, or the address of
    a network that is a single host."""
    found: dict[str, None] = {}
    for network in networks:
        address = (
            network.network_address if network.num_addresses == 1 else network.broadcast_address
        )
        found[str(address)] = None
    return list(found)


def inside(host: str, networks: Sequence[IPv4Network]) -> bool:
    try:
        address = IPv4Address(host)
    except ValueError:
        return False
    return any(address in network for network in networks)


def clean_text(value: object, limit: int = MAX_TEXT_LENGTH) -> str | None:
    """A server-supplied name reduced to printable characters; ``None`` if nothing is left."""
    text = "".join(ch for ch in str(value) if ch.isprintable()).strip()[:limit].strip()
    return text or None


class _Collector(asyncio.DatagramProtocol):
    def __init__(self, networks: Sequence[IPv4Network]) -> None:
        self.networks = networks
        self.replies: list[Reply] = []
        self._per_host: dict[str, int] = {}

    def datagram_received(self, data: bytes, addr: tuple[str | int, int]) -> None:
        host, port = str(addr[0]), int(addr[1])
        if len(self.replies) >= MAX_REPLIES or not inside(host, self.networks):
            return
        count = self._per_host.get(host, 0)
        if count >= MAX_REPLIES_PER_HOST:
            return
        self._per_host[host] = count + 1
        self.replies.append(Reply(data, host, port))

    def error_received(self, exc: Exception) -> None:
        # ICMP "port unreachable" for a unicast target; nothing to do.
        logger.debug("discovery socket error: %s", exc)


async def broadcast(
    networks: Sequence[IPv4Network],
    packets: Iterable[tuple[bytes, int]],
    wait: float,
    *,
    source_port: int = 0,
    share_port: bool = False,
) -> list[Reply]:
    """Send each ``(payload, port)`` to every target of ``networks`` and return the answers
    that arrive within ``wait`` seconds.

    ``source_port`` binds the socket to a fixed port for games that answer only there (the
    Unreal Engine 3 LAN beacon); ``OSError`` is raised if it is taken. ``share_port`` sets
    ``SO_REUSEADDR`` so the port can be bound while a game server on the same host holds it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if share_port:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", source_port))  # noqa: S104 - broadcasts need the wildcard
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    loop = asyncio.get_running_loop()
    transport, collector = await loop.create_datagram_endpoint(
        lambda: _Collector(networks), sock=sock
    )
    try:
        for payload, port in packets:
            for target in targets(networks):
                try:
                    transport.sendto(payload, (target, port))
                except OSError as exc:  # e.g. no route to a configured network
                    logger.debug("cannot send to %s:%d: %s", target, port, exc)
        await asyncio.sleep(wait)
    finally:
        transport.close()
    return collector.replies


async def gather_found(probes: Iterable[Awaitable[T | None]]) -> list[T]:
    """Run follow-up queries, a limited number at a time and each within
    ``FOLLOW_UP_SECONDS``; failures, timeouts and ``None`` results are dropped."""
    limit = asyncio.Semaphore(FOLLOW_UP_CONCURRENCY)

    async def bounded(probe: Awaitable[T | None]) -> T | None:
        async with limit, asyncio.timeout(FOLLOW_UP_SECONDS):
            return await probe

    results = await asyncio.gather(*(bounded(probe) for probe in probes), return_exceptions=True)
    found: list[T] = []
    for result in results:
        if isinstance(result, BaseException):
            if not isinstance(result, Exception):
                raise result
            logger.debug("discovery follow-up query failed: %s", result)
        elif result is not None:
            found.append(result)
    return found


def parse_networks(values: Iterable[str]) -> list[IPv4Network]:
    """``10.0.0.0/24``-style strings as networks (host bits are ignored)."""
    return [ipaddress.IPv4Network(value, strict=False) for value in values]
