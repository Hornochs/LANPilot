"""Encryption of game server credentials at rest.

LANPilot has to send remote-control passwords to game servers, so it cannot store them as
one-way hashes. They are encrypted with AES-256-GCM instead, with a key that lives outside
the database: a copy of the database alone does not reveal them.
"""

from __future__ import annotations

import base64
import binascii
import errno
import json
import os
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from lanpilot.config import ConfigError, Settings

KEY_BYTES = 32
NONCE_BYTES = 12
VERSION = "v1"

HOW_TO_GENERATE = "create one with 'lanpilot secret generate-key'"


class SecretError(Exception):
    """Stored credentials cannot be decrypted: wrong key, or the data was altered."""


def generate_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode()


def _decode_key(text: str, source: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(text.strip())
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != KEY_BYTES:
        raise ConfigError(
            f"{source} is not a valid key (32 random bytes, URL-safe base64); {HOW_TO_GENERATE}"
        )
    return key


def load_key(settings: Settings) -> bytes:
    """Return the encryption key from ``secret_key`` or ``secret_key_file``."""
    if settings.secret_key is not None and settings.secret_key_file is not None:
        raise ConfigError("set either secret_key or secret_key_file, not both")
    if settings.secret_key is not None:
        return _decode_key(settings.secret_key.get_secret_value(), "secret_key")
    if settings.secret_key_file is not None:
        path = settings.secret_key_file
        try:
            mode = path.stat().st_mode
            text = path.read_text()
        except OSError as exc:
            raise ConfigError(f"cannot read secret_key_file {path}: {exc.strerror}") from exc
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError(
                f"secret_key_file {path} is accessible by other users; run 'chmod 600 {path}'"
            )
        return _decode_key(text, f"secret_key_file {path}")
    raise ConfigError(f"no secret_key or secret_key_file is configured; {HOW_TO_GENERATE}")


def write_key_file(path: Path) -> None:
    """Create a new key file readable by its owner only. Refuses to overwrite a key.

    The key is written to a temporary file first and linked into place when it is complete,
    so an interrupted run never leaves an empty or partial key behind.
    """
    if path.exists():
        raise FileExistsError(errno.EEXIST, "a key file exists already", str(path))
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(generate_key() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)  # fails if a key appeared meanwhile; never replaces one
    finally:
        temporary.unlink(missing_ok=True)


class SecretBox:
    """Encrypts small JSON objects, bound to a context.

    The context is authenticated but not encrypted. For server credentials it names the
    adapter and the address they belong to, so an encrypted blob that is copied to another
    server entry, or whose server entry is pointed at a different address, no longer
    decrypts. Whoever can write to the database therefore cannot make LANPilot send a
    stored password to a host of their choosing.
    """

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError("the key must be 32 bytes")
        self._aead = AESGCM(key)

    def encrypt(self, values: Mapping[str, str], *, context: str) -> str:
        nonce = secrets.token_bytes(NONCE_BYTES)
        plaintext = json.dumps(dict(values), sort_keys=True).encode()
        ciphertext = self._aead.encrypt(nonce, plaintext, context.encode())
        return f"{VERSION}:{base64.urlsafe_b64encode(nonce + ciphertext).decode()}"

    def decrypt(self, token: str, *, context: str) -> dict[str, str]:
        version, _, payload = token.partition(":")
        if version != VERSION:
            raise SecretError("unknown format of stored credentials")
        try:
            raw = base64.urlsafe_b64decode(payload)
            plaintext = self._aead.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], context.encode())
            values = json.loads(plaintext)
        except (binascii.Error, ValueError, InvalidTag) as exc:
            raise SecretError(
                "stored credentials cannot be decrypted; the key changed or the entry was altered"
            ) from exc
        if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
            raise SecretError("stored credentials have an unexpected structure")
        return values


def server_context(adapter_id: str, host: str, port: int) -> str:
    return f"server|{adapter_id}|{host.lower()}|{port}"
