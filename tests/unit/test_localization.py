"""Monte-Carlo localization on a synthetic flat: frames, the beam model, tracking and relocation."""

import asyncio
import json
import math
from pathlib import Path

import numpy as np
import pytest

from coincoin.clock import FakeClock
from coincoin.robot.jsonrpc import JsonRpcConnection
from coincoin.robot.localization import (
    CONVERGED_XY_M,
    FLOOR_FIT_INLIER_M,
    EstimatedPose,
    FloorTracker,
    LikelihoodField,
    LinkTofStream,
    Odom,
    OraclePose,
    PoseEstimate,
    TcpTofStream,
    blocking_points,
    obstacle_points,
    parse_tof_frame,
    scan_from,
)
from coincoin.robot.navigation import NavigationMap, Navigator, Pose, SimPose
from tests.fakes.fake_tof import ClockedTof, TofRobotLink, World, feed, flat, nav_map

GRID = flat()
MAP = nav_map(GRID)


def frame_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "rows": 8,
        "cols": 8,
        "distance_mm": [1500] * 64,
        "status": [5] * 64,
        "t_ns": 10,
        "seq": 3,
    }
    params.update(overrides)
    return params


def test_parse_tof_frame_validates_and_classifies_zones() -> None:
    status = [5] * 60 + [255, 255, 4, 9]
    distance = [1500] * 60 + [0, 0, 1000, 50]
    frame = parse_tof_frame(frame_params(status=status, distance_mm=distance))
    assert frame.t_ns == 10 and frame.seq == 3
    assert frame.valid.sum() == 60  # status 4 is invalid, 50 mm is the beak
    assert frame.no_target.sum() == 2
    for bad in (
        {"rows": 4},
        {"distance_mm": [1.0] * 63},
        {"distance_mm": [float("nan")] * 64},
        {"status": "x"},
        {"t_ns": None},
    ):
        with pytest.raises(ValueError):
            parse_tof_frame(frame_params(**bad))
    with pytest.raises(ValueError):
        parse_tof_frame({})


def test_scan_projects_hits_floor_and_free_space() -> None:
    link = TofRobotLink(FakeClock(), World(GRID))
    link.place_at(1.5, 1.7, 0.0)
    state = link.state()
    assert state is not None
    frame = link.frame_now(1)
    scan = scan_from(frame, state, Odom(0, 0, 0, 0.12))
    # Facing the internal wall at x = 3.95, 2.45 m ahead of the robot.
    walls = scan.hits[scan.hits[:, 2] > 0.1]
    assert len(walls) > 10
    assert np.allclose(np.median(walls[:, 0]), 2.45, atol=0.1)
    assert len(scan.floor) > 10 and np.all(scan.floor[:, 2] < 0.05)
    assert len(scan.free) and np.all((scan.free[:, 2] >= 0.05) & (scan.free[:, 2] <= 0.45))
    # No return at all: every zone says the beam crossed open floor.
    blank = parse_tof_frame(frame_params(status=[255] * 64, distance_mm=[0] * 64))
    empty = scan_from(blank, state, Odom(0, 0, 0, 0.12))
    assert not len(empty.hits) and len(empty.free) > 64


def test_likelihood_field_is_two_sided() -> None:
    field = LikelihoodField(MAP)
    x = np.array([1.5, 3.0, 3.97, 4.0, 0.5])
    y = np.array([2.5, 1.0, 1.0, 1.0, 4.4])
    near = field.lookup(x, y)
    surface = field.surface_distance(x, y)
    assert near[0] > 1.0 and near[1] == pytest.approx(0.9, abs=0.06)
    # Inside the 10 cm wall and the 0.9 m cupboard clearance is zero, but the distance to its
    # surface is not: a hit deep inside a cupboard is not a hit on it.
    assert near[2] == near[3] == near[4] == 0
    assert 0 < surface[2] <= surface[3] <= 0.1
    assert surface[4] >= 0.4
    outside = field.surface_distance(np.array([-5.0]), np.array([-5.0]))
    assert outside[0] > 0.4


def kit(
    *, odom_scale: float = 1.1, odom_yaw_scale: float = 0.94, boxes=None, particles: int = 400
) -> tuple[FakeClock, TofRobotLink, EstimatedPose, ClockedTof]:
    clock = FakeClock()
    link = TofRobotLink(
        clock, World(GRID, boxes or []), odom_scale=odom_scale, odom_yaw_scale=odom_yaw_scale
    )
    depth = ClockedTof(link, clock)
    localizer = EstimatedPose(MAP, link.state, depth, clock=clock.now, particles=particles)
    return clock, link, localizer, depth


