"""Keeps known secrets out of the log.

Libraries put connection details into exception texts, and exception texts end up in logs.
Every secret LANPilot handles is registered here once it is known; the formatter then
replaces it wherever it would appear, including in tracebacks.
"""

from __future__ import annotations

import logging

PLACEHOLDER = "[redacted]"
# Shorter strings would match by coincidence and garble unrelated log lines.
MIN_LENGTH = 6


class SecretRedactor:
    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def add(self, secret: str) -> None:
        if len(secret) >= MIN_LENGTH:
            self._secrets.add(secret)

    def redact(self, text: str) -> str:
        # Longest first, so a secret containing another one is replaced as a whole.
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, PLACEHOLDER)
        return text


class RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor, fmt: str | None = None) -> None:
        super().__init__(fmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact(super().format(record))


#: Process-wide instance; secrets are registered where they are loaded or decrypted.
redactor = SecretRedactor()
