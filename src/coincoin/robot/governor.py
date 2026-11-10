"""The sole safety boundary for all model-requested robot actuation."""

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from coincoin.clock import Clock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.capabilities import Capabilities, refused_by_route, transport_of
from coincoin.robot.jsonrpc import JsonRpcError
from coincoin.robot.link import LinkDown, RobotLink, RobotRefused
from coincoin.robot.localization import (
    LOST_XY_M,
    LOST_YAW_RAD,
    RELOC_JUMP_M,
    RELOC_JUMP_RAD,
    TRACK_OBSTACLE_EXPLAINED_M,
    TRACK_OBSTACLE_MIN_HITS,
    TRACK_OBSTACLE_VIEW_M,
    EstimatedPose,
    Odom,
    PoseEstimate,
    Scan,
    blocking_points,
    obstacle_points,
    unexplained_points,
)
from coincoin.robot.move_latency import MoveLatency
from coincoin.robot.navigation import Navigator, Pose, Steering
from coincoin.robot.sim_equipment import select_mode

LOGGER = logging.getLogger(__name__)
ROBOT_ERRORS = (RobotRefused, LinkDown, JsonRpcError, TimeoutError)
# robot.health is polled this often while connected; a degraded or unhealthy verdict refuses
# motion (and stops a running one) until the robot reports healthy again.
HEALTH_POLL_S = 5.0
# Navigation to a named place: a route has its own bound instead of max_move_s, because crossing
# the apartment takes longer than one move. Stop, safety and link checks still run every tick.
NAV_MAX_S = 300.0
NAV_STALL_S = 12.0
NAV_STATIONARY_S = 4.0
NAV_REPLANS = 3
# Honest navigation: when the pose estimate is lost, stop, sweep the head and try to relocalize
# this many times per route before giving up and saying so.
NAV_RELOCALIZATIONS = 2
NAV_UNCERTAIN_S = 3.0
# A relocalization searches around the last estimate within this radius (and heading tolerance).
# With no usable estimate at all it searches the whole flat, and then refuses an ambiguous match
# rather than guessing (docs/localization.md).
RELOC_RADIUS_M = (0.6, 1.5)
RELOC_PRIOR_MAX_M = 1.0
RELOC_HEADING_RAD = (0.35, 1.0)
# Share of the odometry travel and rotation since the last fix that the match window allows.
RELOC_DRIFT_FRACTION = 0.1
# Head sweep for relocalization: wide composites relocalize, narrow wedges do not (microduck#127).
# The head turns +-170 degrees; a quarter of the flat can hide in the 45 degree ToF wedge, so the
# sweep goes as wide as the neck allows in 25 degree steps.
SWEEP_DEGREES = (-150, -125, -100, -75, -50, -25, 0, 25, 50, 75, 100, 125, 150)
SWEEP_HOLD_S = 0.9
# Said when a whole-flat sweep matched two places equally well: never walk on a guess.
LOCATION_AMBIGUOUS = (
    "I cannot tell where I am without risking a mistake: another place fits what I see as well, "
    "so put me back on the dock or tell me where I am"
)
# Obstacles the map does not hold: stop when ToF returns sit on the next stretch of route. The
# stretch covers the duck's radius, the distance walked during the sensor's latency (frame period,
# state lag and one control tick) and braking, plus a margin. Two scans in a row must agree.
NAV_TOF_LATENCY_S = 0.3
NAV_BRAKE_M = 0.1
NAV_OBSTACLE_MARGIN_M = 0.15
NAV_OBSTACLE_SCANS = 2
NAV_OBSTACLE_REPLANS = 4
# Unmapped returns this far along the path excuse a low map fit for a while.
NAV_OBSTACLE_VIEW_M = TRACK_OBSTACLE_VIEW_M
NAV_OBSTACLE_MARK_M = 2.0
# Any return this close ahead of the body stops the duck, mapped or not.
NAV_BUMPER_M = 0.06
# The ToF only sees the near face of a thing; assume it is this deep so a detour clears its back.
NAV_OBSTACLE_DEPTH_M = 0.3
NAV_OBSTACLE_UNCERTAIN_S = 8
ROLLER_MAX_VYAW = 0.3
# robotd clears `sitting` when the rise starts; the gait ignores twists until it ends.
STAND_SETTLE_S = 1.5
STAND_TIMEOUT_S = 6.0
# The walk policy (velstand, policy set v9) stays put under small commands: it does not step
# below roughly vx 0.2 m/s or vyaw 0.8 rad/s. Non-zero walking commands below Settings.min_vx /
# min_vy / min_vyaw are therefore raised to those floors (never above the maximum caps), and the
# tool result says so. With walk_assist (simulator aid) each walking move also opens with a
# short, stronger starter (still inside the policy's trained range of vx +-0.4, vy +-0.3,
# vyaw +-1.0).
WALK_START_S = 0.5
# A turn at the yaw floor can overshoot its 5 degree tolerance; accept a small overshoot rather
# than hunting back and forth at a rate the gait cannot reduce.
TURN_TOLERANCE_DEG = 5.0
TURN_OVERSHOOT_DEG = 12.0
UNITS = {"vx": "m/s", "vy": "m/s", "vyaw": "rad/s"}


def raised_note(raised: dict[str, float]) -> str:
    """Tell the model/user that a speed was raised to the walking gait's minimum."""
    values = ", ".join(f"{key} {abs(value):g} {UNITS[key]}" for key, value in raised.items())
    return (
        f"speed raised to the minimum the walking gait follows ({values}): "
        "slower commands leave the duck standing still"
    )


def walk_starter(params: dict[str, float]) -> dict[str, float]:
    vx, vy, vyaw = params["vx"], params["vy"], params["vyaw"]
    starter = dict(params)
    if vx > 0:
        starter["vx"] = max(vx, 0.25)
    elif vx < 0:
        starter["vx"] = -0.4
    if vy:
        starter["vy"] = math.copysign(0.3, vy)
        if not vx and not vyaw:
            starter["vyaw"] = math.copysign(0.6, vy)
    if vyaw:
        starter["vyaw"] = math.copysign(max(abs(vyaw), 1.0), vyaw)
    return starter


FALLEN_NEEDS_PERSON = (
    "robot fallen; assisted get-up needs robot.setMode, which this link refuses: "
    "a person must put the duck back on its feet"
)
EQUIPMENT = {"roller": "fit the skates", "walk": "remove the skates"}
PERSON_POLL_S = 0.5
ROLLER_TRIP_REFUSED = (
    "I only find my way on foot right now: on skates my wheels report no motion, and the depth "
    "camera is giving me no glide I can trust, so I would lose track of where I am. Not switching "
    "by myself: offer to switch to walk mode first (set_mode walk), then go_to again"
)


def person_wait_reason(mode: str, *, assisted: bool = False) -> str:
    if assisted:
        return (
            f"the simulator is switching to {mode} mode ({EQUIPMENT[mode]}); "
            "motion is disabled until the robot reports that mode"
        )
    return (
        f"waiting for a person to {EQUIPMENT[mode]} and select {mode} mode on the robot; "
        "motion is disabled until the robot reports that mode"
    )


class _NeedsPerson(Exception):
    """robot.setMode was refused by the transport: only someone at the robot can change mode."""


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
    # Non-zero speeds raised to the gait's minimum (Settings.min_*), with their sent values.
    raised: dict[str, float] | None = None


