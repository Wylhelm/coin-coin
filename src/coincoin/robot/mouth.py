"""Playback-clocked RMS envelope for the persistent robot mouth intention."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import Awaitable, Callable

import numpy as np

from coincoin.audio.resample import pcm16_to_float32
from coincoin.clock import Clock


class MouthAnimator:
    def __init__(
        self,
        mouth: Callable[[float], Awaitable[None]],
        played_ms: Callable[[], int],
        clock: Clock,
    ) -> None:
        self._mouth = mouth
        self._played_ms = played_ms
        self.clock = clock
        self._queue: deque[tuple[float, float]] = deque()
        self._pending = b""
        self._pending_ms = 0.0
        self._last_end_ms: float | None = None
        self._opening = 0.0
        self._task: asyncio.Task[None] | None = None
        self._repeat: asyncio.Task[None] | None = None
        self._generation = 0

    def feed(self, pcm: bytes, start_ms: float) -> None:
        if not pcm:
            return
        self._generation += 1
        if self._repeat is not None:
            self._repeat.cancel()
            self._repeat = None
        if self._last_end_ms is not None and start_ms < self._last_end_ms:
            self._queue.clear()
            self._pending = b""
            self._opening = 0.0
            if self._task is not None:
                self._task.cancel()
                self._task = None
        if self._pending and abs(start_ms - (self._pending_ms + len(self._pending) / 48)) > 1:
            self._pending = b""
        if not self._pending:
            self._pending_ms = start_ms
        self._pending += pcm
        self._last_end_ms = start_ms + len(pcm) / 48
        while len(self._pending) >= 1920:
            block, self._pending = self._pending[:1920], self._pending[1920:]
            samples = pcm16_to_float32(block)
            rms = float(np.sqrt(np.mean(samples * samples)))
            db = 20 * math.log10(max(rms, 1e-10))
            target = min(1.0, max(0.0, (db + 45) / 30)) ** 0.7 * 0.9
            tau = 0.03 if target > self._opening else 0.09
            smoothed = self._opening + (target - self._opening) * (1 - math.exp(-0.04 / tau))
            self._opening += max(-0.3, min(0.3, smoothed - self._opening))
            self._queue.append((self._pending_ms, self._opening))
            self._pending_ms += 40
        if self._queue and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while self._queue:
            played = self._played_ms()
            while len(self._queue) > 1 and played > self._queue[1][0]:
                self._queue.popleft()
            position, opening = self._queue[0]
            if played <= position:
                await self.clock.sleep(0.01)
                continue
            self._queue.popleft()
            await self._mouth(opening)
            await self.clock.sleep(0.04)

    async def close(self) -> None:
        self._generation += 1
        generation = self._generation
        tasks = [task for task in (self._task, self._repeat) if task is not None]
        for task in tasks:
            task.cancel()
        self._task = self._repeat = None
        self._queue.clear()
        self._pending = b""
        self._opening = 0
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._generation != generation:
            return
        await self._mouth(0)
        if self._generation == generation:
            self._repeat = asyncio.create_task(self._close_again(self.clock.now() + 0.2))

    async def _close_again(self, deadline: float) -> None:
        await self.clock.sleep(max(0, deadline - self.clock.now()))
        await self._mouth(0)

    async def shutdown(self) -> None:
        await self.close()
        if self._repeat is not None:
            self._repeat.cancel()
            await asyncio.gather(self._repeat, return_exceptions=True)
