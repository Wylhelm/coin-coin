"""Room navigation: planner, steering and the governor's go_to, without a simulator."""

import asyncio
import base64
import itertools
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from coincoin.clock import FakeClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.realtime.tools.robot import schemas
from coincoin.robot.governor import Governor
from coincoin.robot.localization import Odom, PoseEstimate
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


def test_pose_correction_cannot_cross_an_observed_obstacle() -> None:
    nav = NavigationMap(bytes([100] * 121), 11, 11, (0, 0), 0.1, 0.12, {})
    nav = nav.with_obstacles([(0.5, 0.5)])
    assert not nav.segment_unobstructed((0.2, 0.5), (0.8, 0.5))
    assert nav.segment_unobstructed((0.2, 0.5), (0.2, 0.8))


def test_blocked_pose_anchor_follows_later_odometry() -> None:
    start = Odom(1.0, 2.0, math.pi / 2, 0.12)
    end = Odom(1.0, 3.0, math.pi / 2 + 0.2, 0.12)
    anchor = Governor._anchor_at_odom(Pose(3.0, 4.0, math.pi / 2), start, end)
    assert (anchor.x, anchor.y) == pytest.approx((3.0, 5.0))
    assert anchor.yaw == pytest.approx(math.pi / 2 + 0.2)


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
    assert steering_command(steering, Pose(1.0, 0.85, math.pi / 2), 0.3)["vx"] > 0
    assert steering.update(Pose(1.0, 0.91, math.pi / 2), 0.4) is None


def test_walk_does_not_skip_a_waypoint_across_a_blocked_corner() -> None:
    clearance = bytearray([100] * 25)
    clearance[2 * 5 + 2] = 0
    nav = NavigationMap(bytes(clearance), 5, 5, (0, 0), 0.1, 0.1, {})
    start, corner, goal = (0.1, 0.1), (0.3, 0.1), (0.3, 0.3)
    assert nav.segment_clear(start, corner)
    assert nav.segment_clear(corner, goal)
    assert not nav.segment_clear(start, goal)
    steering = Steering([corner, goal], (0.25, 0.0, 1.0), "walk", segment_clear=nav.segment_clear)
    assert steering_command(steering, Pose(*start, 0), 0)["vx"] > 0
    assert steering.index == 0
    steering.update(Pose(*corner, 0), 1)
    assert steering.index == 1


def test_walk_detects_when_deviation_blocks_the_current_waypoint() -> None:
    nav = NavigationMap(bytes([100] * 121), 11, 11, (0, 0), 0.1, 0.1, {})
    nav = nav.with_obstacles([(0.5, 0.5)])
    steering = Steering(
        [(1.0, 0.1)], (0.25, 0.0, 1.0), "walk", route_traversable=nav.segment_traversable
    )
    start = Pose(0.1, 0.1, 0)
    steering.update(start, 0)
    assert steering.route_clear(start)
    assert not steering.route_clear(Pose(0.1, 0.6, 0))


def test_stalled_walk_pivot_uses_the_opposite_direction_without_advancing() -> None:
    steering = Steering(
        [(0.0, 2.0)],
        (0.25, 0.0, 1.0),
        "walk",
        room=lambda point: 0.2,
        recover_stalled_pivot=True,
    )
    assert steering.update(Pose(0, 0, 0), 0) == {"vx": 0.0, "vy": 0.0, "vyaw": 1.0}
    assert steering.update(Pose(0, 0, 0.02), 2.1) == {"vx": 0.0, "vy": 0.0, "vyaw": -1.0}
    assert steering.update(Pose(0, 0, -2.5), 3) == {"vx": 0.0, "vy": 0.0, "vyaw": -1.0}
    assert steering_command(steering, Pose(0, 0, 1.5), 4)["vx"] > 0


