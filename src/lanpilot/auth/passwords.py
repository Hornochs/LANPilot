"""Password hashing (Argon2id) and the password policy."""

from __future__ import annotations

import asyncio
import secrets
import weakref
from collections.abc import Callable
from functools import cache
from typing import Any, TypeVar

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from lanpilot.i18n import M

MIN_LENGTH = 10
# Bounds the work an unauthenticated client can cause with one login attempt.
MAX_LENGTH = 256

_hasher = PasswordHasher()
T = TypeVar("T")


class PasswordPolicyError(ValueError):
    """The password does not meet the policy; the message is safe to show to the user."""


def check_policy(password: str, username: str | None = None) -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(
            M("The password must have at least {minimum} characters.", minimum=MIN_LENGTH)
        )
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(
            M("The password must have at most {maximum} characters.", maximum=MAX_LENGTH)
        )
    if username is not None and password.casefold() == username.casefold():
        raise PasswordPolicyError(M("The password must differ from the user name."))


# Argon2 needs 64 MiB per hash; without a limit, a burst of logins could take gigabytes.
MAX_PARALLEL = 4
_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


async def in_worker(function: Callable[..., T], *args: Any) -> T:
    """Run a hashing function in a thread, at most ``MAX_PARALLEL`` at a time."""
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(MAX_PARALLEL)
    async with slot:
        return await asyncio.to_thread(function, *args)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if len(password) > MAX_LENGTH:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


@cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(16))


def verify_dummy(password: str) -> None:
    """Spend the same time as a real verification, so unknown user names are not revealed."""
    verify_password(_dummy_hash(), password)
