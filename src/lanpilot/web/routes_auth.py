"""Sign in and sign out."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Form, Request
from starlette.responses import RedirectResponse, Response

from lanpilot import audit
from lanpilot.auth import sessions, users
from lanpilot.auth.throttle import LoginThrottle
from lanpilot.i18n import M
from lanpilot.web.deps import AppSettings, CsrfProtected, DbSession, OptionalAuth, client_ip
from lanpilot.web.templating import render

router = APIRouter()

BAD_CREDENTIALS = M("Wrong user name or password.")


def safe_next(value: str) -> str:
    """Accept only a path inside LANPilot as the post-login target; anything else becomes ``/``.

    Prevents the login form from being used as an open redirect.
    """
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    ):
        return "/"
    return value


@router.get("/login", name="login")
async def login_form(request: Request, auth: OptionalAuth, next: str = "/") -> Response:
    target = safe_next(next)
    if auth is not None:
        return RedirectResponse(request.app.state.settings.server.root_path + target, 303)
    return render(request, "login.html", {"next": target, "error": None, "username": ""})


@router.post("/login")
async def login(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    throttle: LoginThrottle = request.app.state.login_throttle
    address = client_ip(request) or "unknown"
    # Throttle on what was typed, bounded so the counters cannot be flooded with long keys.
    typed_name = username.strip().lower()[:64]
    # Only a possible user name goes into the audit log: people type passwords into the
    # name field, and anything else would let anonymous clients write text into the log.
    logged_name = (
        typed_name
        if users.USERNAME_PATTERN.fullmatch(typed_name)
        else ("(empty)" if not typed_name else "(invalid name)")
    )
    target = safe_next(next)

    def failed(message: str, status_code: int, headers: dict[str, str] | None = None) -> Response:
        context = {"next": target, "error": message, "username": username[:64]}
        return render(request, "login.html", context, status_code=status_code, headers=headers)

    wait, mark = throttle.attempt(typed_name, address)
    if wait:
        if throttle.report_refusal(address):
            audit.record(
                db,
                action="login",
                actor_name=logged_name,
                detail={"reason": "throttled"},
                ip=address,
                success=False,
            )
            await audit.prune_failed_logins(db)
            await db.commit()
        minutes = max(1, round(wait / 60))
        return failed(
            M("Too many failed attempts. Try again in about {minutes} minute(s).", minutes=minutes),
            429,
            {"Retry-After": str(wait)},
        )

    user = await users.authenticate(db, username, password)
    if user is None:
        audit.record(
            db,
            action="login",
            actor_name=logged_name,
            detail={"reason": "bad credentials"},
            ip=address,
            success=False,
        )
        await audit.prune_failed_logins(db)
        await db.commit()
        return failed(BAD_CREDENTIALS, 401)

    throttle.record_success(typed_name, address, mark)
    idle_timeout = timedelta(minutes=settings.auth.session_idle_minutes)
    await sessions.purge_expired(db, idle_timeout=idle_timeout)

    spec = sessions.cookie_spec(settings.server)
    # A fresh token on every login; a session the browser already had is discarded.
    previous = request.cookies.get(spec.name)
    if previous:
        old_session = await sessions.load(db, previous, idle_timeout=idle_timeout)
        if old_session is not None:
            await sessions.end(db, old_session)

    lifetime = timedelta(hours=settings.auth.session_lifetime_hours)
    token, _ = await sessions.create(
        db, user, lifetime=lifetime, ip=address, user_agent=request.headers.get("user-agent")
    )
    audit.record(db, action="login", actor=user, ip=address)
    await db.commit()

    response = RedirectResponse(settings.server.root_path + target, status_code=303)
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


@router.post("/logout", dependencies=[CsrfProtected])
async def logout(
    request: Request, db: DbSession, settings: AppSettings, auth: OptionalAuth
) -> Response:
    spec = sessions.cookie_spec(settings.server)
    if auth is not None:
        await sessions.end(db, auth.session)
        audit.record(db, action="logout", actor=auth.user, ip=client_ip(request))
        await db.commit()
    response = RedirectResponse(settings.server.root_path + "/login?notice=signed_out", 303)
    response.delete_cookie(
        spec.name, path=spec.path, secure=spec.secure, httponly=True, samesite="lax"
    )
    return response
