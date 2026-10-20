"""A server's page and the actions on it."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse, Response

from lanpilot import i18n
from lanpilot.auth.roles import Role
from lanpilot.core import maps
from lanpilot.core.actions import ActionError, Actions, Actor
from lanpilot.core.adapters import Capability, Player, RestartMode
from lanpilot.core.runtime import ServerManager, ServerState
from lanpilot.web.deps import Auth, CsrfProtected, DbSession, RequireAuth, client_ip, require_role
from lanpilot.web.templating import render

router = APIRouter(prefix="/servers")

RequireOperator = Annotated[Auth, Depends(require_role(Role.OPERATOR))]
RequireAdmin = Annotated[Auth, Depends(require_role(Role.ADMIN))]


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
    context = {
        "state": state,
        "maps": by_game_type,
        "error": error,
        **await _players(request, state),
        "console_enabled": _actions(request).console_enabled,
    }
    return render(request, "server.html", context, status_code=status_code)


def _actions(request: Request) -> Actions:
    actions: Actions = request.app.state.actions
    return actions


async def _players(request: Request, state: ServerState) -> dict[str, object]:
    """The player list for a page, or why there is none."""
    if not state.can(Capability.PLAYERS) or not state.online:
        return {"players": None, "players_error": None}
    players: list[Player] | None = None
    error: str | None = None
    try:
        players = await _actions(request).list_players(state.server_id)
    except ActionError as exc:
        error = i18n.message_of(exc)
    return {"players": players, "players_error": error}


async def _run(
    request: Request, db: AsyncSession, server_id: int, action: Awaitable[str], notice: str
) -> Response:
    try:
        await action
    except ActionError as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404) from None
        return await _page(
            request, db, server_id, error=i18n.message_of(exc), status_code=exc.status
        )
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


@router.get("/{server_id}/players", name="server_players")
async def server_players(request: Request, auth: RequireAuth, server_id: int) -> Response:
    """The player list as a fragment, for live updates."""
    state = _state(request, server_id)
    return render(request, "_players.html", {"state": state, **await _players(request, state)})


@router.post("/{server_id}/players/kick", dependencies=[CsrfProtected])
async def kick(
    request: Request,
    db: DbSession,
    auth: RequireOperator,
    server_id: int,
    player_id: Annotated[str, Form(max_length=64)],
    reason: Annotated[str, Form(max_length=200)] = "",
) -> Response:
    action = _actions(request).kick(_actor(request, auth), server_id, player_id, reason)
    return await _run(request, db, server_id, action, "player_kicked")


@router.post("/{server_id}/players/ban", dependencies=[CsrfProtected])
async def ban(
    request: Request,
    db: DbSession,
    auth: RequireAdmin,
    server_id: int,
    player_id: Annotated[str, Form(max_length=64)],
    reason: Annotated[str, Form(max_length=200)] = "",
) -> Response:
    action = _actions(request).ban(_actor(request, auth), server_id, player_id, reason)
    return await _run(request, db, server_id, action, "player_banned")


async def _console_page(
    request: Request,
    server_id: int,
    *,
    command: str = "",
    output: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    state = _state(request, server_id)
    if not state.can(Capability.CONSOLE):
        raise HTTPException(status_code=404)
    context = {
        "state": state,
        "enabled": _actions(request).console_enabled,
        "command": command,
        "output": output,
        "error": error,
    }
    return render(request, "console.html", context, status_code=status_code)


@router.get("/{server_id}/console", name="server_console")
async def console_page(request: Request, auth: RequireAdmin, server_id: int) -> Response:
    return await _console_page(request, server_id)


@router.post("/{server_id}/console", dependencies=[CsrfProtected])
async def console(
    request: Request,
    auth: RequireAdmin,
    server_id: int,
    command: Annotated[str, Form(max_length=1000)],
) -> Response:
    try:
        output = await _actions(request).console(_actor(request, auth), server_id, command)
    except ActionError as exc:
        if exc.status == 404:
            raise HTTPException(status_code=404) from None
        return await _console_page(
            request, server_id, command=command, error=i18n.message_of(exc), status_code=exc.status
        )
    return await _console_page(request, server_id, command=command, output=output)
