"""Server-sent events: tells open pages that something changed.

The stream carries no data, only event names such as ``server-3``. The page then fetches
the part it shows through the normal routes, which apply the access checks.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import timedelta
from typing import TypeVar

from fastapi import APIRouter, Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse

from lanpilot.auth import sessions
from lanpilot.core.events import SERVER_CHANGED, Event, EventBus

router = APIRouter()

K = TypeVar("K")

KEEPALIVE_SECONDS = 15.0
# Open streams per session (one per browser tab), per user (who may sign in many times) and
# in all; each holds a subscription.
MAX_STREAMS_PER_SESSION = 8
MAX_STREAMS_PER_USER = 16
MAX_STREAMS = 500


class StreamLimit:
    """Counts the open streams per session and per user."""

    def __init__(self) -> None:
        self._sessions: dict[str, int] = {}
        self._users: dict[int, int] = {}
        self._total = 0

    def acquire(self, session: str, user: int) -> bool:
        in_session = self._sessions.get(session, 0)
        of_user = self._users.get(user, 0)
        if (
            in_session >= MAX_STREAMS_PER_SESSION
            or of_user >= MAX_STREAMS_PER_USER
            or self._total >= MAX_STREAMS
        ):
            return False
        self._sessions[session] = in_session + 1
        self._users[user] = of_user + 1
        self._total += 1
        return True

    def release(self, session: str, user: int) -> None:
        if session not in self._sessions:
            return
        _decrement(self._sessions, session)
        _decrement(self._users, user)
        self._total -= 1


def _decrement(counts: dict[K, int], key: K) -> None:
    count = counts.get(key, 0) - 1
    if count > 0:
        counts[key] = count
    else:
        counts.pop(key, None)


def _frame(event: Event) -> str:
    if event.kind == SERVER_CHANGED:
        return f"event: server-{event.server_id}\ndata: {event.server_id}\n\n"
    return f"event: {event.kind}\ndata: \n\n"


async def event_stream(
    bus: EventBus,
    *,
    still_signed_in: Callable[[], Awaitable[bool]],
    keepalive_seconds: float = KEEPALIVE_SECONDS,
    on_close: Callable[[], None] | None = None,
) -> AsyncGenerator[str]:
    """Yield SSE frames until the client's session ends or the client disconnects.

    The session is checked again every ``keepalive_seconds``, also while events keep the
    stream busy, so a signed-out or removed user stops receiving them.
    """
    try:
        with bus.subscribe() as queue:
            yield "retry: 5000\n: connected\n\n"
            checked = time.monotonic()
            while True:
                try:
                    async with asyncio.timeout(keepalive_seconds):
                        event = await queue.get()
                except TimeoutError:
                    event = None
                if time.monotonic() - checked >= keepalive_seconds:
                    if not await still_signed_in():
                        return
                    checked = time.monotonic()
                yield ": keepalive\n\n" if event is None else _frame(event)
    finally:
        if on_close is not None:
            on_close()


@router.get("/events", name="events")
async def events(request: Request) -> Response:
    # Authenticated by hand with short-lived database sessions: the usual dependency would
    # keep a database connection checked out for as long as the stream is open.
    settings = request.app.state.settings
    token = request.cookies.get(sessions.cookie_spec(settings.server).name, "")
    idle_timeout = timedelta(minutes=settings.auth.session_idle_minutes)

    async def user_id(*, touch: bool) -> int | None:
        async with request.app.state.sessionmaker() as db:
            session = await sessions.load(db, token, idle_timeout=idle_timeout, touch=touch)
            return session.user_id if session is not None else None

    async def still_signed_in() -> bool:
        return await user_id(touch=False) is not None

    user = await user_id(touch=True)
    if user is None:
        return PlainTextResponse("Sign in required.\n", status_code=401)
    limit: StreamLimit = request.app.state.event_streams
    key = sessions.hash_token(token)
    if not limit.acquire(key, user):
        return PlainTextResponse("Too many open pages.\n", status_code=429)

    stream = event_stream(
        request.app.state.bus,
        still_signed_in=still_signed_in,
        on_close=lambda: limit.release(key, user),
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # Tells Nginx not to buffer this response; other proxies need it configured.
            "X-Accel-Buffering": "no",
        },
    )
