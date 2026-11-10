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
    AMBIGUITY_MARGIN,
    CONVERGED_XY_M,
    FLOOR_FIT_INLIER_M,
    EstimatedPose,
    FloorTracker,
    Hypothesis,
    LikelihoodField,
    LinkTofStream,
    Odom,
    OraclePose,
    ParticleFilter,
    PoseEstimate,
    Scan,
    SweepVerdict,
    TcpTofStream,
    blocking_points,
    global_search,
    obstacle_points,
    parse_tof_frame,
    same_place,
    scan_from,
    sweep_verdict,
)
from coincoin.robot.navigation import NavigationMap, Navigator, Pose, SimPose
from tests.fakes.fake_tof import ClockedTof, TofRobotLink, World, feed, flat, nav_map, twin_flat

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


def test_free_rays_do_not_correct_a_2d_map_pose() -> None:
    field = LikelihoodField(MAP)
    filters = [ParticleFilter(field, count=100, seed=3) for _ in range(2)]
    for particle_filter in filters:
        particle_filter.init_pose(Pose(1.5, 1.5, 0), 0.1, 0.1)
    hits = np.array([[2.4, 0.0, 0.2], [2.4, 0.2, 0.2]])
    empty = np.zeros((0, 3))
    # A beam can pass over a mapped floor footprint, or return no target on a dark surface.
    free = np.array([[2.5, 0.0, 0.2], [2.5, 0.2, 0.2]])
    for particle_filter, rays in zip(filters, (empty, free), strict=True):
        particle_filter.correct(Scan(1, Odom(0, 0, 0, 0.12), hits, empty, rays, np.zeros(3)))
    assert np.array_equal(filters[0].log_w, filters[1].log_w)
    assert np.array_equal(filters[0].x, filters[1].x)
    before = filters[0].log_w.copy()
    filters[0].correct(Scan(2, Odom(0, 0, 0, 0.12), empty, empty, free, np.zeros(3)))
    assert np.array_equal(filters[0].log_w, before)


def test_tracking_scores_only_explained_hits_when_an_unmapped_obstacle_dominates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    field = LikelihoodField(MAP)
    particle_filter = ParticleFilter(field, count=100, seed=3)
    particle_filter.init_pose(Pose(1.5, 1.5, 0), 0.01, 0.01)
    hits = np.column_stack((np.arange(20), np.zeros(20), np.full(20, 0.2)))
    monkeypatch.setattr(field, "surface_distance", lambda x, y: np.where(x < 7.0, 0.0, 1.0))
    expected = hits[:6]
    assert np.array_equal(particle_filter.gate(hits), hits)
    assert np.array_equal(particle_filter.gate(hits, require_match=True), expected)

    scored: list[np.ndarray] = []

    def likelihood(x, y, yaw, matched):
        scored.append(matched)
        return np.zeros(len(x))

    monkeypatch.setattr(field, "log_likelihood", likelihood)
    empty = np.zeros((0, 3))
    scan = Scan(1, Odom(0, 0, 0, 0.12), hits, empty, empty, np.zeros(3))
    assert particle_filter.correct(scan, require_match=True)
    assert len(scored) == 1 and np.array_equal(scored[0], expected)
    monkeypatch.setattr(field, "surface_distance", lambda x, y: np.where(x < 6.0, 0.0, 1.0))
    assert particle_filter.correct(scan, require_match=True)
    assert len(scored) == 1  # Too few mapped returns: no localization update.


@pytest.mark.parametrize("heading_only", [False, True])
def test_tracking_rejects_a_single_frame_that_collapses_the_particle_cloud(
    monkeypatch: pytest.MonkeyPatch, heading_only: bool
) -> None:
    field = LikelihoodField(MAP)
    particle_filter = ParticleFilter(field, count=100, seed=3)
    particle_filter.init_pose(Pose(1.5, 1.5, 0), 0.01, 0.01)
    particle_filter.x[:] = 1.5
    particle_filter.y[:] = 1.5
    particle_filter.yaw[:] = 0
    if heading_only:
        particle_filter.yaw[-10:] = 0.3
    else:
        particle_filter.x[-10:] = 1.7
    monkeypatch.setattr(
        field,
        "log_likelihood",
        lambda x, y, yaw, hits: np.where(yaw > 0.1 if heading_only else x > 1.6, 0, -12),
    )
    empty = np.zeros((0, 3))
    scan = Scan(1, Odom(0, 0, 0, 0.12), np.array([[1.0, 0, 0.2]]), empty, empty, np.zeros(3))
    pose, std_xy, std_yaw = particle_filter.estimate()
    before = (
        particle_filter.x.copy(),
        particle_filter.y.copy(),
        particle_filter.yaw.copy(),
        particle_filter.log_w.copy(),
    )
    assert not particle_filter.correct(
        scan, max_shift=max(0.06, 2 * std_xy), max_turn=max(0.1, 2 * std_yaw)
    )
    assert all(
        np.array_equal(current, original)
        for current, original in zip(
            (particle_filter.x, particle_filter.y, particle_filter.yaw, particle_filter.log_w),
            before,
            strict=True,
        )
    )
    assert particle_filter.estimate()[0] == pose
    assert particle_filter.correct(scan)
    after, _, _ = particle_filter.estimate()
    if heading_only:
        assert after.yaw - pose.yaw > 0.2
    else:
        assert after.x - pose.x > 0.15