def test_stalled_pivot_recovery_is_off_without_simulation_walk_assist() -> None:
    steering = Steering([(0.0, 2.0)], (0.25, 0.0, 1.0), "walk", room=lambda point: 1.0)
    steering.update(Pose(0, 0, 0), 0)
    assert steering.update(Pose(0, 0, 0.02), 2.1)["vyaw"] == 1.0
    assert not steering.reversing_pivot


def test_stalled_pivot_recovery_requires_and_keeps_clearance() -> None:
    clearance = [0.05]
    steering = Steering(
        [(0.0, 2.0)],
        (0.25, 0.0, 1.0),
        "walk",
        room=lambda point: clearance[0],
        recover_stalled_pivot=True,
    )
    steering.update(Pose(0, 0, 0), 0)
    assert steering.update(Pose(0, 0, 0.02), 2.1)["vyaw"] == 1.0
    clearance[0] = 0.2
    assert steering.update(Pose(0, 0, 0.02), 2.2)["vyaw"] == -1.0
    clearance[0] = 0.05
    assert steering.update(Pose(0, 0, -0.2), 2.3) == {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}


def test_roller_controls_speed_and_brakes_before_arriving() -> None:
    limits = (0.3, 0.0, 0.3)
    steering = Steering([(3.0, 0.0)], limits, "roller")
    # From rest the wheels push, at the throttle they actually answer rather than at the
    # speed limit: below 0.22 they slip in place and move nothing. The choice is held for a
    # dwell rather than re-made every frame, because the body answers a push a few hundred
    # milliseconds late and flipping the command per frame rectifies into an acceleration
    # nobody asked for.
    assert steering_command(steering, Pose(0, 0, 0), 0)["vx"] == 0.25  # push from rest
    assert steering_command(steering, Pose(0.04, 0, 0), 0.1)["vx"] == 0.25  # held
    assert steering_command(steering, Pose(0.08, 0, 0), 0.2)["vx"] == 0.25  # held
    assert steering_command(steering, Pose(0.14, 0, 0), 0.4)["vx"] == 0.0  # coast at cruise
    assert steering_command(steering, Pose(0.20, 0, 0), 0.6)["vx"] == 0.0  # coast held
    assert steering_command(steering, Pose(0.40, 0, 0), 0.8)["vx"] == -0.45  # too fast: brake
    steering.update(Pose(2.6, 0, 0), 1.0)
    assert steering_command(steering, Pose(2.8, 0, 0), 1.1)["vx"] == -0.45  # brake near goal
    gliding = Steering([(1.0, 0.0)], limits, "roller")
    gliding.update(Pose(0.6, 0, 0), 0)
    assert steering_command(gliding, Pose(0.75, 0, 0), 0.1)["vx"] == -0.45
    for step in range(2, 12):
        result = gliding.update(Pose(0.75, 0, 0), step / 10)
    assert result is None


def test_roller_glides_straight_instead_of_hunting_the_heading() -> None:
    limits = (0.3, 0.0, 0.3)
    # A duck on skates that answers every millimetre of heading error with a saturated yaw
    # command weaves down a straight corridor, and the skating policy pushes through each
    # arc: measured 0.4 m/s while 0.15 was commanded. A dead band lets it glide straight,
    # which is also what keeps the commanded-speed odometry honest.
    steering = Steering([(3.0, 0.0)], limits, "roller")
    steering.update(Pose(0, 0, 0), 0)
    assert steering_command(steering, Pose(0.3, 0.005, 0.02), 0.4)["vyaw"] == 0.0
    # Off the segment the correction is gentle rather than instantly saturated.
    off = Steering([(3.0, 0.0)], limits, "roller")
    off.update(Pose(0, 0, 0), 0)
    gentle = steering_command(off, Pose(0.6, 0.2, 0), 0.4)["vyaw"]
    assert -0.3 < gentle < 0


