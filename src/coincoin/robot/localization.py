"""Where the duck is on the apartment map, from what a real duck can sense.

Honest localization uses only what robotd and tofd publish on the robot: contact odometry and the
IMU in `robot.state`, the head's ToF pose in `robot.state.frames.tof`, and the 8x8 VL53L5CX depth
frames from `tof.stream`. A Monte-Carlo localizer (particle filter) tracks the pose in the map
frame: odometry moves the particles, ToF returns weigh them against the map's clearance field (a
likelihood field), and a head-sweep composite matched over the whole map recovers a lost or
unknown pose. The simulator's true pose is a separate `OraclePose`, for evaluation only.

See docs/localization.md for the model, its limits and measured results.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from coincoin.robot.navigation import NavigationMap, Pose, SimPose
from coincoin.robot.tcp_link import tcp_address

FloatArray = npt.NDArray[np.float64]
Float32Array = npt.NDArray[np.float32]

# VL53L5CX: 8x8 zones over 45 degrees per axis, 4 m. Zone centres keep a half-zone inset; row 0 is
# the top and column 0 the sensor's left (kinematics::tof, sim/tof.py).
TOF_ROWS = TOF_COLS = 8
TOF_FOV_RAD = math.radians(45.0)
TOF_MAX_M = 4.0
# The kinematics crate ignores closer returns: the beak and the sensor window.
TOF_MIN_M = 0.10
TOF_VALID = frozenset({5, 9})
TOF_NO_TARGET = 255

# Returns below this height are the floor, above it the furniture and walls the map holds.
FLOOR_Z_M = 0.05
# Floor plane fit (FloorTracker): sampled planes per frame, the distance (m) that counts a return
# as on a plane, the least floor to fit, and the most tilt and offset believed.
FLOOR_FIT_TRIALS = 64
FLOOR_FIT_INLIER_M = 0.025
FLOOR_FIT_MIN_POINTS = 8
# Farther floor returns graze in at a low angle, where a wall's foot looks just like them.
FLOOR_FIT_MAX_REACH_M = 2.5
FLOOR_FIT_MIN_SPAN_M = 0.3
FLOOR_FIT_MAX_TILT_RAD = math.radians(12)
FLOOR_FIT_MAX_OFFSET_M = 0.06
HIT_MAX_Z_M = 1.5
# A no-target zone clears the map along its beam while the beam stays at furniture height.
FREE_MAX_Z_M = 0.45
FREE_SAMPLES_M = (0.6, 1.2, 1.8, 2.4, 3.0, 3.6)
# Points a return's beam crossed are free too, short of the surface by a margin for map error.
PASS_MARGIN_M = 0.25
PASS_MIN_M = 0.3

# Beam model: a Gaussian around the nearest mapped surface plus a uniform floor for whatever the
# map does not hold (people, chairs, the scan's real furniture). Neighbouring zones are strongly
# correlated, so a frame counts as this many independent beams.
SIGMA_HIT_M = 0.06
RANDOM_FLOOR = 0.25
FREE_PENALTY = 0.3
FRAME_BEAMS = 6.0
# Tracking ignores returns this far from the map at the current estimate (unmapped things), unless
# too few would remain.
GATE_M = 0.12
GATE_MIN_HITS = 6
GATE_MIN_FRACTION = 0.5

# Odometry noise: proportional to the distance and angle moved, plus a small floor per update.
ODOM_TRANS = 0.08
ODOM_ROT = 0.05
ODOM_TRANS_FROM_ROT = 0.02
ODOM_ROT_FROM_TRANS = 0.08
ODOM_FLOOR_M = 0.002
ODOM_FLOOR_RAD = 0.003
# Skates glide where contact odometry sees no step: forward speed is a particle state instead.
ROLLER_SPEED_SIGMA = 0.25
ROLLER_MAX_SPEED = 0.6
ROLLER_DIFFUSION_M = 0.02

PARTICLES = 600
# Global search: a blurred beam model on a coarse grid, then a local climb with the sharp one.
COARSE_SIGMA_M = 0.2
REFINE_BEAMS = 600
REFINE_ROUNDS = 8
CONVERGED_XY_M = 0.15
CONVERGED_YAW_RAD = 0.15
LOST_XY_M = 0.5
LOST_YAW_RAD = 0.5
# Fraction of map-height returns that must land on a mapped surface for the pose to be trusted.
LOW_FIT = 0.35
INSIDE_CAP_M = 0.5
FIT_NEAR_M = 0.12
# A relocalization that moves the pose away from the tracked one (beyond this distance or
# heading) must explain the sweep better, by this map-fit margin, than the best match near it.
RELOC_JUMP_M = 0.3
RELOC_JUMP_RAD = 0.2
RELOC_JUMP_FIT_MARGIN = 0.1
# Heading changes smaller than this are gait sway, not rotation that may have drifted.
ROTATION_STEP_RAD = 0.2
STATE_HISTORY = 200
# Long enough to hold a whole head sweep (8 directions, about 8 s at 15 Hz).
FRAME_HISTORY = 180


def tof_beams(rows: int = TOF_ROWS, cols: int = TOF_COLS, fov: float = TOF_FOV_RAD) -> FloatArray:
    """Unit zone directions in the sensor frame (+x forward, +y left, +z up), row-major."""
    beams = np.zeros((rows * cols, 3))
    for row in range(rows):
        elevation = fov / 2 - (row + 0.5) * fov / rows
        for col in range(cols):
            azimuth = fov / 2 - (col + 0.5) * fov / cols
            beams[row * cols + col] = (
                math.cos(elevation) * math.cos(azimuth),
                math.cos(elevation) * math.sin(azimuth),
                math.sin(elevation),
            )
    return beams


def quat_matrix(w: float, x: float, y: float, z: float) -> FloatArray:
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if not math.isfinite(norm) or norm < 1e-9:
        raise ValueError("invalid quaternion")
    w, x, y, z = w / norm, x / norm, y / norm, z / norm
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
            [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
            [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
        ]
    )


def _wrap(angle: FloatArray | float) -> Any:
    return np.arctan2(np.sin(angle), np.cos(angle))


@dataclass(frozen=True)
class PoseEstimate:
    """A map-frame pose with its uncertainty. `converged` means safe to steer by."""

    x: float
    y: float
    yaw: float
    std_xy: float
    std_yaw: float
    converged: bool
    fit: float = 1.0
    source: str = "estimated"

    @property
    def pose(self) -> Pose:
        return Pose(self.x, self.y, self.yaw)

    @property
    def lost(self) -> bool:
        return self.std_xy > LOST_XY_M or self.std_yaw > LOST_YAW_RAD or self.fit < LOW_FIT

    def describe(self) -> str:
        return (
            f"({self.x:+.2f}, {self.y:+.2f}) m heading {math.degrees(self.yaw):+.0f} deg, "
            f"uncertainty {self.std_xy * 100:.0f} cm / {math.degrees(self.std_yaw):.0f} deg, "
            f"map fit {self.fit:.0%}"
        )


class PoseSource(Protocol):
    """Anything that can tell navigation where the duck is in the navigation map's frame.

    `oracle` (simulator truth, evaluation only), `estimated` (this module's MCL) and a future
    `maploc` source (robotd's SLAM pose behind `robot.map`, after a fixed alignment from its map
    frame to the Roborock-derived navigation map) all fit this shape.
    """

    name: str

    async def estimate(self) -> PoseEstimate: ...

    async def close(self) -> None: ...


class OraclePose:
    """The simulator's true pose. For scoring and evaluation; never the honest controller input."""

    name = "oracle"

    def __init__(self, sim: SimPose) -> None:
        self.sim = sim

    async def estimate(self) -> PoseEstimate:
        pose = await self.sim.read()
        return PoseEstimate(pose.x, pose.y, pose.yaw, 0.0, 0.0, True, source=self.name)

    async def close(self) -> None:
        await self.sim.close()


@dataclass(frozen=True)
class TofFrame:
    t_ns: int
    seq: int
    distance_m: FloatArray
    status: npt.NDArray[np.int64]

    @property
    def valid(self) -> npt.NDArray[np.bool_]:
        return (
            np.isin(self.status, list(TOF_VALID))
            & (self.distance_m >= TOF_MIN_M)
            & (self.distance_m <= TOF_MAX_M)
        )

    @property
    def no_target(self) -> npt.NDArray[np.bool_]:
        return self.status == TOF_NO_TARGET


def parse_tof_frame(params: Mapping[str, Any]) -> TofFrame:
    """Validate a `tof.frame` notification's params. Raises ValueError when malformed."""
    try:
        rows, cols = int(params["rows"]), int(params["cols"])
        distance = np.asarray(params["distance_mm"], dtype=float) / 1000
        status = np.asarray(params["status"], dtype=np.int64)
        t_ns, seq = int(params["t_ns"]), int(params.get("seq", 0))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid ToF frame: {error}") from None
    if (rows, cols) != (TOF_ROWS, TOF_COLS) or distance.shape != (64,) or status.shape != (64,):
        raise ValueError("ToF frame is not 8x8")
    if not np.all(np.isfinite(distance)):
        raise ValueError("non-finite ToF distance")
    return TofFrame(t_ns, seq, distance, status)


@dataclass(frozen=True)
class Odom:
    x: float
    y: float
    yaw: float
    z: float


@dataclass(frozen=True)
class Scan:
    """One frame's returns in the robot's level frame (x forward, y left, z up from the floor).

    `hits` are surfaces at map height, `floor` the returns on the floor, `free` points a no-target
    beam passed through. `odom` is the contact-odometry pose at the frame's time, so scans taken
    while turning in place can be stitched into one composite.
    """

    t_ns: int
    odom: Odom
    hits: FloatArray
    floor: FloatArray
    free: FloatArray
    sensor: FloatArray

    def relative_to(self, odom: Odom) -> tuple[FloatArray, FloatArray]:
        """Hits and free points expressed in the level frame at another odometry pose."""
        dyaw = self.odom.yaw - odom.yaw
        c, s = math.cos(odom.yaw), math.sin(odom.yaw)
        dx, dy = self.odom.x - odom.x, self.odom.y - odom.y
        tx, ty = c * dx + s * dy, -s * dx + c * dy
        rotation = np.array([[math.cos(dyaw), -math.sin(dyaw)], [math.sin(dyaw), math.cos(dyaw)]])
        offset = np.array([tx, ty])
        return self.hits[:, :2] @ rotation.T + offset, self.free[:, :2] @ rotation.T + offset


def _level_rotation(imu_quat: Sequence[float]) -> FloatArray:
    """The trunk's roll and pitch with its heading removed."""
    rotation = quat_matrix(*imu_quat)
    yaw = math.atan2(rotation[1, 0], rotation[0, 0])
    c, s = math.cos(-yaw), math.sin(-yaw)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]) @ rotation