def test_repeated_tracking_contradictions_latch_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, link, localizer, _ = kit(odom_scale=1.0, odom_yaw_scale=1.0, particles=100)
    link.place_at(1.5, 1.5, 0)
    localizer.reset_pose(Pose(1.5, 1.5, 0), 0.01, 0.01)
    localizer.filter.x[:] = 1.5
    localizer.filter.x[-10:] = 1.7
    localizer.filter.y[:] = 1.5
    localizer.filter.yaw[:] = 0
    conflict = True

    def likelihood(x, y, yaw, hits):
        return np.where(x > 1.6, 0.0, -100.0) if conflict else np.zeros(len(x))

    monkeypatch.setattr(localizer.field, "log_likelihood", likelihood)

    def frame(dt: float, seq: int) -> None:
        clock.advance(dt)
        localizer.observe_state(link.state())
        assert localizer.observe_frame(link.frame_now(seq)) is not None

    frame(0.05, 1)
    assert localizer.confirmed and localizer.stats.rejected_corrections == 1
    frame(1.1, 2)  # A separate isolated bad frame is not a persistent contradiction.
    assert localizer.confirmed and localizer.stats.rejected_corrections == 2
    conflict = False
    frame(0.05, 3)  # One benign frame in the window does not erase the contradiction.
    conflict = True
    frame(0.05, 4)
    assert localizer.confirmed
    frame(0.05, 5)
    assert not localizer.confirmed and localizer.stats.tracking_conflicts == 1
    conflict = False
    frame(0.05, 6)
    assert not localizer.current().converged


def test_unmapped_obstacle_freezes_corrections_until_good_evidence_is_sustained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, link, localizer, _ = kit(odom_scale=1.0, odom_yaw_scale=1.0, particles=100)
    link.place_at(1.5, 1.5, 0)
    localizer.reset_pose(Pose(1.5, 1.5, 0), 0.01, 0.01)
    fit = 0.0
    nearby = True
    corrections: list[int] = []
    monkeypatch.setattr(localizer.field, "fit", lambda pose, hits: fit)
    monkeypatch.setattr(localizer, "_nearby_unmapped", lambda scan, pose: nearby)

    def correct(scan, **kwargs):
        corrections.append(scan.t_ns)
        return True

    monkeypatch.setattr(localizer.filter, "correct", correct)

    def frame(dt: float, seq: int) -> None:
        clock.advance(dt)
        localizer.observe_state(link.state())
        assert localizer.observe_frame(link.frame_now(seq)) is not None

    for seq in range(2):
        frame(0.1, seq)
    assert localizer.confirmed and not localizer.current().converged
    assert len(corrections) == 1
    fit = 1.0
    nearby = False
    for seq in range(2, 6):
        frame(0.2, seq)
    assert not localizer.current().converged
    assert len(corrections) == 1
    frame(0.2, 6)
    assert localizer.current().converged
    assert len(corrections) == 2


def test_sustained_obstacle_conflict_latches_unconfirmed_until_an_explicit_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, link, localizer, _ = kit(odom_scale=1.0, odom_yaw_scale=1.0, particles=100)
    link.place_at(1.5, 1.5, 0)
    localizer.reset_pose(Pose(1.5, 1.5, 0), 0.01, 0.01)
    fit = 0.0
    nearby = True
    monkeypatch.setattr(localizer.field, "fit", lambda pose, hits: fit)
    monkeypatch.setattr(localizer, "_nearby_unmapped", lambda scan, pose: nearby)

    def frame(dt: float, seq: int) -> None:
        clock.advance(dt)
        localizer.observe_state(link.state())
        assert localizer.observe_frame(link.frame_now(seq)) is not None

    for seq in range(3):
        frame(0.1, seq)
    frame(8.1, 3)
    assert not localizer.confirmed and localizer.stats.fit_conflicts == 1
    fit = 1.0
    nearby = False
    for seq in range(4, 16):
        frame(0.2, seq)
    assert not localizer.current().converged
    assert localizer.stats.fit_conflicts == 1
    localizer.reset_pose(Pose(1.5, 1.5, 0), 0.01, 0.01)
    assert localizer.current().converged


