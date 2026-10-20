"""Finding Unreal Tournament servers on the LAN without a password.

Each game has its own LAN query, answered on a port other than the WebAdmin LANPilot
controls it through, so a find carries the host and, for UT99 and UT2004, the GameSpy query
port; the form keeps the default WebAdmin port. Measured facts:
docs/games/unreal-tournament.md.

* UT99: ``REPORTQUERY`` broadcast to 8777-8786 is answered with ``ut <query port>``; the
  GameSpy query on that port gives name and game type.
* UT2004: ``80 00 00 00 00`` broadcast to 10777 is answered from the Unreal 2 query port
  (game port + 1); its details give name, game type and game port, and the GameSpy query
  port LANPilot uses is the game port + 10.
* UT3: the Unreal Engine 3 LAN beacon on 14001 answers only to port 14001, so the query is
  sent from there; the socket shares the port with a UT3 server on the same host.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from ipaddress import IPv4Network

from opengsq.protocols.unreal2 import Unreal2
from opengsq.protocols.ut3 import UT3

from lanpilot.core import discovery
from lanpilot.core.adapters import DiscoveredServer
from lanpilot.games.unreal.common import query_status

UT99_BEACON_PORTS = tuple(range(8777, 8787))
UT99_BEACON_QUERY = b"REPORTQUERY"
UT99_BEACON_ANSWER = re.compile(rb"ut (\d{1,5})")
UT2004_LAN_PORT = 10777
UT2004_LAN_QUERY = b"\x80\x00\x00\x00\x00"
UT2004_GAMESPY_OFFSET = 10
UT3_BEACON_PORT = 14001
# The beacon answers to this port of the sender, whatever port the query came from.
UT3_ANSWER_PORT = 14001
DETAIL_TIMEOUT_SECONDS = 3.0


async def discover_ut99(
    adapter_id: str, networks: Sequence[IPv4Network], wait: float
) -> list[DiscoveredServer]:
    packets = [(UT99_BEACON_QUERY, port) for port in UT99_BEACON_PORTS]
    found: dict[tuple[str, int], None] = {}
    for reply in await discovery.broadcast(networks, packets, wait):
        match = UT99_BEACON_ANSWER.fullmatch(reply.data.strip(b"\x00"))
        if match and 0 < int(match.group(1)) < 65536:
            found[(reply.host, int(match.group(1)))] = None
    return await discovery.gather_found(_gamespy(adapter_id, host, port) for host, port in found)


async def discover_ut2004(
    adapter_id: str, networks: Sequence[IPv4Network], wait: float
) -> list[DiscoveredServer]:
    replies = await discovery.broadcast(networks, [(UT2004_LAN_QUERY, UT2004_LAN_PORT)], wait)
    senders = {
        (reply.host, reply.port): None
        for reply in replies
        if reply.data.startswith(UT2004_LAN_QUERY) and len(reply.data) > len(UT2004_LAN_QUERY)
    }

    async def detail(host: str, port: int) -> DiscoveredServer | None:
        details = await Unreal2(host, port, DETAIL_TIMEOUT_SECONDS).get_details()
        game_port = int(details.game_port)
        if not 0 < game_port < 65536 - UT2004_GAMESPY_OFFSET:
            return None
        return DiscoveredServer(
            adapter_id,
            host,
            name=discovery.clean_text(details.server_name),
            game_type=discovery.clean_text(details.game_type),
            values={"query_port": str(game_port + UT2004_GAMESPY_OFFSET)},
        )

    return await discovery.gather_found(detail(host, port) for host, port in senders)


async def discover_ut3(
    adapter_id: str, networks: Sequence[IPv4Network], wait: float
) -> list[DiscoveredServer]:
    beacon = UT3("255.255.255.255")
    replies = await discovery.broadcast(
        networks,
        [(beacon._build_query_packet(), UT3_BEACON_PORT)],
        wait,
        source_port=UT3_ANSWER_PORT,
        share_port=True,
    )
    found: dict[str, DiscoveredServer] = {}
    for reply in replies:
        # Our own query comes back too (type "SQ"); the check accepts only answers ("SR")
        # carrying our nonce.
        if reply.host in found or not beacon._is_valid_response(reply.data):
            continue
        try:
            parsed = beacon._parse_response(reply.data)
        except Exception:  # noqa: S112 - a malformed beacon is skipped
            continue
        found[reply.host] = DiscoveredServer(
            adapter_id,
            reply.host,
            name=discovery.clean_text(parsed.get("name", "")),
            game_type=discovery.clean_text(parsed.get("game_type", "")),
        )
    return list(found.values())


async def _gamespy(adapter_id: str, host: str, port: int) -> DiscoveredServer:
    try:
        status = await query_status(host, port)
    except Exception:
        return DiscoveredServer(adapter_id, host, values={"query_port": str(port)})
    return DiscoveredServer(
        adapter_id,
        host,
        name=discovery.clean_text(status.name),
        game_type=discovery.clean_text(status.game_type or ""),
        values={"query_port": str(port)},
    )
