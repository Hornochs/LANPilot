"""Finding map images online: the chain of sources, remembered misses, a background queue.

The sources configured in ``images.sources`` are asked in order; the first image that can
be fetched and processed is stored like an upload (``MapImageIndex.store``) with the
source's id as provider. Maps no source had an image for are remembered in
``map_image_misses`` so they are not asked about with every poll, and so is an image an
administrator removed, so it does not come straight back.

How hard to try depends on who asks (``Reach``): the poll asks for the current map of
every server and respects every remembered miss; "fetch missing images" asks again for maps
that were not found before; a request for one map asks in any case.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lanpilot.config import ImageSettings
from lanpilot.core import catalog, image_sources
from lanpilot.core.adapters import MapRef
from lanpilot.core.adapters.registry import AdapterRegistry
from lanpilot.core.image_sources import Fetcher, FetchError, ImageSource
from lanpilot.core.map_images import ImageError, MapImageIndex, images_directory, normalise_key
from lanpilot.db.base import utcnow
from lanpilot.db.models import MapImageMiss

logger = logging.getLogger(__name__)

REASON_NOT_FOUND = "not_found"
REASON_REMOVED = "removed"
RETRY_AFTER_FAILURE_SECONDS = 600.0
# The poll reports the current map every few seconds. After a lookup found nothing, or a
# remembered miss applied, it is not queued again for this long (memory only).
QUIET_AFTER_MISS_SECONDS = 3600.0
# Maps the lookup remembers not to ask for again soon; expired ones are dropped beyond this.
MAX_REMEMBERED = 10_000
PAUSE_SECONDS = 0.5  # between two maps, to be gentle with the sources
# While idle, the index is read again this often: "lanpilot images prefetch" stores images
# from another process.
RELOAD_SECONDS = 60.0
MAX_QUEUE = 10_000


class Reach(StrEnum):
    AUTOMATIC = "automatic"  # the current map of a polled server
    MISSING = "missing"  # an administrator asked for all missing images
    ONE = "one"  # an administrator asked for this map


class Outcome(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    FAILED = "failed"  # a source could not be reached; worth trying again later
    SKIPPED = "skipped"  # there is an image already, or a remembered miss applies


@dataclass(frozen=True)
class Wanted:
    adapter_id: str
    lookup: Mapping[str, str]
    ref: MapRef
    reach: Reach

    @property
    def key(self) -> tuple[str, str]:
        """Images belong to a game (adapter id) and a lower-case map key."""
        return self.adapter_id, normalise_key(self.ref.key)


class ImageLookup:
    def __init__(
        self,
        settings: ImageSettings,
        *,
        index: MapImageIndex,
        sessionmaker: async_sessionmaker[AsyncSession],
        data_dir: Path,
        registry: AdapterRegistry,
        sources: Mapping[str, ImageSource] | None = None,
        fetcher: Callable[[frozenset[str]], Fetcher] | None = None,
        pause_seconds: float = PAUSE_SECONDS,
        on_found: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._index = index
        self._sessionmaker = sessionmaker
        self._data_dir = data_dir
        self._registry = registry
        available = image_sources.SOURCES if sources is None else sources
        self._sources = [available[name] for name in settings.sources if name in available]
        self._fetcher = fetcher
        self._pause = pause_seconds
        self.on_found = on_found
        # Map -> monotonic time before which the poll does not queue it again.
        self._not_before: dict[tuple[str, str], float] = {}
        self._queue: asyncio.Queue[Wanted] = asyncio.Queue(MAX_QUEUE)
        self._queued: set[tuple[str, str]] = set()
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self._settings.online and bool(self._sources)

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset().union(*(source.hosts for source in self._sources))

    # --- queue -------------------------------------------------------------------------

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._work(), name="map image lookup")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def open_fetcher(self) -> Fetcher:
        """A fetcher limited to the hosts of the configured sources."""
        factory = image_sources.Fetcher if self._fetcher is None else self._fetcher
        return factory(self.hosts)

    def want(self, adapter_id: str, ref: MapRef, reach: Reach = Reach.AUTOMATIC) -> bool:
        """Queue a lookup for a map of the given game. Cheap; returns whether it was queued."""
        adapter = self._registry.get(adapter_id)
        if not self.enabled or adapter is None:
            return False
        wanted = Wanted(adapter_id, adapter.image_lookup, ref, reach)
        if wanted.key in self._queued or self._index.filename(*wanted.key):
            return False
        now = time.monotonic()
        if reach is Reach.AUTOMATIC and self._not_before.get(wanted.key, 0) > now:
            return False
        if len(self._not_before) > MAX_REMEMBERED:
            self._not_before = {
                key: until for key, until in self._not_before.items() if until > now
            }
        try:
            self._queue.put_nowait(wanted)
        except asyncio.QueueFull:
            return False
        self._queued.add(wanted.key)
        return True

    def want_all(self, adapter_id: str, refs: Iterable[MapRef], reach: Reach) -> int:
        return sum(self.want(adapter_id, ref, reach) for ref in refs)

    async def _work(self) -> None:
        async with self.open_fetcher() as fetcher:
            while True:
                try:
                    async with asyncio.timeout(RELOAD_SECONDS):
                        wanted = await self._queue.get()
                except TimeoutError:
                    await self._reload_index()
                    continue
                try:
                    outcome = await self.lookup(fetcher, wanted)
                except Exception:
                    logger.exception("looking up an image for %s failed", wanted.ref.key)
                    outcome = Outcome.FAILED
                finally:
                    self._queued.discard(wanted.key)
                if outcome is Outcome.FOUND and self.on_found is not None:
                    self.on_found()
                if outcome in (Outcome.NOT_FOUND, Outcome.SKIPPED):
                    self._not_before[wanted.key] = time.monotonic() + QUIET_AFTER_MISS_SECONDS
                if outcome is not Outcome.SKIPPED:
                    await asyncio.sleep(self._pause)

    async def _reload_index(self) -> None:
        async with self._sessionmaker() as db:
            changed = await self._index.load(db)
        if changed and self.on_found is not None:
            self.on_found()

    # --- one lookup --------------------------------------------------------------------

    async def lookup(self, fetcher: Fetcher, wanted: Wanted) -> Outcome:
        """Ask the sources for one map and store the first usable image."""
        game, key = wanted.key
        if self._index.filename(game, key) or await self._remembered_miss(wanted):
            return Outcome.SKIPPED

        stock = catalog.load().name(wanted.adapter_id, wanted.ref.key) is not None
        failed = False
        for source in self._sources:
            if source.custom_maps_only and stock:
                continue
            try:
                url = await source.find(fetcher, wanted.lookup, wanted.ref)
                data = await fetcher.get_image(url) if url else None
            except FetchError as exc:
                logger.info("image source %s: %s", source.id, exc)
                failed = True
                continue
            if url is None or data is None:
                continue
            try:
                async with self._sessionmaker() as db:
                    await self._index.store(
                        db,
                        images_directory(self._data_dir),
                        game,
                        key,
                        data,
                        provider=source.id,
                        source_url=url[:500],
                    )
                    await self._forget_miss(db, game, key)
            except ImageError:
                continue  # not an image after all; try the next source
            logger.info("map image for %s/%s from %s", game, key, source.id)
            return Outcome.FOUND

        if failed:
            self._not_before[wanted.key] = time.monotonic() + RETRY_AFTER_FAILURE_SECONDS
            return Outcome.FAILED
        async with self._sessionmaker() as db:
            await remember_miss(db, game, key, REASON_NOT_FOUND)
            await db.commit()
        return Outcome.NOT_FOUND

    async def _remembered_miss(self, wanted: Wanted) -> bool:
        if wanted.reach is Reach.ONE:
            return False
        game, key = wanted.key
        async with self._sessionmaker() as db:
            miss = await _miss(db, game, key)
        if miss is None:
            return False
        if miss.reason == REASON_REMOVED:
            return True
        if wanted.reach is Reach.MISSING:
            return False
        return miss.checked_at > utcnow() - timedelta(days=self._settings.retry_days)

    async def _forget_miss(self, db: AsyncSession, game: str, key: str) -> None:
        await db.execute(
            delete(MapImageMiss).where(MapImageMiss.game == game, MapImageMiss.map_key == key)
        )
        await db.commit()


async def _miss(db: AsyncSession, game: str, key: str) -> MapImageMiss | None:
    query = select(MapImageMiss).where(MapImageMiss.game == game, MapImageMiss.map_key == key)
    return (await db.execute(query)).scalar_one_or_none()


async def remember_miss(db: AsyncSession, game: str, map_key: str, reason: str) -> None:
    """Record that no online image is wanted for this map right now. Does not commit."""
    key = normalise_key(map_key)
    miss = await _miss(db, game, key)
    if miss is None:
        miss = MapImageMiss(game=game, map_key=key)
        db.add(miss)
    miss.reason = reason
    miss.checked_at = utcnow()
    await db.flush()


async def forget_not_found(db: AsyncSession) -> int:
    """Forget every "not found". Called at start-up: an update may have brought new sources
    or new identifiers, which deserve a chance before ``retry_days`` have passed. Images an
    administrator removed stay remembered. Commits."""
    result = await db.execute(delete(MapImageMiss).where(MapImageMiss.reason == REASON_NOT_FOUND))
    await db.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def misses(db: AsyncSession, game: str) -> dict[str, MapImageMiss]:
    rows = (await db.execute(select(MapImageMiss).where(MapImageMiss.game == game))).scalars()
    return {row.map_key: row for row in rows}
