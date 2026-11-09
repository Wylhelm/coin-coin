"""Opt-in: honest go_to in the scanned apartment, steering only by odometry, IMU and the 8x8 ToF.

The controller never sees the simulator's pose: `EstimatedPose` (MCL) is its only pose source. The
oracle is read beside it purely to score the estimate. Hold the global simulator lock:

    DUCK_TEST_NAVIGATION_SIM=1 flock -w 1800 /tmp/microduck-sim-agents.lock \\
        .venv/bin/python -m pytest -s --no-cov \\
        tests/integration/test_navigation_honest_simulation.py

Each run prints the estimated-vs-true error (RMS, max), time and contacts, and appends a JSON line
to $DUCK_NAV_RESULTS when set. Scenes with a box or a closed door are copies made in tmp_path;
the scanned apartment is never modified.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from coincoin.clock import SystemClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.governor import Governor, ImmediateToolResult, ToolResult
from coincoin.robot.localization import EstimatedPose, TcpTofStream
from coincoin.robot.navigation import NavigationMap, Navigator, Pose, SimPose
from tests.integration.sim_stack import SimStack, apartment_dir, sim_stack

pytestmark = pytest.mark.skipif(
    os.environ.get("DUCK_TEST_NAVIGATION_SIM") != "1",
    reason="requires the scanned apartment, sibling simulator, built robotd and tofd",
)

LEG_TIMEOUT_S = 300
# A box in the open area west of the hallway, on the route to the bedroom.
BOX = '<geom name="test_box" type="box" pos="-2.5 -0.14 0.225" size="0.125 0.125 0.225" />'
# A closed door across the passage into the bedroom.
DOOR = '<geom name="test_door" type="box" pos="-5.48 -0.05 0.4" size="0.75 0.025 0.4" />'


@dataclass
class Leg:
    destination: str
    result: ToolResult | None = None
    seconds: float = 0.0
    errors: list[float] = field(default_factory=list)
    converged_errors: list[float] = field(default_factory=list)
    yaw_errors: list[float] = field(default_factory=list)
    converged: int = 0
    samples: int = 0
    contact_s: float = 0.0
    contact_samples: int = 0
    contact_steps: int = 0
    contact_events: int = 0
    contact_physics_s: float = 0.0
    contact_details: list[dict[str, Any]] = field(default_factory=list)
    final_error: float = math.nan
    final_localization_error: float = math.nan
    final_converged: bool | None = None

    def summary(self) -> dict[str, Any]:
        rms = math.sqrt(sum(e * e for e in self.errors) / len(self.errors)) if self.errors else 0
        yaw_rms = (
            math.sqrt(sum(e * e for e in self.yaw_errors) / len(self.yaw_errors))
            if self.yaw_errors
            else 0
        )
        return {
            "destination": self.destination,
            "ok": bool(self.result and self.result.ok),
            "reason": self.result.reason if self.result else None,
            "seconds": round(self.seconds, 1),
            "rms_m": round(rms, 3),
            "max_m": round(max(self.errors, default=0.0), 3),
            "max_converged_m": (
                round(max(self.converged_errors), 3) if self.converged_errors else None
            ),
            "yaw_rms_deg": round(math.degrees(yaw_rms), 1),
            "converged_pct": round(100 * self.converged / max(self.samples, 1)),
            "contact_s": round(self.contact_s, 2),
            "contact_samples": self.contact_samples,
            "contact_steps": self.contact_steps,
            "contact_events": self.contact_events,
            "contact_physics_s": round(self.contact_physics_s, 3),
            "contact_details": self.contact_details,
            "final_error_m": round(self.final_error, 3),
            "final_localization_error_m": round(self.final_localization_error, 3),
            "final_converged": self.final_converged,
        }


def scene_with(tmp_path: Path, *geoms: str) -> Path:
    scene = apartment_dir() / "scene_appartement.xml"
    if not geoms:
        return scene
    text = scene.read_text()
    extra = "".join(
        geom.replace("<geom ", '<geom group="3" contype="1" conaffinity="1" rgba="0.6 0.4 0.2 1" ')
        for geom in geoms
    )
    head, tail = text.rsplit("</worldbody>", 1)
    copy = tmp_path / "scene_with_obstacles.xml"
    copy.write_text(head + extra + "</worldbody>" + tail)
    return copy


def capture_final(leg: Leg, initial: dict, final: dict, navigator: Navigator) -> None:
    leg.final_error = math.dist(final["position"], navigator.places[leg.destination])
    if navigator.localizer is not None:
        estimate = navigator.localizer.current()
        leg.final_localization_error = math.dist(final["position"], (estimate.x, estimate.y))
        leg.final_converged = estimate.converged
        if estimate.converged:
            leg.converged_errors.append(leg.final_localization_error)
    leg.contact_steps = final["contact_steps"] - initial["contact_steps"]
    leg.contact_events = final["contact_events"] - initial["contact_events"]
    leg.contact_physics_s = leg.contact_steps * final["contact_step_s"]
    leg.contact_details = [
        event for event in final["contact_details"] if event["event"] > initial["contact_events"]
    ]


async def drive(stack: SimStack, governor: Governor, navigator: Navigator, destination: str) -> Leg:
    leg = Leg(destination)
    initial = await stack.probe()
    leg.contact_samples = int(bool(initial["contact_count"]))
    started = await governor.go_to(destination, mode="walk")
    loop = asyncio.get_running_loop()
    at = loop.time()
    if not started.ok or isinstance(started, ImmediateToolResult):
        leg.result = started
        await asyncio.sleep(2.0)
        final = await stack.probe()
        capture_final(leg, initial, final, navigator)
        return leg
    waiter = asyncio.create_task(governor.wait_motion())
    contact_since: float | None = None
    try:
        async with asyncio.timeout(LEG_TIMEOUT_S):
            while not waiter.done():
                probe = await stack.probe()
                estimate = navigator.localizer.current() if navigator.localizer else None
                now = loop.time() - at
                if estimate is not None and estimate.std_xy < math.inf:
                    leg.samples += 1
                    leg.converged += estimate.converged
                    error = math.dist(probe["position"], (estimate.x, estimate.y))
                    leg.errors.append(error)
                    if estimate.converged:
                        leg.converged_errors.append(error)
                    yaw = probe["yaw"] - estimate.yaw
                    leg.yaw_errors.append(abs(math.atan2(math.sin(yaw), math.cos(yaw))))
                if probe["contact_count"]:
                    leg.contact_samples += 1
                    contact_since = now if contact_since is None else contact_since
                elif contact_since is not None:
                    leg.contact_s = max(leg.contact_s, now - contact_since)
                    contact_since = None
                if probe["contact_steps"] > initial["contact_steps"]:
                    await governor.stop(reason="diagnostic collision")
                    break
                if leg.samples % 20 == 1 and estimate is not None:
                    print(
                        f"{destination} {now:5.1f}s true=({probe['position'][0]:+.2f}, "
                        f"{probe['position'][1]:+.2f}) est=({estimate.x:+.2f}, {estimate.y:+.2f}) "
                        f"err={leg.errors[-1] if leg.errors else math.nan:.2f} "
                        f"std={estimate.std_xy:.2f} conv={estimate.converged} "
                        f"contacts={probe['contact_count']}",
                        flush=True,
                    )
                await asyncio.sleep(0.25)
    except TimeoutError:
        await governor.stop()
    leg.result = await waiter if waiter.done() else None
    leg.seconds = loop.time() - at
    if contact_since is not None:
        leg.contact_s = max(leg.contact_s, leg.seconds - contact_since)
    await asyncio.sleep(2.0)
    final = await stack.probe()
    leg.contact_samples += bool(final["contact_count"])
    capture_final(leg, initial, final, navigator)
    return leg


def report(name: str, legs: list[Leg], navigator: Navigator) -> list[dict[str, Any]]:
    rows = [leg.summary() for leg in legs]
    for row in rows:
        print(f"[{name}] {json.dumps(row, ensure_ascii=False)}", flush=True)
    if path := os.environ.get("DUCK_NAV_RESULTS"):
        with Path(path).open("a") as out:
            for row in rows:
                out.write(json.dumps({"scenario": name, **row}, ensure_ascii=False) + "\n")
    assert navigator.localizer is not None
    stats = navigator.localizer.stats
    print(f"[{name}] frames={stats.frames} used={stats.used} skipped={stats.skipped}", flush=True)
    return rows


async def run(tmp_path: Path, name: str, scene: Path, destinations: list[str]) -> list[Leg]:
    nav_map = NavigationMap.load(apartment_dir() / "navigation_appartement.json")
    async with sim_stack(tmp_path, scene, tof=True) as stack:
        startup = await stack.probe()
        assert startup["contact_steps"] == 0, startup
        localizer = EstimatedPose(
            nav_map, stack.link.state, TcpTofStream(stack.tof_url), clock=SystemClock().now
        )
        # The SIT keyframe puts the duck on the dock, `départ`, facing +x: a known start.
        localizer.start_at("départ@0")
        localizer.start()
        navigator = Navigator(
            nav_map, localizer, oracle=SimPose(stack.body_url), localizer=localizer
        )
        governor = Governor(
            stack.link,
            Settings(max_move_s=30, walk_assist=True),
            SystemClock(),
            Journal(tmp_path / "journal"),
            navigator=navigator,
        )
        await governor.start()
        legs: list[Leg] = []
        try:
            assert navigator.honest
            for destination in destinations:
                leg = await drive(stack, governor, navigator, destination)
                legs.append(leg)
                if not (leg.result and leg.result.ok):
                    break
        finally:
            report(name, legs, navigator)
            await governor.close()
            await navigator.close()
        return legs


async def test_honest_tour_of_the_apartment(tmp_path: Path) -> None:
    legs = await run(tmp_path, "plain", scene_with(tmp_path), ["ma chambre", "salon"])
    for leg in legs:
        assert leg.result is not None and leg.result.ok, leg.summary()
        assert leg.final_error <= 0.20, leg.summary()
        assert leg.final_converged and leg.final_localization_error <= 0.20, leg.summary()
        assert max(leg.converged_errors, default=0.0) <= 0.20, leg.summary()
        assert leg.contact_steps == 0 and leg.contact_samples == 0, leg.summary()
    assert len(legs) == 2


async def test_honest_return_from_corridor_with_biased_initial_pose(tmp_path: Path) -> None:
    nav_map = NavigationMap.load(apartment_dir() / "navigation_appartement.json")
    async with sim_stack(tmp_path, scene_with(tmp_path)) as stack:
        await stack.place([-2.72, -0.515], math.radians(-4.6))
        await asyncio.sleep(0.5)
        assert (await stack.probe())["contact_steps"] == 0
        localizer = EstimatedPose(
            nav_map, stack.link.state, TcpTofStream(stack.tof_url), clock=SystemClock().now
        )
        localizer.reset_pose(Pose(-2.68, -0.71, math.radians(-8)), 0.06, math.radians(3.3))
        localizer.start()
        navigator = Navigator(
            nav_map, localizer, oracle=SimPose(stack.body_url), localizer=localizer
        )
        governor = Governor(
            stack.link,
            Settings(max_move_s=30, walk_assist=True),
            SystemClock(),
            Journal(tmp_path / "journal"),
            navigator=navigator,
        )
        await governor.start()
        try:
            await asyncio.sleep(0.5)
            leg = await drive(stack, governor, navigator, "départ")
        finally:
            await governor.close()
            await navigator.close()
        assert leg.result is not None and leg.result.ok, leg.summary()
        assert leg.final_error <= 0.20, leg.summary()
        assert leg.final_converged and leg.final_localization_error <= 0.20, leg.summary()
        assert leg.contact_steps == 0 and leg.contact_samples == 0, leg.summary()


async def test_contact_probe_records_physics_steps_between_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contact_log = tmp_path / "contacts.jsonl"
    monkeypatch.setenv("DUCK_DIAG_CONTACT_LOG", str(contact_log))
    async with sim_stack(tmp_path, scene_with(tmp_path, BOX), tof=False) as stack:
        before = await stack.probe()
        await stack.place([-2.5, -0.14], 0)
        await asyncio.sleep(0.05)
        await stack.place([0.0, 0.0], 0)
        await asyncio.sleep(0.2)
        after = await stack.probe()
    assert before["contact_count"] == after["contact_count"] == 0
    assert after["contact_steps"] > before["contact_steps"]
    assert after["contact_events"] > before["contact_events"]
    assert after["contact_details"][-1]["robot_body"]
    assert after["contact_step_s"] > 0
    records = [json.loads(line) for line in contact_log.read_text().splitlines()]
    assert records[0] == {"ready": True}
    assert len(records) - 1 == after["contact_events"]


async def test_honest_detour_around_an_unmapped_box(tmp_path: Path) -> None:
    (leg,) = await run(tmp_path, "box", scene_with(tmp_path, BOX), ["ma chambre"])
    assert leg.contact_steps == 0 and leg.contact_samples == 0, leg.summary()
    assert leg.result is not None, leg.summary()
    assert leg.final_converged is False or leg.final_localization_error <= 0.20, leg.summary()
    assert max(leg.converged_errors, default=0.0) <= 0.20, leg.summary()
    if not leg.result.ok:
        assert leg.result.reason, leg.summary()
        pytest.xfail(f"unmapped box could not be bypassed: {leg.result.reason}")
    assert leg.final_error <= 0.20, leg.summary()


async def test_honest_gives_up_at_a_closed_door(tmp_path: Path) -> None:
    (leg,) = await run(tmp_path, "door", scene_with(tmp_path, DOOR), ["ma chambre"])
    assert leg.result is not None and not leg.result.ok, leg.summary()
    assert leg.contact_steps == 0 and leg.contact_samples == 0, leg.summary()
    assert leg.result.reason, leg.summary()
    assert leg.final_converged is False or leg.final_localization_error <= 0.20, leg.summary()
    assert max(leg.converged_errors, default=0.0) <= 0.20, leg.summary()
