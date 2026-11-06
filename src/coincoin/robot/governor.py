"""The sole safety boundary for all model-requested robot actuation."""

import asyncio
import contextlib
import math
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from coincoin.clock import Clock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.jsonrpc import JsonRpcError
from coincoin.robot.link import LinkDown, RobotLink, RobotRefused
from coincoin.robot.move_latency import MoveLatency

ROBOT_ERRORS = (RobotRefused, LinkDown, JsonRpcError, TimeoutError)
LOOK_POINTS = {
    "centre": (1.0, 0.0, 0.0),
    "gauche": (1.0, 1.0, 0.0),
    "droite": (1.0, -1.0, 0.0),
    "haut": (1.0, 0.0, 0.6),
    "bas": (1.0, 0.0, -0.4),
    "moi": (1.0, 0.0, 0.35),
}


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    detail: str = ""
    reason: str = ""
    clamped: dict[str, Any] | None = None


def _finite(*values: object) -> bool:
    return all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in values
    )


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


class Governor:
    def __init__(self, link: RobotLink, settings: Settings, clock: Clock, journal: Journal):
        self.link = link
        self.settings = settings
        self.clock = clock
        self.journal = journal
        self._motion: asyncio.Task[ToolResult] | None = None
        self._look: asyncio.Task[ToolResult] | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._mode_task: asyncio.Task[None] | None = None
        self._mode: str | None = None
        self._mode_at = -math.inf
        self._mode_lock = asyncio.Lock()
        self._action_lock = asyncio.Lock()
        self._action_calls: set[asyncio.Task[Any]] = set()
        self._stop_generation = 0
        self._mode_recovery_generation: int | None = None
        self._move_id = 0
        self._last_motion_command_at = -math.inf
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise LinkDown("Governor is closed")
        if self._monitor is None:
            self._monitor = asyncio.create_task(self._watch())

    async def close(self) -> None:
        if self._closed:
            return
        for task in (self._monitor, self._mode_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._monitor = self._mode_task = None
        await self.stop("governor closed")
        self._closed = True

    @property
    def moving(self) -> bool:
        return self._motion is not None and not self._motion.done()

    async def wait_motion(self) -> ToolResult | None:
        task = self._motion
        if task is None:
            return None
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                return ToolResult(False, reason="motion interrupted")
            raise

    async def wait_look(self) -> ToolResult:
        task = self._look
        if task is None:
            return ToolResult(False, reason="look interrupted")
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                return ToolResult(False, reason="look interrupted")
            raise

    def _refuse(self, method: str, reason: str) -> ToolResult:
        self.journal.write("tool_refused", method=method, reason=reason)
        return ToolResult(False, reason=reason)

    def _failure(
        self, method: str, error: RobotRefused | LinkDown | JsonRpcError | TimeoutError
    ) -> ToolResult:
        if isinstance(error, RobotRefused):
            self.journal.write("robot_refused", method=method, reason=error.reason)
            return ToolResult(False, reason=error.reason)
        reason = "robot request timed out" if isinstance(error, TimeoutError) else str(error)
        self.journal.write("error", component="governor", method=method, reason=reason)
        return ToolResult(False, reason=reason)

    def _safety_reason(self) -> str | None:
        if not self.link.connected:
            return "robot link down"
        state = self.link.state()
        age = self.link.state_age()
        if state is None or not math.isfinite(age) or age > 1:
            return "robot state stale"
        safety = state.get("safety")
        if (
            not isinstance(safety, dict)
            or type(safety.get("fallen")) is not bool
            or type(safety.get("picked_up", False)) is not bool
        ):
            return "robot safety state unavailable"
        if safety.get("picked_up", False):
            return "robot picked up"
        if safety["fallen"]:
            return "robot fallen"
        return None

    def _check_safety(self, generation: int, *, allow_fallen: bool = False) -> None:
        if generation != self._stop_generation or self._closed:
            raise RobotRefused("action cancelled by stop")
        if (reason := self._safety_reason()) and not (allow_fallen and reason == "robot fallen"):
            raise RobotRefused(reason)

    async def _refresh_mode(self) -> None:
        async with self._mode_lock:
            if self._mode is not None and self.clock.now() - self._mode_at < 30:
                return
            result = await self.link.call("robot.mode")
            if not isinstance(result, dict) or result.get("mode") not in ("walk", "roller"):
                self._mode = None
                raise RobotRefused("unsupported or unavailable robot mode")
            changed = self._mode is not None and self._mode != result["mode"]
            self._mode = result["mode"]
            self._mode_at = self.clock.now()
            if changed and self.moving:
                await self.stop("robot mode changed")

    async def _update_mode(self) -> None:
        try:
            await self._refresh_mode()
        except ROBOT_ERRORS as error:
            self._mode = None
            self._mode_at = self.clock.now()
            self._failure("robot.mode", error)
            await self.stop("robot mode unavailable")

    async def set_mode(self, mode: str) -> ToolResult:
        if mode not in ("walk", "roller"):
            return self._refuse("set_mode", "mode must be walk or roller")
        generation = self._stop_generation
        recovering = False
        try:
            await self.start()
            async with self._action_lock, self._mode_lock:
                before = await self._policies()
                simulated = before.get("simulated") is True
                self._check_safety(generation, allow_fallen=simulated)
                recovering = simulated and self._safety_reason() == "robot fallen"
                if recovering:
                    self._mode_recovery_generation = generation

                async def call(method: str, params: dict[str, Any] | None = None) -> Any:
                    return await self._action_call(
                        generation, method, params, allow_fallen=simulated
                    )

                await self._cancel(self._motion)
                self._motion = None
                await self._action_call(
                    generation, "robot.stop", timeout=0.1, allow_fallen=simulated
                )
                current = await call("robot.mode")
                if not isinstance(current, dict) or current.get("mode") not in ("walk", "roller"):
                    raise RobotRefused("unsupported or unavailable robot mode")
                if current["mode"] != mode or recovering:
                    was_sitting = before.get("sitting")
                    self._mode = None
                    await call("robot.setMode", {"mode": mode})
                    deadline = self.clock.now() + 10
                    settled_at: float | None = None
                    while True:
                        self._check_safety(generation, allow_fallen=simulated)
                        current = await call("robot.mode")
                        if not isinstance(current, dict) or current.get("mode") not in (
                            "walk",
                            "roller",
                        ):
                            raise RobotRefused("unsupported or unavailable robot mode")
                        policies = await call("robot.policies")
                        if (
                            not isinstance(policies, dict)
                            or type(policies.get("enabled")) is not bool
                        ):
                            raise RobotRefused("robot policies unavailable")
                        state = self.link.state()
                        if (
                            current.get("mode") == mode
                            and (not policies["enabled"] or policies.get("homed") is True)
                            and policies.get("sitting") == was_sitting
                            and (state is not None and state.get("policy") != "rise")
                            and self._safety_reason() is None
                        ):
                            if settled_at is None:
                                settled_at = self.clock.now()
                            if not recovering or self.clock.now() - settled_at >= 0.5:
                                health = await call("robot.health")
                                if (
                                    not isinstance(health, dict)
                                    or health.get("healthy") is not True
                                ):
                                    raise RobotRefused("robot unhealthy after mode change")
                                break
                        else:
                            settled_at = None
                        if self.clock.now() >= deadline:
                            raise RobotRefused("robot mode change did not complete within 10 s")
                        await self.clock.sleep(0.1)
                self._mode = mode
                self._mode_at = self.clock.now()
        except ROBOT_ERRORS as error:
            self._mode = None
            self._mode_at = -math.inf
            outcome = self._failure("set_mode", error)
            await self.stop("mode change failed")
            return outcome
        finally:
            if recovering and self._mode_recovery_generation == generation:
                self._mode_recovery_generation = None
        self.journal.write("tool_result", method="set_mode", mode=mode)
        return ToolResult(True, detail=f"locomotion mode: {mode}")

    async def _watch(self) -> None:
        last_reason: str | None = None
        was_connected = False
        while True:
            connected = self.link.connected
            if connected and not was_connected:
                self._mode = None
                self._mode_at = -math.inf
                try:
                    await self.mouth(0)
                except LinkDown as error:
                    self._failure("robot.mouth", error)
            reason = self._safety_reason()
            recovering = (
                reason == "robot fallen" and self._mode_recovery_generation == self._stop_generation
            )
            if reason is not None and reason != last_reason and not recovering:
                await self.stop(reason)
            last_reason = reason
            was_connected = connected
            if (
                connected
                and self.clock.now() - self._mode_at >= 30
                and (self._mode_task is None or self._mode_task.done())
            ):
                self._mode_task = asyncio.create_task(self._update_mode())
            await self.clock.sleep(0.1)

    def _limits(self) -> tuple[float, float, float]:
        if self._mode == "roller":
            return 0.3, 0.0, 0.3
        if self._mode != "walk":
            raise RobotRefused("robot mode unavailable")
        return self.settings.max_vx, self.settings.max_vy, self.settings.max_vyaw

    async def _action_call(
        self,
        generation: int,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,  # noqa: ASYNC109 - RobotLink contract
        *,
        allow_fallen: bool = False,
    ) -> Any:
        async def send() -> Any:
            self._check_safety(generation, allow_fallen=allow_fallen)
            return await self.link.call(method, params, timeout=timeout)

        self._check_safety(generation, allow_fallen=allow_fallen)
        task = asyncio.create_task(send())
        self._action_calls.add(task)
        try:
            result = await task
        except asyncio.CancelledError:
            if generation != self._stop_generation:
                raise RobotRefused("action cancelled by stop") from None
            raise
        finally:
            self._action_calls.discard(task)
        self._check_safety(generation, allow_fallen=allow_fallen)
        return result

    async def _policies(self, generation: int | None = None) -> dict[str, Any]:
        policies = (
            await self.link.call("robot.policies")
            if generation is None
            else await self._action_call(generation, "robot.policies")
        )
        if not isinstance(policies, dict) or type(policies.get("enabled")) is not bool:
            raise RobotRefused("robot policies unavailable")
        return policies

    async def _prepare(self, generation: int) -> dict[str, Any]:
        await self.start()
        self._check_safety(generation)
        await self._refresh_mode()
        policies = await self._policies(generation)
        self._check_safety(generation)
        if not policies["enabled"]:
            await self._action_call(generation, "robot.enable", {"on": True})
        deadline = self.clock.now() + 5
        while policies.get("homed") is not True or not policies["enabled"]:
            self._check_safety(generation)
            if self.clock.now() >= deadline:
                raise RobotRefused("robot did not reach home within 5 s")
            await self.clock.sleep(0.1)
            policies = await self._policies(generation)
        self._check_safety(generation)
        return policies

    async def _cancel(self, task: asyncio.Task[Any] | None) -> None:
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def stop(self, reason: str = "stop") -> ToolResult:
        self._stop_generation += 1
        motion, look = self._motion, self._look
        self._motion = self._look = None
        # Never wait for an action's RPC response or cleanup before sending STOP.
        for task in (*self._action_calls, motion, look):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        self.journal.write("state", action="stop", reason=reason)
        try:
            if not self.link.connected:
                return self._refuse("stop", "robot link down; relying on deadman")
            try:
                await self.link.call("robot.stop", timeout=0.1)
            except ROBOT_ERRORS as error:
                result = self._failure("stop", error)
            else:
                result = ToolResult(True, detail=reason)
            await self.mouth(0)
        except ROBOT_ERRORS as error:
            return self._failure("stop", error)
        finally:
            await self._cancel(motion)
            await self._cancel(look)
        return result

    async def _replace_motion(
        self, generation: int, run: Callable[[bool], Coroutine[Any, Any, ToolResult]]
    ) -> None:
        async with self._action_lock:
            self._check_safety(generation)
            replacing = self.moving
            await self._cancel(self._motion)
            self._check_safety(generation)
            self._motion = asyncio.create_task(run(replacing))

    async def move(self, vx: float, vy: float, vyaw: float, duration_s: float) -> ToolResult:
        requested_at = self.clock.now()
        if not _finite(vx, vy, vyaw, duration_s) or duration_s <= 0:
            return self._refuse("move", "velocities and positive duration must be finite")
        generation = self._stop_generation
        try:
            await self._prepare(generation)
            limits = self._limits()
            requested = {"vx": vx, "vy": vy, "vyaw": vyaw, "duration_s": duration_s}
            actual = {
                "vx": _clamp(vx, -limits[0], limits[0]),
                "vy": _clamp(vy, -limits[1], limits[1]),
                "vyaw": _clamp(vyaw, -limits[2], limits[2]),
                "duration_s": min(duration_s, self.settings.max_move_s),
            }
            clamped = {key: value for key, value in actual.items() if requested[key] != value}
            params = {key: actual[key] for key in ("vx", "vy", "vyaw")}
            self._move_id += 1
            move_id = self._move_id
            await self._replace_motion(
                generation,
                lambda replacing: self._run_move(
                    params, actual["duration_s"], generation, requested_at, move_id, replacing
                ),
            )
        except ROBOT_ERRORS as error:
            return self._failure("move", error)
        self.journal.write(
            "tool_call", method="move", move_id=move_id, requested=requested, actual=actual
        )
        return ToolResult(True, detail="movement started", clamped=clamped or None)

    async def _tick(self, next_tick: float, deadline: float) -> float:
        next_tick = max(next_tick, self.clock.now())
        await self.clock.sleep(max(0, min(next_tick, deadline) - self.clock.now()))
        return next_tick + 1 / self.settings.move_hz

    async def _run_move(
        self,
        params: dict[str, float],
        duration: float,
        generation: int,
        requested_at: float,
        move_id: int,
        replacing: bool,
    ) -> ToolResult:
        start = self.clock.now()
        deadline = start + duration
        next_tick = start + 1 / self.settings.move_hz
        observation: MoveLatency | None = None
        end_reason = "cancelled"
        try:
            while self.clock.now() < deadline:
                self._check_safety(generation)
                if observation is None:
                    observation = MoveLatency(
                        self.link,
                        self.clock,
                        self.journal,
                        move_id=move_id,
                        requested_at=requested_at,
                        params=params,
                        duration=duration,
                        replacing=replacing,
                        baseline_after=self._last_motion_command_at,
                    )
                else:
                    observation.observe()
                self._last_motion_command_at = self.clock.now()
                await self.link.notify("robot.move", params)
                self._check_safety(generation)
                if next_tick == start + 1 / self.settings.move_hz:
                    self.journal.write(
                        "latency",
                        method="move",
                        move_id=move_id,
                        move_to_first_command_ms=round((self.clock.now() - requested_at) * 1000, 3),
                    )
                observation.observe()
                next_tick = await self._tick(next_tick, deadline)
            self._check_safety(generation)
            if observation is not None:
                observation.observe()
            end_reason = "motion_ended"
            await self.link.call("robot.stop", timeout=0.1)
            await self.mouth(0)
            result = ToolResult(True, detail="movement completed")
            self.journal.write("tool_result", method="move", duration_s=self.clock.now() - start)
            return result
        except ROBOT_ERRORS as error:
            end_reason = "robot_error"
            result = self._failure("move", error)
            await self.stop(result.reason)
            return result
        finally:
            if observation is not None:
                observation.finish(end_reason)

    def _yaw(self) -> float:
        state = self.link.state()
        odom = state.get("odom") if state is not None else None
        yaw = odom.get("yaw") if isinstance(odom, dict) else None
        if not isinstance(yaw, (int, float)) or not _finite(yaw):
            raise RobotRefused("odometry yaw unavailable")
        return float(yaw)

    async def turn(self, degrees: float) -> ToolResult:
        if not _finite(degrees):
            return self._refuse("turn", "angle must be finite")
        generation = self._stop_generation
        angle = _clamp(degrees, -360, 360)
        try:
            await self._prepare(generation)
            yaw = self._yaw()
            max_rate = self._limits()[2]
            await self._replace_motion(
                generation, lambda replacing: self._run_turn(angle, yaw, max_rate, generation)
            )
        except ROBOT_ERRORS as error:
            return self._failure("turn", error)
        self.journal.write("tool_call", method="turn", degrees=angle)
        return ToolResult(
            True, detail="turn started", clamped={"degrees": angle} if angle != degrees else None
        )

    async def _run_turn(
        self, degrees: float, yaw: float, max_rate: float, generation: int
    ) -> ToolResult:
        target = math.radians(degrees)
        deadline = self.clock.now() + abs(target) / max_rate * 1.5 + 1
        next_tick = self.clock.now() + 1 / self.settings.move_hz
        progress = 0.0
        try:
            while True:
                self._check_safety(generation)
                current = self._yaw()
                progress += math.atan2(math.sin(current - yaw), math.cos(current - yaw))
                yaw = current
                remaining = target - progress
                if abs(remaining) <= math.radians(5):
                    break
                if self.clock.now() >= deadline:
                    raise RobotRefused("turn timed out")
                rate = (0.3 if abs(remaining) < math.radians(15) else 0.8) * max_rate
                self._last_motion_command_at = self.clock.now()
                await self.link.notify(
                    "robot.move", {"vx": 0.0, "vy": 0.0, "vyaw": math.copysign(rate, remaining)}
                )
                next_tick = await self._tick(next_tick, deadline)
            await self.link.call("robot.stop", timeout=0.1)
            await self.mouth(0)
            self.journal.write("tool_result", method="turn", degrees=math.degrees(progress))
            return ToolResult(True, detail=f"turned {math.degrees(progress):.1f} degrees")
        except ROBOT_ERRORS as error:
            result = self._failure("turn", error)
            await self.stop(result.reason)
            return result

    async def look(
        self,
        direction: str | None = None,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        hold_s: float = 3,
    ) -> ToolResult:
        if not _finite(hold_s) or hold_s < 0:
            return self._refuse("look", "hold duration must be finite and nonnegative")
        if direction is not None:
            if any(value is not None for value in (x, y, z)) or direction not in LOOK_POINTS:
                return self._refuse("look", "use one known direction or a complete point")
            x, y, z = LOOK_POINTS[direction]
        elif x is None and y is None and z is None:
            x, y, z = LOOK_POINTS["centre"]
        if not _finite(x, y, z):
            return self._refuse("look", "look point must have three finite coordinates")
        generation = self._stop_generation
        hold = min(10, hold_s)
        try:
            await self.start()
            self._check_safety(generation)
            async with self._action_lock:
                self._check_safety(generation)
                await self._cancel(self._look)
                self._look = None
                result = await self._action_call(generation, "robot.look", {"x": x, "y": y, "z": z})
                head = result.get("head") if isinstance(result, dict) else None
                names = ("neck_pitch", "head_pitch", "head_yaw", "head_roll")
                if not isinstance(head, dict) or not _finite(*(head.get(name) for name in names)):
                    raise RobotRefused("robot.look returned invalid head joints")
                self._check_safety(generation)
                joints = {name: head[name] for name in names}
                self._look = asyncio.create_task(self._hold_look(joints, hold, generation))
        except ROBOT_ERRORS as error:
            return self._failure("look", error)
        clamped: dict[str, Any] = {}
        if hold != hold_s:
            clamped["hold_s"] = hold
        if result.get("clamped") is True:
            clamped["head"] = joints
        self.journal.write("tool_call", method="look", direction=direction, hold_s=hold)
        return ToolResult(True, detail="looking", clamped=clamped or None)

    async def _hold_look(self, head: dict[str, float], hold: float, generation: int) -> ToolResult:
        deadline = self.clock.now() + hold
        try:
            while self.clock.now() < deadline:
                self._check_safety(generation)
                await self.link.notify("robot.head", head)
                await self.clock.sleep(min(0.2, max(0, deadline - self.clock.now())))
            return ToolResult(True, detail="look completed")
        except ROBOT_ERRORS as error:
            result = self._failure("look", error)
            await self.stop("look interrupted")
            return result

    async def set_pose(self, z: float, roll: float, pitch: float) -> ToolResult:
        if not _finite(z, roll, pitch):
            return self._refuse("set_pose", "pose must be finite")
        generation = self._stop_generation
        requested = {"z": z, "roll": roll, "pitch": pitch}
        actual = {
            "z": _clamp(z, -0.025, 0.010),
            "roll": _clamp(roll, -0.26, 0.26),
            "pitch": _clamp(pitch, -0.26, 0.26),
        }
        try:
            await self._prepare(generation)
            self._check_safety(generation)
            await self.link.notify("robot.pose", {**actual, "active": True})
        except ROBOT_ERRORS as error:
            return self._failure("set_pose", error)
        clamped = {key: value for key, value in actual.items() if requested[key] != value}
        self.journal.write("tool_call", method="set_pose", actual=actual)
        return ToolResult(True, detail="pose set", clamped=clamped or None)

    async def reset_pose(self) -> ToolResult:
        try:
            await self.link.notify("robot.pose", {"active": False})
        except ROBOT_ERRORS as error:
            return self._failure("reset_pose", error)
        self.journal.write("tool_call", method="reset_pose")
        return ToolResult(True, detail="pose reset")

    async def mouth(self, open_: float) -> None:
        if not _finite(open_):
            self.journal.write(
                "error", component="governor", method="mouth", reason="invalid opening"
            )
            raise ValueError("Mouth opening must be finite")
        await self.link.notify("robot.mouth", {"open": _clamp(open_, 0, 1)})

    async def skills(self) -> list[str]:
        result = await self.link.call("robot.skills")
        if not isinstance(result, dict) or not isinstance(result.get("skills"), list):
            raise RobotRefused("robot skill list unavailable")
        names: list[str] = []
        for skill in result["skills"]:
            if not isinstance(skill, dict) or not isinstance(skill.get("name"), str):
                raise RobotRefused("invalid robot skill list")
            names.append(skill["name"])
        built_in = result.get("built_in", [])
        if not isinstance(built_in, list) or any(not isinstance(name, str) for name in built_in):
            raise RobotRefused("invalid built-in skill list")
        return list(dict.fromkeys(name for name in [*names, *built_in] if name != "sit_toggle"))

    async def do_skill(self, name: str) -> ToolResult:
        generation = self._stop_generation
        try:
            names = await self.skills()
            if name not in names:
                return self._refuse("do_skill", f"unknown skill; available: {', '.join(names)}")
            await self._prepare(generation)
            async with self._action_lock:
                self._check_safety(generation)
                await self._cancel(self._motion)
                self._motion = None
                await self._action_call(generation, "robot.stop", timeout=0.1)
                result = await self._action_call(generation, "robot.do", {"skill": name})
        except ROBOT_ERRORS as error:
            return self._failure("do_skill", error)
        self.journal.write("tool_result", method="do_skill", name=name, result=result)
        return ToolResult(True, detail=f"skill {name} accepted")

    async def _set_sitting(self, sitting: bool) -> ToolResult:
        method = "sit" if sitting else "stand"
        generation = self._stop_generation
        try:
            policies = await self._policies(generation)
            current = policies.get("sitting")
            if type(current) is not bool:
                return self._refuse(method, "je ne sais pas si je suis assis")
            if current is sitting:
                return ToolResult(True, detail=f"already {'sitting' if sitting else 'standing'}")
            await self._prepare(generation)
            async with self._action_lock:
                policies = await self._policies(generation)
                current = policies.get("sitting")
                if type(current) is not bool:
                    return self._refuse(method, "je ne sais pas si je suis assis")
                self._check_safety(generation)
                if current is not sitting:
                    await self._cancel(self._motion)
                    self._motion = None
                    await self._action_call(generation, "robot.stop", timeout=0.1)
                    await self._action_call(generation, "robot.do", {"skill": "sit_toggle"})
        except ROBOT_ERRORS as error:
            return self._failure(method, error)
        self.journal.write("tool_result", method=method)
        return ToolResult(True, detail=f"{method} accepted")

    async def sit(self) -> ToolResult:
        return await self._set_sitting(True)

    async def stand(self) -> ToolResult:
        if self._safety_reason() == "robot fallen":
            try:
                policies = await self._policies()
                if policies.get("simulated") is True:
                    mode = await self.link.call("robot.mode")
                    if not isinstance(mode, dict) or mode.get("mode") not in ("walk", "roller"):
                        raise RobotRefused("unsupported or unavailable robot mode")
                    return await self.set_mode(mode["mode"])
            except ROBOT_ERRORS as error:
                return self._failure("stand", error)
        return await self._set_sitting(False)

    async def status(self) -> ToolResult:
        try:
            policies = await self._policies()
            health = await self.link.call("robot.health")
            mode = await self.link.call("robot.mode")
        except ROBOT_ERRORS as error:
            return self._failure("status", error)
        state = self.link.state()
        safety = state.get("safety", {}) if state is not None else {}
        sitting = policies.get("sitting")
        posture = "sitting" if sitting is True else "standing" if sitting is False else "unknown"
        mode_name = mode.get("mode", "unknown") if isinstance(mode, dict) else "unknown"
        self.journal.write("robot_status", state=state, policies=policies, health=health, mode=mode)
        if state is None or self.link.state_age() > 1:
            summary = "etat perime / stale state"
        elif safety.get("fallen") is True:
            summary = "je suis tombe / fallen"
        elif safety.get("picked_up") is True:
            summary = "je suis souleve / picked up"
        elif safety.get("fallen") is not False or safety.get("picked_up") is not False:
            summary = "securite inconnue / unknown safety"
        else:
            summary = {"sitting": "assis", "standing": "debout", "unknown": "inconnu"}[posture]
            summary += f" / {posture}"
        healthy = isinstance(health, dict) and health.get("healthy") is True
        all_clear = (
            healthy
            and policies["enabled"]
            and policies.get("homed") is True
            and state is not None
            and self.link.state_age() <= 1
            and safety.get("fallen") is False
            and safety.get("picked_up") is False
            and mode_name in ("walk", "roller")
        )
        summary += f", mode {mode_name}, "
        summary += "tout va bien" if all_clear else "etat a verifier"
        if self.moving:
            summary += ", mouvement en cours / motion active"
        return ToolResult(
            True,
            detail=summary,
        )
