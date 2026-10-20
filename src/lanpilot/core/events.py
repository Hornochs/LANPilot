"""In-process event bus that feeds the browsers' live updates.

Events only say *that* something changed. Clients then fetch what they are allowed to see
through the normal, authorised routes, so no data is pushed past the access checks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

SERVER_CHANGED = "server"  # one server's state changed; server_id is set
SERVERS_CHANGED = "servers"  # a server was added, edited or removed
RESYNC = "resync"  # the subscriber missed events and should reload everything


@dataclass(frozen=True)
class Event:
    kind: str
    server_id: int | None = None


class EventBus:
    def __init__(self, *, max_queue: int = 100) -> None:
        self._max_queue = max_queue
        self._subscribers: set[asyncio.Queue[Event]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(self._max_queue)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def publish(self, event: Event) -> None:
        """Deliver to every subscriber without waiting; never blocks the publisher."""
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A client that does not keep up gets one "reload everything" instead of an
                # ever-growing backlog.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(Event(RESYNC))
