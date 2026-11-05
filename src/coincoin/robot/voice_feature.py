"""Mouth and quack lifecycle wiring; all simulation sounds share browser output."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from coincoin.audio.mixer import OutputMixer
from coincoin.robot.governor import ROBOT_ERRORS
from coincoin.robot.mouth import MouthAnimator
from coincoin.robot.quack import QuackUnavailable, RobotQuack, StreamQuack
from coincoin.statemachine import Event

if TYPE_CHECKING:
    from coincoin.app import App


class VoiceFeature:
    """Reusable installer; each App receives its own voice runtime."""

    def __init__(self, sound_directory: Path = Path("assets/sounds")) -> None:
        self.sound_directory = sound_directory

    async def install(self, app: App) -> None:
        runtime = _VoiceRuntime(self.sound_directory)
        await runtime.install(app)


class _VoiceRuntime:
    def __init__(self, sound_directory: Path) -> None:
        self.sound_directory = sound_directory
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_inquire = float("-inf")
        self._closing = False
        self._quack_active = False

    async def install(self, app: App) -> None:
        if app.robot is None:
            raise RuntimeError("VoiceFeature requires RobotFeature first")
        self.app = app
        physical = app.audio
        self.mouth = MouthAnimator(
            self._mouth,
            lambda: physical.played_ms,
            app.clock,
        )
        self.mixer = OutputMixer(physical, app.clock, self.mouth.feed)
        app.audio = self.mixer
        app.robot.quack = (
            StreamQuack(
                self.mixer,
                self.sound_directory,
                self._speaking,
                lambda reason: app.journal.write("error", component="quack", reason=reason),
            )
            if app.settings.quack_sink == "stream"
            else RobotQuack(app.robot.link, app.clock, self._speaking)
        )
        app.on("quack", self.automatic)
        app.on("mouth_close", self.mouth.close)
        app.on("barge_in", self.cancel)
        app.on("voice_shutdown", self.close)
        app.on("stop", self.cancel)

    async def _mouth(self, opening: float) -> None:
        assert self.app.robot is not None
        try:
            await self.app.robot.governor.mouth(opening)
        except ROBOT_ERRORS as exc:
            self.app.journal.write("error", component="mouth", reason=str(exc))

    async def _speaking(self, active: bool) -> None:
        self._quack_active = active
        if not self._closing:
            await self.app._apply(Event("quack_started" if active else "quack_done"))

    async def automatic(self, tag: str) -> None:
        if tag == "inquire":
            if self.app.clock.now() - self._last_inquire < 30:
                if not self._quack_active:
                    await self._speaking(False)
                return
            self._last_inquire = self.app.clock.now()
        task = asyncio.create_task(self._automatic(tag))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _automatic(self, tag: str) -> None:
        assert self.app.robot is not None
        try:
            await self.app.robot.quack.quack(tag)
        except QuackUnavailable:
            if not self._quack_active:
                await self._speaking(False)
        except (OSError, ValueError, TimeoutError, *ROBOT_ERRORS) as exc:
            self.app.journal.write("error", component="quack", reason=str(exc))
            if not self._quack_active:
                await self._speaking(False)

    async def cancel(self) -> None:
        assert self.app.robot is not None
        await self.app.robot.quack.cancel()
        await self.mouth.close()

    async def close(self) -> None:
        self._closing = True
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        assert self.app.robot is not None
        await self.app.robot.quack.cancel()
        await self.mouth.shutdown()