def test_roller_yaw_command_is_a_heading_error_not_a_rate() -> None:
    # The skating policy reads twist[2] as a heading error and answers it with several times
    # its magnitude in rad/s. Handing it a rate turned every correction that many times too
    # strong: the duck spun in place while gliding, its estimate fell behind the spin, and the
    # speed loop pushed on the stale reading until it fell over.
    limits = (0.3, 0.0, 1.0)
    off = Steering([(1.0, 0.0), (1.0, 2.0)], limits, "roller")
    off.update(Pose(0, 0, 0), 0)
    small = steering_command(off, Pose(0.4, 0.12, 0), 0.4)["vyaw"]
    assert small != 0
    assert abs(small) < Steering.ROLLER_YAW_RATE / Steering.ROLLER_YAW_RESPONSE
    # A 0.12 rad error on a 0.4 m/s glide answers 1.2 * 0.12 rad/s, so the command carries
    # 0.144 / 5.5 rad and the body turns by that much rather than by 0.144 rad/s.
    assert small == pytest.approx(-1.2 * math.atan2(0.12, 0.6) / Steering.ROLLER_YAW_RESPONSE)


def test_roller_pivots_on_the_spot_instead_of_carving_an_arc() -> None:
    # Carving a turn as an arc at cruise sweeps a 0.22 m radius and drags the duck most of a
    # metre sideways -- a 135 degree turn out of the salon pinned it against the wall. The
    # governor's `turn` primitive shows the body pivots cleanly when it is not pushing, so the
    # glide is braked out first and the body turns from rest.
    limits = (0.3, 0.0, 0.3)
    steering = Steering([(-2.0, 0.0)], limits, "roller")
    # A glide from cruise braking at the measured 0.12 m/s^2, sampled at the 20 Hz the governor
    # steers at, with the body answering the 0.30 pivot at 0.78 rad/s.
    x, speed, yaw = 0.0, 0.40, 0.5
    # One sample of history so the first command sees a moving body rather than an empty track.
    steering.update(Pose(-0.02, 0, 0.5), 0.0)
    commands = []
    for step in range(1, 160):
        command = steering.update(Pose(x, 0, yaw), step * 0.05)
        commands.append(command)
        assert command is not None
        if command["vx"] < 0.0:
            speed = max(0.0, speed - 0.12 * 0.05)
        elif command["vx"] > 0.0:
            speed = min(0.45, speed + 0.9 * 0.05)
        else:
            speed = max(0.0, speed - 0.02 * 0.05)
        x += speed * 0.05
        if command["vyaw"] != 0.0:
            # The skates answer a heading error at 5.5 rad/s per rad while gliding, and the
            # 0.30 pivot at 0.78 rad/s from rest.
            yaw += command["vyaw"] * (5.5 if speed >= 0.05 else 0.78 / steering.ROLLER_PIVOT) * 0.05
    assert commands[0]["vx"] == -0.45  # brake the glide out, do not steer it
    assert commands[0]["vyaw"] == 0.0
    # Once the glide is down the brake comes off and the body turns on the spot, and the
    # residual the estimate still reads -- it carries up to 0.10 m/s of noise while the body
    # turns -- must not put the brake back on mid-pivot.
    pivoting = next(index for index, command in enumerate(commands) if command["vyaw"] != 0.0)
    assert pivoting > 5
    pivot_run = list(itertools.takewhile(lambda command: command["vx"] == 0.0, commands[pivoting:]))
    assert len(pivot_run) > 10
    for command in pivot_run:
        assert command["vx"] == 0.0
        assert command["vyaw"] == pytest.approx(steering.ROLLER_PIVOT)
    # A residual drift of 0.12 m/s is under the speed the skates must be down to before a pivot
    # is safe, so it does not hold the brake: the body turns at once, and the residual the
    # estimate still reads -- it carries up to 0.10 m/s of noise while the body turns -- must
    # not put the brake back on mid-pivot.
    creeping = Steering([(-2.0, 0.0)], limits, "roller")
    creeping.update(Pose(0, 0, 0.5), 0.0)
    creeping.update(Pose(0.012, 0, 0.5), 0.1)
    creeps = [creeping.update(Pose(0.012, 0, 0.5), step * 0.1) for step in range(2, 30)]
    assert all(command is not None for command in creeps)
    assert creeps[0]["vx"] == 0.0
    assert creeps[0]["vyaw"] == pytest.approx(creeping.ROLLER_PIVOT)
    assert all(command["vx"] == 0.0 for command in creeps)


