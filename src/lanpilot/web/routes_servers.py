"""A server's page and the actions on it."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from lanpilot.auth.roles import Role
from lanpilot.core import maps
from lanpilot.core.actions import ActionError, Actions, Actor
from lanpilot.core.adapters import RestartMode
from lanpilot.core.runtime import ServerManager, ServerState
from lanpilot.web.deps import Auth, CsrfProtected, DbSession, RequireAuth, client_ip, require_role
from lanpilot.web.templating import render

router = APIRouter(prefix="/servers")

RequireOperator = Annotated[Auth, Depends(require_role(Role.OPERATOR))]


def _manager(request: Request) -> ServerManager:
    manager: ServerManager = request.app.state.manager
    return manager


def _state(request: Request, server_id: int) -> ServerState:
    state = _manager(request).state(server_id)
    if state is None:
        raise HTTPException(status_code=404)
    return state


def _actor(request: Request, auth: Auth) -> Actor:
    return Actor(auth.user.id, auth.user.username, auth.role, client_ip(request))


async def _page(
    request: Request,
    db: AsyncSession,
    server_id: int,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    state = _state(request, server_id)
    by_game_type: dict[str, list[object]] = {}
    for row in await maps.list_maps(db, server_id):
        by_game_type.setdefault(row.game_type, []).append(row)
    context = {"state": state, "maps": by_game_type, "error": error}
    return render(request, "server.html", context, status_code=status_code)


async def _run(
    request: Request, db: AsyncSession, server_id: int, action: Awaitable[str], notice: str
) -> Response:
    try:
        await action
    except ActionError as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404) from None
        return await _page(request, db, server_id, error=str(exc), status_code=exc.status)
    await _manager(request).poll_now(server_id)
    root = request.app.state.settings.server.root_path
    return RedirectResponse(f"{root}/servers/{server_id}?notice={notice}", status_code=303)


@router.get("/{server_id}", name="server")
async def server_page(
    request: Request, db: DbSession, auth: RequireAuth, server_id: int
) -> Response:
    return await _page(request, db, server_id)


@router.get("/{server_id}/card", name="server_card")
async def server_card(request: Request, auth: RequireAuth, server_id: int) -> Response:
    """One dashboard card as a fragment, for live updates."""
    return render(request, "_card.html", {"state": _state(request, server_id)})


@router.get("/{server_id}/live", name="server_live")
async def server_live(request: Request, auth: RequireAuth, server_id: int) -> Response:
    """The state-dependent part of the server page as a fragment, for live updates."""
    return render(request, "_server_live.html", {"state": _state(request, server_id)})


@router.post("/{server_id}/map", dependencies=[CsrfProtected])
async def change_map(
    request: Request,
    db: DbSession,
    auth: RequireOperator,
    server_id: int,
    map_id: Annotated[int, Form()],
) -> Response:
    actions: Actions = request.app.state.actions
    action = actions.change_map(_actor(request, auth), server_id, map_id)
    return await _run(request, db, server_id, action, "map_changed")


@router.post("/{server_id}/restart", dependencies=[CsrfProtected])
async def restart(
    request: Request,
    db: DbSession,
    auth: RequireOperator,
    server_id: int,
    mode: Annotated[RestartMode, Form()],
) -> Response:
    actions: Actions = request.app.state.actions
    action = actions.restart(_actor(request, auth), server_id, mode)
    return await _run(request, db, server_id, action, f"restarted_{mode.value}")


@router.post("/{server_id}/maps/refresh", dependencies=[CsrfProtected])
async def refresh_maps(
    request: Request, db: DbSession, auth: RequireOperator, server_id: int
) -> Response:
    actions: Actions = request.app.state.actions
    action = actions.refresh_maps(_actor(request, auth), server_id)
    return await _run(request, db, server_id, action, "maps_refreshed")
