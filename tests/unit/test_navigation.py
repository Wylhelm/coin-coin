"""Room navigation: planner, steering and the governor's go_to, without a simulator."""

import asyncio
import base64
import json
import math
from pathlib import Path

import pytest

from coincoin.clock import FakeClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.realtime.tools.robot import schemas
from coincoin.robot.governor import Governor
from coincoin.robot.navigation import NavigationMap, Navigator, Pose, Steering
from tests.fakes.fake_robotd import FakeRobotLink

SIZE = 60
RESOLUTION = 0.1


def room_with_wall(door: bool = True) -> NavigationMap:
    """A 6 m square room centred on the origin, split at x=0 by a wall with an optional door."""
    clearance = bytearray(SIZE * SIZE)
    for row in range(SIZE):
        for col in range(SIZE):
            edge = min(row, col, SIZE - 1 - row, SIZE - 1 - col)
            wall = abs(col - SIZE // 2)
            if not (door and abs(row - SIZE // 2) <= 4):
                edge = min(edge, wall)
            clearance[row * SIZE + col] = min(100, edge * 10)
    origin = (-SIZE / 2 * RESOLUTION, -SIZE / 2 * RESOLUTION)
    return NavigationMap(
        bytes(clearance),
        SIZE,
        SIZE,
        origin,
        RESOLUTION,
        0.12,
        {"départ": (-2.0, -2.0), "chambre": (2.0, 2.0)},
    )


def test_load_round_trip(tmp_path: Path) -> None:
    source = room_with_wall()
    path = tmp_path / "nav.json"
    path.write_text(
        json.dumps(
            {
                "clearance_cm_uint8_base64": base64.b64encode(source.clearance).decode(),
                "width": SIZE,
                "height": SIZE,
                "origin_xy_m": list(source.origin),
                "resolution_m": RESOLUTION,
                "robot_radius_m": 0.12,
                "places": {"salon": {"x": 1.0, "y": 0.5}},
            }
        ),
        encoding="utf-8",
    )
    loaded = NavigationMap.load(path)
    assert loaded.places == {"salon": (1.0, 0.5)}
    assert loaded.clearance == source.clearance
    with pytest.raises(ValueError):
        NavigationMap(b"\0", 2, 2, (0, 0), 0.1, 0.1, {})


def test_plan_goes_through_the_door_and_stays_clear() -> None:
    nav = room_with_wall()
    route = nav.plan((-2.0, -2.0), (2.0, 2.0))
    assert route is not None
    assert math.dist(route[-1], (2.0, 2.0)) < 0.15
    points = [(-2.0, -2.0), *route]
    crossing = [
        a[1] + (b[1] - a[1]) * (0 - a[0]) / (b[0] - a[0])
        for a, b in zip(points, points[1:], strict=False)
        if (a[0] < 0) != (b[0] < 0)
    ]
    assert len(crossing) == 1 and abs(crossing[0]) < 0.45
    for a, b in zip(points, points[1:], strict=False):
        for step in range(21):
            t = step / 20
            point = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            assert nav.free(nav.cell(point))


def test_plan_refuses_unreachable_goals() -> None:
    nav = room_with_wall(door=False)
    assert nav.plan((-2.0, -2.0), (2.0, 2.0)) is None
    assert nav.plan((-2.0, -2.0), (50.0, 50.0)) is None


def test_shortcut_does_not_cut_between_blocked_cells() -> None:
    clearance = bytearray([100] * 25)
    clearance[1 * 5 + 2] = clearance[2 * 5 + 1] = 0
    nav = NavigationMap(bytes(clearance), 5, 5, (0, 0), 0.1, 0.1, {})
    assert not nav._visible((1, 1), (3, 3))
    route = nav.plan((0.1, 0.1), (0.3, 0.3))
    assert route is not None
    assert len(route) > 1


def test_plan_includes_the_free_cell_when_the_start_is_blocked() -> None:
    nav = room_with_wall()
    start = (0.0, 2.0)
    snapped = nav._snap(start)
    assert snapped is not None
    route = nav.plan(start, (2.0, 2.0))
    assert route is not None
    assert route[0] == nav.centre(snapped)
    assert route[-1] == (2.0, 2.0)


def test_plan_does_not_target_a_blocked_goal_from_its_snapped_cell() -> None:
    nav = room_with_wall()
    goal = (0.0, 2.0)
    snapped = nav._snap(goal)
    assert snapped is not None
    start = nav.centre(snapped)
    assert nav.plan(start, goal) == [start]


def test_clearance_is_measured_beyond_the_robot() -> None:
    nav = room_with_wall()
    assert nav.clearance_m((-1.5, -1.5)) == pytest.approx(0.88)
    assert nav.clearance_m((0.0, 2.0)) == 0
    assert nav.clearance_m((99.0, 0.0)) == 0


def steering_command(steering: Steering, pose: Pose, now: float) -> dict[str, float]:
    command = steering.update(pose, now)
    assert command is not None
    return command


def test_walk_pivots_then_walks_with_steering() -> None:
    steering = Steering([(1.0, 0.0), (1.0, 1.0)], (0.25, 0.2, 1.0), "walk")
    assert steering.update(Pose(0, 0, 2.0), 0) == {"vx": 0.0, "vy": 0.0, "vyaw": -1.0}
    command = steering.update(Pose(0, 0, 0.1), 0.1)
    assert command is not None
    assert command["vx"] == 0.25 and command["vyaw"] == pytest.approx(-0.15)
    assert steering_command(steering, Pose(0.8, 0, 0), 0.2)["vyaw"] == 1.0
    assert steering.remaining(Pose(1.0, 0.5, 0)) == pytest.approx(0.5)
    assert steering.update(Pose(1.0, 0.85, math.pi / 2), 0.3) is None


def test_roller_controls_speed_and_brakes_before_arriving() -> None:
    limits = (0.3, 0.0, 0.3)
    steering = Steering([(3.0, 0.0)], limits, "roller")
    assert steering_command(steering, Pose(0, 0, 0), 0)["vx"] == 0.3  # push from rest
    assert steering_command(steering, Pose(0.04, 0, 0), 0.1)["vx"] == 0.3  # below cruise
    assert steering_command(steering, Pose(0.08, 0, 0), 0.2)["vx"] == 0.0  # coast
    steering.update(Pose(0.12, 0, 0), 0.3)
    assert steering_command(steering, Pose(0.16, 0, 0), 0.4)["vx"] == -0.3  # too fast: brake
    steering.update(Pose(2.6, 0, 0), 0.5)
    assert steering_command(steering, Pose(2.8, 0, 0), 0.6)["vx"] == -0.3  # brake near goal
    gliding = Steering([(1.0, 0.0)], limits, "roller")
    gliding.update(Pose(0.6, 0, 0), 0)
    assert steering_command(gliding, Pose(0.75, 0, 0), 0.1)["vx"] == -0.3
    for step in range(2, 12):
        result = gliding.update(Pose(0.75, 0, 0), step / 10)
    assert result is None


def test_roller_turns_in_a_slow_arc_and_brakes_when_fast() -> None:
    limits = (0.3, 0.0, 0.3)
    steering = Steering([(-2.0, 0.0)], limits, "roller")
    assert steering.update(Pose(0, 0, 0.5), 0) == {"vx": 0.15, "vy": 0.0, "vyaw": 0.3}
    steering = Steering([(-2.0, 0.0)], limits, "roller")
    steering.update(Pose(0, 0, 0.5), 0)
    assert steering_command(steering, Pose(0.05, 0, 0.5), 0.1)["vx"] == -0.3


def test_roller_corrects_deviation_from_a_long_segment() -> None:
    steering = Steering([(-0.5, 0.4)], (0.3, 0.0, 0.3), "roller")
    steering.update(Pose(-3.4, -0.65, 0.35), 0)
    command = steering.update(Pose(-2.2, 0.1, 0), 3)
    assert command is not None
    assert command["vyaw"] < 0


def test_roller_does_not_skip_a_corner_while_still_far_from_it() -> None:
    steering = Steering([(0.8, 0), (0.8, 0.8)], (0.3, 0.0, 0.3), "roller")
    steering.update(Pose(0, 0, 0), 0)
    steering.update(Pose(0.59, 0.19, 0), 2)
    assert steering.index == 0
    steering.update(Pose(0.8, 0, 0), 4)
    assert steering.index == 1


def test_blocked_start_does_not_skip_its_nearby_escape_waypoint() -> None:
    steering = Steering(
        [(0.0, -0.1), (2.0, -0.1)],
        (0.3, 0.0, 0.3),
        "roller",
        room=lambda point: 0.0 if point[1] > -0.08 else 0.1,
    )
    steering.update(Pose(0, 0, -math.pi / 2), 0)
    assert steering.index == 0
    steering.update(Pose(0, -0.1, -math.pi / 2), 1)
    assert steering.index == 1


def test_roller_slows_down_where_walls_are_close() -> None:
    steering = Steering([(5.0, 0.0)], (0.3, 0.0, 0.3), "roller", room=lambda _: 0.0)
    steering.update(Pose(0, 0, 0), 0)
    assert steering_command(steering, Pose(0.02, 0, 0), 0.1)["vx"] == 0.0
    assert steering_command(steering, Pose(0.1, 0, 0), 0.2)["vx"] == -0.3


def test_roller_brakes_before_reaching_the_edge_of_a_narrow_corridor() -> None:
    steering = Steering([(5.0, 0.0)], (0.3, 0.0, 0.3), "roller", room=lambda _: 0.1)
    steering.update(Pose(0, 0, 0), 0)
    assert steering_command(steering, Pose(0.05, 0, 0), 0.1)["vx"] == -0.3


def test_go_to_schema_only_exists_with_places() -> None:
    assert "go_to" not in schemas(0.3, 0.2, 1.0, 10)
    schema = schemas(0.3, 0.2, 1.0, 10, ["salon", "ma chambre"])["go_to"]
    assert schema["properties"]["place"]["enum"] == ["salon", "ma chambre"]
    assert schema["properties"]["mode"]["enum"] == ["walk", "roller"]
    assert schema["required"] == ["place"]


class FakeNavigator(Navigator):
    def __init__(self, link: FakeRobotLink, nav: NavigationMap) -> None:
        self.link = link
        self.map = nav
        self.closed = False

    async def pose(self) -> Pose:
        state = self.link.state()
        assert state is not None
        x, y, _ = state["odom"]["position"]
        return Pose(x, y, state["odom"]["yaw"])

    async def close(self) -> None:
        self.closed = True


async def pump() -> None:
    for _ in range(12):
        await asyncio.sleep(0)


@pytest.fixture
async def rig(tmp_path: Path):
    clock = FakeClock()
    link = FakeRobotLink(clock)
    link.policies["simulated"] = True
    link.position[:2] = [-2.0, -2.0]
    navigator = FakeNavigator(link, room_with_wall())
    governor = Governor(link, Settings(), clock, Journal(tmp_path), navigator=navigator)
    await governor.start()
    await pump()
    yield governor, link, clock, navigator
    await governor.close()
    assert navigator.closed


async def test_go_to_arrives_and_stops(rig) -> None:
    governor, link, clock, _ = rig
    assert governor.places() == ["départ", "chambre"]
    started = await governor.go_to("chambre")
    assert started.ok and started.detail == "going to chambre (walk)"
    for _ in range(2400):
        clock.advance(0.05)
        await pump()
        if not governor.moving:
            break
    result = await governor.wait_motion()
    assert result.ok and result.detail.startswith("arrived at chambre")
    assert math.dist(link.position[:2], (2.0, 2.0)) <= Steering.ARRIVAL_M
    assert any(record.method == "robot.stop" for record in link.records)
    assert (await governor.go_to("chambre")).detail == "already at chambre"


async def test_go_to_refusals(rig, tmp_path: Path) -> None:
    governor, link, _, _ = rig
    unknown = await governor.go_to("cuisine")
    assert not unknown.ok and "known: départ, chambre" in unknown.reason
    assert not (await governor.go_to("chambre", mode="swim")).ok
    link.safety["fallen"] = True
    assert not (await governor.go_to("chambre")).ok
    assert not any(record.method == "robot.move" for record in link.records)
    bare = Governor(link, Settings(), FakeClock(), Journal(tmp_path / "bare"))
    assert bare.places() == []
    assert (await bare.go_to("chambre")).reason == "navigation is not configured"


@pytest.mark.parametrize("simulated", [False, None, "true", 1])
async def test_go_to_refuses_non_simulated_or_unverified_robots(rig, simulated: object) -> None:
    governor, link, _, _ = rig
    link.policies["simulated"] = simulated
    link.records.clear()
    result = await governor.go_to("chambre", mode="roller")
    assert not result.ok and "requires a simulated robot" in result.reason
    assert not any(
        record.method in {"robot.move", "robot.setMode", "robot.enable", "robot.do"}
        for record in link.records
    )


async def test_go_to_stops_when_replanning_repeats_a_stationary_route(rig, monkeypatch) -> None:
    governor, link, clock, navigator = rig

    async def fixed_pose() -> Pose:
        return Pose(-2.0, -2.0, 0)

    monkeypatch.setattr(navigator, "pose", fixed_pose)
    link.records.clear()
    assert (await governor.go_to("chambre")).ok
    waiter = asyncio.create_task(governor.wait_motion())
    for _ in range(300):
        clock.advance(0.05)
        await pump()
        if waiter.done():
            break
    assert waiter.done(), "navigation continued trying while the robot was stationary"
    result = await waiter
    assert result is not None and not result.ok and "stuck" in result.reason
    stops = [record for record in link.records if record.method == "robot.stop"]
    assert len(stops) >= 2
    assert not any(
        record.method == "robot.move" for record in link.records if record.t > stops[-1].t
    )


async def test_go_to_does_not_treat_rotation_as_stationary(rig, monkeypatch) -> None:
    governor, link, clock, navigator = rig

    async def rotating_pose() -> Pose:
        return Pose(-2.0, -2.0, 0.1 * clock.now())

    monkeypatch.setattr(navigator, "pose", rotating_pose)
    link.records.clear()
    assert (await governor.go_to("chambre")).ok
    for _ in range(120):
        clock.advance(0.05)
        await pump()
    assert governor.moving
    assert not any(record.method == "robot.stop" for record in link.records)
    await governor.stop()