BEAMS = tof_beams()


def scan_from(
    frame: TofFrame, state: Mapping[str, Any], odom: Odom, floor: FloorTracker | None = None
) -> Scan:
    """Project a frame through the head's ToF pose and the IMU tilt into the level frame.

    With a `floor` tracker, returns on the floor plane the frame itself shows count as floor even
    where a small tilt error lifts them above FLOOR_Z_M (see FloorTracker).
    """
    tof = state["frames"]["tof"]
    level = _level_rotation(state["imu"]["quat"])
    sensor = level @ np.asarray(tof["pos"], dtype=float) + np.array([0.0, 0.0, odom.z])
    directions = BEAMS @ (level @ quat_matrix(*tof["quat"])).T
    points = sensor + directions * frame.distance_m[:, None]
    valid = frame.valid
    on_floor = points[:, 2] < FLOOR_Z_M
    plane = floor.observe(points[valid]) if floor is not None else None
    if plane is not None:
        on_floor |= np.abs(points[:, 2] - plane.at(points[:, 0], points[:, 1])) <= (
            FLOOR_FIT_INLIER_M
        )
    hits = points[valid & ~on_floor & (points[:, 2] <= HIT_MAX_Z_M)]
    floor_points = points[valid & on_floor]
    free: list[FloatArray] = []
    for direction in directions[frame.no_target]:
        for distance in FREE_SAMPLES_M:
            point = sensor + direction * distance
            if not FLOOR_Z_M <= point[2] <= FREE_MAX_Z_M:
                break
            free.append(point)
    # A return also says the beam crossed open floor on its way: without that, a pose amid
    # clutter, where every return lands near some surface, scores as well as the right one.
    for direction, distance in zip(directions[valid], frame.distance_m[valid], strict=True):
        for along in (0.5 * distance, distance - PASS_MARGIN_M):
            if along < PASS_MIN_M:
                continue
            point = sensor + direction * along
            if FLOOR_Z_M <= point[2] <= FREE_MAX_Z_M:
                free.append(point)
    return Scan(
        frame.t_ns,
        odom,
        hits,
        floor_points,
        np.array(free).reshape(-1, 3),
        sensor,
    )


@dataclass(frozen=True)
class FloorPlane:
    """The floor in the level frame as z = a·x + b·y + c."""

    a: float = 0.0
    b: float = 0.0
    c: float = 0.0

    def at(self, x: Any, y: Any) -> Any:
        return self.a * x + self.b * y + self.c

    @property
    def tilt_rad(self) -> float:
        return math.atan(math.hypot(self.a, self.b))


