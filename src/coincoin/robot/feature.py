"""Robot lifecycle, tool adapters and instruction context for the voice service."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Coroutine
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coincoin.realtime.tools.robot import (
    install_tools,
    invalid_arguments,
    tool_availability,
    tool_description,
)
from coincoin.realtime.tools.vision import install_tools as install_vision_tools
from coincoin.robot.capabilities import Capabilities, probe
from coincoin.robot.factory import create_robot_link
from coincoin.robot.governor import (
    NAV_MAX_S,
    ROBOT_ERRORS,
    ROLLER_MAX_VYAW,
    Governor,
    ImmediateToolResult,
    ToolResult,
    raised_note,
    turn_time_limit_s,
)
from coincoin.robot.link import RobotLink
from coincoin.robot.localization import (
    DepthSource,
    EstimatedPose,
    LinkTofStream,
    OraclePose,
    TcpTofStream,
)
from coincoin.robot.navigation import NavigationMap, Navigator, SimPose
from coincoin.robot.quack import QuackSink, QuackUnavailable
from coincoin.robot.webrtc_link import WebRtcLink

if TYPE_CHECKING:
    from coincoin.app import App

LINK_LOST = "je ne sens plus mes pattes"
_AWAITED_MOTIONS = frozenset({"move", "turn", "go_to"})


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
        self._sequence = asyncio.Lock()

    async def install(self, app: App) -> None:
        self.app = app
        self._closing = False
        self.link = (
            self._provided_link
            if self._provided_link is not None
            else create_robot_link(
                app.settings.robot_link,
                frame_url=app.settings.frame_link,
                token=app.settings.relay_token.get_secret_value(),
                clock=app.clock,
                on_up=self._up,
                on_down=self._down,
            )
        )
        self.governor = Governor(
            self.link,
            app.settings,
            app.clock,
            app.journal,
            navigator=self._navigator(app, self.link),
        )
        self.quack = self._provided_quack or NullQuack(app)
        self.contracts = install_tools(app, self.dispatch, places=self.governor.places())
        install_vision_tools(app, self.link)
        app.instructions_context("skills", self.skills_context)
        app.instructions_context("robot", self.robot_context)
        self.governor.on_notice = app.announce
        self._apply_capabilities(None)
        app.on("stop", self.stop)
        app.on("reset_pose", self.reset_pose)
        app.on("mouth_close", self.mouth_close)
        app.on("shutdown", self.close)
        await self.governor.start()
        self._spawn(self._connect())

    @staticmethod
    def _navigator(app: App, link: RobotLink) -> Navigator | None:
        settings = app.settings
        if not settings.nav_map:
            return None
        try:
            nav_map = NavigationMap.load(Path(settings.nav_map))
        except (OSError, ValueError, KeyError) as error:
            app.journal.write("error", component="navigation", reason=str(error))
            return None
        oracle = SimPose(settings.sim_pose_link) if settings.sim_pose_link else None
        localizer = None
        start = ""
        if settings.tof_link:
            depth: DepthSource
            if settings.tof_link == "link":
                if not isinstance(link, WebRtcLink):
                    app.journal.write(
                        "error",
                        component="navigation",
                        reason="TOF_LINK=link needs a WebRTC ROBOT_LINK",
                    )
                    return None
                depth = LinkTofStream(link)
            else:
                depth = TcpTofStream(settings.tof_link, settings.relay_token.get_secret_value())
            localizer = EstimatedPose(
                nav_map,
                link.state,
                depth,
                clock=app.clock.now,
            )
            try:
                start = localizer.start_at(settings.nav_start)
            except ValueError as error:
                app.journal.write("error", component="navigation", reason=str(error))
                return None
            localizer.start()
        if settings.nav_pose == "oracle":
            if oracle is None:
                app.journal.write(
                    "error", component="navigation", reason="NAV_POSE=oracle needs SIM_POSE_LINK"
                )
                return None
            source = OraclePose(SimPose(settings.sim_pose_link))
        elif localizer is None:
            app.journal.write(
                "error", component="navigation", reason="NAV_POSE=estimated needs TOF_LINK"
            )
            return None
        else:
            source = localizer
        app.journal.write(
            "state", navigation=sorted(nav_map.places), pose_source=source.name, start=start
        )
        return Navigator(nav_map, source, oracle=oracle, localizer=localizer)

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
        self._spawn(self._probe())

    async def _probe(self) -> None:
        try:
            capabilities = await probe(self.link)
        except ROBOT_ERRORS as error:
            self.app.journal.write("error", component="robot probe", reason=str(error))
            return
        self.governor.capabilities = capabilities
        self._apply_capabilities(capabilities)
        self.app.journal.write("robot_capabilities", **capabilities.as_dict())

    def _apply_capabilities(self, capabilities: Capabilities | None) -> None:
        # Tools and descriptions follow what this robot reported, never assumptions; the
        # registry snapshot is taken at the next Realtime session open.
        for name in self.contracts:
            reason = tool_availability(name, capabilities)
            if name == "go_to" and self.governor.navigator is None:
                reason = reason or "no navigation map configured"
            self.app.tools.configure(
                name,
                available=reason is None,
                reason=reason or "",
                description=tool_description(
                    name,
                    capabilities,
                    equipment_assisted=bool(self.governor.settings.sim_equipment_socket),
                ),
            )

    async def robot_context(self) -> str:
        capabilities = self.governor.capabilities
        if capabilities is None:
            return "robot capabilities not probed yet; tool results are authoritative"
        self._apply_capabilities(capabilities)
        parts = [
            f"link {capabilities.transport}",
            "simulated robot" if capabilities.simulated else "real robot",
            f"mode {capabilities.mode or 'unknown'}",
        ]
        if self.governor.settings.sim_equipment_socket:
            parts.append(
                "the simulator fits or removes the skates by itself when the mode changes; "
                "never ask a person for it"
            )
        elif capabilities.mode_switch_needs_person:
            parts.append(
                "changing walk/roller needs a person to fit or remove the skates and select the "
                "mode on the robot; say so when asked"
            )
        if self.governor.awaiting_person:
            parts.append(
                "mode change in progress; motion is disabled"
                if self.governor.settings.sim_equipment_socket
                else "waiting for that person now; motion is disabled"
            )
        return "; ".join(parts)

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
        await self.governor.cancel_person_wait("bouton")
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
                await self.governor.cancel_person_wait("outil stop")
                outcome = await self.governor.stop("outil stop")
                await self.quack.cancel()
                await self.app._emit("mouth_close")
            elif name == "status":
                outcome = await self.governor.status()
            else:
                outcome = await self._sequenced(name, arguments)
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
        if result.get("raised"):
            text += f" (raised: {result['raised']})"
        self.app.journal.write("robot_action", name=name, result=result)
        await self.app.notify_action(text, ok=result["ok"])
        return result

    async def _sequenced(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        # Actions run one at a time in call order so "walk, then turn, then sit" is not
        # collapsed into the last command; stop bypasses the queue and drops pending steps.
        generation = self.governor.stop_generation
        async with self._sequence:
            if self.governor.stop_generation != generation:
                return ToolResult(False, reason="cancelled by stop")
            timeout_s = self._execution_timeout_s(name, arguments)
            if not math.isfinite(timeout_s):
                reason = "robot action timeout is not finite"
                self.app.journal.write("tool_refused", method=name, reason=reason)
                return ToolResult(False, reason=reason)
            try:
                async with asyncio.timeout(timeout_s):
                    outcome: ToolResult = await getattr(self.governor, name)(**arguments)
                    if (
                        not outcome.ok
                        or name not in _AWAITED_MOTIONS
                        or isinstance(outcome, ImmediateToolResult)
                    ):
                        return outcome
                    finished = await self.governor.wait_motion()
                    if finished is None:
                        return ToolResult(False, reason="motion interrupted")
                    if not finished.ok:
                        return finished
                    finished = replace(finished, clamped=outcome.clamped, raised=outcome.raised)
                    if outcome.raised:
                        detail = f"{finished.detail}; {raised_note(outcome.raised)}"
                        finished = replace(finished, detail=detail)
                    return finished
            except TimeoutError:
                reason = f"Tool {name} timed out"
                self.app.journal.write("error", component="robot", method=name, reason=reason)
                stopped = await self.governor.stop(reason)
                if not stopped.ok:
                    reason += f"; stop failed: {stopped.reason}"
                return ToolResult(False, reason=reason)

    def _execution_timeout_s(self, name: str, arguments: dict[str, Any]) -> float:
        timeout_s = self.app.tools.timeout_s
        if name == "move":
            duration_s = max(0.0, min(arguments["duration_s"], self.app.settings.max_move_s))
            timeout_s += duration_s
        elif name == "turn":
            angle = min(abs(arguments["degrees"]), 360)
            slowest_rate = min(self.app.settings.max_vyaw, ROLLER_MAX_VYAW)
            timeout_s += turn_time_limit_s(angle, slowest_rate)
        elif name == "go_to":
            # A mode change, standing up and the whole route all fit inside this bound.
            timeout_s += NAV_MAX_S + 20
        return timeout_s
