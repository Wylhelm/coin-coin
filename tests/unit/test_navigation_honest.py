"""Honest go_to on the synthetic flat: localization, sweeps, unmapped obstacles, refusals."""

import asyncio
import json
import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from coincoin.clock import FakeClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.governor import Governor, ImmediateToolResult
from coincoin.robot.localization import EstimatedPose, LikelihoodField, Odom
from coincoin.robot.navigation import Navigator, Pose
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


async def test_roller_is_refused_when_the_depth_stream_measures_no_glide(tmp_path: Path) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        # The ToF stops delivering frames: with no glide to measure, a trip on skates would drift
        # with nothing to check it against, so the refusal stands.
        rig.depth.closed = True
        await rig.run_for(1.0)
        assert not rig.localizer.glide_odometry
        refused, _ = await rig.go_to("cuisine", mode="roller")
        assert not refused.ok and "no glide I can trust" in refused.reason
        assert "simulator pose" not in refused.reason
        assert rig.link.mode == "walk"  # refused, not switched behind the user's back
        rig.link.mode = "roller"
        await rig.run_for(31)  # the governor re-reads the mode every 30 s
        refused, _ = await rig.go_to("cuisine")
        assert not refused.ok and "set_mode walk" in refused.reason
        refused, _ = await rig.go_to("cuisine", mode="walk")
        assert not refused.ok and "set_mode walk" in refused.reason
        assert rig.link.mode == "roller" and rig.calls("robot.setMode") == 0
        assert rig.calls("robot.move") == 0


async def test_go_to_on_skates_uses_the_glide_the_depth_frames_measure(tmp_path: Path) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        assert rig.localizer.glide_odometry
        started, result = await rig.go_to("cuisine", mode="roller")
        assert started.ok, started
        assert "roller" in started.detail
        assert rig.link.mode == "roller"
        assert result is not None and result.ok, result
        assert math.dist(rig.link.position[:2], MAP.places["cuisine"]) < 0.5
        assert max(rig.errors) < 0.4


async def test_status_says_where_the_duck_is(tmp_path: Path) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        status = await rig.governor.status()
        assert status.ok and "position: a environ 0.0 m du repere salon" in status.detail
    async with Rig(tmp_path, [], "", (1.5, 1.5, 0.0)) as rig:
        status = await rig.governor.status()
        assert "position: incertaine" in status.detail


async def test_skating_invalidates_position_until_a_walking_sweep(tmp_path: Path) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        assert rig.localizer.current().converged
        rig.governor.navigation = "arrived at salon"
        assert (await rig.governor.set_mode("roller")).ok
        assert rig.localizer.glide and not rig.localizer.current().converged
        status = (await rig.governor.status()).detail
        assert "position: incertaine" in status
        assert "arrived at salon" not in status
        assert (await rig.governor.set_mode("walk")).ok
        assert not rig.localizer.glide and not rig.localizer.current().converged
        started, result = await rig.go_to("cuisine")
        assert started.ok and result is not None and result.ok
        assert rig.calls("robot.look") >= 7


@pytest.mark.parametrize("assisted", [False, True])
async def test_pending_equipment_change_invalidates_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, assisted: bool
) -> None:
    from coincoin.robot import governor as module

    async def pending_equipment(*args):
        await asyncio.Event().wait()

    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        rig.link.remote_mode_switch = False
        rig.governor.navigation = "arrived at salon"
        if assisted:
            rig.governor.settings.sim_equipment_socket = "/simulated/duck.sock"
            monkeypatch.setattr(module, "select_mode", pending_equipment)
        result = await rig.governor.set_mode("roller")
        assert result.ok and rig.governor.awaiting_person == "roller"
        status = (await rig.governor.status()).detail
        assert "position: incertaine" in status
        assert "arrived at salon" not in status
        assert ("the simulator is switching" in status) is assisted
        assert not rig.localizer.current().converged


async def test_pickup_invalidates_a_confirmed_position(tmp_path: Path) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        assert rig.localizer.current().converged
        rig.governor.navigation = "arrived at salon"
        rig.link.safety["picked_up"] = True
        await rig.run_for(0.2)
        assert not rig.localizer.current().converged
        assert "arrived at salon" not in (await rig.governor.status()).detail


async def test_status_reports_manual_motion_as_unconfirmed_without_stopping_it(
    tmp_path: Path,
) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        rig.governor.navigation = "arrived at salon"
        started = await rig.governor.move(0.2, 0, 0, 1)
        assert started.ok
        await rig.run_for(0.3)
        assert rig.governor.moving
        status = (await rig.governor.status()).detail
        assert "position: incertaine" in status
        assert "arrived at salon" not in status
        assert rig.governor.moving
        await rig.run_for(1.2)
        assert "position: incertaine" in (await rig.governor.status()).detail