def test_nearby_unmapped_check_ignores_distant_and_sideways_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, localizer, _ = kit()
    empty = np.zeros((0, 3))
    monkeypatch.setattr(localizer.field, "lookup", lambda x, y: np.ones(len(x)))
    hits = np.array([[0.5, -0.05, 0.2], [0.6, 0, 0.2], [0.7, 0.05, 0.2]])
    scan = Scan(1, Odom(0, 0, 0, 0.12), hits, empty, empty, np.zeros(3))
    assert localizer._nearby_unmapped(scan, Pose(1.5, 1.5, 0))
    far = Scan(1, scan.odom, hits + [2.0, 0, 0], empty, empty, np.zeros(3))
    side = Scan(1, scan.odom, hits + [0, 1.0, 0], empty, empty, np.zeros(3))
    assert not localizer._nearby_unmapped(far, Pose(1.5, 1.5, 0))
    assert not localizer._nearby_unmapped(side, Pose(1.5, 1.5, 0))


def test_low_fit_alone_does_not_latch_an_obstacle_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, link, localizer, _ = kit(odom_scale=1.0, odom_yaw_scale=1.0, particles=100)
    link.place_at(1.5, 1.5, 0)
    localizer.reset_pose(Pose(1.5, 1.5, 0), 0.01, 0.01)
    fit = 0.0
    monkeypatch.setattr(localizer.field, "fit", lambda pose, hits: fit)
    monkeypatch.setattr(localizer, "_nearby_unmapped", lambda scan, pose: False)
    for seq in range(10):
        clock.advance(0.1)
        localizer.observe_state(link.state())
        assert localizer.observe_frame(link.frame_now(seq)) is not None
    assert localizer.confirmed and not localizer.current().converged
    assert localizer.stats.fit_conflicts == 0
    fit = 1.0
    for seq in range(10, 20):
        clock.advance(0.1)
        localizer.observe_state(link.state())
        assert localizer.observe_frame(link.frame_now(seq)) is not None
    assert localizer.current().converged


def test_sweep_collects_frames_without_correcting_the_tracked_pose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, link, localizer, _ = kit(odom_scale=1.0, odom_yaw_scale=1.0, particles=100)
    link.place_at(1.5, 1.5, 0)
    localizer.reset_pose(Pose(1.5, 1.5, 0), 0.01, 0.01)
    corrections: list[int] = []
    monkeypatch.setattr(
        localizer.filter, "correct", lambda scan, **kwargs: corrections.append(scan.t_ns) or True
    )
    localizer.collecting_sweep = True
    for seq in range(3):
        clock.advance(0.1)
        localizer.observe_state(link.state())
        assert localizer.observe_frame(link.frame_now(seq)) is not None
    assert len(localizer.recent_scans(0)) == 3
    assert not corrections
    latest = localizer.latest_scan()
    assert latest is not None and localizer._last_t_ns == latest.t_ns
    localizer.collecting_sweep = False
    clock.advance(0.1)
    localizer.observe_state(link.state())
    assert localizer.observe_frame(link.frame_now(3)) is not None
    assert len(corrections) == 1


@pytest.mark.parametrize("heading", [0.0, math.pi - 0.02])
def test_sweep_refinement_respects_its_position_and_heading_window(
    monkeypatch: pytest.MonkeyPatch, heading: float
) -> None:
    field = LikelihoodField(MAP)

    def score(x, y, yaw, hits, sigma=0.06):
        return -(
            (x - 3.0) ** 2
            + (y - 1.5) ** 2
            + 5
            * np.asarray(np.arctan2(np.sin(yaw - heading - 0.8), np.cos(yaw - heading - 0.8))) ** 2
        )

    monkeypatch.setattr(field, "log_likelihood", score)
    matches = global_search(
        field,
        np.zeros((8, 2)),
        centre=(1.5, 1.5),
        radius=0.1,
        heading=heading,
        heading_tolerance=0.1,
        step_m=0.05,
        keep=8,
    )
    assert matches
    assert all(math.dist((h.pose.x, h.pose.y), (1.5, 1.5)) <= 0.100001 for h in matches)
    assert all(abs(math.remainder(h.pose.yaw - heading, math.tau)) <= 0.100001 for h in matches)