def test_roller_pivot_gain_climbs_when_the_body_does_not_answer() -> None:
    # The break-away is asymmetric: +0.30 turns the body at 0.8 rad/s but -0.30 only kicks it
    # 0.3 rad and then sticks. Measured, the clockwise direction needs -0.50 to keep moving.
    # Rather than pick the larger command for both directions -- which spins the counter
    # clockwise side at 2.9 rad/s -- the gain starts low and climbs only while the body is
    # not answering.
    limits = (0.3, 0.0, 0.3)
    steering = Steering([(-2.0, 0.0)], limits, "roller")
    steering.update(Pose(0, 0, 0.5), 0)
    assert steering.update(Pose(0, 0, 0.5), 0.1)["vyaw"] == pytest.approx(0.3)
    # The body answers: a progress of 0.05 rad per frame keeps the gain where it is.
    yaw = 0.5
    for step in range(2, 12):
        yaw += 0.06
        assert steering.update(Pose(0, 0, yaw), step / 10)["vyaw"] == pytest.approx(0.3)
    # Now it stops answering: the gain steps up every ROLLER_PIVOT_STALL_S.
    assert steering.update(Pose(0, 0, yaw), 1.55)["vyaw"] == pytest.approx(0.4)
    assert steering.update(Pose(0, 0, yaw), 2.1)["vyaw"] == pytest.approx(0.5)
    # ... and never past the ceiling that still keeps the body on its skates.
    assert steering.update(Pose(0, 0, yaw), 2.7)["vyaw"] == pytest.approx(0.5)


def test_roller_pivot_gain_resets_when_the_turn_reverses() -> None:
    limits = (0.3, 0.0, 0.3)
    steering = Steering([(0.0, -2.0)], limits, "roller")
    assert steering.update(Pose(0, 0, 0.5), 0)["vyaw"] == pytest.approx(-0.3)
    assert steering.update(Pose(0, 0, 0.5), 0.1)["vyaw"] == pytest.approx(-0.3)
    assert steering.update(Pose(0, 0, 0.5), 0.6)["vyaw"] == pytest.approx(-0.4)
    assert steering.update(Pose(0, 0, 0.5), 1.1)["vyaw"] == pytest.approx(-0.5)
    # The waypoint moved behind us: the turn flips and the gain starts over rather than
    # carrying the larger command into the direction that never needed it.
    assert steering.update(Pose(0, 0, 2.5), 1.6)["vyaw"] == pytest.approx(0.3)


def test_roller_pivot_gain_falls_back_once_the_body_answers_again() -> None:
    # A pivot that needed the larger kick must not keep spinning at that rate: the gain walks
    # back down to the break-away value as soon as the body starts answering, and stops there.
    limits = (0.3, 0.0, 0.3)
    steering = Steering([(-2.0, 0.0)], limits, "roller")
    assert steering.update(Pose(0, 0, 0.5), 0)["vyaw"] == pytest.approx(0.3)
    assert steering.update(Pose(0, 0, 0.5), 0.6)["vyaw"] == pytest.approx(0.4)
    assert steering.update(Pose(0, 0, 0.5), 1.2)["vyaw"] == pytest.approx(0.5)
    yaw = 0.5
    for step in range(2, 6):
        yaw += 0.06
        expected = max(0.3, 0.5 - 0.1 * (step - 1))
        assert steering.update(Pose(0, 0, yaw), 1.2 + step / 10)["vyaw"] == pytest.approx(expected)
    # Below the break-away value the pivot would stall again, so the gain floors there.
    yaw += 0.06
    assert steering.update(Pose(0, 0, yaw), 1.9)["vyaw"] == pytest.approx(0.3)


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