async def test_sweep_does_not_reuse_forward_state_or_its_last_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coincoin.robot import governor as module

    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        sweeps = 0
        moves_at_sweep = 0

        def bumper(scan):
            if sweeps and rig.calls("robot.move") > moves_at_sweep:
                return np.zeros((0, 2))
            return np.array([[0.15, 0.0]])

        async def relocalize(generation, navigator, prior):
            nonlocal sweeps, moves_at_sweep
            sweeps += 1
            moves_at_sweep = rig.calls("robot.move")
            await rig.clock.sleep(0.5)
            return replace(prior, converged=True)

        monkeypatch.setattr(module, "obstacle_points", bumper)
        monkeypatch.setattr(module, "blocking_points", lambda *args, **kwargs: np.zeros((0, 2)))
        monkeypatch.setattr(module, "unexplained_points", lambda *args, **kwargs: np.zeros((0, 2)))
        monkeypatch.setattr(rig.governor, "_relocalize", relocalize)
        started, result = await rig.go_to("cuisine")
        assert started.ok and result is not None and result.ok, result
        assert sweeps == 1
        assert rig.calls("robot.move") > moves_at_sweep


async def test_empty_sweep_does_not_reconfirm_an_old_pose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        assert rig.localizer.current().converged
        rig.governor._unsupervised_motion = True

        async def no_observations(generation, localizer):
            return []

        monkeypatch.setattr(rig.governor, "_sweep", no_observations)
        started, _ = await rig.go_to("cuisine")
        assert not started.ok and "I do not know where I am" in started.reason
        assert not rig.localizer.current().converged
        assert rig.calls("robot.move") == 0


async def test_go_to_stops_an_active_manual_motion_before_sweeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        started = await rig.governor.move(0.2, 0, 0, 5)
        assert started.ok
        await rig.run_for(0.3)
        assert rig.governor.moving and rig.calls("robot.move") > 0

        async def stopped_sweep(generation, localizer):
            assert not rig.governor.moving
            assert rig.calls("robot.stop") > 0
            return []

        monkeypatch.setattr(rig.governor, "_sweep", stopped_sweep)
        result, _ = await rig.go_to("cuisine")
        assert not result.ok and "I do not know where I am" in result.reason
        assert not rig.governor.moving


async def test_already_at_destination_stops_the_previous_route(tmp_path: Path) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        started = await rig.governor.go_to("cuisine")
        assert started.ok
        await rig.run_for(0.2)
        assert rig.governor.moving
        already = await rig.governor.go_to("salon")
        assert isinstance(already, ImmediateToolResult) and already.detail == "already at salon"
        assert not rig.governor.moving
        assert rig.governor.navigation == "arrived at salon"
        assert rig.calls("robot.stop") > 0


async def test_rejected_sweep_stays_unconfirmed_on_the_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        prior = rig.localizer.current()

        async def sweep(*args):
            return []

        def wander(*args, **kwargs):
            pose = rig.localizer.current().pose
            rig.localizer.reset_pose(Pose(pose.x + 3, pose.y, pose.yaw), 0.01, 0.01)
            return []

        monkeypatch.setattr(rig.governor, "_sweep", sweep)
        monkeypatch.setattr(rig.localizer, "relocalize", wander)
        task = asyncio.create_task(
            rig.governor._relocalize(rig.governor.stop_generation, rig.navigator, prior)
        )
        while not task.done():
            await rig.step()
        assert not task.result().converged
        assert not rig.localizer.current().converged
        assert "position: incertaine" in (await rig.governor.status()).detail
        started, _ = await rig.go_to("cuisine")
        assert not started.ok and rig.calls("robot.move") == 0


async def test_uncertain_retry_keeps_obstacles_until_the_pose_is_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        rig.localizer.confirmed = False
        rig.navigator.set_obstacles([(1.75, 1.5)])

        async def sweep(*args):
            return []

        def rejected(scans, **kwargs):
            x, y = kwargs["centre"]
            assert rig.navigator.obstacles
            assert not kwargs["accept"](Pose(x + 0.5, y, 0))
            return []

        monkeypatch.setattr(rig.governor, "_sweep", sweep)
        monkeypatch.setattr(rig.localizer, "relocalize", rejected)
        started, _ = await rig.go_to("cuisine")
        assert not started.ok and "I do not know where I am" in started.reason
        assert rig.navigator.obstacles and rig.calls("robot.move") == 0


