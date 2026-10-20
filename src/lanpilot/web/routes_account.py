"""Start page and the signed-in user's own account."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Request
from starlette.responses import RedirectResponse, Response

from lanpilot import audit, i18n
from lanpilot.auth import passwords, sessions, users
from lanpilot.auth.throttle import LoginThrottle
from lanpilot.i18n import M
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


@router.post("/account/language", dependencies=[CsrfProtected])
async def change_language(
    request: Request,
    db: DbSession,
    auth: RequireAuth,
    language: Annotated[str, Form()] = "",
) -> Response:
    try:
        users.set_language(auth.user, language)
    except users.UserError as exc:
        return render(request, "account.html", {"error": exc.args[0]}, status_code=400)
    await db.commit()
    root = request.app.state.settings.server.root_path
    return RedirectResponse(f"{root}/account?notice=language_changed", status_code=303)


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
    throttle: LoginThrottle = request.app.state.login_throttle
    # Its own key, so failed attempts here do not lock the sign-in, but the same limits.
    key, address = f"{user.username} (password change)", client_ip(request) or "unknown"
    wait, mark = throttle.attempt(key, address)
    if wait:
        return failed(M("Too many wrong passwords. Try again later."))
    if not await passwords.in_worker(
        passwords.verify_password, user.password_hash, current_password
    ):
        audit.record(
            db,
            action="password.change",
            actor=user,
            detail={"reason": "wrong current password"},
            ip=client_ip(request),
            success=False,
        )
        await db.commit()
        return failed(M("The current password is wrong."))
    throttle.record_success(key, address, mark)
    if new_password != new_password_repeat:
        return failed(M("The two new passwords do not match."))

    try:
        await users.set_password(db, user, new_password)  # ends all sessions, this one included
    except users.UserError as exc:
        return failed(i18n.message_of(exc))

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