class FloorTracker:
    """Finds the floor plane in each frame, so floor returns are never taken for obstacles.

    The level frame is only as good as the IMU's mounting and the head's kinematics: a few
    degrees of error lift floor returns a metre or two ahead to 0.1-0.3 m, where they look like
    unmapped obstacles on the path (measured: 2 deg of pitch with an ideal simulator, 3-4 deg
    with realism `real`, plus +-3 deg of gait sway). Most of the ToF's lower zones see the floor,
    so the plane they share is found frame by frame, and only the returns on it are reclassified:
    a plane found wrongly (little floor in view) cannot move any other return.
    """

    def __init__(self) -> None:
        self.plane: FloorPlane | None = None
        self.fits = 0
        self._rng = np.random.default_rng(0)

    def observe(self, points: FloatArray) -> FloorPlane | None:
        self.plane = self._fit(points)
        self.fits += self.plane is not None
        return self.plane

    def _fit(self, points: FloatArray) -> FloorPlane | None:
        """The plane that best fits most returns among plausible floors (a sampled consensus).

        Wall feet and box sides also lie low and close; a least-squares fit through everything
        near the floor tilts towards them, while near-vertical surfaces never gather many returns
        on one gently tilted plane.
        """
        x, y, z = points[:, 0], points[:, 1], points[:, 2]
        reach = np.hypot(x, y)
        plausible = (reach <= FLOOR_FIT_MAX_REACH_M) & (
            np.abs(z) <= FLOOR_FIT_MAX_OFFSET_M + math.tan(FLOOR_FIT_MAX_TILT_RAD) * reach
        )
        if plausible.sum() < FLOOR_FIT_MIN_POINTS:
            return None
        x, y, z, reach = x[plausible], y[plausible], z[plausible], reach[plausible]
        picks = self._rng.integers(0, len(x), (FLOOR_FIT_TRIALS, 3))
        design = np.stack((x[picks], y[picks], np.ones(picks.shape)), axis=-1)
        solvable = np.abs(np.linalg.det(design)) > 1e-4
        if not solvable.any():
            return None
        planes = np.linalg.solve(design[solvable], z[picks][solvable][..., None])[..., 0]
        plausible_plane = (
            np.hypot(planes[:, 0], planes[:, 1]) <= math.tan(FLOOR_FIT_MAX_TILT_RAD)
        ) & (np.abs(planes[:, 2]) <= FLOOR_FIT_MAX_OFFSET_M)
        if not plausible_plane.any():
            return None
        planes = planes[plausible_plane]
        residual = np.abs(z[None, :] - (planes[:, :1] * x + planes[:, 1:2] * y + planes[:, 2:]))
        # Truncated squared residuals: a plane tilted to also graze a wall foot pays for every
        # floor return it no longer sits on, so the closest fit wins, not the widest.
        cost = np.minimum(residual, FLOOR_FIT_INLIER_M) ** 2
        best = residual[int(np.argmin(cost.sum(axis=1)))] <= FLOOR_FIT_INLIER_M
        if best.sum() < FLOOR_FIT_MIN_POINTS or np.ptp(reach[best]) < FLOOR_FIT_MIN_SPAN_M:
            return None
        design = np.column_stack((x[best], y[best], np.ones(int(best.sum()))))
        (a, b, c), *_ = np.linalg.lstsq(design, z[best], rcond=None)
        plane = FloorPlane(float(a), float(b), float(c))
        if plane.tilt_rad > FLOOR_FIT_MAX_TILT_RAD or abs(plane.c) > FLOOR_FIT_MAX_OFFSET_M:
            return None
        return plane


class LikelihoodField:
    """The navigation map's clearance grid as a distance-to-nearest-surface lookup."""

    def __init__(self, nav_map: NavigationMap) -> None:
        self.map = nav_map
        self.distance = (
            np.frombuffer(nav_map.clearance, dtype=np.uint8)
            .reshape(nav_map.height, nav_map.width)
            .astype(np.float32)
            / 100
        )
        self.origin = np.array(nav_map.origin)
        self.resolution = nav_map.resolution
        # Clearance is zero everywhere inside walls and furniture, which would let a hit that
        # lands deep behind a surface score as well as one on it: every pose pushed toward the
        # wall would look as good. Depth inside the blocked area makes the field two-sided.
        self.inside = self._depth_inside(self.distance <= 0, nav_map.resolution)
        self.robot_radius = nav_map.robot_radius
        rows, cols = np.nonzero(self.distance >= nav_map.robot_radius)
        self.free_cells = np.column_stack(
            (self.origin[0] + cols * self.resolution, self.origin[1] + rows * self.resolution)
        )

    @staticmethod
    def _depth_inside(blocked: npt.NDArray[np.bool_], resolution: float) -> Float32Array:
        """Approximate depth of each blocked cell below the nearest free cell, capped."""
        depth = np.zeros(blocked.shape, dtype=np.float32)
        remaining = blocked.copy()
        layers = int(round(INSIDE_CAP_M / resolution))
        for layer in range(layers):
            padded = np.pad(remaining, 1, constant_values=True)
            interior = remaining.copy()
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    interior &= padded[
                        1 + dr : 1 + dr + blocked.shape[0], 1 + dc : 1 + dc + blocked.shape[1]
                    ]
            depth[remaining & ~interior] = (layer + 0.5) * resolution
            remaining = interior
        depth[remaining] = INSIDE_CAP_M
        return depth

    def lookup(self, x: FloatArray, y: FloatArray) -> Float32Array:
        """Distance from world points to the nearest mapped obstacle; 0 outside the map."""
        col = np.rint((x - self.origin[0]) / self.resolution).astype(np.int64)
        row = np.rint((y - self.origin[1]) / self.resolution).astype(np.int64)
        inside = (row >= 0) & (row < self.map.height) & (col >= 0) & (col < self.map.width)
        result = np.zeros(np.shape(x), dtype=np.float32)
        result[inside] = self.distance[row[inside], col[inside]]
        return result

    def surface_distance(self, x: FloatArray, y: FloatArray) -> Float32Array:
        """Unsigned distance to the nearest mapped surface, from either side of it."""
        col = np.rint((x - self.origin[0]) / self.resolution).astype(np.int64)
        row = np.rint((y - self.origin[1]) / self.resolution).astype(np.int64)
        inside = (row >= 0) & (row < self.map.height) & (col >= 0) & (col < self.map.width)
        result = np.full(np.shape(x), INSIDE_CAP_M, dtype=np.float32)
        r, c = row[inside], col[inside]
        result[inside] = self.distance[r, c] + self.inside[r, c]
        return result

    def log_likelihood(
        self,
        x: FloatArray,
        y: FloatArray,
        yaw: FloatArray,
        hits: FloatArray,
        free: FloatArray,
        sigma: float = SIGMA_HIT_M,
    ) -> FloatArray:
        """Per-pose log-likelihood of level-frame hits and free points (2D arrays ok)."""
        c, s = np.cos(yaw)[..., None], np.sin(yaw)[..., None]
        total = np.zeros(np.shape(x))
        if len(hits):
            wx = x[..., None] + c * hits[:, 0] - s * hits[:, 1]
            wy = y[..., None] + s * hits[:, 0] + c * hits[:, 1]
            d = self.surface_distance(wx, wy)
            total += np.log(np.exp(-0.5 * (d / sigma) ** 2) + RANDOM_FLOOR).sum(axis=-1)
        if len(free):
            wx = x[..., None] + c * free[:, 0] - s * free[:, 1]
            wy = y[..., None] + s * free[:, 0] + c * free[:, 1]
            blocked = self.lookup(wx, wy) < self.resolution
            total += np.log(np.where(blocked, FREE_PENALTY, 1.0)).sum(axis=-1)
        # The duck stands on free floor, never inside furniture or walls.
        inside = self.lookup(x, y) < 0.5 * self.robot_radius
        return np.where(inside, total - 20.0, total)

    def fit(self, pose: Pose, hits: FloatArray) -> float:
        """Fraction of hits that land on a mapped surface from this pose."""
        if not len(hits):
            return 1.0
        c, s = math.cos(pose.yaw), math.sin(pose.yaw)
        wx = pose.x + c * hits[:, 0] - s * hits[:, 1]
        wy = pose.y + s * hits[:, 0] + c * hits[:, 1]
        return float(np.mean(self.surface_distance(wx, wy) <= FIT_NEAR_M))