async def drive(
    clock: FakeClock, link: TofRobotLink, localizer: EstimatedPose, legs, step: float = 0.05
) -> list[float]:
    """Command (vx, vyaw, seconds) legs and return the estimate's error at each step."""
    errors: list[float] = []
    for vx, vyaw, seconds in legs:
        await link.notify("robot.move", {"vx": vx, "vy": 0.0, "vyaw": vyaw})
        for _ in range(round(seconds / step)):
            clock.advance(step)
            for _ in range(4):
                await asyncio.sleep(0)
            estimate = localizer.current()
            errors.append(math.dist((estimate.x, estimate.y), link.position[:2]))
    await link.notify("robot.move", {"vx": 0.0, "vy": 0.0, "vyaw": 0.0})
    return errors


async def test_tracking_from_a_known_start_beats_drifting_odometry() -> None:
    clock, link, localizer, depth = kit()
    link.place_at(1.0, 1.6, 0.0)
    localizer.reset_pose(Pose(1.0, 1.6, 0.0))
    task = asyncio.create_task(feed(localizer, link, depth))
    try:
        legs = [(0.2, 0.0, 10), (0.0, 0.6, 2.6), (0.2, 0.0, 8), (0.0, 0.6, 1.3)]
        errors = await drive(clock, link, localizer, legs)
    finally:
        await depth.close()
        task.cancel()
    estimate = localizer.current()
    truth = (link.position[0], link.position[1])
    # Dead reckoning from the same start, with the same 10 % scale and 6 % yaw errors.
    c, s = math.cos(0.0), math.sin(0.0)
    reckoned = (
        1.0 + c * link.odom[0] - s * link.odom[1],
        1.6 + s * link.odom[0] + c * link.odom[1],
    )
    assert math.dist(reckoned, truth) > 0.3
    assert estimate.converged and estimate.source == "estimated"
    assert math.dist((estimate.x, estimate.y), truth) < 0.12
    assert abs(math.remainder(estimate.yaw - link.yaw, math.tau)) < math.radians(6)
    assert float(np.sqrt(np.mean(np.square(errors)))) < 0.1
    assert localizer.stats.used > 200


async def test_kidnapped_robot_is_reported_lost() -> None:
    clock, link, localizer, depth = kit(odom_scale=1.0, odom_yaw_scale=1.0)
    link.place_at(1.5, 1.5, 0.0)
    localizer.reset_pose(Pose(1.5, 1.5, 0.0))
    task = asyncio.create_task(feed(localizer, link, depth))
    try:
        await drive(clock, link, localizer, [(0.0, 0.0, 2)])
        assert localizer.current().converged
        link.place_at(6.0, 2.0, math.pi / 2)
        await drive(clock, link, localizer, [(0.0, 0.0, 4)])
    finally:
        await depth.close()
        task.cancel()
    estimate = localizer.current()
    assert not estimate.converged and estimate.lost
    assert estimate.fit < 0.5


async def sweep(clock: FakeClock, link: TofRobotLink, localizer: EstimatedPose) -> list:
    since = localizer.latest_t_ns() or 0
    for degrees in (-75, -50, -25, 0, 25, 50, 75, 0):
        link.head[2] = math.radians(degrees)
        for _ in range(18):
            clock.advance(0.05)
            for _ in range(4):
                await asyncio.sleep(0)
    return localizer.recent_scans(since)


@pytest.mark.parametrize(
    ("truth", "prior", "heading_known"),
    [
        ((1.5, 2.0, 0.3), (1.9, 2.3), True),
        ((6.3, 1.2, 2.2), (6.0, 1.5), False),
    ],
)
async def test_head_sweep_relocalizes_near_a_prior(truth, prior, heading_known: bool) -> None:
    clock, link, localizer, depth = kit()
    link.place_at(*truth)
    localizer.reset_region(prior, 0.8)
    assert not localizer.current().converged
    task = asyncio.create_task(feed(localizer, link, depth))
    try:
        scans = await sweep(clock, link, localizer)
    finally:
        await depth.close()
        task.cancel()
    assert len(scans) > 60  # 10 Hz on the test clock's 50 ms ticks
    hypotheses = localizer.relocalize(
        scans,
        centre=prior,
        radius=0.8,
        heading=truth[2] + 0.1 if heading_known else None,
        heading_tolerance=0.4 if heading_known else math.pi,
    )
    assert hypotheses
    estimate = localizer.current()
    assert estimate.converged
    assert math.dist((estimate.x, estimate.y), truth[:2]) < CONVERGED_XY_M
    assert abs(math.remainder(estimate.yaw - truth[2], math.tau)) < 0.15