def test_roller_keeps_its_push_where_walls_are_close() -> None:
    # The skates have no slow gear: below their dead band they slip in place and push
    # nothing. Lowering the target where walls are close used to ask for 0.14 m/s and get a
    # duck gliding at 0.06, so the branch keeps cruise and lets the ToF guard the front.
    steering = Steering([(5.0, 0.0)], (0.3, 0.0, 0.3), "roller", room=lambda _: 0.0)
    steering.update(Pose(0, 0, 0), 0)
    assert steering_command(steering, Pose(0.02, 0, 0), 0.1)["vx"] == 0.25
    assert steering_command(steering, Pose(0.04, 0, 0), 0.2)["vx"] == 0.25
    assert steering_command(steering, Pose(0.2, 0, 0), 1.0)["vx"] == 0.25
    assert steering_command(steering, Pose(0.38, 0, 0), 1.9)["vx"] == 0.25
    # A push ends on its own clock even while the duck is moving. Letting motion re-arm it
    # meant the burst never ended on a working push, and the f17 salon leg ran up to 0.47 m/s
    # before tipping over. The clock is seconds long, not tenths: a 0.3 s burst never broke
    # the static friction the skates sit in and the f18 salon leg covered 0.11 m in 40 s.
    assert steering_command(steering, Pose(0.44, 0, 0), 2.2)["vx"] == 0.0
    # Past the coast, the glide has lost its speed again, so the next push starts.
    assert steering_command(steering, Pose(0.48, 0, 0), 2.7)["vx"] == 0.25


def test_roller_brakes_most_of_a_metre_before_the_goal() -> None:
    # Braking from cruise takes 0.40**2 / (2 * 0.12) = 0.67 m, so the approach has to start
    # that far out rather than at the goal. Sampled at the 20 Hz the governor steers at.
    steering = Steering([(2.0, 0.0)], (0.3, 0.0, 0.3), "roller")
    x, speed = 0.0, 0.0
    commands = []
    for step in range(120):
        commands.append(steering.update(Pose(x, 0, 0), step * 0.05))
        speed = min(0.38, speed + 0.9 * 0.05)
        x += speed * 0.05
    assert commands[5]["vx"] == 0.25  # still far: push
    braking = next(index for index, command in enumerate(commands) if command["vx"] == -0.45)
    assert 1.25 < braking * 0.019 < 1.45  # 0.6-0.75 m out, not at the goal


def test_roller_brakes_in_bursts_instead_of_holding_the_reverse_push() -> None:
    # Held, the reverse push leaves the skates with a standing nose-up attitude that nothing
    # damps once the command drops to zero: measured 29 degrees after a continuous brake, and
    # the body fell over 80 degrees within 1.2 s of the command going quiet. Pulsed 0.25 s on,
    # 0.20 s off, it never banked past 8.4 degrees and came to a standstill that held.
    steering = Steering([(2.0, 0.0)], (0.3, 0.0, 0.3), "roller")
    steering.update(Pose(0, 0, 0), 0.0)
    x = 0.0
    commands = []
    for step in range(1, 130):
        command = steering.update(Pose(x, 0, 0), step * 0.05)
        assert command is not None
        commands.append((step * 0.05, command))
        x += 0.38 * 0.05
    braking = [now for now, command in commands if command["vx"] == -0.45]
    assert braking, "the approach must brake"
    # Group the brake ticks into bursts: on for 0.25 s, coast for 0.20 s, then on again.
    bursts: list[list[float]] = []
    for now in braking:
        if bursts and now - bursts[-1][-1] < 0.08:
            bursts[-1].append(now)
        else:
            bursts.append([now])
    assert len(bursts) >= 2, "a held brake is one endless burst"
    assert len(bursts[0]) == round(Steering.ROLLER_BRAKE_PULSE_S / 0.05)
    assert bursts[1][0] - bursts[0][-1] == pytest.approx(
        Steering.ROLLER_BRAKE_REST_S + 0.05, abs=0.06
    )
    rest = [command for _, command in commands if command["vx"] == 0.0]
    assert rest, "the brake must let go between bursts"