def kit(
    *,
    odom_scale: float = 1.1,
    odom_yaw_scale: float = 0.94,
    boxes=None,
    particles: int = 400,
    grid: np.ndarray | None = None,
) -> tuple[FakeClock, TofRobotLink, EstimatedPose, ClockedTof]:
    clock = FakeClock()
    link = TofRobotLink(
        clock,
        World(GRID if grid is None else grid, boxes or []),
        odom_scale=odom_scale,
        odom_yaw_scale=odom_yaw_scale,
    )
    depth = ClockedTof(link, clock)
    nav = MAP if grid is None else nav_map(grid)
    localizer = EstimatedPose(nav, link.state, depth, clock=clock.now, particles=particles)
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
    """Turn the head through the governor's whole sweep and return the frames it collected."""
    since = localizer.latest_t_ns() or 0
    for degrees in (*range(-150, 151, 25), 0):
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


def test_sweep_verdict_needs_a_gap_between_different_places() -> None:
    """A whole-map match is trusted only when the best place clearly beats the best other one."""
    beams = 600
    best = Hypothesis(Pose(1.5, 2.5, 0.0), 300.0, beams)
    assert sweep_verdict([]) is None
    alone = sweep_verdict([best])
    assert alone is not None and alone.rival is None
    assert alone.margin == math.inf and not alone.ambiguous
    # A second guess about the same place is not a second place.
    nearby = Hypothesis(Pose(1.6, 2.4, 0.05), 290.0, beams)
    assert same_place(best.pose, nearby.pose)
    assert sweep_verdict([best, nearby]).rival is None  # type: ignore[union-attr]
    # A look-alike 5 m away that explains the sweep almost as well: refuse rather than guess.
    twin = Hypothesis(Pose(6.5, 2.5, 0.0), 270.0, beams)
    assert not same_place(best.pose, twin.pose)
    verdict = sweep_verdict([best, nearby, twin])
    assert isinstance(verdict, SweepVerdict) and verdict.rival == twin
    assert verdict.margin == pytest.approx(0.05)
    assert verdict.margin < AMBIGUITY_MARGIN and verdict.ambiguous
    # Two headings at one spot are two answers too: the duck would set off in the wrong one.
    turned = sweep_verdict([best, Hypothesis(Pose(1.5, 2.5, math.pi), 300.0, beams)])
    assert turned is not None and turned.ambiguous
    exact = sweep_verdict([best, Hypothesis(Pose(6.5, 2.5, 0.0), 300.0, beams)])
    assert exact is not None and exact.margin == pytest.approx(0.0) and exact.ambiguous
    # Clearly the better place: trusted.
    clear = sweep_verdict([best, Hypothesis(Pose(6.5, 2.5, 0.0), 200.0, beams)])
    assert clear is not None and clear.margin == pytest.approx(1 / 6)
    assert not clear.ambiguous
    assert f"needs {AMBIGUITY_MARGIN:.2f}" in clear.describe()


async def test_whole_map_sweep_refuses_two_identical_rooms() -> None:
    """Two identical rooms fit one sweep: the duck says so instead of picking one and walking."""
    clock, link, localizer, depth = kit(grid=twin_flat())
    link.place_at(1.5, 2.5, 0.0)
    task = asyncio.create_task(feed(localizer, link, depth))
    try:
        scans = await sweep(clock, link, localizer)
    finally:
        await depth.close()
        task.cancel()
    assert len(scans) > 100  # about 5 Hz of usable frames over the 13 s sweep
    assert not localizer.relocalize(scans)
    verdict = localizer.last_verdict
    assert verdict is not None and verdict.ambiguous
    assert verdict.rival is not None
    apart = math.dist(
        (verdict.best.pose.x, verdict.best.pose.y), (verdict.rival.pose.x, verdict.rival.pose.y)
    )
    assert apart > 1.0
    assert not localizer.current().converged