async def test_a_single_wedge_is_not_trusted() -> None:
    clock, link, localizer, depth = kit()
    link.place_at(1.5, 2.0, 0.0)
    localizer.reset_region((1.5, 2.0), 0.5)
    task = asyncio.create_task(feed(localizer, link, depth))
    try:
        await drive(clock, link, localizer, [(0.0, 0.0, 3)])
    finally:
        await depth.close()
        task.cancel()
    assert not localizer.current().converged
    assert not localizer.relocalize([])


def test_start_specs() -> None:
    clock, link, localizer, _ = kit()
    assert "unknown" in localizer.start_at("")
    assert not localizer.initialized
    assert localizer.start_at("salon@90") == "start at salon, heading 90 deg"
    estimate = localizer.current()
    assert estimate.converged and math.dist((estimate.x, estimate.y), (1.5, 1.5)) < 0.1
    assert abs(estimate.yaw - math.pi / 2) < 0.1
    assert localizer.start_at("cuisine") == "start near cuisine, heading unknown"
    assert localizer.initialized and not localizer.current().converged
    assert localizer.start_at("2, 1.5, -45").startswith("start at (2, 1.5)")
    assert localizer.current().converged
    for bad in ("nowhere", "1,2", "1,2,nan"):
        with pytest.raises(ValueError):
            localizer.start_at(bad)


def test_estimate_description_and_lost_flags() -> None:
    good = PoseEstimate(1, 2, math.pi / 2, 0.05, 0.02, True, 0.8)
    assert good.describe() == (
        "(+1.00, +2.00) m heading +90 deg, uncertainty 5 cm / 1 deg, map fit 80%"
    )
    assert not good.lost and good.pose == Pose(1, 2, math.pi / 2)
    assert PoseEstimate(0, 0, 0, 0.8, 0.1, False).lost
    assert PoseEstimate(0, 0, 0, 0.1, 0.1, False, fit=0.1).lost


def test_blocking_points_only_report_unmapped_returns_on_the_path() -> None:
    field = LikelihoodField(MAP)
    pose = Pose(1.0, 2.5, 0.0)
    path = [(1.0, 2.5), (3.0, 2.5)]
    points = np.array(
        [
            [0.5, 0.0],  # on the path, open floor on the map: an obstacle
            [0.6, 0.6],  # beside the path
            [-0.5, 0.0],  # behind
            [1.5, 0.0],  # beyond the lookahead
        ]
    )
    ahead = blocking_points(field, pose, points, path, lookahead_m=0.8, half_width_m=0.17)
    assert ahead.tolist() == [[1.5, 2.5]]
    turned = blocking_points(
        field,
        Pose(1.0, 2.5, math.pi / 2),
        np.array([[0.0, -0.5]]),
        path,
        lookahead_m=1,
        half_width_m=0.17,
    )
    assert turned.tolist() == [[1.5, 2.5]]
    # The cupboard is on the map: seeing it is not a surprise.
    wall = blocking_points(
        field,
        Pose(1.0, 3.5, math.pi / 2),
        np.array([[0.45, 0.0]]),
        [(1.0, 3.5), (1.0, 4.5)],
        lookahead_m=1,
        half_width_m=0.17,
    )
    assert not len(wall)
    assert not len(
        blocking_points(field, pose, np.zeros((0, 2)), path, lookahead_m=1, half_width_m=1)
    )
    assert not len(blocking_points(field, pose, points, path[:1], lookahead_m=1, half_width_m=1))


def test_obstacle_points_drop_returns_above_the_duck() -> None:
    clock = FakeClock()
    link = TofRobotLink(clock, World(GRID))
    link.place_at(1.5, 2.5, 0.0)
    state = link.state()
    assert state is not None
    scan = scan_from(link.frame_now(1), state, Odom(0, 0, 0, 0.12))
    low = obstacle_points(scan)
    assert 0 < len(low) < len(scan.hits)


def test_marked_obstacles_reroute_or_block() -> None:
    nav = Navigator(MAP, OraclePose(SimPose("tcp://127.0.0.1:9")))
    direct = nav.plan((1.5, 2.5), (6.5, 1.5))
    assert direct is not None
    nav.mark_obstacles([(2.8, 2.5)])
    around = nav.plan((1.5, 2.5), (6.5, 1.5))
    assert around is not None and around != direct
    assert min(math.dist(point, (2.8, 2.5)) for point in densify(around)) > MAP.robot_radius
    # Something across the only door: no way through.
    nav.mark_obstacles([(4.0, y / 10) for y in range(20, 30)])
    assert nav.plan((1.5, 2.5), (6.5, 1.5)) is None
    assert MAP.with_obstacles([]) is MAP


