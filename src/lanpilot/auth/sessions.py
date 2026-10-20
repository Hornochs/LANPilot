"""Server-side browser sessions.

The cookie carries a random token; the database stores only its SHA-256 hash, so a copy of
the database does not let anyone take over a session.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lanpilot.config import ServerSettings
from lanpilot.db.base import utcnow
from lanpilot.db.models import User, UserSession

COOKIE_NAME = "lanpilot_session"
MAX_TOKEN_LENGTH = 128
# How stale last_seen_at may get before it is written again; keeps reads from causing writes.
TOUCH_INTERVAL = timedelta(seconds=60)


@dataclass(frozen=True)
class CookieSpec:
    name: str
    path: str
    secure: bool


def cookie_spec(server: ServerSettings) -> CookieSpec:
    path = server.root_path or "/"
    if not server.is_https:
        return CookieSpec(COOKIE_NAME, path, secure=False)
    # __Host- binds the cookie to this exact host; browsers only accept it with Path=/.
    prefix = "__Host-" if path == "/" else "__Secure-"
    return CookieSpec(prefix + COOKIE_NAME, path, secure=True)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create(
    db: AsyncSession,
    user: User,
    *,
    lifetime: timedelta,
    ip: str | None,
    user_agent: str | None,
) -> tuple[str, UserSession]:
    """Create a session and return the cookie token together with the stored row."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session = UserSession(
        token_hash=hash_token(token),
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        created_at=now,
        last_seen_at=now,
        expires_at=now + lifetime,
        ip=ip[:45] if ip else None,
        user_agent=user_agent[:255] if user_agent else None,
    )
    db.add(session)
    await db.flush()
    return token, session


async def load(
    db: AsyncSession,
    token: str,
    *,
    idle_timeout: timedelta,
    now: datetime | None = None,
    touch: bool = True,
) -> UserSession | None:
    """Return the live session for a cookie token; expired ones are removed.

    ``touch=False`` checks validity without counting as activity.
    """
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return None
    session = (
        await db.execute(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if session is None:
        return None

    now = now or utcnow()
    expired = now >= session.expires_at or now - session.last_seen_at > idle_timeout
    if expired or not session.user.is_active:
        await db.delete(session)
        await db.commit()
        return None

    if touch and now - session.last_seen_at > TOUCH_INTERVAL:
        session.last_seen_at = now
        await db.commit()
    return session


async def end(db: AsyncSession, session: UserSession) -> None:
    await db.delete(session)


async def purge_expired(db: AsyncSession, *, idle_timeout: timedelta) -> None:
    now = utcnow()
    await db.execute(
        delete(UserSession).where(
            (UserSession.expires_at <= now) | (UserSession.last_seen_at < now - idle_timeout)
        )
    )
