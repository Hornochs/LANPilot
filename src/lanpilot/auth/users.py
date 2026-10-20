"""User accounts: creation, authentication and the rules that protect the last admin.

Functions change the session but do not commit, so callers can add an audit entry to the
same transaction. They raise ``UserError`` before changing anything, so a caller that
catches it has nothing to roll back.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lanpilot import i18n
from lanpilot.auth import passwords
from lanpilot.auth.roles import Role
from lanpilot.db.base import utcnow
from lanpilot.db.models import User, UserSession
from lanpilot.i18n import M

USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")
LAST_ADMIN = M("This is the last active administrator; LANPilot would be left without one.")


class UserError(Exception):
    """A rule was violated; the message is safe to show to the user."""


def normalise_username(raw: str) -> str:
    username = raw.strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise UserError(
            M(
                "User names have 3 to 64 characters: letters, digits, dot, dash and underscore, "
                "starting with a letter or digit."
            )
        )
    return username


async def get_user(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)


async def get_by_username(db: AsyncSession, username: str) -> User | None:
    return (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()


async def list_users(db: AsyncSession) -> Sequence[User]:
    return (await db.execute(select(User).order_by(User.username))).scalars().all()


async def create_user(db: AsyncSession, username: str, password: str, role: Role) -> User:
    name = normalise_username(username)
    try:
        passwords.check_policy(password, name)
    except passwords.PasswordPolicyError as exc:
        raise UserError(i18n.message_of(exc)) from exc
    if await get_by_username(db, name) is not None:
        raise UserError(M("A user named {name} already exists.", name=repr(name)))

    user = User(
        username=name,
        password_hash=await passwords.in_worker(passwords.hash_password, password),
        role=role.value,
    )
    db.add(user)
    await db.flush()
    return user


def set_language(user: User, language: str | None) -> None:
    """``None`` (or "") follows the browser."""
    if language and language not in i18n.LANGUAGES:
        raise UserError(M("This language is not available."))
    user.language = language or None


async def authenticate(db: AsyncSession, username: str, password: str) -> User | None:
    """Return the user if the credentials are right and the account is active."""
    try:
        user = await get_by_username(db, normalise_username(username))
    except UserError:
        user = None
    if user is None or not user.is_active:
        await passwords.in_worker(passwords.verify_dummy, password)
        return None
    if not await passwords.in_worker(passwords.verify_password, user.password_hash, password):
        return None

    if passwords.needs_rehash(user.password_hash):
        user.password_hash = await passwords.in_worker(passwords.hash_password, password)
    user.last_login_at = utcnow()
    return user


async def _is_last_active_admin(db: AsyncSession, user: User) -> bool:
    if user.role != Role.ADMIN.value or not user.is_active:
        return False
    others = await db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == Role.ADMIN.value, User.is_active, User.id != user.id)
    )
    return not others


async def _end_sessions(db: AsyncSession, user: User) -> None:
    await db.execute(delete(UserSession).where(UserSession.user_id == user.id))


async def set_password(db: AsyncSession, user: User, password: str) -> None:
    """Set a new password and end every session of the user."""
    try:
        passwords.check_policy(password, user.username)
    except passwords.PasswordPolicyError as exc:
        raise UserError(i18n.message_of(exc)) from exc
    user.password_hash = await passwords.in_worker(passwords.hash_password, password)
    await _end_sessions(db, user)


async def set_role(db: AsyncSession, user: User, role: Role) -> None:
    if role is not Role.ADMIN and await _is_last_active_admin(db, user):
        raise UserError(LAST_ADMIN)
    user.role = role.value


async def set_active(db: AsyncSession, user: User, active: bool) -> None:
    if not active:
        if await _is_last_active_admin(db, user):
            raise UserError(LAST_ADMIN)
        await _end_sessions(db, user)
    user.is_active = active


async def delete_user(db: AsyncSession, user: User) -> None:
    if await _is_last_active_admin(db, user):
        raise UserError(LAST_ADMIN)
    await _end_sessions(db, user)
    await db.delete(user)
