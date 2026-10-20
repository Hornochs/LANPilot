"""Stored map lists.

The list a server reports is cached in the database: the map picker stays instant, and a
map change can be validated against it. Whatever the server reports is untrusted, so
entries are bounded and filtered before they are stored.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lanpilot.core import catalog
from lanpilot.core.adapters import MapRef
from lanpilot.db.models import ServerMap
from lanpilot.i18n import M

SOURCE_PROTOCOL = "protocol"
SOURCE_MANUAL = "manual"  # added by an administrator, e.g. a track outside the playlist
MAX_MAPS = 5000
MAX_KEY_LENGTH = 200
MAX_NAME_LENGTH = 200
MAX_GAME_TYPE_LENGTH = 100
MAX_EXTRA_ITEMS = 8
MAX_EXTRA_VALUE_LENGTH = 200


def _printable(text: str) -> bool:
    return bool(text) and all(ch.isprintable() for ch in text)


def _clean(reported: Iterable[MapRef]) -> list[MapRef]:
    cleaned: dict[tuple[str, str], MapRef] = {}
    for item in reported:
        if len(cleaned) >= MAX_MAPS:
            break
        game_type = item.game_type or ""
        if (
            not _printable(item.key)
            or len(item.key) > MAX_KEY_LENGTH
            or len(game_type) > MAX_GAME_TYPE_LENGTH
            or (game_type and not _printable(game_type))
        ):
            continue
        name = item.name if item.name and _printable(item.name) else None
        extra = {
            str(key)[:50]: str(value)[:MAX_EXTRA_VALUE_LENGTH]
            for key, value in list(item.extra.items())[:MAX_EXTRA_ITEMS]
        }
        cleaned.setdefault(
            (item.key, game_type),
            MapRef(item.key, name[:MAX_NAME_LENGTH] if name else None, game_type, extra),
        )
    return list(cleaned.values())


async def replace_reported(
    db: AsyncSession, server_id: int, reported: Iterable[MapRef], adapter_id: str
) -> int:
    """Replace the maps a server reported earlier with a new report. Does not commit.

    Maps the server reports without a name get the catalogue's name for that game.
    """
    names = catalog.load()
    manual = set(
        (
            await db.execute(
                select(ServerMap.key, ServerMap.game_type).where(
                    ServerMap.server_id == server_id, ServerMap.source == SOURCE_MANUAL
                )
            )
        ).all()
    )
    # A map added by hand stays one, also once the server lists it (e.g. after it was
    # inserted into the playlist).
    maps = [
        names.named(adapter_id, item)
        for item in _clean(reported)
        if (item.key, item.game_type or "") not in manual
    ]
    await db.execute(
        delete(ServerMap).where(
            ServerMap.server_id == server_id, ServerMap.source == SOURCE_PROTOCOL
        )
    )
    for item in maps:
        db.add(
            ServerMap(
                server_id=server_id,
                key=item.key,
                name=item.name,
                game_type=item.game_type or "",
                source=SOURCE_PROTOCOL,
                extra=json.dumps(dict(item.extra), sort_keys=True),
            )
        )
    await db.flush()
    return len(maps)


async def find_map(db: AsyncSession, server_id: int, key: str) -> ServerMap | None:
    query = select(ServerMap).where(ServerMap.server_id == server_id, ServerMap.key == key)
    return (await db.execute(query)).scalars().first()


async def add_manual(db: AsyncSession, server_id: int, ref: MapRef, adapter_id: str) -> ServerMap:
    """Store a map an administrator added. Raises ``ValueError`` if it is not storable or
    already in the list. Does not commit."""
    cleaned = _clean([ref])
    if not cleaned:
        raise ValueError(M("This map name cannot be stored."))
    item = catalog.load().named(adapter_id, cleaned[0])
    if await find_map(db, server_id, item.key) is not None:
        raise ValueError(M("This map is already in the list."))
    row = ServerMap(
        server_id=server_id,
        key=item.key,
        name=item.name,
        game_type=item.game_type or "",
        source=SOURCE_MANUAL,
        extra=json.dumps(dict(item.extra), sort_keys=True),
    )
    db.add(row)
    await db.flush()
    return row


async def list_maps(db: AsyncSession, server_id: int) -> Sequence[ServerMap]:
    query = (
        select(ServerMap)
        .where(ServerMap.server_id == server_id)
        .order_by(ServerMap.game_type, ServerMap.name, ServerMap.key)
    )
    return (await db.execute(query)).scalars().all()


async def count_maps(db: AsyncSession, server_id: int) -> int:
    return len(await list_maps(db, server_id))


async def get_map(db: AsyncSession, server_id: int, map_id: int) -> ServerMap | None:
    """The map with this id, but only if it belongs to the given server."""
    row = await db.get(ServerMap, map_id)
    return row if row is not None and row.server_id == server_id else None


def to_ref(row: ServerMap) -> MapRef:
    return MapRef(row.key, row.name, row.game_type or None, json.loads(row.extra))