class ImmediateToolResult(ToolResult):
    """A completed action that did not start a motion task."""


def _finite(*values: object) -> bool:
    return all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in values
    )


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def turn_time_limit_s(degrees: float, max_rate: float) -> float:
    """Return the governor's maximum turning time for an angle and angular rate."""
    return abs(math.radians(degrees)) / max_rate * 1.5 + 1


class Governor:
    def __init__(
        self,
        link: RobotLink,
        settings: Settings,
        clock: Clock,
        journal: Journal,
        navigator: Navigator | None = None,
    ):
        self.link = link
        self.settings = settings
        self.clock = clock
        self.journal = journal
        self.navigator = navigator
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
        # What the connected robot allows; set on every link-up by the runtime.
        self.capabilities: Capabilities | None = None
        # Spoken notices outside a tool call (person-assisted mode change finished or timed out).
        self.on_notice: Callable[[str], Awaitable[None]] | None = None
        self._person_target: str | None = None
        self._person_task: asyncio.Task[None] | None = None
        self._health_problem: str | None = None
        self._health_at = -math.inf
        self._health_task: asyncio.Task[None] | None = None
        # Navigation state for status: going to / arrived at / failed to reach a place.
        self.navigation = ""
        # A move or turn ran outside go_to's checks since the last head-sweep fix.
        self._unsupervised_motion = False
        self._blocked_anchor: tuple[Pose, Odom | None] | None = None

    async def start(self) -> None:
        if self._closed:
            raise LinkDown("Governor is closed")
        if self._monitor is None:
            self._monitor = asyncio.create_task(self._watch())

    async def close(self) -> None:
        if self._closed:
            return
        for task in (self._monitor, self._mode_task, self._person_task, self._health_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._monitor = self._mode_task = self._person_task = self._health_task = None
        self._person_target = None
        await self.stop("governor closed")
        if self.navigator is not None:
            await self.navigator.close()
        self._closed = True

    @property
    def stop_generation(self) -> int:
        return self._stop_generation

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
        return self._health_problem

    async def _update_health(self) -> None:
        """Poll robot.health; degraded or unhealthy refuses motion until it clears."""
        try:
            health = await self.link.call("robot.health", timeout=2.0)
        except ROBOT_ERRORS as error:
            # Unknown is not a verdict: keep the last one, the state stream still gates motion.
            LOGGER.debug("robot.health poll failed: %s", error)
            return
        finally:
            self._health_at = self.clock.now()
        problem = None
        if isinstance(health, dict) and health.get("healthy") is False:
            reason = health.get("reason")
            verdict = "degraded" if health.get("degraded") is True else "unhealthy"
            problem = f"robot {verdict}" + (f" ({reason})" if isinstance(reason, str) else "")
            problem += "; motion refused until it reports healthy"
        if problem != self._health_problem:
            self.journal.write("robot_health", problem=problem, health=health)
            if problem:
                LOGGER.warning("%s", problem)
        self._health_problem = problem

    def _check_safety(self, generation: int, *, allow_fallen: bool = False) -> None:
        if generation != self._stop_generation or self._closed:
            raise RobotRefused("action cancelled by stop")
        if self._person_target is not None:
            raise RobotRefused(
                person_wait_reason(
                    self._person_target, assisted=bool(self.settings.sim_equipment_socket)
                )
            )
        if (reason := self._safety_reason()) and not (allow_fallen and reason == "robot fallen"):
            raise RobotRefused(reason)

    def _accept_mode(self, mode: str) -> None:
        if self._mode is not None and self._mode != mode:
            self.navigation = ""
        self._mode = mode
        self._mode_at = self.clock.now()
        if self.navigator is None or not self.navigator.honest:
            return
        localizer = self.navigator.localizer
        if localizer is None:
            return
        if localizer.glide != (mode == "roller"):
            localizer.confirmed = False
            self.navigation = ""
        localizer.glide = mode == "roller"

    def _glide_measured(self) -> bool:
        """Whether the skates' glide is currently measured from the depth frames.

        On skates the wheels report no motion, so a trip without this measurement would drift
        with nothing to check it against: better to stay put and say so.
        """
        navigator = self.navigator
        localizer = navigator.localizer if navigator is not None else None
        return localizer is not None and localizer.glide_odometry

    async def _refresh_mode(self) -> None:
        async with self._mode_lock:
            if self._mode is not None and self.clock.now() - self._mode_at < 30:
                return
            result = await self.link.call("robot.mode")
            if not isinstance(result, dict) or result.get("mode") not in ("walk", "roller"):
                self._mode = None
                raise RobotRefused("unsupported or unavailable robot mode")
            changed = self._mode is not None and self._mode != result["mode"]
            self._accept_mode(result["mode"])
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
        if self._person_target is not None or (
            self.capabilities is not None and self.capabilities.remote_mode_switch is False
        ):
            return await self._request_person(mode)
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
                    try:
                        await call("robot.setMode", {"mode": mode})
                    except JsonRpcError as error:
                        if refused_by_route(error):
                            raise _NeedsPerson(error.message) from error
                        raise
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
                self._accept_mode(mode)
        except _NeedsPerson as refusal:
            self.journal.write("robot_refused", method="robot.setMode", reason=str(refusal))
            self.capabilities = replace(
                self.capabilities or Capabilities(transport_of(self.link)),
                remote_mode_switch=False,
            )
            if recovering:
                return self._refuse("set_mode", FALLEN_NEEDS_PERSON)
            return await self._request_person(mode)
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

    @property
    def awaiting_person(self) -> str | None:
        """The mode a person was asked to select, while Coin-Coin waits for it."""
        return self._person_target

    async def _request_person(self, mode: str) -> ToolResult:
        """Ask for a person-assisted mode change; never actuate while it is pending."""
        try:
            await self.start()
            current = await self.link.call("robot.mode")
        except ROBOT_ERRORS as error:
            return self._failure("set_mode", error)
        if not isinstance(current, dict) or current.get("mode") not in ("walk", "roller"):
            return self._refuse("set_mode", "unsupported or unavailable robot mode")
        # Selecting the mode the robot already reports is normally a no-op. On a fallen
        # simulated robot it is the recovery request itself: robotd turns a re-selection into a
        # mode_support stand-up, so the equipment assistant must be asked even for that mode.
        if current["mode"] == mode and not await self._assisted_recovery():
            await self.cancel_person_wait("mode already selected")
            self._accept_mode(mode)
            self.journal.write("tool_result", method="set_mode", mode=mode)
            return ToolResult(True, detail=f"locomotion mode: {mode}")
        # Equipment changes may move the duck: nothing may navigate until they finish.
        if (
            self.navigator is not None
            and self.navigator.honest
            and self.navigator.localizer is not None
        ):
            self.navigator.localizer.confirmed = False
        stop_reason = (
            "simulation equipment change"
            if self.settings.sim_equipment_socket
            else "mode change needs a person"
        )
        await self.stop(stop_reason)
        self.navigation = ""
        timeout_s = self.settings.person_mode_timeout_s
        if self._person_target != mode or self._person_task is None or self._person_task.done():
            await self.cancel_person_wait("mode request replaced")
            self._person_target = mode
            self._person_task = asyncio.create_task(self._await_person(mode, timeout_s))
        self.journal.write(
            "tool_result", method="set_mode", mode=mode, person_required=True, timeout_s=timeout_s
        )
        return ToolResult(
            True,
            detail=(
                f"PENDING simulation equipment assistance: fitting/removing skates and selecting "
                f"{mode} locally. Do not ask a human or move until confirmation."
                if self.settings.sim_equipment_socket
                else f"PENDING, not done: this link cannot switch modes. Tell the user now that a "
                f"person must {EQUIPMENT[mode]} and then select {mode} mode on the robot itself. "
                f"All motion stays disabled until the robot reports {mode} mode; you will be told "
                f"when it does, or after {timeout_s:g} s if nobody does. Do not call move yet."
            ),
        )

    async def _assisted_recovery(self) -> bool:
        """Whether the simulated equipment assistant can stand a fallen robot up right now."""
        if not self.settings.sim_equipment_socket or self._safety_reason() != "robot fallen":
            return False
        try:
            policies = await self.link.call("robot.policies")
        except ROBOT_ERRORS:
            return False
        return isinstance(policies, dict) and policies.get("simulated") is True

    async def cancel_person_wait(self, reason: str) -> None:
        task, self._person_task = self._person_task, None
        if self._person_target is not None:
            self.journal.write("state", person_mode=None, reason=reason)
        self._person_target = None
        await self._cancel(task)

    async def wait_person(self) -> None:
        """Wait until a pending person-assisted mode change finishes, is cancelled or expires."""
        task = self._person_task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)

    async def _notice(self, text: str) -> None:
        self.journal.write("state", notice=text)
        if self.on_notice is not None:
            try:
                await self.on_notice(text)
            except Exception as error:  # noqa: BLE001 - a notice must never break the governor
                self.journal.write("error", component="governor notice", reason=str(error))

    async def _await_person(self, mode: str, timeout_s: float) -> None:
        deadline = self.clock.now() + timeout_s
        last_error = ""
        try:
            if self.settings.sim_equipment_socket:
                try:
                    remote = await self.link.call("robot.policies")
                    if not isinstance(remote, dict) or remote.get("simulated") is not True:
                        raise RobotRefused("equipment assistance requires a simulated robot")
                    # A fallen robot is exactly what the assistant's mode_support hand is for:
                    # re-selecting the current mode stands it up. Every other safety reason
                    # still refuses, and the wait below releases only once it is upright.
                    if (reason := self._safety_reason()) and reason != "robot fallen":
                        raise RobotRefused(reason)
                    if self._closed or self._person_target != mode:
                        raise RobotRefused("simulation equipment request cancelled")
                    async with asyncio.timeout(min(timeout_s, 30)):
                        await select_mode(Path(self.settings.sim_equipment_socket), mode)
                    self.journal.write("state", sim_equipment=mode)
                except (*ROBOT_ERRORS, OSError, ValueError) as error:
                    self._person_target = None
                    self.journal.write("robot_refused", method="set_mode", reason=str(error))
                    await self._notice(f"Simulation equipment change failed: {error}")
                    return
            while True:
                # Robot state is re-read every poll; a pick-up or a link outage while the
                # equipment is changed is expected and does not end the wait.
                current = None
                if self.link.connected:
                    try:
                        answer = await self.link.call("robot.mode")
                        current = answer.get("mode") if isinstance(answer, dict) else None
                        if self.settings.sim_equipment_socket and current == mode:
                            policies = await self.link.call("robot.policies")
                            if (
                                not isinstance(policies, dict)
                                or type(policies.get("enabled")) is not bool
                                or (policies["enabled"] and policies.get("homed") is not True)
                                or self._safety_reason() is not None
                            ):
                                current = None
                    except ROBOT_ERRORS as error:
                        last_error = str(error)
                if current == mode:
                    self._person_target = None
                    self._accept_mode(mode)
                    self.journal.write("tool_result", method="set_mode", mode=mode, person=True)
                    await self._notice(
                        f"Le robot est maintenant en mode {mode}; tu peux de nouveau bouger. "
                        f"(robot now reports {mode} mode; motion allowed again)"
                    )
                    return
                if self.clock.now() >= deadline:
                    self._person_target = None
                    reason = f"nobody selected {mode} mode within {timeout_s:g} s"
                    if last_error:
                        reason += f" (last error: {last_error})"
                    self.journal.write("robot_refused", method="set_mode", reason=reason)
                    await self._notice(
                        f"Personne n'a selectionne le mode {mode}; je reste comme je suis. "
                        f"({reason}; still in the previous mode)"
                    )
                    return
                await self.clock.sleep(PERSON_POLL_S)
        finally:
            if self._person_target == mode and asyncio.current_task() is self._person_task:
                self._person_target = None

    async def _watch(self) -> None:
        last_reason: str | None = None
        was_connected = False
        while True:
            connected = self.link.connected
            if connected and not was_connected:
                self._mode = None
                self._mode_at = -math.inf
                self._health_at = -math.inf
                try:
                    await self.mouth(0)
                except LinkDown as error:
                    self._failure("robot.mouth", error)
            reason = self._safety_reason()
            recovering = (
                reason == "robot fallen" and self._mode_recovery_generation == self._stop_generation
            )
            if reason is not None and reason != last_reason and not recovering:
                if (
                    reason == "robot picked up"
                    and self.navigator is not None
                    and self.navigator.honest
                    and self.navigator.localizer is not None
                ):
                    self.navigator.localizer.confirmed = False
                    self.navigation = ""
                await self.stop(reason)
            last_reason = reason
            was_connected = connected
            if (
                connected
                and self.clock.now() - self._mode_at >= 30
                and (self._mode_task is None or self._mode_task.done())
            ):
                self._mode_task = asyncio.create_task(self._update_mode())
            if (
                connected
                and self.clock.now() - self._health_at >= HEALTH_POLL_S
                and (self._health_task is None or self._health_task.done())
            ):
                self._health_task = asyncio.create_task(self._update_health())
            await self.clock.sleep(0.1)

    def _limits(self) -> tuple[float, float, float]:
        if self._mode == "roller":
            return 0.3, 0.0, ROLLER_MAX_VYAW
        if self._mode != "walk":
            raise RobotRefused("robot mode unavailable")
        return self.settings.max_vx, self.settings.max_vy, self.settings.max_vyaw

    def _floors(self, limits: tuple[float, float, float]) -> tuple[float, float, float]:
        """Smallest non-zero speeds the current gait follows, never above the caps.

        Walk only: the skating policy's yaw cap (ROLLER_MAX_VYAW) is below the walk yaw floor and
        its forward command is a push, not a speed, so roller commands are sent as asked.
        """
        if self._mode != "walk":
            return 0.0, 0.0, 0.0
        settings = self.settings
        return (
            min(settings.min_vx, limits[0]),
            min(settings.min_vy, limits[1]),
            min(settings.min_vyaw, limits[2]),
        )

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

    async def _rise_if_sitting(self, generation: int, policies: dict[str, Any]) -> None:
        """Stand a seated duck up first: a seated gait ignores walking commands."""
        if policies.get("sitting") is not True:
            return
        async with self._action_lock:
            await self._cancel(self._motion)
            self._motion = None
            await self._action_call(generation, "robot.stop", timeout=0.1)
            await self._action_call(generation, "robot.do", {"skill": "sit_toggle"})
        deadline = self.clock.now() + STAND_TIMEOUT_S
        retry_at = self.clock.now() + 0.5
        while (await self._policies(generation)).get("sitting") is True:
            if self.clock.now() >= deadline:
                raise RobotRefused(f"robot did not stand up within {STAND_TIMEOUT_S:g} s")
            if self.clock.now() >= retry_at:
                # robotd refuses the toggle while it is still settling into the seat.
                async with self._action_lock:
                    await self._action_call(generation, "robot.do", {"skill": "sit_toggle"})
                retry_at = self.clock.now() + 0.5
            await self.clock.sleep(0.1)
        await self.clock.sleep(STAND_SETTLE_S)
        self._check_safety(generation)
        self.journal.write("tool_result", method="stand", automatic=True)

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
        self,
        generation: int,
        run: Callable[[bool], Coroutine[Any, Any, ToolResult]],
        *,
        supervised: bool = False,
    ) -> None:
        async with self._action_lock:
            self._check_safety(generation)
            replacing = self.moving
            await self._cancel(self._motion)
            self._check_safety(generation)
            self._motion = asyncio.create_task(run(replacing))
            if not supervised:
                self._unsupervised_motion = True
                self.navigation = ""

    async def _stop_other_motion(self, generation: int) -> None:
        async with self._action_lock:
            self._check_safety(generation)
            motion = self._motion
            if motion is not None and motion is not asyncio.current_task() and not motion.done():
                motion.cancel()
                try:
                    await self._action_call(generation, "robot.stop", timeout=0.1)
                finally:
                    await self._cancel(motion)
                    if self._motion is motion:
                        self._motion = None

    async def move(self, vx: float, vy: float, vyaw: float, duration_s: float) -> ToolResult:
        requested_at = self.clock.now()
        if not _finite(vx, vy, vyaw, duration_s) or duration_s <= 0:
            return self._refuse("move", "velocities and positive duration must be finite")
        if vx == vy == vyaw == 0:
            return self._refuse(
                "move", "no velocity: use vx > 0 to go forward, vx < 0 back, vy sideways"
            )
        generation = self._stop_generation
        try:
            await self._rise_if_sitting(generation, await self._prepare(generation))
            limits = self._limits()
            requested = {"vx": vx, "vy": vy, "vyaw": vyaw, "duration_s": duration_s}
            capped = {
                "vx": _clamp(vx, -limits[0], limits[0]),
                "vy": _clamp(vy, -limits[1], limits[1]),
                "vyaw": _clamp(vyaw, -limits[2], limits[2]),
                "duration_s": min(duration_s, self.settings.max_move_s),
            }
            actual = dict(capped)
            raised: dict[str, float] = {}
            for key, floor in zip(("vx", "vy", "vyaw"), self._floors(limits), strict=True):
                if actual[key] and abs(actual[key]) < floor:
                    actual[key] = raised[key] = math.copysign(floor, actual[key])
            starter = None
            if self._mode == "walk" and self.settings.walk_assist:
                starter = walk_starter({key: actual[key] for key in ("vx", "vy", "vyaw")})
            clamped = {key: value for key, value in capped.items() if requested[key] != value}
            params = {key: actual[key] for key in ("vx", "vy", "vyaw")}
            self._move_id += 1
            move_id = self._move_id
            await self._replace_motion(
                generation,
                lambda replacing: self._run_move(
                    params,
                    actual["duration_s"],
                    generation,
                    requested_at,
                    move_id,
                    replacing,
                    starter,
                ),
            )
        except ROBOT_ERRORS as error:
            return self._failure("move", error)
        self.journal.write(
            "tool_call",
            method="move",
            move_id=move_id,
            requested=requested,
            actual=actual,
            starter=starter,
            raised=raised or None,
        )
        detail = "movement started"
        if raised:
            LOGGER.info("move %d: %s (requested %s)", move_id, raised_note(raised), requested)
            detail += f"; {raised_note(raised)}"
        return ToolResult(True, detail=detail, clamped=clamped or None, raised=raised or None)

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
        starter: dict[str, float] | None = None,
    ) -> ToolResult:
        start = self.clock.now()
        deadline = start + duration
        starter_until = start + min(WALK_START_S, duration) if starter else start
        next_tick = start + 1 / self.settings.move_hz
        observation: MoveLatency | None = None
        end_reason = "cancelled"
        try:
            while self.clock.now() < deadline:
                self._check_safety(generation)
                command = starter if starter and self.clock.now() < starter_until else params
                if observation is None:
                    observation = MoveLatency(
                        self.link,
                        self.clock,
                        self.journal,
                        move_id=move_id,
                        requested_at=requested_at,
                        params=command,
                        duration=duration,
                        replacing=replacing,
                        baseline_after=self._last_motion_command_at,
                    )
                else:
                    observation.observe()
                self._last_motion_command_at = self.clock.now()
                await self.link.notify("robot.move", command)
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
            await self._rise_if_sitting(generation, await self._prepare(generation))
            yaw = self._yaw()
            limits = self._limits()
            max_rate = limits[2]
            min_rate = self._floors(limits)[2]
            await self._replace_motion(
                generation,
                lambda replacing: self._run_turn(angle, yaw, max_rate, generation, min_rate),
            )
        except ROBOT_ERRORS as error:
            return self._failure("turn", error)
        # The fine approach normally slows to 0.3 x max rate; the gait floor may forbid it.
        raised = {"vyaw": min_rate} if min_rate > 0.3 * max_rate else None
        self.journal.write("tool_call", method="turn", degrees=angle, raised=raised)
        detail = "turn started"
        if raised:
            detail += f"; {raised_note(raised)}"
        return ToolResult(
            True,
            detail=detail,
            clamped={"degrees": angle} if angle != degrees else None,
            raised=raised,
        )

    async def _run_turn(
        self,
        degrees: float,
        yaw: float,
        max_rate: float,
        generation: int,
        min_rate: float = 0.0,
    ) -> ToolResult:
        target = math.radians(degrees)
        deadline = self.clock.now() + turn_time_limit_s(degrees, max_rate)
        next_tick = self.clock.now() + 1 / self.settings.move_hz
        progress = 0.0
        try:
            while True:
                self._check_safety(generation)
                current = self._yaw()
                progress += math.atan2(math.sin(current - yaw), math.cos(current - yaw))
                yaw = current
                remaining = target - progress
                if abs(remaining) <= math.radians(TURN_TOLERANCE_DEG):
                    break
                if (
                    min_rate > 0
                    and remaining * target < 0
                    and abs(remaining) <= math.radians(TURN_OVERSHOOT_DEG)
                ):
                    break
                if self.clock.now() >= deadline:
                    raise RobotRefused("turn timed out")
                rate = (0.3 if abs(remaining) < math.radians(15) else 0.8) * max_rate
                rate = max(rate, min_rate)
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

    def places(self) -> list[str]:
        return list(self.navigator.places) if self.navigator is not None else []

    async def go_to(self, place: str, mode: str | None = None) -> ToolResult:
        """Plan a route to a named place and follow it with the navigator's pose source.

        With the default (estimated) source the duck must know where it is before it sets off: an
        unconverged estimate gets one head-sweep relocalization, and if that does not settle it
        the request is refused with the reason, rather than walking on a guess.
        """
        navigator = self.navigator
        if navigator is None:
            return self._refuse("go_to", "navigation is not configured")
        if place not in navigator.places:
            return self._refuse("go_to", f"unknown place; known: {', '.join(navigator.places)}")
        if mode is not None and mode not in ("walk", "roller"):
            return self._refuse("go_to", "mode must be walk or roller")
        if navigator.honest and mode == "roller" and not self._glide_measured():
            return self._refuse("go_to", ROLLER_TRIP_REFUSED)
        generation = self._stop_generation
        try:
            await self.start()
            self._check_safety(generation)
            policies = await self._policies(generation)
            if policies.get("simulated") is not True:
                return self._refuse(
                    "go_to", "room navigation requires a simulated robot and simulated pose"
                )
            if navigator.honest or mode is not None:
                await self._refresh_mode()
            if navigator.honest and self._mode == "roller" and not self._glide_measured():
                return self._refuse("go_to", ROLLER_TRIP_REFUSED)
            if mode is not None:
                if self._mode != mode:
                    switched = await self.set_mode(mode)
                    if not switched.ok or self._person_target is not None:
                        return switched
                if generation != self._stop_generation:
                    return self._refuse("go_to", "action cancelled by stop")
                if navigator.honest and self._mode == "roller" and not self._glide_measured():
                    return self._refuse("go_to", ROLLER_TRIP_REFUSED)
            await self._rise_if_sitting(generation, await self._prepare(generation))
            estimate = await navigator.estimate()
            # A manual move or turn was only tracked frame by frame, with no check that the
            # match still holds: confirm with a head sweep before trusting it for a whole trip.
            if navigator.honest and (not estimate.converged or self._unsupervised_motion):
                estimate = await self._relocalize(generation, navigator, estimate)
                if not estimate.converged:
                    raise RobotRefused(self._lost_reason(place, navigator, estimate))
                self._unsupervised_motion = False
            navigator.clear_obstacles()
            self._blocked_anchor = None
            pose = estimate.pose
            goal = navigator.places[place]
            arrival = Steering.ROLLER_ARRIVAL_M if self._mode == "roller" else Steering.ARRIVAL_M
            if math.dist((pose.x, pose.y), goal) <= arrival:
                await self._stop_other_motion(generation)
                self.navigation = f"arrived at {place}"
                self.journal.write("tool_result", method="go_to", place=place, already=True)
                return ImmediateToolResult(True, detail=f"already at {place}")
            route = await asyncio.to_thread(navigator.plan, (pose.x, pose.y), goal)
            if route is None:
                raise RobotRefused(f"no known path to {place}")
            if math.dist(route[-1], goal) > arrival:
                raise RobotRefused(f"cannot reach {place}: its mapped location is obstructed")
            locomotion = self._mode
            limits = self._limits()
            self.navigation = f"going to {place}"
            await self._replace_motion(
                generation,
                lambda replacing: self._run_route(place, route, limits, locomotion, generation),
                supervised=True,
            )
        except ROBOT_ERRORS as error:
            result = self._failure("go_to", error)
            self.navigation = f"did not go to {place}: {result.reason}"
            return result
        self.journal.write(
            "tool_call",
            method="go_to",
            place=place,
            mode=locomotion,
            route=route,
            start=[pose.x, pose.y],
            pose_source=navigator.source.name,
        )
        return ToolResult(True, detail=f"going to {place} ({locomotion})")

    async def _run_route(
        self,
        place: str,
        route: list[tuple[float, float]],
        limits: tuple[float, float, float],
        mode: str | None,
        generation: int,
    ) -> ToolResult:
        navigator = self.navigator
        assert navigator is not None
        localizer = navigator.localizer
        if localizer is not None:
            localizer.glide = mode == "roller"
        start = self.clock.now()
        deadline = start + NAV_MAX_S
        next_tick = start + 1 / self.settings.move_hz
        goal = route[-1]

        def make_steering(waypoints: list[tuple[float, float]]) -> Steering:
            planning_map = navigator.map.with_obstacles(navigator.obstacles)
            return Steering(
                waypoints,
                limits,
                mode,
                planning_map.clearance_m,
                planning_map.segment_clear,
                route_traversable=planning_map.segment_traversable,
                recover_stalled_pivot=self.settings.walk_assist,
            )

        steering = make_steering(route)
        best, best_at, replans = math.inf, start, 0
        stationary_at = start
        observed_pose = None
        last_replan_at: tuple[float, float] | None = None
        last_route: list[tuple[float, float]] | None = None
        deviation_replans = 0
        deviation_anchor: tuple[float, float] | None = None
        relocalizations = obstacle_replans = 0
        blocked_checked = False
        uncertain_since: float | None = None
        first_scan = localizer.latest_scan() if localizer else None
        checked_scan_t = first_scan.t_ns if first_scan else 0
        obstacle_streak: list[tuple[float, float]] = []
        obstacle_seen_at: float | None = None
        streak = 0
        forward = False
        graze: tuple[float, float] | None = None
        lookahead = (
            navigator.map.robot_radius
            + limits[0] * NAV_TOF_LATENCY_S
            + NAV_BRAKE_M
            + NAV_OBSTACLE_MARGIN_M
        )
        try:
            while True:
                self._check_safety(generation)
                estimate = await navigator.estimate()
                self._check_safety(generation)
                now = self.clock.now()
                pose = estimate.pose
                diffuse = estimate.std_xy > LOST_XY_M or estimate.std_yaw > LOST_YAW_RAD
                if navigator.honest and localizer is not None and not localizer.confirmed:
                    diffuse = True
                scan = localizer.latest_scan() if localizer is not None else None
                if (
                    localizer is not None
                    and scan is not None
                    and scan.t_ns > checked_scan_t
                    and not diffuse
                ):
                    checked_scan_t = scan.t_ns
                    scan_pose = self._scan_pose(estimate, scan, localizer)
                    points = obstacle_points(scan)
                    path = [(pose.x, pose.y), *steering.route[steering.index :]]
                    half_width = navigator.map.robot_radius + 0.05
                    in_view = blocking_points(
                        localizer.field,
                        scan_pose,
                        points,
                        path,
                        lookahead_m=NAV_OBSTACLE_VIEW_M,
                        half_width_m=half_width,
                        explained_m=TRACK_OBSTACLE_EXPLAINED_M,
                    )
                    obstacle_seen_at = now if len(in_view) else obstacle_seen_at
                    if (
                        navigator.honest
                        and len(in_view) >= TRACK_OBSTACLE_MIN_HITS
                        and self._blocked_anchor is None
                    ):
                        self._blocked_anchor = (scan_pose, scan.odom)
                    radius = navigator.map.robot_radius
                    close = points[
                        (points[:, 0] > 0)
                        & (points[:, 0] < radius + NAV_BUMPER_M)
                        & (np.abs(points[:, 1]) < 0.75 * radius)
                    ]
                    # Mapped surfaces only: unmapped ones are the obstacle check's business.
                    bumped = forward and len(close) > len(
                        unexplained_points(
                            localizer.field,
                            scan_pose,
                            close,
                            explained_m=TRACK_OBSTACLE_EXPLAINED_M,
                        )
                    )
                    ahead = blocking_points(
                        localizer.field,
                        scan_pose,
                        points,
                        path,
                        lookahead_m=lookahead,
                        half_width_m=half_width,
                        explained_m=TRACK_OBSTACLE_EXPLAINED_M,
                    )
                    gap = float(
                        localizer.field.lookup(np.array([scan_pose.x]), np.array([scan_pose.y]))[0]
                    )
                    if (
                        bumped
                        and not len(ahead)
                        and gap - radius <= NAV_BUMPER_M + 0.03
                        and (graze is None or math.dist(graze, (pose.x, pose.y)) > 0.3)
                    ):
                        # The map agrees the duck is grazing a wall: steering cut a corner.
                        self.journal.write("navigation", place=place, event="too_close")
                        graze = (pose.x, pose.y)
                        await self.link.call("robot.stop", timeout=0.1)
                        self._check_safety(generation)
                        replanned = await asyncio.to_thread(navigator.plan, (pose.x, pose.y), goal)
                        self._check_safety(generation)
                        if replanned is None or replans >= NAV_REPLANS:
                            raise RobotRefused(f"stuck against a wall on the way to {place}")
                        replans += 1
                        steering = make_steering(replanned)
                        forward = False
                        now = self.clock.now()
                        best, best_at = math.inf, now
                        observed_pose, stationary_at = pose, now
                        next_tick = now + 1 / self.settings.move_hz
                        continue
                    if bumped and not len(ahead) and navigator.honest:
                        # A mapped surface is right in front of the duck where the map says there
                        # is room: the pose is off, whatever its uncertainty says.
                        self.journal.write("navigation", place=place, event="pose_contradicted")
                        estimate = replace(estimate, converged=False)
                        diffuse = True
                    elif len(ahead):
                        streak += 1
                        # Mark all of it, not just the part across the path: a closed door seen
                        # where the path meets it would otherwise leave the rest of it open.
                        seen = unexplained_points(
                            localizer.field,
                            scan_pose,
                            points,
                            explained_m=TRACK_OBSTACLE_EXPLAINED_M,
                            max_range_m=NAV_OBSTACLE_MARK_M,
                        )
                        obstacle_streak.extend(
                            _with_depth(seen, (scan_pose.x, scan_pose.y), NAV_OBSTACLE_DEPTH_M)
                        )
                    else:
                        streak, obstacle_streak = 0, []
                    if streak >= NAV_OBSTACLE_SCANS:
                        await self.link.call("robot.stop", timeout=0.1)
                        forward = False
                        self._check_safety(generation)
                        known = list(navigator.obstacles)
                        if navigator.honest and self._blocked_anchor is None:
                            self._blocked_anchor = (estimate.pose, localizer.latest_odom())
                        navigator.mark_obstacles(obstacle_streak)
                        self.journal.write(
                            "navigation",
                            place=place,
                            obstacle=[[round(x, 2), round(y, 2)] for x, y in obstacle_streak[:20]],
                        )
                        streak, obstacle_streak = 0, []
                        replanned = None
                        if obstacle_replans < NAV_OBSTACLE_REPLANS:
                            replanned = await asyncio.to_thread(
                                navigator.plan, (pose.x, pose.y), goal
                            )
                        self._check_safety(generation)
                        if (
                            replanned is None
                            and navigator.honest
                            and not blocked_checked
                            and relocalizations < NAV_RELOCALIZATIONS
                        ):
                            # A pose a few centimetres off makes mapped walls look unmapped, and
                            # those "obstacles" close the corridor. Before calling the way
                            # blocked, relocalize, forget what this stop marked, and look again.
                            self.journal.write("navigation", place=place, event="verify_blocked")
                            relocalizations += 1
                            blocked_checked = True
                            if self._blocked_anchor is None:
                                self._blocked_anchor = (estimate.pose, localizer.latest_odom())
                            estimate = await self._relocalize(generation, navigator, estimate)
                            if not estimate.converged:
                                raise RobotRefused(
                                    f"the way to {place} may be blocked by something that is "
                                    "not on my map; I lost track of where I am and stopped, "
                                    "and a head sweep did not confirm my position "
                                    f"({estimate.describe()})"
                                )
                            navigator.set_obstacles(known)
                            replanned = await asyncio.to_thread(
                                navigator.plan, (estimate.x, estimate.y), goal
                            )
                            if replanned is None:
                                raise RobotRefused(f"no known path to {place} from where I am")
                            steering = make_steering(replanned)
                            latest = localizer.latest_scan()
                            checked_scan_t = latest.t_ns if latest else checked_scan_t
                            now = self.clock.now()
                            best, best_at, uncertain_since = math.inf, now, None
                            observed_pose, stationary_at = estimate.pose, now
                            next_tick = now + 1 / self.settings.move_hz
                            continue
                        if replanned is None:
                            if navigator.honest and localizer is not None:
                                localizer.confirmed = False
                            raise RobotRefused(
                                f"the way to {place} may be blocked by something that is not on "
                                "my map; I stopped and cannot confirm my position"
                            )
                        obstacle_replans += 1
                        blocked_checked = False
                        self.journal.write(
                            "navigation",
                            place=place,
                            obstacle_replan=obstacle_replans,
                            route=replanned,
                        )
                        steering = make_steering(replanned)
                        now = self.clock.now()
                        best, best_at = math.inf, now
                        observed_pose, stationary_at = pose, now
                        next_tick = now + 1 / self.settings.move_hz
                        continue
                if navigator.honest and not estimate.converged:
                    uncertain_since = now if uncertain_since is None else uncertain_since
                else:
                    uncertain_since = None
                # Something unmapped filling the view lowers the map fit without the duck being
                # lost: give the obstacle check time to reach it before calling that lost.
                patience = (
                    NAV_OBSTACLE_UNCERTAIN_S
                    if obstacle_seen_at is not None and now - obstacle_seen_at <= 1
                    else NAV_UNCERTAIN_S
                )
                if navigator.honest and (
                    diffuse or (uncertain_since is not None and now - uncertain_since >= patience)
                ):
                    await self.link.call("robot.stop", timeout=0.1)
                    forward = False
                    self._check_safety(generation)
                    if relocalizations >= NAV_RELOCALIZATIONS:
                        raise RobotRefused(
                            f"I lost track of where I am on the way to {place} and stopped "
                            f"({estimate.describe()})"
                        )
                    relocalizations += 1
                    estimate = await self._relocalize(generation, navigator, estimate)
                    if not estimate.converged:
                        if navigator.obstacles:
                            raise RobotRefused(
                                f"the way to {place} may be blocked by something that is not "
                                "on my map; I lost track of where I am and stopped "
                                f"({estimate.describe()})"
                            )
                        raise RobotRefused(
                            f"I lost track of where I am on the way to {place} and stopped "
                            f"({estimate.describe()})"
                        )
                    replanned = await asyncio.to_thread(
                        navigator.plan, (estimate.x, estimate.y), goal
                    )
                    if replanned is None:
                        raise RobotRefused(f"no known path to {place} from where I am")
                    steering = make_steering(replanned)
                    latest = localizer.latest_scan() if localizer is not None else None
                    checked_scan_t = latest.t_ns if latest else checked_scan_t
                    streak, obstacle_streak, obstacle_seen_at = 0, [], None
                    now = self.clock.now()
                    best, best_at, uncertain_since = math.inf, now, None
                    observed_pose, stationary_at = estimate.pose, now
                    next_tick = now + 1 / self.settings.move_hz
                    continue
                reversing_pivot = steering.reversing_pivot
                command = steering.update(pose, now)
                if steering.reversing_pivot and not reversing_pivot:
                    self.journal.write("navigation", place=place, event="reverse_stalled_pivot")
                if command is not None and not steering.route_clear(pose):
                    await self.link.call("robot.stop", timeout=0.1)
                    forward = False
                    self._check_safety(generation)
                    here = (pose.x, pose.y)
                    if deviation_anchor is None or math.dist(here, deviation_anchor) >= 0.2:
                        deviation_anchor, deviation_replans = here, 0
                    replanned = None
                    if deviation_replans < NAV_REPLANS:
                        replanned = await asyncio.to_thread(navigator.plan, here, goal)
                    self._check_safety(generation)
                    if replanned is None:
                        raise RobotRefused(f"cannot keep a safe path to {place}; I stopped")
                    deviation_replans += 1
                    self.journal.write(
                        "navigation", place=place, event="route_deviation", route=replanned
                    )
                    steering = make_steering(replanned)
                    now = self.clock.now()
                    best, best_at = math.inf, now
                    observed_pose, stationary_at = pose, now
                    next_tick = now + 1 / self.settings.move_hz
                    continue
                if command is None:
                    if navigator.honest and not estimate.converged:
                        await self.link.call("robot.stop", timeout=0.1)
                        forward = False
                        self._check_safety(generation)
                        if relocalizations >= NAV_RELOCALIZATIONS:
                            raise RobotRefused(
                                f"I cannot confirm that I reached {place}; I stopped"
                            )
                        relocalizations += 1
                        estimate = await self._relocalize(generation, navigator, estimate)
                        if not estimate.converged:
                            raise RobotRefused(
                                f"I cannot confirm that I reached {place}; I stopped"
                            )
                        pose = estimate.pose
                        if math.dist((pose.x, pose.y), goal) > Steering.ARRIVAL_M:
                            replanned = await asyncio.to_thread(
                                navigator.plan, (pose.x, pose.y), goal
                            )
                            if replanned is None:
                                raise RobotRefused(f"no known path to {place} from where I am")
                            steering = make_steering(replanned)
                            latest = localizer.latest_scan() if localizer is not None else None
                            checked_scan_t = latest.t_ns if latest else checked_scan_t
                            streak, obstacle_streak, obstacle_seen_at = 0, [], None
                            now = self.clock.now()
                            best, best_at, uncertain_since = math.inf, now, None
                            observed_pose, stationary_at = pose, now
                            next_tick = now + 1 / self.settings.move_hz
                            continue
                    arrival = Steering.ROLLER_ARRIVAL_M if mode == "roller" else Steering.ARRIVAL_M
                    if math.dist((pose.x, pose.y), navigator.places[place]) > arrival:
                        raise RobotRefused(
                            f"cannot reach {place}: its mapped location is obstructed"
                        )
                    break
                if now >= deadline:
                    raise RobotRefused(f"did not reach {place} within {NAV_MAX_S:g} s")
                if observed_pose is None:
                    observed_pose = pose
                elif (
                    math.dist((pose.x, pose.y), (observed_pose.x, observed_pose.y)) >= 0.04
                    or abs(math.remainder(pose.yaw - observed_pose.yaw, 2 * math.pi)) >= 0.2
                ):
                    observed_pose, stationary_at = pose, now
                remaining = steering.remaining(pose)
                if remaining < best - 0.1:
                    best, best_at = remaining, now
                if now - best_at >= NAV_STALL_S or now - stationary_at >= NAV_STATIONARY_S:
                    await self.link.call("robot.stop", timeout=0.1)
                    forward = False
                    self._check_safety(generation)
                    replanned = None
                    if replans < NAV_REPLANS:
                        replanned = await asyncio.to_thread(navigator.plan, (pose.x, pose.y), goal)
                    self._check_safety(generation)
                    here = (pose.x, pose.y)
                    if replanned is None or (
                        replanned == last_route
                        and last_replan_at is not None
                        and math.dist(here, last_replan_at) < 0.1
                    ):
                        raise RobotRefused(f"stuck on the way to {place}")
                    replans += 1
                    self.journal.write("navigation", place=place, replan=replans, route=replanned)
                    steering = make_steering(replanned)
                    last_replan_at, last_route = here, replanned
                    best, best_at = math.inf, now
                    observed_pose, stationary_at = pose, now
                    continue
                self._last_motion_command_at = now
                forward = command["vx"] > 0.05
                await self.link.notify("robot.move", command)
                next_tick = await self._tick(next_tick, deadline)
            await self.link.call("robot.stop", timeout=0.1)
            await self.mouth(0)
            elapsed = self.clock.now() - start
            self.journal.write(
                "tool_result",
                method="go_to",
                place=place,
                elapsed_s=round(elapsed, 1),
                pose=[pose.x, pose.y, pose.yaw],
                pose_source=navigator.source.name,
                relocalizations=relocalizations,
                obstacle_replans=obstacle_replans,
            )
            self.navigation = f"arrived at {place}"
            return ToolResult(True, detail=f"arrived at {place} in {elapsed:.0f} s")
        except ROBOT_ERRORS as error:
            result = self._failure("go_to", error)
            self.navigation = f"stopped before reaching {place}: {result.reason}"
            await self.stop(result.reason)
            return result
        except asyncio.CancelledError:
            self.navigation = f"stopped before reaching {place}: interrupted"
            raise

    @staticmethod
    def _scan_pose(estimate: PoseEstimate, scan: Scan, localizer: EstimatedPose) -> Pose:
        """The pose at the scan's instant: the current pose minus odometry since the scan."""
        latest = localizer.latest_odom()
        if latest is None:
            return estimate.pose
        dx, dy = latest.x - scan.odom.x, latest.y - scan.odom.y
        c, s = math.cos(-latest.yaw), math.sin(-latest.yaw)
        fx, fy = c * dx - s * dy, s * dx + c * dy
        c, s = math.cos(estimate.yaw), math.sin(estimate.yaw)
        return Pose(
            estimate.x - (c * fx - s * fy),
            estimate.y - (s * fx + c * fy),
            estimate.yaw - math.remainder(latest.yaw - scan.odom.yaw, 2 * math.pi),
        )

    @staticmethod
    def _anchor_at_odom(anchor: Pose, start: Odom | None, end: Odom | None) -> Pose:
        if start is None or end is None:
            return anchor
        dx, dy = end.x - start.x, end.y - start.y
        c, s = math.cos(start.yaw), math.sin(start.yaw)
        forward, side = c * dx + s * dy, -s * dx + c * dy
        c, s = math.cos(anchor.yaw), math.sin(anchor.yaw)
        return Pose(
            anchor.x + c * forward - s * side,
            anchor.y + s * forward + c * side,
            anchor.yaw + math.remainder(end.yaw - start.yaw, 2 * math.pi),
        )

    async def _relocalize(
        self, generation: int, navigator: Navigator, prior: PoseEstimate
    ) -> PoseEstimate:
        """Stand still, sweep the head, and match the composite near the last estimate."""
        localizer = navigator.localizer
        if localizer is None:
            return prior
        self.journal.write("localization", event="sweep", prior=prior.describe())
        prior_odom = localizer.latest_odom()
        exclude_unmapped = bool(
            self._blocked_anchor is not None or navigator.obstacles or localizer.obstacle_conflict
        )
        localizer.confirmed = False
        # A verdict belongs to the sweep that produced it: never report a stale one.
        localizer.last_verdict = None
        await self._stop_other_motion(generation)
        localizer.collecting_sweep = True
        try:
            scans = await self._sweep(generation, localizer)
        finally:
            localizer.collecting_sweep = False
        reference = scans[-1].odom if scans else localizer.latest_odom()
        prior_pose = self._anchor_at_odom(prior.pose, prior_odom, reference)
        prior = replace(prior, x=prior_pose.x, y=prior_pose.y, yaw=prior_pose.yaw)
        centre = heading = None
        radius = heading_tolerance = None
        if localizer.initialized and prior.std_xy <= RELOC_PRIOR_MAX_M:
            centre = (prior.x, prior.y)
            # The window also grows with the unchecked track: the spread can be overconfident.
            drift_xy = RELOC_DRIFT_FRACTION * localizer.travel_since_fix
            drift_yaw = RELOC_DRIFT_FRACTION * localizer.rotation_since_fix
            radius = min(max(RELOC_RADIUS_M[0], 3 * prior.std_xy, drift_xy), RELOC_RADIUS_M[1])
            if prior.std_yaw <= RELOC_HEADING_RAD[1] / 2:
                heading = prior.yaw
                heading_tolerance = min(
                    max(RELOC_HEADING_RAD[0], 3 * prior.std_yaw, drift_yaw), RELOC_HEADING_RAD[1]
                )
        elif not self.settings.nav_global_relocalization or exclude_unmapped:
            self.journal.write("localization", event="no_prior", scans=len(scans))
            return replace(prior, converged=False)
        correction_map = navigator.map.with_obstacles(navigator.obstacles)
        anchor = (
            self._anchor_at_odom(*self._blocked_anchor, localizer.latest_odom())
            if self._blocked_anchor is not None
            else None
        )

        def accept(pose: Pose) -> bool:
            if anchor is not None and (
                math.dist((anchor.x, anchor.y), (pose.x, pose.y)) > RELOC_JUMP_M
                or abs(math.remainder(pose.yaw - anchor.yaw, 2 * math.pi)) > RELOC_JUMP_RAD
            ):
                return False
            if centre is None:
                return True
            if radius is not None and math.dist(centre, (pose.x, pose.y)) > radius + 0.2:
                return False
            return not navigator.obstacles or correction_map.segment_unobstructed(
                centre, (pose.x, pose.y)
            )

        localizer.relocalizing = True
        try:
            hypotheses = await asyncio.to_thread(
                localizer.relocalize,
                scans,
                centre=centre,
                radius=radius,
                heading=heading,
                heading_tolerance=heading_tolerance if heading_tolerance is not None else math.pi,
                prior=prior.pose if centre is not None else None,
                exclude_unmapped=exclude_unmapped,
                accept=accept,
            )
        finally:
            localizer.relocalizing = False
        self._check_safety(generation)
        await self.clock.sleep(0.3)
        estimate = await navigator.estimate()
        if not hypotheses or not accept(estimate.pose):
            # A rejected match must not become trusted on the next request.
            localizer.confirmed = False
            estimate = replace(estimate, converged=False)
        elif estimate.converged:
            self._blocked_anchor = None
        verdict = localizer.last_verdict
        if estimate.converged:
            event = "relocalized"
        elif verdict is not None and verdict.ambiguous:
            event = "location_ambiguous"
        else:
            event = "relocalization_failed"
        self.journal.write(
            "localization",
            event=event,
            scans=len(scans),
            hypotheses=len(hypotheses),
            estimate=estimate.describe(),
            **({"verdict": verdict.describe()} if verdict is not None else {}),
        )
        return estimate

    def _lost_reason(self, place: str, navigator: Navigator, estimate: PoseEstimate) -> str:
        """Refuse a trip honestly, saying so when the sweep could not tell two places apart."""
        verdict = navigator.localizer.last_verdict if navigator.localizer is not None else None
        if verdict is not None and verdict.ambiguous:
            return f"{LOCATION_AMBIGUOUS}; I will not walk to {place} ({estimate.describe()})"
        return f"I do not know where I am, so I will not walk to {place} ({estimate.describe()})"

    async def _sweep(self, generation: int, localizer: EstimatedPose) -> list[Scan]:
        """Look across the room and return the ToF scans gathered on the way."""
        since = localizer.latest_t_ns() or 0
        names = ("neck_pitch", "head_pitch", "head_yaw", "head_roll")
        await self._cancel(self._look)
        self._look = None
        for degrees in (*SWEEP_DEGREES, 0):
            # A look point is a direction vector: its yaw is atan2(y, x), so the x must go negative
            # past +-90 degrees instead of leaning on tan(), which repeats the quadrant.
            point = {
                "x": math.cos(math.radians(degrees)),
                "y": math.sin(math.radians(degrees)),
                "z": 0.0,
            }
            result = await self._action_call(generation, "robot.look", point)
            head = result.get("head") if isinstance(result, dict) else None
            if not isinstance(head, dict) or not _finite(*(head.get(name) for name in names)):
                raise RobotRefused("robot.look returned invalid head joints")
            joints = {name: head[name] for name in names}
            hold_until = self.clock.now() + SWEEP_HOLD_S
            while self.clock.now() < hold_until:
                self._check_safety(generation)
                await self.link.notify("robot.head", joints)
                await self.clock.sleep(0.15)
        return localizer.recent_scans(since)

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
                    # The simulated equipment assistant can stand the duck up itself by
                    # re-selecting the current mode, so a link that refuses robot.setMode is
                    # no reason to ask a person.
                    assisted = bool(self.settings.sim_equipment_socket)
                    if (
                        self.capabilities is not None
                        and self.capabilities.mode_switch_needs_person
                        and not assisted
                    ):
                        return self._refuse("stand", FALLEN_NEEDS_PERSON)
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
        if self.navigation:
            summary += f", navigation: {self.navigation}"
        if self._person_target is not None:
            summary += ", " + person_wait_reason(
                self._person_target, assisted=bool(self.settings.sim_equipment_socket)
            )
        if self.navigator is not None:
            summary += f", position: {await self._where()}"
        return ToolResult(
            True,
            detail=summary,
        )

    async def _where(self) -> str:
        """The estimated position in words, or plainly that it is not known."""
        navigator = self.navigator
        assert navigator is not None
        try:
            estimate = await navigator.estimate()
        except ROBOT_ERRORS as error:
            return f"inconnue / unknown ({error})"
        if (
            not estimate.converged
            or not math.isfinite(estimate.std_xy)
            or (navigator.honest and self._unsupervised_motion)
        ):
            return (
                "incertaine, je dois regarder autour de moi avant de partir / "
                f"uncertain, a head sweep is needed before a trip ({estimate.describe()})"
            )
        name, goal = min(
            navigator.places.items(),
            key=lambda item: math.dist(item[1], (estimate.x, estimate.y)),
        )
        distance = math.dist(goal, (estimate.x, estimate.y))
        return (
            f"a environ {distance:.1f} m du repere {name} / "
            f"about {distance:.1f} m from landmark {name} ({estimate.describe()})"
        )


def _with_depth(
    points: Iterable[Any], origin: tuple[float, float], depth_m: float
) -> list[tuple[float, float]]:
    """The seen surface points plus where the object probably continues behind them."""
    out: list[tuple[float, float]] = []
    for x, y in points:
        dx, dy = float(x) - origin[0], float(y) - origin[1]
        reach = math.hypot(dx, dy)
        out.append((float(x), float(y)))
        if reach < 1e-6:
            continue
        for k in range(1, round(depth_m / 0.1) + 1):
            scale = 1 + 0.1 * k / reach
            out.append((origin[0] + dx * scale, origin[1] + dy * scale))
    return out
