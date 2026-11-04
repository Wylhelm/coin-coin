"""Injectable monotonic clocks, including a manually advanced test clock."""

import asyncio
import heapq
import itertools
import math
import time
from typing import Protocol, runtime_checkable


def _check_duration(s: float) -> None:
    if not math.isfinite(s) or s < 0:
        raise ValueError("Duration must be finite and nonnegative")


@runtime_checkable
class Clock(Protocol):
    """Provide monotonic seconds and asynchronous waits."""

    def now(self) -> float:
        """Return the current monotonic time in seconds."""
        ...

    async def sleep(self, s: float) -> None:
        """Wait for s monotonic seconds without blocking the event loop."""
        ...


class SystemClock:
    """Use the system monotonic clock and asyncio scheduler."""

    def now(self) -> float:
        """Return monotonic seconds, independent of wall-clock changes."""
        return time.monotonic()

    async def sleep(self, s: float) -> None:
        """Wait for a finite, nonnegative duration."""
        _check_duration(s)
        await asyncio.sleep(s)


class FakeClock:
    """Advance manually; release due sleeps in deadline, then insertion order."""

    def __init__(self, start: float = 0.0) -> None:
        if not math.isfinite(start):
            raise ValueError("Initial time must be finite")
        self._now = start
        self._sequence = itertools.count()
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []

    def now(self) -> float:
        """Return the manually controlled monotonic time in seconds."""
        return self._now

    async def sleep(self, s: float) -> None:
        """Wait until advance() reaches the deadline; zero yields one loop turn."""
        _check_duration(s)
        if s == 0:
            await asyncio.sleep(0)
            return
        if not math.isfinite(self._now + s):
            raise ValueError("Sleep deadline must be finite")
        future = asyncio.get_running_loop().create_future()
        sleeper = (self._now + s, next(self._sequence), future)
        heapq.heappush(self._sleepers, sleeper)
        try:
            await future
        finally:
            if future.cancelled() and sleeper in self._sleepers:
                self._sleepers.remove(sleeper)
                heapq.heapify(self._sleepers)

    def advance(self, s: float) -> None:
        """Advance without real waiting and schedule all now-due sleepers."""
        _check_duration(s)
        if not math.isfinite(self._now + s):
            raise ValueError("Resulting time must be finite")
        self._now += s
        while self._sleepers and self._sleepers[0][0] <= self._now:
            _, _, future = heapq.heappop(self._sleepers)
            if not future.done():
                future.set_result(None)