async def test_whole_map_sweep_converges_in_a_distinctive_room() -> None:
    """With no prior, a room the map makes unique is still found from the sweep alone."""
    clock, link, localizer, depth = kit()
    link.place_at(1.5, 2.0, 0.3)
    task = asyncio.create_task(feed(localizer, link, depth))
    try:
        scans = await sweep(clock, link, localizer)
    finally:
        await depth.close()
        task.cancel()
    assert localizer.relocalize(scans)
    verdict = localizer.last_verdict
    assert verdict is not None and not verdict.ambiguous
    assert verdict.margin >= AMBIGUITY_MARGIN
    estimate = localizer.current()
    assert estimate.converged
    assert math.dist((estimate.x, estimate.y), (1.5, 2.0)) < CONVERGED_XY_M


def test_rejected_sweep_candidate_does_not_replace_the_prior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coincoin.robot import localization as module

    _, _, localizer, _ = kit()
    localizer.reset_pose(Pose(1.5, 1.5, 0), 0.01, 0.01)
    original = localizer.filter
    before = original.x.copy()
    monkeypatch.setattr(
        module, "global_search", lambda *args, **kwargs: [Hypothesis(Pose(1.9, 1.5, 0), 0)]
    )
    hits = np.array([[2.0, y, 0.2] for y in np.linspace(-0.2, 0.2, 8)])
    empty = np.zeros((0, 3))
    scan = Scan(1, Odom(0, 0, 0, 0.12), hits, empty, empty, np.zeros(3))
    assert not localizer.relocalize([scan], accept=lambda pose: False)
    assert localizer.filter is original and np.array_equal(localizer.filter.x, before)
    assert not localizer.confirmed and not localizer.current().converged


def test_obstacle_aware_sweep_excludes_unmapped_returns_at_all_heights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coincoin.robot import localization as module

    _, _, localizer, _ = kit()
    prior = Pose(1.5, 1.5, 0)
    localizer.reset_pose(prior, 0.01, 0.01)
    scored: list[np.ndarray] = []

    def search(field, hits, **kwargs):
        scored.append(hits.copy())
        return [Hypothesis(prior, 0)]

    monkeypatch.setattr(module, "global_search", search)
    monkeypatch.setattr(
        localizer.field, "surface_distance", lambda x, y: np.where(x > 1.8, 1.0, 0.0)
    )
    mapped = [[0.1, y, 0.2] for y in np.linspace(-0.2, 0.2, 8)]
    unmapped = [[0.5, 0.0, 0.2], [0.5, 0.1, 0.8]]
    hits = np.array([*mapped, *unmapped])
    empty = np.zeros((0, 3))
    scan = Scan(1, Odom(0, 0, 0, 0.12), hits, empty, empty, np.zeros(3))
    assert localizer.relocalize([scan], prior=prior, exclude_unmapped=True)
    assert np.array_equal(scored[0], np.array(mapped)[:, :2])
    scored.clear()
    assert localizer.relocalize([scan], prior=prior)
    assert np.array_equal(scored[0], hits[:, :2])
    with pytest.raises(ValueError, match="prior pose"):
        localizer.relocalize([scan], exclude_unmapped=True)


def test_start_specs() -> None:
    clock, link, localizer, _ = kit()
    assert "unknown" in localizer.start_at("")
    assert not localizer.initialized
    assert localizer.start_at("salon@90") == "start at salon, heading 90 deg (odometry origin)"
    estimate = localizer.current()
    assert not estimate.converged
    localizer.observe_state(_state(100, 0, 0, 0))
    estimate = localizer.current()
    assert estimate.converged and math.dist((estimate.x, estimate.y), (1.5, 1.5)) < 0.1
    assert abs(estimate.yaw - math.pi / 2) < 0.1
    assert localizer.start_at("cuisine") == "start near cuisine, heading unknown"
    assert localizer.initialized and not localizer.current().converged
    assert localizer.start_at("2, 1.5, -45").startswith("start at (2, 1.5)")
    assert localizer.current().converged
    for bad in ("nowhere", "1,2", "1,2,nan", "salon@nan", "salon@inf"):
        with pytest.raises(ValueError):
            localizer.start_at(bad)


def _state(t_ns: int, x: float, y: float, yaw: float) -> dict:
    return {"t_ns": t_ns, "odom": {"position": [x, y, 0.12], "yaw": yaw}}


