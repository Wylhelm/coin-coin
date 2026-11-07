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
from coincoin.robot.governor import Governor, ToolResult
from coincoin.robot.localization import EstimatedPose, TcpTofStream
from coincoin.robot.navigation import NavigationMap, Navigator, SimPose
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
    yaw_errors: list[float] = field(default_factory=list)
    converged: int = 0
    samples: int = 0
    contact_s: float = 0.0
    final_error: float = math.nan

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
            "yaw_rms_deg": round(math.degrees(yaw_rms), 1),
            "converged_pct": round(100 * self.converged / max(self.samples, 1)),
            "contact_s": round(self.contact_s, 2),
            "final_error_m": round(self.final_error, 3),
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


async def drive(stack: SimStack, governor: Governor, navigator: Navigator, destination: str) -> Leg:
    leg = Leg(destination)
    started = await governor.go_to(destination, mode="walk")
    loop = asyncio.get_running_loop()
    at = loop.time()
    if not started.ok:
        leg.result = started
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
                    leg.errors.append(math.dist(probe["position"], (estimate.x, estimate.y)))
                    yaw = probe["yaw"] - estimate.yaw
                    leg.yaw_errors.append(abs(math.atan2(math.sin(yaw), math.cos(yaw))))
                if probe["contact_count"]:
                    contact_since = now if contact_since is None else contact_since
                elif contact_since is not None:
                    leg.contact_s = max(leg.contact_s, now - contact_since)
                    contact_since = None
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
    final = await stack.probe()
    leg.final_error = math.dist(final["position"], navigator.places[destination])
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
        assert leg.final_error <= 0.35, leg.summary()
        assert leg.contact_s < 2.0, leg.summary()
    assert len(legs) == 2


@pytest.mark.xfail(
    reason="measured limitation (docs/localization.md): in the open area west of the start the "
    "estimate is biased 0.1-0.25 m south, so the mapped walls seen around the box look "
    "unexplained, get marked too, and close the corridor",
    strict=False,
)
async def test_honest_detour_around_an_unmapped_box(tmp_path: Path) -> None:
    (leg,) = await run(tmp_path, "box", scene_with(tmp_path, BOX), ["ma chambre"])
    assert leg.result is not None and leg.result.ok, leg.summary()
    assert leg.final_error <= 0.35, leg.summary()
    assert leg.contact_s < 2.0, leg.summary()


async def test_honest_gives_up_at_a_closed_door(tmp_path: Path) -> None:
    (leg,) = await run(tmp_path, "door", scene_with(tmp_path, DOOR), ["ma chambre"])
    assert leg.result is not None and not leg.result.ok, leg.summary()
    assert leg.contact_s < 2.0, leg.summary()
    assert leg.result.reason, leg.summary()