async def test_blocked_sweep_does_not_confirm_a_distant_lookalike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        prior = rig.localizer.current()
        rig.localizer.confirmed = False
        rig.governor._blocked_anchor = (prior.pose, rig.localizer.latest_odom())

        async def sweep(*args):
            assert rig.localizer.collecting_sweep
            return []

        def rejected(scans, **kwargs):
            assert not rig.localizer.collecting_sweep
            assert kwargs["exclude_unmapped"]
            assert kwargs["accept"](prior.pose)
            assert not kwargs["accept"](Pose(prior.x + 0.7, prior.y, prior.yaw))
            return []

        monkeypatch.setattr(rig.governor, "_sweep", sweep)
        monkeypatch.setattr(rig.localizer, "relocalize", rejected)
        started, _ = await rig.go_to("cuisine")
        assert not started.ok and rig.governor._blocked_anchor is not None
        assert not rig.localizer.current().converged and rig.calls("robot.move") == 0


async def test_sweep_transports_the_prior_to_the_reference_odometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        prior = rig.localizer.current()
        start = rig.localizer.latest_odom()
        scan = rig.localizer.latest_scan()
        assert start is not None and scan is not None
        end = Odom(start.x + 0.1, start.y, start.yaw + 0.1, start.z)
        expected = rig.governor._anchor_at_odom(prior.pose, start, end)

        async def sweep(*args):
            assert rig.localizer.collecting_sweep
            return [replace(scan, odom=end)]

        def rejected(scans, **kwargs):
            assert kwargs["prior"] == expected
            assert kwargs["centre"] == (expected.x, expected.y)
            assert kwargs["heading"] == expected.yaw
            return []

        monkeypatch.setattr(rig.governor, "_sweep", sweep)
        monkeypatch.setattr(rig.localizer, "relocalize", rejected)
        task = asyncio.create_task(
            rig.governor._relocalize(rig.governor.stop_generation, rig.navigator, prior)
        )
        while not task.done():
            await rig.step()
        assert not task.result().converged
        assert not rig.localizer.collecting_sweep


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
        assert "blocked by something that is not on my map" in result.reason
        assert "stopped" in result.reason
        assert rig.closest > 0.08
        assert rig.wall > -0.01  # touching, at the map grid's resolution, but never through
        assert rig.link.twist == [0.0, 0.0, 0.0]
        assert not rig.localizer.confirmed
        rig.localizer._fit = 1.0
        assert not rig.localizer.current().converged
        assert "position: incertaine" in (await rig.governor.status()).detail


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
        assert "I lost track of where I am" in result.reason and "stopped" in result.reason
        assert rig.link.twist == [0.0, 0.0, 0.0]
        assert rig.calls("robot.look") >= 7


async def test_tracking_conflict_stops_without_the_uncertainty_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        started = await rig.governor.go_to("cuisine")
        assert started.ok
        motion = rig.governor._motion
        assert motion is not None
        await rig.run_for(0.2)
        stops = rig.calls("robot.stop")
        moves = rig.calls("robot.move")

        async def unconfirmed(generation, navigator, prior):
            assert rig.calls("robot.stop") > stops
            return replace(prior, converged=False)

        monkeypatch.setattr(rig.governor, "_relocalize", unconfirmed)
        rig.localizer.confirmed = False
        began = rig.clock.now()
        while not motion.done() and rig.clock.now() - began < 1:
            await rig.step()
        assert motion.done()
        result = await motion
        assert not result.ok and "I lost track of where I am" in result.reason
        assert rig.calls("robot.move") == moves
        assert rig.link.twist == [0.0, 0.0, 0.0]


@pytest.mark.parametrize("pose", ["estimated", "oracle"])
def test_settings_choose_the_pose_source(pose: str) -> None:
    assert Settings(nav_pose=pose).nav_pose == pose  # type: ignore[arg-type]


async def test_go_to_after_a_manual_move_relocalizes_first_and_reports_its_state(
    tmp_path: Path,
) -> None:
    async with Rig(tmp_path, [], "salon@0", (1.5, 1.5, 0.0)) as rig:
        call = asyncio.create_task(rig.governor.move(0.2, 0.0, 0.0, 2.0))
        while not call.done():
            await rig.step()
        assert call.result().ok
        motion = rig.governor._motion
        assert motion is not None
        while not motion.done():
            await rig.step()
        assert rig.calls("robot.look") == 0
        started, result = await rig.go_to("cuisine")
        assert started.ok, started
        assert rig.calls("robot.look") >= 7
        assert result is not None and result.ok, result
        assert rig.governor.navigation == "arrived at cuisine"
        assert rig.localizer.travel_since_fix < 6


async def test_navigation_state_says_why_a_trip_stopped(tmp_path: Path) -> None:
    async with Rig(tmp_path, [DOOR], "salon@0", (1.5, 1.5, 0.0)) as rig:
        started, result = await rig.go_to("cuisine")
        assert started.ok and rig.governor.navigation.startswith("stopped before reaching")
        assert result is not None and result.reason in rig.governor.navigation