@pytest.mark.parametrize("state_first", [False, True])
def test_start_is_the_odometry_origin(state_first: bool) -> None:
    """Standing up turned the duck 57 degrees and slid it before Coin-Coin started."""
    _, _, localizer, _ = kit()
    turned = _state(1_000, 0.21, -0.1, math.radians(-57))
    if state_first:
        localizer.observe_state(turned)
    localizer.start_at("salon@90")
    if not state_first:
        localizer.observe_state(turned)
    estimate = localizer.current()
    # Origin (1.5, 1.5) facing +y: odometry x is map +y, odometry y is map -x.
    assert math.dist((estimate.x, estimate.y), (1.6, 1.71)) < 0.05
    assert abs(math.degrees(math.remainder(estimate.yaw - math.radians(33), math.tau))) < 3
    assert estimate.converged
    # Later states move the estimate by the odometry difference, not from the origin again.
    localizer.observe_state(_state(2_000, 0.21, -0.1, math.radians(-57)))
    assert math.dist((localizer.current().x, localizer.current().y), (1.6, 1.71)) < 0.05


def test_a_later_reset_cancels_a_pending_origin() -> None:
    _, _, localizer, _ = kit()
    localizer.start_at("2, 1.5, 0")
    localizer.reset_pose(Pose(5.0, 1.0, 0.0))
    localizer.observe_state(_state(1_000, 1.0, 0.0, 1.0))
    estimate = localizer.current()
    assert math.dist((estimate.x, estimate.y), (5.0, 1.0)) < 0.3


def test_start_after_stationary_state_initializes_and_tracks_later_odom() -> None:
    _, _, localizer, _ = kit()
    localizer.observe_state(_state(1_000, 0, 0, 0))
    localizer.start_at("salon@90")
    assert localizer.current().converged
    localizer.observe_state(_state(2_000, 0.2, 0, 0))
    estimate = localizer.current()
    assert math.dist((estimate.x, estimate.y), (1.5, 1.7)) < 0.05


def test_unknown_start_cancels_a_pending_origin() -> None:
    _, _, localizer, _ = kit()
    localizer.start_at("salon@0")
    localizer.start_at("")
    localizer.observe_state(_state(1_000, 1.0, 0, 0))
    assert not localizer.initialized
    assert not localizer.current().converged


def test_estimate_description_and_lost_flags() -> None:
    good = PoseEstimate(1, 2, math.pi / 2, 0.05, 0.02, True, 0.8)
    assert good.describe() == (
        "(+1.00, +2.00) m heading +90 deg, particle spread 5 cm / 1 deg, map fit 80%"
    )
    assert (
        "uncertainty 0 cm / 0 deg" in PoseEstimate(0, 0, 0, 0, 0, True, source="oracle").describe()
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


def test_temporary_obstacles_are_not_localization_landmarks() -> None:
    _, _, localizer, _ = kit()
    navigator = Navigator(MAP, localizer, localizer=localizer)
    original_field = localizer.field
    point = (2.8, 2.5)
    assert float(original_field.lookup(np.array([point[0]]), np.array([point[1]]))[0]) > 0.15
    navigator.mark_obstacles([point])
    assert localizer.field is original_field
    assert not navigator.map.with_obstacles(navigator.obstacles).free(MAP.cell(point))
    navigator.clear_obstacles()
    assert localizer.field is original_field


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


def test_floor_tracker_handles_observed_gait_tilt_without_hiding_a_low_obstacle() -> None:
    slope = math.tan(math.radians(18))
    box = [
        (x, y, slope * x + 0.01 + height)
        for x in (0.9, 1.0, 1.1)
        for y in (-0.1, 0.0, 0.1)
        for height in (0.07, 0.15)
    ]
    points = _floor_with(box, pitch_deg=18)
    plane = FloorTracker().observe(points)
    assert plane is not None
    assert abs(math.degrees(plane.tilt_rad) - 18) < 0.5
    inliers = np.abs(points[:, 2] - plane.at(points[:, 0], points[:, 1])) <= FLOOR_FIT_INLIER_M
    assert inliers[:40].all() and not inliers[40:].any()


def test_floor_tracker_does_not_treat_a_sparse_steep_surface_as_the_floor() -> None:
    xs, ys = np.meshgrid(np.linspace(0.5, 1.8, 5), [-0.3, 0.3])
    obstacle = np.column_stack((xs.ravel(), ys.ravel(), (0.28 * xs + 0.05 * ys + 0.01).ravel()))
    distractors = np.column_stack(
        (np.linspace(0.4, 2.0, 54), np.linspace(-0.5, 0.5, 54), np.full(54, 1.0))
    )
    assert FloorTracker().observe(np.vstack((obstacle, distractors))) is None


def test_floor_tracker_needs_enough_floor_in_view() -> None:
    assert FloorTracker().observe(np.array([(1.0, 0.0, 0.0), (1.5, 0.2, 0.0)])) is None
