"""Honest go_to on the synthetic flat: localization, sweeps, unmapped obstacles, refusals."""

import asyncio
import json
import math
from pathlib import Path

import numpy as np
import pytest

from coincoin.clock import FakeClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.governor import Governor
from coincoin.robot.localization import EstimatedPose, LikelihoodField
from coincoin.robot.navigation import Navigator
from tests.fakes.fake_tof import Box, ClockedTof, TofRobotLink, World, feed, flat, nav_map

GRID = flat()
MAP = nav_map(GRID)
# A closed door: the only doorway between the two rooms, which the map shows open.
DOOR = Box(3.9, 1.95, 4.1, 2.95, 2.0)
CHAIR = Box(2.55, 1.75, 2.85, 2.05, 0.45)
WALLS = LikelihoodField(MAP)


async def _inline(func, /, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def _deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plans and sweep matches run inline, so thread timing never leaks into the fake clock."""
    monkeypatch.setattr(asyncio, "to_thread", _inline)


class Rig:
    def __init__(self, tmp_path: Path, boxes: list[Box], start: str, truth: tuple[float, ...]):
        self.clock = FakeClock()
        self.link = TofRobotLink(
            self.clock, World(GRID, boxes), odom_scale=1.05, odom_yaw_scale=0.97
        )
        self.link.place_at(*truth)
        self.depth = ClockedTof(self.link, self.clock, before=self.track)
        self.localizer = EstimatedPose(MAP, self.link.state, self.depth, clock=self.clock.now)
        self.localizer.start_at(start)
        self.navigator = Navigator(MAP, self.localizer, localizer=self.localizer)
        self.journal_dir = tmp_path
        self.governor = Governor(
            self.link, Settings(), self.clock, Journal(tmp_path), navigator=self.navigator
        )
        self.boxes = boxes
        self.closest = math.inf
        self.wall = math.inf
        self.errors: list[float] = []

    def track(self) -> None:
        x, y = self.link.position[:2]
        for box in self.boxes:
            dx = max(box.x0 - x, 0, x - box.x1)
            dy = max(box.y0 - y, 0, y - box.y1)
            self.closest = min(self.closest, math.hypot(dx, dy))
        # Gap between the body and the nearest mapped wall: the fake world lets it pass through.
        gap = float(WALLS.lookup(np.array([x]), np.array([y]))[0]) - MAP.robot_radius
        self.wall = min(self.wall, gap)
        if self.localizer.current().converged:
            estimate = self.localizer.current()
            self.errors.append(math.dist((estimate.x, estimate.y), (x, y)))

    async def __aenter__(self) -> "Rig":
        self.feeder = asyncio.create_task(feed(self.localizer, self.link, self.depth))
        await self.governor.start()
        await self.run_for(0.5)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.governor.close()
        self.feeder.cancel()

    async def step(self) -> None:
        if self.localizer.relocalizing:
            # The sweep match runs in a worker thread: let it finish on real time.
            await asyncio.sleep(0.01)
            return
        self.clock.advance(0.05)
        for _ in range(10):
            await asyncio.sleep(0)

    async def run_for(self, seconds: float) -> None:
        for _ in range(round(seconds / 0.05)):
            await self.step()

    async def go_to(self, place: str, mode: str | None = None, limit_s: float = 150):
        call = asyncio.create_task(self.governor.go_to(place, mode))
        while not call.done():
            await self.step()
        started = call.result()
        if not started.ok:
            return started, None
        motion = self.governor._motion
        assert motion is not None
        deadline = self.clock.now() + limit_s
        while not motion.done() and self.clock.now() < deadline:
            await self.step()
        return started, await motion

    def journal(self) -> list[dict]:
        records = []
        for path in sorted(self.journal_dir.rglob("*.jsonl")):
            records += [json.loads(line) for line in path.read_text().splitlines() if line]
        return records

    def calls(self, method: str) -> int:
        return sum(record.method == method for record in self.link.records)


async def test_honest_go_to_from_a_known_start_arrives(tmp_path: Path) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        started, result = await rig.go_to("cuisine")
        assert started.ok and started.detail == "going to cuisine (walk)"
        assert result is not None and result.ok, result
        assert math.dist(rig.link.position[:2], MAP.places["cuisine"]) < 0.35
        assert max(rig.errors) < 0.25
        assert rig.calls("robot.look") == 0
    finals = [r for r in rig.journal() if r.get("method") == "go_to" and "pose_source" in r]
    assert finals and all(r["pose_source"] == "estimated" for r in finals)


async def test_unknown_start_sweeps_then_refuses(tmp_path: Path) -> None:
    async with Rig(tmp_path, [], "", (1.5, 1.5, 0.0)) as rig:
        started, _ = await rig.go_to("cuisine")
        assert not started.ok
        assert "I do not know where I am, so I will not walk to cuisine" in started.reason
        assert rig.calls("robot.look") >= 7
        assert rig.calls("robot.move") == 0
    events = [r.get("event") for r in rig.journal() if r.get("type") == "localization"]
    assert "sweep" in events and "no_prior" in events


async def test_start_near_a_place_is_confirmed_by_a_head_sweep(tmp_path: Path) -> None:
    async with Rig(tmp_path, [], "cuisine", (6.3, 1.7, 2.5)) as rig:
        assert not rig.localizer.current().converged
        started, result = await rig.go_to("salon")
        assert started.ok, started
        assert rig.calls("robot.look") >= 7
        assert result is not None and result.ok, result
        assert math.dist(rig.link.position[:2], MAP.places["salon"]) < 0.35


async def test_roller_is_refused_without_the_simulator_pose(tmp_path: Path) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        refused, _ = await rig.go_to("cuisine", mode="roller")
        assert not refused.ok and "contact odometry cannot see a glide" in refused.reason
        rig.link.mode = "roller"
        await rig.run_for(31)  # the governor re-reads the mode every 30 s
        refused, _ = await rig.go_to("cuisine")
        assert not refused.ok and "ask for walk mode" in refused.reason
        assert rig.calls("robot.move") == 0


async def test_unmapped_chair_is_seen_and_walked_around(tmp_path: Path) -> None:
    async with Rig(tmp_path, [CHAIR], "salon@0", (1.5, 1.5, 0.0)) as rig:
        started, result = await rig.go_to("cuisine")
        assert started.ok
        assert result is not None and result.ok, result
        assert rig.closest > 0.08
        assert rig.wall > -0.01  # touching, at the map grid's resolution, but never through
        assert rig.navigator.obstacles
    replans = [r for r in rig.journal() if "obstacle_replan" in r]
    assert replans


async def test_closed_door_stops_the_duck_and_says_so(tmp_path: Path) -> None:
    async with Rig(tmp_path, [DOOR], "salon@0", (1.5, 1.5, 0.0)) as rig:
        started, result = await rig.go_to("cuisine")
        assert started.ok
        assert result is not None and not result.ok
        assert "blocked by something that is not on my map (a closed door?)" in result.reason
        assert rig.closest > 0.08
        assert rig.wall > -0.01  # touching, at the map grid's resolution, but never through
        assert rig.link.twist == [0.0, 0.0, 0.0]


async def test_kidnapped_duck_stops_and_says_it_is_lost(tmp_path: Path) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        call = asyncio.create_task(rig.governor.go_to("cuisine"))
        while not call.done():
            await rig.step()
        assert call.result().ok
        motion = rig.governor._motion
        assert motion is not None
        await rig.run_for(2)
        rig.link.place_at(6.8, 4.3, -math.pi / 2)
        for _ in range(4000):
            if motion.done():
                break
            await rig.step()
        result = await motion
        assert not result.ok
        assert "I lost track of where I am on the way to cuisine and stopped" in result.reason
        assert rig.link.twist == [0.0, 0.0, 0.0]
        assert rig.calls("robot.look") >= 7


@pytest.mark.parametrize("pose", ["estimated", "oracle"])
def test_settings_choose_the_pose_source(pose: str) -> None:
    assert Settings(nav_pose=pose).nav_pose == pose  # type: ignore[arg-type]
