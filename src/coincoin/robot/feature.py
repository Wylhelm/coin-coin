"""Robot lifecycle, tool adapters and instruction context for the voice service."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from coincoin.realtime.tools.robot import install_tools, invalid_arguments
from coincoin.robot.governor import ROBOT_ERRORS, Governor, ToolResult
from coincoin.robot.link import RobotLink
from coincoin.robot.quack import QuackSink, QuackUnavailable
from coincoin.robot.tcp_link import TcpLink

if TYPE_CHECKING:
    from coincoin.app import App

LINK_LOST = "je ne sens plus mes pattes"


class NullQuack:
    """S5 placeholder: explicit journal evidence, never robot.sound in simulation."""

    def __init__(self, app: App) -> None:
        self.app = app

    async def quack(self, tag: str, hold_s: float = 0) -> None:
        self.app.journal.write("quack_stub", tag=tag, hold_s=hold_s)

    async def cancel(self) -> None:
        self.app.journal.write("quack_stub", action="cancel")


class RobotFeature:
    """Reusable installer; each App owns independent transport and safety tasks."""

    def __init__(self, link: RobotLink | None = None, quack: QuackSink | None = None) -> None:
        self._link = link
        self._quack = quack
        self.runtime: _RobotRuntime | None = None

    async def install(self, app: App) -> None:
        runtime = _RobotRuntime(self._link, self._quack)
        app.robot = runtime
        await runtime.install(app)
        self.runtime = runtime


class _RobotRuntime:
    def __init__(self, link: RobotLink | None = None, quack: QuackSink | None = None) -> None:
        self._provided_link = link
        self._provided_quack = quack
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False
        self._lost = False

    async def install(self, app: App) -> None:
        self.app = app
        self._closing = False
        self.link = self._provided_link or TcpLink(
            app.settings.robot_link,
            frame_url=app.settings.frame_link,
            token=app.settings.relay_token.get_secret_value(),
            clock=app.clock,
            on_up=self._up,
            on_down=self._down,
        )
        self.governor = Governor(self.link, app.settings, app.clock, app.journal)
        self.quack = self._provided_quack or NullQuack(app)
        self.contracts = install_tools(app, self.dispatch)
        app.instructions_context("skills", self.skills_context)
        app.on("stop", self.stop)
        app.on("reset_pose", self.reset_pose)
        app.on("mouth_close", self.mouth_close)
        app.on("shutdown", self.close)
        await self.governor.start()
        self._spawn(self._connect())

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _connect(self) -> None:
        try:
            await self.link.connect()
        except ROBOT_ERRORS:
            await self._down()

    async def _up(self) -> None:
        self._lost = False
        self.app.journal.write("state", robot="connected")

    async def _down(self) -> None:
        if self._closing or self._lost:
            return
        self._lost = True
        self.app.journal.write("error", component="robot", reason=LINK_LOST)
        self._spawn(self._announce_loss())

    async def _announce_loss(self) -> None:
        await self.governor.stop("robot link lost")
        await self.app.announce(LINK_LOST)

    async def skills_context(self) -> str:
        names: list[str] = []
        if self.link.connected:
            try:
                names = await self.governor.skills()
            except ROBOT_ERRORS as error:
                self.app.journal.write("error", component="robot skills", reason=str(error))
        self.contracts["do_skill"]["properties"]["name"]["enum"] = names
        return ", ".join(names) if names else "unavailable; do not invent skills"

    async def stop(self) -> None:
        await self.governor.stop("bouton")
        await self.quack.cancel()

    async def reset_pose(self) -> None:
        await self.governor.reset_pose()

    async def mouth_close(self) -> None:
        try:
            await self.governor.mouth(0)
        except ROBOT_ERRORS as error:
            self.app.journal.write("error", component="robot mouth", reason=str(error))

    async def close(self) -> None:
        self._closing = True
        await self.app._emit("voice_shutdown")
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        try:
            await self.governor.close()
        finally:
            await self.link.close()

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if reason := invalid_arguments(name, arguments, self.contracts[name]):
            result: dict[str, Any] = {"ok": False, "reason": reason}
        elif not self.link.connected:
            await self._down()
            result = {"ok": False, "reason": LINK_LOST}
        elif name == "quack":
            try:
                await self.quack.quack(**arguments)
                result = {"ok": True, "detail": "quack completed"}
            except QuackUnavailable as error:
                result = {"ok": False, "reason": str(error)}
            except (OSError, ValueError, TimeoutError, *ROBOT_ERRORS) as error:
                self.app.journal.write("error", component="quack", reason=str(error))
                result = {"ok": False, "reason": str(error)}
        else:
            if name == "stop":
                outcome = await self.governor.stop("outil stop")
                await self.quack.cancel()
                await self.app._emit("mouth_close")
            else:
                action = getattr(self.governor, name)
                outcome: ToolResult = await action(**arguments)
            result = (
                {key: value for key, value in asdict(outcome).items() if value not in ("", None)}
                if outcome.ok
                else {"ok": False, "reason": outcome.reason}
            )
            if not self.link.connected:
                await self._down()
                result = {"ok": False, "reason": LINK_LOST}
        text = f"{name}: {result.get('detail') if result['ok'] else result.get('reason')}"
        if result.get("clamped"):
            text += f" (clamped: {result['clamped']})"
        self.app.journal.write("robot_action", name=name, result=result)
        await self.app.notify_action(text, ok=result["ok"])
        return result
