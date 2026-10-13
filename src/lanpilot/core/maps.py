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

from lanpilot.core.adapters import MapRef
from lanpilot.db.models import ServerMap

SOURCE_PROTOCOL = "protocol"
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


async def replace_reported(db: AsyncSession, server_id: int, reported: Iterable[MapRef]) -> int:
    """Replace the maps a server reported earlier with a new report. Does not commit."""
    maps = _clean(reported)
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
