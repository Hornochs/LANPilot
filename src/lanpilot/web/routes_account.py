"""Start page and the signed-in user's own account."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Request
from starlette.responses import RedirectResponse, Response

from lanpilot import audit
from lanpilot.auth import passwords, sessions, users
from lanpilot.web.deps import AppSettings, CsrfProtected, DbSession, RequireAuth, client_ip
from lanpilot.web.templating import render

router = APIRouter()


@router.get("/", name="home")
async def home(request: Request, auth: RequireAuth) -> Response:
    return render(request, "home.html", {"states": request.app.state.manager.states()})


@router.get("/cards", name="cards")
async def cards(request: Request, auth: RequireAuth) -> Response:
    """The dashboard grid as a fragment, for live updates."""
    return render(request, "_grid.html", {"states": request.app.state.manager.states()})


@router.get("/account", name="account")
async def account(request: Request, auth: RequireAuth) -> Response:
    return render(request, "account.html", {"error": None})


@router.post("/account/password", dependencies=[CsrfProtected])
async def change_password(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    auth: RequireAuth,
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    new_password_repeat: Annotated[str, Form()] = "",
) -> Response:
    def failed(message: str) -> Response:
        return render(request, "account.html", {"error": message}, status_code=400)

    user = auth.user
    if not await asyncio.to_thread(passwords.verify_password, user.password_hash, current_password):
        audit.record(
            db,
            action="password.change",
            actor=user,
            detail={"reason": "wrong current password"},
            ip=client_ip(request),
            success=False,
        )
        await db.commit()
        return failed("The current password is wrong.")
    if new_password != new_password_repeat:
        return failed("The two new passwords do not match.")

    try:
        await users.set_password(db, user, new_password)  # ends all sessions, this one included
    except users.UserError as exc:
        return failed(str(exc))

    lifetime = timedelta(hours=settings.auth.session_lifetime_hours)
    token, _ = await sessions.create(
        db,
        user,
        lifetime=lifetime,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    audit.record(db, action="password.change", actor=user, ip=client_ip(request))
    await db.commit()

    spec = sessions.cookie_spec(settings.server)
    response = RedirectResponse(
        settings.server.root_path + "/account?notice=password_changed", status_code=303
    )
    response.set_cookie(
        spec.name,
        token,
        max_age=int(lifetime.total_seconds()),
        path=spec.path,
        secure=spec.secure,
        httponly=True,
        samesite="lax",
    )
    return response
