"""Request dependencies: database session, authentication, authorisation, CSRF."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lanpilot.auth import sessions
from lanpilot.auth.roles import Role
from lanpilot.config import Settings
from lanpilot.db.models import User, UserSession
from lanpilot.i18n import M

CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"


class NotAuthenticatedError(Exception):
    """No valid session; handled by redirecting to the login page."""


class ForbiddenError(Exception):
    """The session is valid but the request is not allowed."""


@dataclass(frozen=True)
class Auth:
    user: User
    session: UserSession

    @property
    def role(self) -> Role:
        return Role(self.user.role)

    @property
    def is_admin(self) -> bool:
        return self.role.allows(Role.ADMIN)

    @property
    def is_operator(self) -> bool:
        return self.role.allows(Role.OPERATOR)


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


AppSettings = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def client_ip(request: Request) -> str | None:
    """Client address. Behind a trusted proxy Uvicorn has already applied X-Forwarded-For."""
    return request.client.host if request.client else None


async def optional_auth(request: Request, db: DbSession, settings: AppSettings) -> Auth | None:
    request.state.auth = None
    token = request.cookies.get(sessions.cookie_spec(settings.server).name)
    if not token:
        return None
    session = await sessions.load(
        db, token, idle_timeout=timedelta(minutes=settings.auth.session_idle_minutes)
    )
    if session is None:
        return None
    auth = Auth(user=session.user, session=session)
    request.state.auth = auth
    return auth


OptionalAuth = Annotated[Auth | None, Depends(optional_auth)]


async def require_auth(auth: OptionalAuth) -> Auth:
    if auth is None:
        raise NotAuthenticatedError
    return auth


RequireAuth = Annotated[Auth, Depends(require_auth)]


def require_role(required: Role) -> Callable[[Auth], Awaitable[Auth]]:
    async def dependency(auth: RequireAuth) -> Auth:
        if not auth.role.allows(required):
            raise ForbiddenError(M("This page requires the {value} role.", value=required.value))
        return auth

    return dependency


RequireAdmin = Annotated[Auth, Depends(require_role(Role.ADMIN))]


async def csrf_protect(request: Request, auth: RequireAuth) -> None:
    """Require the session's CSRF token, sent as a form field or (by HTMX) as a header."""
    sent = request.headers.get(CSRF_HEADER)
    if sent is None:
        value = (await request.form()).get(CSRF_FIELD)
        sent = value if isinstance(value, str) else None
    if sent is None or not hmac.compare_digest(sent.encode(), auth.session.csrf_token.encode()):
        raise ForbiddenError(M("The form has expired. Reload the page and try again."))


CsrfProtected = Depends(csrf_protect)