def densify(route: list[tuple[float, float]]) -> list[tuple[float, float]]:
    points = []
    for (ax, ay), (bx, by) in zip(route, route[1:], strict=False):
        steps = max(1, math.ceil(math.dist((ax, ay), (bx, by)) / 0.02))
        points += [(ax + (bx - ax) * i / steps, ay + (by - ay) * i / steps) for i in range(steps)]
    return points


async def test_tcp_tof_stream_subscribes_and_reconnects() -> None:
    connections: list[list[bytes]] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        lines = [await reader.readline(), await reader.readline()]
        connections.append(lines)
        if len(connections) == 1:
            writer.write(b'{"jsonrpc":"2.0","id":1,"result":{"accepted":false}}\n')
        else:
            writer.write(b'{"jsonrpc":"2.0","id":1,"result":{"accepted":true}}\n')
            writer.write(b'{"jsonrpc":"2.0","method":"tof.frame","params":{"rows":2}}\n')
            message = {"jsonrpc": "2.0", "method": "tof.frame", "params": frame_params()}
            writer.write(json.dumps(message).encode() + b"\n")
        await writer.drain()
        await reader.read()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    stream = TcpTofStream(f"tcp://127.0.0.1:{port}", "secret", retry_s=0.01)
    try:
        frames = stream.frames()
        frame = await asyncio.wait_for(anext(frames), 5)
        assert frame.seq == 3 and frame.valid.all()
        assert connections[0][0] == b"secret\n"
        assert json.loads(connections[0][1])["method"] == "tof.stream"
        assert stream.errors == 2  # one refusal, one malformed frame
    finally:
        await stream.close()
        await frames.aclose()
        server.close()
        await server.wait_closed()


async def test_navigator_sources() -> None:
    clock, link, localizer, depth = kit()
    localizer.reset_pose(Pose(1, 1, 0))
    honest = Navigator(MAP, localizer, localizer=localizer)
    assert honest.honest and await honest.truth() is None
    assert (await honest.estimate()).converged
    assert len(localizer.stats.updates) == 1
    await honest.close()
    assert depth.closed
    oracle = Navigator(MAP, SimPose("tcp://127.0.0.1:9"))
    assert not oracle.honest and oracle.source.name == "oracle"
    await oracle.close()


def test_map_fixture_is_navigable() -> None:
    assert isinstance(MAP, NavigationMap)
    assert MAP.plan(MAP.places["salon"], MAP.places["coin"]) is not None


