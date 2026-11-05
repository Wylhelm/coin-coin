"""Cancanement output contract shared by streamed and robot sounds."""

from __future__ import annotations

import asyncio
import math
import random
import wave
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
import soxr

from coincoin.audio.mixer import OutputMixer
from coincoin.audio.resample import float32_to_pcm16, pcm16_to_float32
from coincoin.clock import Clock
from coincoin.robot.link import RobotLink

TAGS = frozenset({"greet", "inquire", "chirp", "coo", "alarm", "peck", "wheee"})


class QuackUnavailable(ValueError):
    """The already-journaled bank failure disables only quacks."""


def _validate(tag: str, hold_s: float) -> None:
    if tag not in TAGS or not math.isfinite(hold_s) or not 0 <= hold_s <= 3:
        raise ValueError("Invalid quack tag or hold duration")
    if tag != "wheee" and hold_s:
        raise ValueError("Only wheee supports hold_s")


@runtime_checkable
class QuackSink(Protocol):
    """Play a tagged sound without exposing where the audio is rendered."""

    async def quack(self, tag: str, hold_s: float = 0.0) -> None:
        """Play tag, holding it for hold_s seconds when the tag supports holds."""
        ...

    async def cancel(self) -> None:
        """Immediately cancel the current cancanement."""
        ...


class StreamQuack:
    def __init__(
        self,
        mixer: OutputMixer,
        directory: Path,
        speaking: Callable[[bool], Awaitable[None]],
        error: Callable[[str], None],
    ) -> None:
        self.mixer = mixer
        self.speaking = speaking
        self.error = error
        self._bank: dict[str, list[bytes]] = {}
        self._task: asyncio.Task[None] | None = None
        for tag in TAGS:
            for path in sorted((directory / tag).glob("*.wav")):
                try:
                    pcm = self._load(path)
                    self._bank.setdefault(tag, []).append(pcm)
                except (OSError, ValueError, wave.Error) as exc:
                    self.error(f"Cannot load {path.name}: {exc}")
        if not self._bank:
            self.error("Sound bank absent or empty; quacks disabled")

    @staticmethod
    def _load(path: Path) -> bytes:
        with wave.open(str(path), "rb") as wav:
            channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
            raw = wav.readframes(wav.getnframes())
        if width != 2 or not raw or len(raw) % (2 * channels):
            raise ValueError("Sound bank requires nonempty PCM16 WAV files")
        samples = pcm16_to_float32(raw).reshape(-1, channels).mean(axis=1)
        if rate != 24_000:
            samples = soxr.resample(samples, rate, 24_000).astype(np.float32)
        return float32_to_pcm16(samples)

    async def quack(self, tag: str, hold_s: float = 0) -> None:
        _validate(tag, hold_s)
        if not self._bank:
            raise QuackUnavailable("Sound bank unavailable; quacks disabled")
        if tag not in self._bank:
            raise ValueError(f"No sound available for {tag}")
        await self.cancel()
        pcm = random.choice(self._bank[tag])
        if tag == "wheee" and hold_s:
            samples = len(pcm) // 2
            start, end = samples // 3 * 2, samples * 2 // 3 * 2
            middle = pcm[start:end] or pcm
            target = round(hold_s * 24_000) * 2
            center_length = max(0, target - start - (len(pcm) - end))
            center = (middle * (center_length // len(middle) + 1))[:center_length]
            pcm = (pcm[:start] + center + pcm[end:])[:target]
        task = asyncio.create_task(self._play(pcm))
        self._task = task
        try:
            await task
        finally:
            if self._task is task:
                self._task = None

    async def _play(self, pcm: bytes) -> None:
        await self.speaking(True)
        try:
            await self.mixer.quack(pcm)
        except asyncio.CancelledError:
            await self.mixer.cancel_quack()
            raise
        finally:
            await self.speaking(False)

    async def cancel(self) -> None:
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._task = None
        await self.mixer.cancel_quack()


class RobotQuack:
    def __init__(
        self,
        link: RobotLink,
        clock: Clock,
        speaking: Callable[[bool], Awaitable[None]],
    ) -> None:
        self.link = link
        self.clock = clock
        self.speaking = speaking
        self._task: asyncio.Task[None] | None = None
        self._held = False

    async def quack(self, tag: str, hold_s: float = 0) -> None:
        _validate(tag, hold_s)
        await self.cancel()
        self._task = asyncio.current_task()
        await self.speaking(True)
        try:
            if tag != "wheee":
                await self.link.notify("robot.sound", {"tag": tag})
            else:
                self._held = True
                end = self.clock.now() + hold_s
                while True:
                    await self.link.notify("robot.sound", {"tag": tag, "hold": True})
                    if self.clock.now() >= end:
                        break
                    await self.clock.sleep(min(0.1, end - self.clock.now()))
        finally:
            self._task = None
            if self._held:
                self._held = False
                await self.link.notify("robot.sound", {"tag": "wheee", "hold": False})
            await self.speaking(False)

    async def cancel(self) -> None:
        task = self._task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
