"""Server-sent events: tells open pages that something changed.

The stream carries no data, only event names such as ``server-3``. The page then fetches
the part it shows through the normal routes, which apply the access checks.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import timedelta

from fastapi import APIRouter, Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse

from lanpilot.auth import sessions
from lanpilot.core.events import SERVER_CHANGED, Event, EventBus

router = APIRouter()

KEEPALIVE_SECONDS = 15.0


def _frame(event: Event) -> str:
    if event.kind == SERVER_CHANGED:
        return f"event: server-{event.server_id}\ndata: {event.server_id}\n\n"
    return f"event: {event.kind}\ndata: \n\n"


async def event_stream(
    bus: EventBus,
    *,
    still_signed_in: Callable[[], Awaitable[bool]],
    keepalive_seconds: float = KEEPALIVE_SECONDS,
) -> AsyncGenerator[str]:
    """Yield SSE frames until the client's session ends or the client disconnects."""
    with bus.subscribe() as queue:
        yield "retry: 5000\n: connected\n\n"
        while True:
            try:
                async with asyncio.timeout(keepalive_seconds):
                    event = await queue.get()
            except TimeoutError:
                if not await still_signed_in():
                    return
                yield ": keepalive\n\n"
                continue
            yield _frame(event)


@router.get("/events", name="events")
async def events(request: Request) -> Response:
    # Authenticated by hand with short-lived database sessions: the usual dependency would
    # keep a database connection checked out for as long as the stream is open.
    settings = request.app.state.settings
    token = request.cookies.get(sessions.cookie_spec(settings.server).name, "")
    idle_timeout = timedelta(minutes=settings.auth.session_idle_minutes)

    async def signed_in(*, touch: bool) -> bool:
        async with request.app.state.sessionmaker() as db:
            return (
                await sessions.load(db, token, idle_timeout=idle_timeout, touch=touch) is not None
            )

    if not await signed_in(touch=True):
        return PlainTextResponse("Sign in required.\n", status_code=401)

    stream = event_stream(request.app.state.bus, still_signed_in=lambda: signed_in(touch=False))
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # Tells Nginx not to buffer this response; other proxies need it configured.
            "X-Accel-Buffering": "no",
        },
    )