@dataclass(frozen=True)
class Hypothesis:
    pose: Pose
    score: float


def global_search(
    field: LikelihoodField,
    hits: FloatArray,
    free: FloatArray,
    *,
    step_m: float = 0.1,
    yaw_bins: int = 72,
    centre: tuple[float, float] | None = None,
    radius: float | None = None,
    heading: float | None = None,
    heading_tolerance: float = math.pi,
    keep: int = 40,
    max_beams: int = 160,
) -> list[Hypothesis]:
    """Match a composite scan over every free cell and heading; best hypotheses first."""
    if len(hits) < 8:
        return []
    fine_hits, fine_free = hits, free
    rng = np.random.default_rng(0)
    if len(hits) > max_beams:
        hits = hits[rng.choice(len(hits), max_beams, replace=False)]
    if len(free) > max_beams // 2:
        free = free[rng.choice(len(free), max_beams // 2, replace=False)]
    cells = field.free_cells
    stride = max(1, round(step_m / field.resolution))
    keep_mask = (np.rint((cells[:, 0] - field.origin[0]) / field.resolution) % stride == 0) & (
        np.rint((cells[:, 1] - field.origin[1]) / field.resolution) % stride == 0
    )
    cells = cells[keep_mask]
    if centre is not None and radius is not None:
        cells = cells[np.hypot(cells[:, 0] - centre[0], cells[:, 1] - centre[1]) <= radius]
    if not len(cells):
        return []
    yaws = np.linspace(-math.pi, math.pi, yaw_bins, endpoint=False)
    if heading is not None:
        yaws = yaws[np.abs(_wrap(yaws - heading)) <= heading_tolerance]
        if not len(yaws):
            yaws = np.array([heading])
    best: list[tuple[float, float, float, float]] = []
    chunk = max(1, 400_000 // max(1, len(hits) * len(yaws)))
    for start in range(0, len(cells), chunk):
        block = cells[start : start + chunk]
        x = np.repeat(block[:, 0], len(yaws)).reshape(-1, len(yaws))
        y = np.repeat(block[:, 1], len(yaws)).reshape(-1, len(yaws))
        yaw = np.broadcast_to(yaws, x.shape)
        scores = field.log_likelihood(x, y, yaw, hits, free, sigma=COARSE_SIGMA_M)
        flat = scores.ravel()
        top = np.argpartition(flat, -min(keep, flat.size))[-min(keep, flat.size) :]
        best.extend(
            (float(flat[i]), float(x.ravel()[i]), float(y.ravel()[i]), float(yaw.ravel()[i]))
            for i in top
        )
        best = sorted(best, reverse=True)[:keep]
    # The coarse grid is 10 cm and 5 degrees; at 3 m that is a quarter-metre miss, so the true
    # pose's peak is easily stepped over. Each candidate is climbed to its local optimum with the
    # full composite before the candidates are compared.
    rng_fine = np.random.default_rng(1)
    if len(fine_hits) > REFINE_BEAMS:
        fine_hits = fine_hits[rng_fine.choice(len(fine_hits), REFINE_BEAMS, replace=False)]
    if len(fine_free) > REFINE_BEAMS // 2:
        fine_free = fine_free[rng_fine.choice(len(fine_free), REFINE_BEAMS // 2, replace=False)]
    refined = [
        _refine(field, Pose(x, y, yaw), fine_hits, fine_free, step_m, 2 * math.pi / yaw_bins)
        for _, x, y, yaw in best
    ]
    refined.sort(key=lambda hypothesis: hypothesis.score, reverse=True)
    return refined


def _refine(
    field: LikelihoodField,
    pose: Pose,
    hits: FloatArray,
    free: FloatArray,
    step_m: float,
    step_yaw: float,
) -> Hypothesis:
    """Pattern search around a pose on a 3x3x3 stencil, halving the step each round."""
    offsets = np.array([(a, b, c) for a in (-1, 0, 1) for b in (-1, 0, 1) for c in (-1, 0, 1)])
    x, y, yaw = pose.x, pose.y, pose.yaw
    score = -math.inf
    for _ in range(REFINE_ROUNDS):
        xs = x + offsets[:, 0] * step_m
        ys = y + offsets[:, 1] * step_m
        yaws = yaw + offsets[:, 2] * step_yaw
        scores = field.log_likelihood(xs, ys, yaws, hits, free)
        best = int(np.argmax(scores))
        x, y, yaw, score = float(xs[best]), float(ys[best]), float(yaws[best]), float(scores[best])
        if offsets[best].any():
            continue
        step_m /= 2
        step_yaw /= 2
    return Hypothesis(Pose(x, y, float(_wrap(yaw))), score)


class ParticleFilter:
    """Particles (x, y, yaw, forward speed) in the map frame."""

    def __init__(self, field: LikelihoodField, count: int = PARTICLES, seed: int = 0) -> None:
        self.field = field
        self.count = count
        self.rng = np.random.default_rng(seed)
        self.x = np.zeros(count)
        self.y = np.zeros(count)
        self.yaw = np.zeros(count)
        self.speed = np.zeros(count)
        self.log_w = np.zeros(count)
        self.initialized = False

    def _set(self, x: FloatArray, y: FloatArray, yaw: FloatArray) -> None:
        self.x, self.y, self.yaw = x.astype(float), y.astype(float), _wrap(yaw).astype(float)
        self.speed = np.zeros(len(x))
        self.log_w = np.zeros(len(x))
        self.initialized = True

    def init_pose(self, pose: Pose, std_xy: float = 0.1, std_yaw: float = 0.1) -> None:
        n = self.count
        self._set(
            pose.x + self.rng.normal(0, std_xy, n),
            pose.y + self.rng.normal(0, std_xy, n),
            pose.yaw + self.rng.normal(0, std_yaw, n),
        )

    def init_region(
        self, centre: tuple[float, float], radius: float, yaw: float | None = None
    ) -> None:
        cells = self.field.free_cells
        near = cells[np.hypot(cells[:, 0] - centre[0], cells[:, 1] - centre[1]) <= radius]
        if not len(near):
            raise ValueError("no free floor in the start region")
        n = self.count * 4
        picked = near[self.rng.integers(0, len(near), n)]
        jitter = self.rng.uniform(-0.5, 0.5, (n, 2)) * self.field.resolution
        headings = (
            self.rng.uniform(-math.pi, math.pi, n)
            if yaw is None
            else yaw + self.rng.normal(0, 0.15, n)
        )
        self._set(picked[:, 0] + jitter[:, 0], picked[:, 1] + jitter[:, 1], headings)

    def init_hypotheses(self, hypotheses: Sequence[Hypothesis]) -> None:
        if not hypotheses:
            raise ValueError("no hypotheses")
        scores = np.array([h.score for h in hypotheses])
        weights = np.exp(scores - scores.max())
        picks = self.rng.choice(len(hypotheses), self.count, p=weights / weights.sum())
        base = np.array([[h.pose.x, h.pose.y, h.pose.yaw] for h in hypotheses])[picks]
        self._set(
            base[:, 0] + self.rng.normal(0, 0.05, self.count),
            base[:, 1] + self.rng.normal(0, 0.05, self.count),
            base[:, 2] + self.rng.normal(0, 0.05, self.count),
        )

    def predict(self, dx: float, dy: float, dyaw: float, dt: float, glide: bool) -> None:
        """Move by an odometry step expressed in the robot frame at the step's start."""
        n = len(self.x)
        rng = self.rng
        if glide:
            # Contact odometry stays put on skates: forward speed is unknown, so it diffuses.
            self.speed = np.clip(
                self.speed + rng.normal(0, ROLLER_SPEED_SIGMA * math.sqrt(dt), n),
                -0.2,
                ROLLER_MAX_SPEED,
            )
            forward = self.speed * dt + rng.normal(0, ROLLER_DIFFUSION_M * math.sqrt(dt), n)
            side = rng.normal(0, ROLLER_DIFFUSION_M * math.sqrt(dt), n)
        else:
            distance = math.hypot(dx, dy)
            sigma_t = ODOM_TRANS * distance + ODOM_TRANS_FROM_ROT * abs(dyaw) + ODOM_FLOOR_M
            forward = dx + rng.normal(0, sigma_t, n)
            side = dy + rng.normal(0, sigma_t, n)
        sigma_r = ODOM_ROT * abs(dyaw) + ODOM_ROT_FROM_TRANS * math.hypot(dx, dy) + ODOM_FLOOR_RAD
        turn = dyaw + rng.normal(0, sigma_r, n)
        c, s = np.cos(self.yaw), np.sin(self.yaw)
        self.x = self.x + c * forward - s * side
        self.y = self.y + s * forward + c * side
        self.yaw = _wrap(self.yaw + turn)

    def correct(self, scan: Scan) -> None:
        if not len(scan.hits) and not len(scan.free):
            return
        hits, free = self.gate(scan.hits), scan.free
        log_l = self.field.log_likelihood(self.x, self.y, self.yaw, hits, free)
        beams = len(hits) + len(free) / len(FREE_SAMPLES_M)
        self.log_w = self.log_w + log_l * min(1.0, FRAME_BEAMS / max(1.0, beams))
        self.log_w -= self.log_w.max()
        weights = np.exp(self.log_w)
        weights /= weights.sum()
        if 1 / np.sum(weights**2) < 0.5 * len(weights) or len(weights) != self.count:
            self._resample(weights)

    def gate(self, hits: FloatArray) -> FloatArray:
        """Drop returns that land far from any mapped surface from the current estimate.

        An unmapped closed door or a chair otherwise pulls the cloud to where the map has a wall
        that looks like it (a door across a doorway lines up with the wall beside it). When most
        returns are unexplained the estimate itself is in doubt, so they all count.
        """
        if len(hits) < 2 * GATE_MIN_HITS:
            return hits
        pose, _, _ = self.estimate()
        c, s = math.cos(pose.yaw), math.sin(pose.yaw)
        wx = pose.x + c * hits[:, 0] - s * hits[:, 1]
        wy = pose.y + s * hits[:, 0] + c * hits[:, 1]
        keep = self.field.surface_distance(wx, wy) <= GATE_M
        if keep.sum() < max(GATE_MIN_HITS, GATE_MIN_FRACTION * len(hits)):
            return hits
        return hits[keep]

    def _resample(self, weights: FloatArray) -> None:
        n = self.count
        positions = (self.rng.random() + np.arange(n)) / n
        index = np.minimum(np.searchsorted(np.cumsum(weights), positions), len(weights) - 1)
        self.x, self.y, self.yaw = self.x[index], self.y[index], self.yaw[index]
        self.speed = self.speed[index]
        self.log_w = np.zeros(n)

    def estimate(self) -> tuple[Pose, float, float]:
        weights = np.exp(self.log_w - self.log_w.max())
        weights /= weights.sum()
        x = float(np.sum(weights * self.x))
        y = float(np.sum(weights * self.y))
        sin, cos = (
            float(np.sum(weights * np.sin(self.yaw))),
            float(np.sum(weights * np.cos(self.yaw))),
        )
        yaw = math.atan2(sin, cos)
        std_xy = math.sqrt(float(np.sum(weights * ((self.x - x) ** 2 + (self.y - y) ** 2))))
        resultant = min(1.0, math.hypot(sin, cos))
        std_yaw = math.sqrt(-2 * math.log(resultant)) if resultant > 1e-9 else math.pi
        return Pose(x, y, yaw), std_xy, std_yaw


class DepthSource(Protocol):
    """A stream of ToF frames, whatever carries them (relay TCP today, WebRTC later)."""

    def frames(self) -> AsyncIterator[TofFrame]: ...

    async def close(self) -> None: ...


class TcpTofStream:
    """Subscribe to tofd through duck-relay's ToF port and yield its frames, reconnecting."""

    def __init__(self, url: str, token: str = "", retry_s: float = 1.0) -> None:
        self._address = tcp_address(url)
        self._token = token
        self._retry_s = retry_s
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False
        self.errors = 0

    async def frames(self) -> AsyncGenerator[TofFrame, None]:
        while not self._closed:
            try:
                reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(*self._address, limit=1 << 20), 3
                )
                if self._token:
                    self._writer.write(self._token.encode() + b"\n")
                self._writer.write(b'{"jsonrpc":"2.0","id":1,"method":"tof.stream"}\n')
                await self._writer.drain()
                while line := await asyncio.wait_for(reader.readline(), 5):
                    message = json.loads(line)
                    if message.get("method") == "tof.frame":
                        try:
                            yield parse_tof_frame(message.get("params") or {})
                        except ValueError:
                            self.errors += 1
                    elif "error" in message:
                        raise ConnectionError(str(message["error"]))
                    elif isinstance(message.get("result"), dict) and not message["result"].get(
                        "accepted", True
                    ):
                        raise ConnectionError("tofd refused the stream")
            except (OSError, TimeoutError, ValueError, ConnectionError):
                self.errors += 1
            finally:
                await self._drop()
            if not self._closed:
                await asyncio.sleep(self._retry_s)

    async def _drop(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def close(self) -> None:
        self._closed = True
        await self._drop()


class NotifyingLink(Protocol):
    """The part of WebRtcLink that LinkTofStream needs."""

    generation: int

    @property
    def connected(self) -> bool: ...

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,  # noqa: ASYNC109 - RobotLink contract
    ) -> Any: ...

    def add_notification_listener(
        self, method: str, callback: Callable[[Any], None]
    ) -> Callable[[], None]: ...


class LinkTofStream:
    """Subscribe to tofd over the robot link itself (WebRTC: mediad routes tof.stream to tofd).

    tof.frame notifications come back on the control channel. A subscription dies with its
    channel, so a new channel (link.generation) is subscribed again; so is a silent one.
    """

    def __init__(self, link: NotifyingLink, retry_s: float = 1.0, silence_s: float = 5.0) -> None:
        self._link = link
        self._retry_s = retry_s
        self._silence_s = silence_s
        self._closed = False
        self._wake = asyncio.Event()
        self.errors = 0

    async def frames(self) -> AsyncGenerator[TofFrame, None]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=8)

        def push(params: Any) -> None:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(params)

        remove = self._link.add_notification_listener("tof.frame", push)
        subscribed = -1
        try:
            while not self._closed:
                if not self._link.connected:
                    await self._sleep(self._retry_s)
                    continue
                if subscribed != self._link.generation:
                    generation = self._link.generation
                    try:
                        result = await self._link.call("tof.stream", timeout=3.0)
                    except Exception:
                        self.errors += 1
                        await self._sleep(self._retry_s)
                        continue
                    if isinstance(result, dict) and not result.get("accepted", True):
                        self.errors += 1
                        await self._sleep(self._retry_s)
                        continue
                    subscribed = generation
                try:
                    params = await asyncio.wait_for(queue.get(), self._silence_s)
                except TimeoutError:
                    self.errors += 1
                    # Same channel, no frames: tofd answers one stream per lane, so only a new
                    # channel can restart it; nothing to resend until the generation changes.
                    continue
                try:
                    yield parse_tof_frame(params or {})
                except ValueError:
                    self.errors += 1
        finally:
            remove()

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)

    async def close(self) -> None:
        self._closed = True
        self._wake.set()


def _odom(state: Mapping[str, Any]) -> Odom | None:
    odom = state.get("odom")
    if not isinstance(odom, dict):
        return None
    try:
        x, y, z = (float(v) for v in odom["position"])
        yaw = float(odom["yaw"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(map(math.isfinite, (x, y, z, yaw))):
        return None
    return Odom(x, y, yaw, z)


@dataclass
class _Stamped:
    t_ns: int
    odom: Odom
    state: Mapping[str, Any]


@dataclass
class LocalizerStats:
    frames: int = 0
    used: int = 0
    skipped: int = 0
    updates: list[tuple[float, PoseEstimate]] = field(default_factory=list)


class EstimatedPose:
    """Monte-Carlo localization fed by robot.state (odometry, IMU, head) and ToF frames.

    `state` returns the link's latest robot.state; the localizer polls it to keep a short history,
    so each ToF frame is projected with the head pose, tilt and odometry of its own instant.
    """

    name = "estimated"

    def __init__(
        self,
        nav_map: NavigationMap,
        state: Callable[[], Mapping[str, Any] | None],
        depth: DepthSource,
        *,
        clock: Callable[[], float] | None = None,
        particles: int = PARTICLES,
        seed: int = 0,
    ) -> None:
        self.map = nav_map
        self.field = LikelihoodField(nav_map)
        self.filter = ParticleFilter(self.field, particles, seed)
        self._state = state
        self._depth = depth
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._history: deque[_Stamped] = deque(maxlen=STATE_HISTORY)
        self._scans: deque[Scan] = deque(maxlen=FRAME_HISTORY)
        self._last_odom: Odom | None = None
        self._last_t_ns: int | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._fit = 1.0
        self.glide = False
        self.relocalizing = False
        # Whether the filter's pose came from a known start or a head-sweep match.
        self.confirmed = False
        self.stats = LocalizerStats()
        self.floor = FloorTracker()
        # Odometry distance (m) and rotation (rad) since the pose was last confirmed: the
        # filter's spread alone understates how far a long unchecked track may have drifted.
        self.travel_since_fix = 0.0
        self.rotation_since_fix = 0.0
        self._rotation_anchor: float | None = None
        self.frame_event = asyncio.Event()

    # Lifecycle -----------------------------------------------------------------------------
    def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._poll_state()),
                asyncio.create_task(self._consume()),
            ]

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        await self._depth.close()

    # Initialisation ------------------------------------------------------------------------
    def reset_pose(self, pose: Pose, std_xy: float = 0.1, std_yaw: float = 0.1) -> None:
        """Start from a known pose (the dock, a place the user confirmed)."""
        self.filter.init_pose(pose, std_xy, std_yaw)
        self._fit = 1.0
        self.confirmed = True
        self.travel_since_fix = self.rotation_since_fix = 0.0
        self._rotation_anchor = None

    def reset_region(
        self, centre: tuple[float, float], radius: float, yaw: float | None = None
    ) -> None:
        """Start somewhere in a region: not trusted until a head sweep has confirmed it.

        A single 45-degree wedge can pull the particles into a confident wrong pose (a rotated
        twin of the room), so frames alone never promote a region start to `converged`.
        """
        self.filter.init_region(centre, radius, yaw)
        self._fit = 1.0
        self.confirmed = False

    def start_at(self, spec: str) -> str:
        """Apply a NAV_START value: `place`, `place@heading_deg` or `x,y,heading_deg`; '' = unknown.

        Returns a short description for the journal. A place without heading starts as a 0.5 m
        region with unknown heading, which the first go_to confirms with a head sweep.
        """
        spec = spec.strip()
        if not spec:
            self.confirmed = False
            return "unknown start: the first go_to relocalizes with a head sweep"
        name, _, heading = spec.partition("@")
        if name in self.map.places:
            x, y = self.map.places[name]
            if heading:
                self.reset_pose(Pose(x, y, math.radians(float(heading))), 0.07, 0.07)
                return f"start at {name}, heading {float(heading):g} deg"
            self.reset_region((x, y), 0.5)
            return f"start near {name}, heading unknown"
        parts = [float(value) for value in spec.split(",")]
        if len(parts) != 3 or not all(math.isfinite(value) for value in parts):
            raise ValueError(f"NAV_START must be a place, place@deg or x,y,deg: {spec!r}")
        self.reset_pose(Pose(parts[0], parts[1], math.radians(parts[2])), 0.07, 0.07)
        return f"start at ({parts[0]:g}, {parts[1]:g}), heading {parts[2]:g} deg"

    @property
    def initialized(self) -> bool:
        return self.filter.initialized

    def use_map(self, nav_map: NavigationMap) -> None:
        """Weigh beams against another version of the map (with obstacles the ToF has found)."""
        self.field = LikelihoodField(nav_map)
        self.filter.field = self.field

    # Feeding -------------------------------------------------------------------------------
    async def _poll_state(self) -> None:
        while True:
            self.observe_state(self._state())
            await asyncio.sleep(0.02)

    def observe_state(self, state: Mapping[str, Any] | None) -> None:
        if not state:
            return
        t_ns = state.get("t_ns")
        odom = _odom(state)
        if not isinstance(t_ns, int) or odom is None:
            return
        if self._history and t_ns <= self._history[-1].t_ns:
            return
        self._history.append(_Stamped(t_ns, odom, state))

    async def _consume(self) -> None:
        async for frame in self._depth.frames():
            self.observe_frame(frame)

    def _odom_at(self, t_ns: int) -> tuple[Odom, Mapping[str, Any]] | None:
        history = self._history
        if not history:
            return None
        after = next((item for item in history if item.t_ns >= t_ns), None)
        before = next((item for item in reversed(history) if item.t_ns <= t_ns), None)
        if before is None:
            return None if after is None or after.t_ns - t_ns > 2e8 else (after.odom, after.state)
        if after is None:
            return (before.odom, before.state) if t_ns - before.t_ns <= 3e8 else None
        if after.t_ns == before.t_ns:
            return before.odom, before.state
        f = (t_ns - before.t_ns) / (after.t_ns - before.t_ns)
        a, b = before.odom, after.odom
        odom = Odom(
            a.x + f * (b.x - a.x),
            a.y + f * (b.y - a.y),
            float(a.yaw + f * _wrap(b.yaw - a.yaw)),
            a.z + f * (b.z - a.z),
        )
        nearest = before if f < 0.5 else after
        return odom, nearest.state

    def observe_frame(self, frame: TofFrame) -> Scan | None:
        """Predict to the frame's instant with odometry, then weigh particles by its returns."""
        self.stats.frames += 1
        if self.relocalizing:
            # A sweep match is reshaping the particle set in a worker thread.
            self.stats.skipped += 1
            return None
        found = self._odom_at(frame.t_ns)
        if found is None:
            self.stats.skipped += 1
            return None
        odom, state = found
        try:
            scan = scan_from(frame, state, odom, self.floor)
        except (KeyError, TypeError, ValueError):
            self.stats.skipped += 1
            return None
        self._scans.append(scan)
        self.advance(odom, frame.t_ns)
        if self.filter.initialized:
            self.filter.correct(scan)
            pose, _, _ = self.filter.estimate()
            fit = self.field.fit(pose, scan.hits)
            if len(scan.hits) >= 6:
                self._fit = 0.7 * self._fit + 0.3 * fit
            self.stats.used += 1
        self.frame_event.set()
        self.frame_event = asyncio.Event()
        return scan

    def advance(self, odom: Odom, t_ns: int) -> None:
        previous, previous_t = self._last_odom, self._last_t_ns
        self._last_odom, self._last_t_ns = odom, t_ns
        if previous is None or previous_t is None or not self.filter.initialized:
            return
        dt = max(0.0, (t_ns - previous_t) / 1e9)
        c, s = math.cos(previous.yaw), math.sin(previous.yaw)
        dx, dy = odom.x - previous.x, odom.y - previous.y
        self.travel_since_fix += math.hypot(dx, dy)
        if self._rotation_anchor is None:
            self._rotation_anchor = previous.yaw
        turned = abs(float(_wrap(odom.yaw - self._rotation_anchor)))
        if turned >= ROTATION_STEP_RAD:
            self.rotation_since_fix += turned
            self._rotation_anchor = odom.yaw
        self.filter.predict(
            c * dx + s * dy,
            -s * dx + c * dy,
            float(_wrap(odom.yaw - previous.yaw)),
            min(dt, 0.5),
            self.glide,
        )

    # Reading -------------------------------------------------------------------------------
    def current(self) -> PoseEstimate:
        if not self.filter.initialized:
            return PoseEstimate(0, 0, 0, math.inf, math.pi, False, 0.0)
        pose, std_xy, std_yaw = self.filter.estimate()
        # Between frames, carry the estimate forward with the newest odometry.
        latest = self._history[-1].odom if self._history else None
        if latest is not None and self._last_odom is not None and not self.glide:
            last = self._last_odom
            c, s = math.cos(last.yaw), math.sin(last.yaw)
            dx, dy = latest.x - last.x, latest.y - last.y
            fx, fy = c * dx + s * dy, -s * dx + c * dy
            cy, sy = math.cos(pose.yaw), math.sin(pose.yaw)
            pose = Pose(
                pose.x + cy * fx - sy * fy,
                pose.y + sy * fx + cy * fy,
                float(_wrap(pose.yaw + _wrap(latest.yaw - last.yaw))),
            )
        converged = std_xy <= CONVERGED_XY_M and std_yaw <= CONVERGED_YAW_RAD
        return PoseEstimate(
            pose.x,
            pose.y,
            pose.yaw,
            std_xy,
            std_yaw,
            converged and self._fit >= LOW_FIT and self.confirmed,
            self._fit,
        )

    async def estimate(self) -> PoseEstimate:
        estimate = self.current()
        self.stats.updates.append((self._clock(), estimate))
        del self.stats.updates[:-2000]
        return estimate

    def recent_scans(self, since_ns: int) -> list[Scan]:
        return [scan for scan in self._scans if scan.t_ns >= since_ns]

    def latest_scan(self) -> Scan | None:
        return self._scans[-1] if self._scans else None

    def latest_odom(self) -> Odom | None:
        return self._history[-1].odom if self._history else None

    def latest_t_ns(self) -> int | None:
        return self._history[-1].t_ns if self._history else None

    def relocalize(
        self,
        scans: Sequence[Scan],
        *,
        centre: tuple[float, float] | None = None,
        radius: float | None = None,
        heading: float | None = None,
        heading_tolerance: float = math.pi,
        prior: Pose | None = None,
    ) -> list[Hypothesis]:
        """Match a stationary head-sweep composite over the map and restart the filter.

        `heading` is the composite's reference heading in the map frame when it is known (the IMU
        keeps yaw far better than contact odometry keeps position), which removes the rotated
        look-alikes a rectangular room offers. With the tracked `prior`, a match away from it is
        only taken when it explains the sweep clearly better than the best match near it: in a
        wide room the ToF reaches few walls, and a look-alike a metre off may score as well.
        """
        if not scans:
            return []
        reference = scans[-1].odom
        hits: list[FloatArray] = []
        free: list[FloatArray] = []
        for scan in scans:
            h, f = scan.relative_to(reference)
            hits.append(h)
            free.append(f)
        all_hits = np.concatenate(hits) if hits else np.zeros((0, 2))
        all_free = np.concatenate(free) if free else np.zeros((0, 2))
        hypotheses = global_search(
            self.field,
            all_hits,
            all_free,
            centre=centre,
            radius=radius,
            heading=heading,
            heading_tolerance=heading_tolerance,
        )
        if not hypotheses:
            return []
        if prior is not None:
            best = hypotheses[0].pose
            jumped = (
                math.dist((best.x, best.y), (prior.x, prior.y)) > RELOC_JUMP_M
                or abs(float(_wrap(best.yaw - prior.yaw))) > RELOC_JUMP_RAD
            )
            if jumped:
                near = global_search(
                    self.field,
                    all_hits,
                    all_free,
                    centre=(prior.x, prior.y),
                    radius=RELOC_JUMP_M,
                    heading=prior.yaw,
                    heading_tolerance=RELOC_JUMP_RAD,
                    keep=8,
                )
                if near and self.field.fit(best, all_hits) < (
                    self.field.fit(near[0].pose, all_hits) + RELOC_JUMP_FIT_MARGIN
                ):
                    hypotheses = near
        self.filter.init_hypotheses(hypotheses)
        reference_t = scans[-1].t_ns
        composite = Scan(
            reference_t,
            reference,
            np.column_stack((all_hits, np.full(len(all_hits), 0.2))),
            np.zeros((0, 3)),
            np.column_stack((all_free, np.full(len(all_free), 0.2))),
            np.zeros(3),
        )
        for _ in range(3):
            self.filter.correct(composite)
            self.filter.x += self.filter.rng.normal(0, 0.02, len(self.filter.x))
            self.filter.y += self.filter.rng.normal(0, 0.02, len(self.filter.y))
            self.filter.yaw = _wrap(
                self.filter.yaw + self.filter.rng.normal(0, 0.02, len(self.filter.yaw))
            )
        self.filter.correct(composite)
        pose, std_xy, std_yaw = self.filter.estimate()
        self._fit = self.field.fit(pose, composite.hits)
        self.confirmed = (
            std_xy <= CONVERGED_XY_M and std_yaw <= CONVERGED_YAW_RAD and self._fit >= LOW_FIT
        )
        if self.confirmed:
            self.travel_since_fix = self.rotation_since_fix = 0.0
            self._rotation_anchor = None
        self._last_odom, self._last_t_ns = reference, reference_t
        # Odometry since the reference frame is applied on the next frame.
        return hypotheses


def obstacle_points(scan: Scan, max_z: float = 0.35) -> FloatArray:
    """Level-frame returns at the duck's own height: anything it could walk into."""
    return scan.hits[scan.hits[:, 2] <= max_z][:, :2]


def unexplained_points(
    field: LikelihoodField,
    pose: Pose,
    points: FloatArray,
    *,
    explained_m: float = 0.1,
    max_range_m: float = math.inf,
) -> FloatArray:
    """Level-frame points, placed at `pose` in the map, that no mapped surface accounts for."""
    if not len(points):
        return np.zeros((0, 2))
    points = points[np.hypot(points[:, 0], points[:, 1]) <= max_range_m]
    c, s = math.cos(pose.yaw), math.sin(pose.yaw)
    world = np.column_stack(
        (pose.x + c * points[:, 0] - s * points[:, 1], pose.y + s * points[:, 0] + c * points[:, 1])
    )
    return world[field.lookup(world[:, 0], world[:, 1]) > explained_m]


def blocking_points(
    field: LikelihoodField,
    pose: Pose,
    points: FloatArray,
    path: Sequence[tuple[float, float]],
    *,
    lookahead_m: float,
    half_width_m: float,
    explained_m: float = 0.1,
) -> FloatArray:
    """World points that sit on the next `lookahead_m` of path and that the map does not hold."""
    if len(path) < 2:
        return np.zeros((0, 2))
    world = unexplained_points(field, pose, points, explained_m=explained_m)
    if not len(world):
        return world
    near = np.zeros(len(world), dtype=bool)
    travelled = 0.0
    for a, b in zip(path, path[1:], strict=False):
        if travelled >= lookahead_m:
            break
        ax, ay = a
        bx, by = b
        length = math.hypot(bx - ax, by - ay)
        if length < 1e-6:
            continue
        usable = min(length, lookahead_m - travelled)
        ux, uy = (bx - ax) / length, (by - ay) / length
        along = (world[:, 0] - ax) * ux + (world[:, 1] - ay) * uy
        across = np.abs(-(world[:, 0] - ax) * uy + (world[:, 1] - ay) * ux)
        near |= (along >= -half_width_m) & (along <= usable) & (across <= half_width_m)
        travelled += length
    return world[near]