def test_roller_measures_speed_over_a_window_not_frame_to_frame() -> None:
    # The pose is a particle filter's, and frame to frame it wanders by a couple of
    # centimetres: at 20 Hz that reads as +-0.45 m/s. Every consumer of the speed believed
    # that noise -- down the salon a duck standing still with a zero command was reported at
    # 0.39 m/s, the approach to the goal latched its brake 0.46 m early, and the leg was
    # abandoned a hand's width from where it was going.
    limits = (0.3, 0.0, 0.3)
    steering = Steering([(5.0, 0.0)], limits, "roller")
    wander = (0.0, 0.02, -0.01, 0.015, -0.02, 0.01, -0.015, 0.02, 0.0, -0.02, 0.015, -0.01)
    for step, offset in enumerate(wander * 2):
        steering.update(Pose(offset, 0.0, 0.0), step * 0.05)
    assert math.hypot(*steering._velocity) < 0.06


def test_roller_still_reads_a_real_glide_through_that_wander() -> None:
    limits = (0.3, 0.0, 0.3)
    steering = Steering([(5.0, 0.0)], limits, "roller")
    wander = (0.0, 0.02, -0.01, 0.015, -0.02, 0.01, -0.015, 0.02, 0.0, -0.02, 0.015, -0.01)
    for step, offset in enumerate(wander):
        steering.update(Pose(step * 0.019 + offset, 0.0, 0.0), step * 0.05)
    assert math.hypot(*steering._velocity) == pytest.approx(0.38, abs=0.08)


def test_go_to_schema_only_exists_with_places() -> None:
    assert "go_to" not in schemas(0.3, 0.2, 1.0, 10)
    schema = schemas(0.3, 0.2, 1.0, 10, ["salon", "ma chambre"])["go_to"]
    assert schema["properties"]["place"]["enum"] == ["salon", "ma chambre"]
    assert schema["properties"]["mode"]["enum"] == ["walk", "roller"]
    assert schema["required"] == ["place"]


class FakeNavigator(Navigator):
    """Steers by the fake link's odometry, as the oracle source would by the simulator's truth."""

    def __init__(self, link: FakeRobotLink, nav: NavigationMap) -> None:
        self.link = link
        self.map = nav
        self.source = SimpleNamespace(name="oracle")
        self.oracle = None
        self.localizer = None
        self.obstacles = []
        self.closed = False

    async def estimate(self) -> PoseEstimate:
        pose = await self.pose()
        return PoseEstimate(pose.x, pose.y, pose.yaw, 0.0, 0.0, True, source="oracle")

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


async def test_walking_shortcut_uses_the_walking_arrival_tolerance(rig) -> None:
    governor, link, _, _ = rig
    link.position[:2] = [2.25, 2.0]
    started = await governor.go_to("chambre")
    assert started.ok and started.detail.startswith("going to chambre")
    assert governor.moving


async def test_go_to_refuses_a_route_snapped_away_from_the_destination(rig, monkeypatch) -> None:
    governor, _, _, navigator = rig
    monkeypatch.setattr(navigator, "plan", lambda *_: [(1.0, 2.0)])
    result = await governor.go_to("chambre")
    assert not result.ok and "its mapped location is obstructed" in result.reason
    assert not governor.moving


async def test_route_cannot_report_arrival_at_a_different_snapped_goal(rig) -> None:
    governor, link, _, _ = rig
    link.position[:2] = [1.0, 2.0]
    result = await governor._run_route(
        "chambre", [(1.0, 2.0)], (0.3, 0.0, 0.8), "walk", governor.stop_generation
    )
    assert not result.ok and "its mapped location is obstructed" in result.reason


