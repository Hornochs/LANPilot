"""Password hashing (Argon2id) and the password policy."""

from __future__ import annotations

import secrets
from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 10
# Bounds the work an unauthenticated client can cause with one login attempt.
MAX_LENGTH = 256

_hasher = PasswordHasher()


class PasswordPolicyError(ValueError):
    """The password does not meet the policy; the message is safe to show to the user."""


def check_policy(password: str, username: str | None = None) -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"The password must have at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f"The password must have at most {MAX_LENGTH} characters.")
    if username is not None and password.casefold() == username.casefold():
        raise PasswordPolicyError("The password must differ from the user name.")


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
