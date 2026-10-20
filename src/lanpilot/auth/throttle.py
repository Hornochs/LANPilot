"""In-memory login throttling.

LANPilot runs as a single process, so counters can live in memory. They are lost on
restart, which only ever makes the limit more lenient for a moment.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

Key = tuple[str, ...]


class LoginThrottle:
    """Limits failed logins per (user name, client address) pair and per client address.

    The pair limit stops guessing one account's password. Keying it on the address as well
    keeps an attacker from locking a legitimate user out from elsewhere. The address limit
    stops one client from trying many user names.
    """

    def __init__(
        self,
        *,
        max_failures: int = 5,
        max_failures_per_address: int = 30,
        window_seconds: float = 900,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._max_failures_per_address = max_failures_per_address
        self._window = window_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._failures: dict[Key, deque[float]] = {}
        self._refusals_reported: dict[str, float] = {}

    def _recent(self, key: Key) -> deque[float]:
        entries = self._failures.get(key)
        if entries is None:
            return deque()
        cutoff = self._clock() - self._window
        while entries and entries[0] <= cutoff:
            entries.popleft()
        if not entries:
            del self._failures[key]
        return entries

    def _wait(self, key: Key, limit: int) -> float:
        entries = self._recent(key)
        if len(entries) < limit:
            return 0.0
        # Blocked until the oldest failure that counts towards the limit leaves the window.
        return entries[len(entries) - limit] + self._window - self._clock()

    def retry_after(self, username: str, address: str) -> int:
        """Seconds until another attempt is allowed; 0 means it is allowed now."""
        wait = max(
            self._wait(("pair", username, address), self._max_failures),
            self._wait(("address", address), self._max_failures_per_address),
        )
        return int(wait) + 1 if wait > 0 else 0

    def attempt(self, username: str, address: str) -> tuple[int, float | None]:
        """Check and count an attempt in one step: ``(seconds to wait, None)`` if it is not
        allowed now, else ``(0, mark)`` with the attempt already counted as a failure.

        Counting before the password is checked keeps parallel requests from all passing
        the check while the first ones are still being verified. Pass ``mark`` to
        :meth:`record_success` if the password was right.
        """
        wait = self.retry_after(username, address)
        if wait:
            return wait, None
        return 0, self.record_failure(username, address)

    def report_refusal(self, address: str) -> bool:
        """Whether a refused attempt from ``address`` should be written to the audit log:
        the first one per window, so a client that keeps trying writes one entry."""
        now = self._clock()
        last = self._refusals_reported.get(address)
        if last is not None and now - last < self._window:
            return False
        if len(self._refusals_reported) >= self._max_entries:
            self._refusals_reported = {
                key: when
                for key, when in self._refusals_reported.items()
                if now - when < self._window
            }
        self._refusals_reported[address] = now
        return True

    def record_failure(self, username: str, address: str) -> float:
        if len(self._failures) >= self._max_entries:
            self._evict()
        now = self._clock()
        for key in (("pair", username, address), ("address", address)):
            self._failures.setdefault(key, deque()).append(now)
        return now

    def record_success(self, username: str, address: str, mark: float | None = None) -> None:
        """Forget the failures of this pair, and the attempt counted under ``mark``."""
        self._failures.pop(("pair", username, address), None)
        entries = self._failures.get(("address", address))
        if mark is not None and entries is not None and mark in entries:
            entries.remove(mark)
            if not entries:
                del self._failures[("address", address)]

    def _evict(self) -> None:
        for key in list(self._failures):
            self._recent(key)
        if len(self._failures) >= self._max_entries:
            # Still full of live entries: drop the oldest half rather than grow without bound.
            oldest_first = sorted(self._failures, key=lambda key: self._failures[key][-1])
            for key in oldest_first[: len(oldest_first) // 2]:
                del self._failures[key]
