"""The contract every source of availability implements.

Only Libby is implemented, because it's the only audiobook platform with a
public availability API (see docs/platforms.md for what happened to the others).
The interface exists anyway so adding a second source later is a new file
rather than a rewrite.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from ..matching import Book
from ..models import Availability, Scope


class RateLimiter:
    """One global tap for the whole process, that tunes itself.

    Nobody publishes a rate limit for this API, so picking a constant means
    either being needlessly slow or occasionally rude. Instead this does what
    congestion control does: speed up gently while everything is fine, and slow
    down hard the moment the server complains.

      * every `probe_after` consecutive clean responses, the rate rises 20%,
        up to `ceiling`
      * any 429 or 5xx halves it, down to `floor`, and pauses everyone

    The result settles at whatever the API actually tolerates, and a run that
    starts at 120/min may well finish at 300 — or drop to 60 and stay there if
    that's what the server wants. Either way it's measured, not assumed.
    """

    def __init__(self, per_minute: float, *, floor: float = 30, ceiling: float = 300,
                 probe_after: int = 40, adaptive: bool = True):
        self.start_rate = max(per_minute, 1.0)
        self.rate = self.start_rate
        self.floor = min(floor, self.start_rate)
        self.ceiling = max(ceiling, self.start_rate)
        self.probe_after = probe_after
        self.adaptive = adaptive

        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self._clean = 0
        self.throttles = 0
        self.increases = 0

    @property
    def interval(self) -> float:
        return 60.0 / self.rate

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            due = max(now, self._next_at)
            self._next_at = due + self.interval
            delay = due - now
        if delay > 0:
            await asyncio.sleep(delay)

    async def record_success(self) -> None:
        if not self.adaptive:
            return
        async with self._lock:
            self._clean += 1
            if self._clean >= self.probe_after and self.rate < self.ceiling:
                self._clean = 0
                self.rate = min(self.ceiling, self.rate * 1.2)
                self.increases += 1

    async def record_throttle(self) -> None:
        async with self._lock:
            self._clean = 0
            self.throttles += 1
            if self.adaptive:
                self.rate = max(self.floor, self.rate / 2)

    async def back_off(self, seconds: float) -> None:
        """Push every pending request out — after a 429, everyone waits."""
        async with self._lock:
            self._next_at = max(self._next_at, time.monotonic() + seconds)

    def snapshot(self) -> dict:
        return {
            "rate_per_minute": round(self.rate, 1),
            "started_at": round(self.start_rate, 1),
            "ceiling": round(self.ceiling, 1),
            "floor": round(self.floor, 1),
            "throttled": self.throttles,
            "speed_ups": self.increases,
            "adaptive": self.adaptive,
        }


class ProviderError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Provider(Protocol):
    name: str

    async def search_scopes(self, query: str) -> list[Scope]:
        """Find places to look — for Libby, libraries matching a name."""

    async def lookup(self, book: Book, scope: Scope, fmt: str, threshold: float) -> Availability:
        """Availability of one book, in one scope, in one format."""