@pytest.mark.parametrize("has_oracle_op", [True, False])
async def test_sim_pose_prefers_oracle_pose_and_falls_back_to_read(has_oracle_op: bool) -> None:
    asked: list[str] = []

    async def body(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while line := await reader.readline():
            op = json.loads(line)["op"]
            asked.append(op)
            if op == "oracle.pose" and has_oracle_op:
                answer = {"oracle": True, "x": 1.0, "y": 2.0, "yaw": 0.5}
            elif op == "oracle.pose":
                answer = {"error": "unknown op"}
            else:
                half = 0.25
                answer = {
                    "trunk": [1.0, 2.0],
                    "imu": {"quat": [math.cos(half), 0, 0, math.sin(half)]},
                }
            writer.write((json.dumps(answer) + "\n").encode())
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(body, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    pose = SimPose(f"tcp://127.0.0.1:{port}")
    try:
        for _ in range(2):
            truth = await pose.read()
            assert (truth.x, truth.y) == (1.0, 2.0) and truth.yaw == pytest.approx(0.5)
    finally:
        await pose.close()
        server.close()
        await server.wait_closed()
    expected = ["oracle.pose"] * 2 if has_oracle_op else ["oracle.pose", "read", "read"]
    assert asked == expected


class NotifyingFake:
    """WebRtcLink's notification surface: listeners, generation, call."""

    def __init__(self) -> None:
        self.generation = 1
        self.connected = True
        self.listeners: dict[str, list] = {}
        self.calls: list[tuple[str, int]] = []
        self.accept = [False, True]

    async def call(self, method, params=None, timeout=2.0):  # noqa: ASYNC109 - RobotLink contract
        self.calls.append((method, self.generation))
        return {"accepted": self.accept.pop(0) if self.accept else True}

    def add_notification_listener(self, method, callback):
        self.listeners.setdefault(method, []).append(callback)
        return lambda: self.listeners[method].remove(callback)

    def emit(self, method, params) -> None:
        for callback in list(self.listeners.get(method, ())):
            callback(params)


async def eventually(predicate) -> None:
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


async def test_link_tof_stream_subscribes_per_channel_generation() -> None:
    link = NotifyingFake()
    stream = LinkTofStream(link, retry_s=0.01, silence_s=0.05)
    frames = stream.frames()
    try:
        first = asyncio.ensure_future(anext(frames))
        await eventually(lambda: len(link.calls) >= 2)
        link.emit("tof.frame", {"rows": 2})  # malformed: counted, skipped
        link.emit("tof.frame", frame_params(seq=7))
        assert (await asyncio.wait_for(first, 2)).seq == 7
        assert link.calls == [("tof.stream", 1), ("tof.stream", 1)]
        assert stream.errors == 2  # one refusal, one malformed frame
        # Silence on the same channel does not resubscribe; a new channel does.
        nxt = asyncio.ensure_future(anext(frames))
        await asyncio.sleep(0.12)
        assert len(link.calls) == 2 and stream.errors >= 3
        link.generation = 2
        await eventually(lambda: len(link.calls) >= 3)
        assert link.calls[-1] == ("tof.stream", 2)
        link.emit("tof.frame", frame_params(seq=8))
        assert (await asyncio.wait_for(nxt, 2)).seq == 8
    finally:
        await stream.close()
        await frames.aclose()
    assert link.listeners["tof.frame"] == []


async def test_jsonrpc_connection_forwards_other_server_notifications() -> None:
    reader = asyncio.StreamReader()
    seen: list[tuple[str, object]] = []

    class Sink:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    connection = JsonRpcConnection(
        reader, Sink(), on_notification=lambda method, params: seen.append((method, params))
    )
    try:
        for message in (
            {"jsonrpc": "2.0", "method": "robot.state", "params": {"t": 1}},
            {"jsonrpc": "2.0", "method": "tof.frame", "params": {"seq": 1}},
        ):
            reader.feed_data(json.dumps(message).encode() + b"\n")
        for _ in range(20):
            await asyncio.sleep(0)
        assert seen == [("tof.frame", {"seq": 1})]
        assert connection.state() == {"t": 1}
    finally:
        await connection.close()


def test_tof_link_mode_needs_a_webrtc_link(tmp_path: Path, monkeypatch) -> None:
    from coincoin.config import Settings
    from coincoin.journal import Journal
    from coincoin.robot.feature import _RobotRuntime
    from tests.fakes.fake_robotd import FakeRobotLink

    monkeypatch.setattr(NavigationMap, "load", staticmethod(lambda path: MAP))

    class _App:
        settings = Settings(nav_map="map.json", tof_link="link")
        journal = Journal(tmp_path)
        clock = FakeClock()

    assert _RobotRuntime._navigator(_App(), FakeRobotLink()) is None  # type: ignore[arg-type]
    logged = "".join(path.read_text() for path in tmp_path.glob("**/*.jsonl"))
    assert "TOF_LINK=link needs a WebRTC ROBOT_LINK" in logged


def _floor_with(extra: list[tuple[float, float, float]], pitch_deg: float = 0.0) -> np.ndarray:
    xs, ys = np.meshgrid(np.linspace(0.4, 2.2, 8), np.linspace(-0.6, 0.6, 5))
    zs = np.tan(math.radians(pitch_deg)) * xs + 0.01
    floor = np.column_stack((xs.ravel(), ys.ravel(), zs.ravel()))
    return np.vstack((floor, np.array(extra).reshape(-1, 3)))


def test_floor_tracker_stays_level_beside_a_wall_foot() -> None:
    wall = [(1.0, float(y), z) for y in np.linspace(-0.6, 0.6, 7) for z in (0.03, 0.08, 0.15)]
    plane = FloorTracker().observe(_floor_with(wall))
    assert plane is not None
    assert plane.tilt_rad < math.radians(0.5)
    assert abs(plane.at(1.0, 0.0) - 0.01) < 0.01


def test_floor_tracker_follows_a_tilted_level_frame_but_not_a_box() -> None:
    box = [(1.2, y, z) for y in (-0.1, 0.0, 0.1) for z in (0.12, 0.2, 0.3)]
    points = _floor_with(box, pitch_deg=4.0)
    plane = FloorTracker().observe(points)
    assert plane is not None
    assert abs(math.degrees(plane.tilt_rad) - 4.0) < 0.5
    inliers = np.abs(points[:, 2] - plane.at(points[:, 0], points[:, 1])) <= FLOOR_FIT_INLIER_M
    assert inliers[:40].all() and not inliers[40:].any()


def test_floor_tracker_needs_enough_floor_in_view() -> None:
    assert FloorTracker().observe(np.array([(1.0, 0.0, 0.0), (1.5, 0.2, 0.0)])) is None