async def test_route_cannot_report_arrival_with_unconfirmed_localization(rig, monkeypatch) -> None:
    governor, _, _, navigator = rig
    navigator.source.name = "estimated"

    async def uncertain() -> PoseEstimate:
        return PoseEstimate(2.0, 2.0, 0.0, 0.2, 0.1, False)

    monkeypatch.setattr(navigator, "estimate", uncertain)
    result = await governor._run_route(
        "chambre", [(2.0, 2.0)], (0.3, 0.0, 0.8), "walk", governor.stop_generation
    )
    assert not result.ok and "cannot confirm" in result.reason


async def test_route_stops_before_replanning_an_unreachable_waypoint(rig, monkeypatch) -> None:
    governor, link, _, navigator = rig
    navigator.map = room_with_wall(door=False)
    monkeypatch.setattr(navigator, "plan", lambda *_: None)
    link.records.clear()
    result = await governor._run_route(
        "chambre", [(2.0, 2.0)], (0.3, 0.0, 0.8), "walk", governor.stop_generation
    )
    assert not result.ok and "cannot keep a safe path" in result.reason
    assert any(record.method == "robot.stop" for record in link.records)
    assert not any(record.method == "robot.move" for record in link.records)


async def test_route_sweeps_before_refusing_an_untraversable_deviation(rig, monkeypatch) -> None:
    governor, _, _, navigator = rig
    navigator.source.name = "estimated"
    navigator.map = room_with_wall(door=False)
    monkeypatch.setattr(navigator, "plan", lambda *_: None)
    sweeps = []

    async def relocalize(generation: int, navigator_: object, prior: PoseEstimate) -> PoseEstimate:
        sweeps.append(prior.describe())
        return PoseEstimate(2.0, 2.0, 0.0, 0.02, 0.01, True)

    monkeypatch.setattr(governor, "_relocalize", relocalize)
    result = await governor._run_route(
        "chambre", [(2.0, 2.0)], (0.3, 0.0, 0.8), "walk", governor.stop_generation
    )
    # A route stops being traversable when the estimate slides into a wall, and that is usually
    # the estimate rather than the room: sweep before giving up on the trip.
    assert sweeps, "the deviation was refused without sweeping the head first"
    assert not result.ok and "cannot keep a safe path" in result.reason


async def test_route_admits_it_lost_track_when_that_sweep_does_not_converge(
    rig, monkeypatch
) -> None:
    governor, _, _, navigator = rig
    navigator.source.name = "estimated"
    navigator.map = room_with_wall(door=False)
    monkeypatch.setattr(navigator, "plan", lambda *_: None)

    async def relocalize(generation: int, navigator_: object, prior: PoseEstimate) -> PoseEstimate:
        return PoseEstimate(2.0, 2.0, 0.0, 0.6, 0.4, False)

    monkeypatch.setattr(governor, "_relocalize", relocalize)
    result = await governor._run_route(
        "chambre", [(2.0, 2.0)], (0.3, 0.0, 0.8), "walk", governor.stop_generation
    )
    assert not result.ok and "I lost track of where I am" in result.reason


async def test_route_stops_when_replanning_repeats_an_unsafe_segment(rig, monkeypatch) -> None:
    governor, link, _, navigator = rig
    navigator.map = room_with_wall(door=False)
    monkeypatch.setattr(navigator, "plan", lambda *_: [(2.0, 2.0)])
    link.records.clear()
    result = await governor._run_route(
        "chambre", [(2.0, 2.0)], (0.3, 0.0, 0.8), "walk", governor.stop_generation
    )
    assert not result.ok and "cannot keep a safe path" in result.reason
    assert len([record for record in link.records if record.method == "robot.stop"]) >= 3
    assert not any(record.method == "robot.move" for record in link.records)


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
