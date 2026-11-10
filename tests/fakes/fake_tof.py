"""A tiny synthetic flat for localization tests: a map, a ray-cast ToF and a robot that drifts.

The world is a boolean occupancy grid (walls are full height) plus optional low boxes the map
does not hold. Frames are rendered at the robot's true pose; the robot reports contact odometry
that drifts from that truth, like the real duck's.
"""

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from coincoin.clock import Clock
from coincoin.robot.localization import TOF_MAX_M, TofFrame, parse_tof_frame, tof_beams
from coincoin.robot.navigation import NavigationMap
from tests.fakes.fake_robotd import FakeRobotLink

RESOLUTION = 0.05
# The head's ToF in the trunk frame: ahead of the neck, at the duck's eye height.
NECK = (0.03, 0.0, 0.10)
EYE = 0.05
BEAMS = tof_beams()


def flat() -> np.ndarray:
    """An 8 x 5 m flat in two rooms joined by one door, with walls that break the symmetry.

    Origin at the lower-left corner; True is blocked. Left room: a cupboard in its top-left corner
    and a short wall stub. Right room: an L-shaped counter.
    """
    height, width = round(5 / RESOLUTION), round(8 / RESOLUTION)
    grid = np.zeros((height, width), dtype=bool)

    def block(x0: float, y0: float, x1: float, y1: float) -> None:
        r0, r1 = round(y0 / RESOLUTION), round(y1 / RESOLUTION)
        c0, c1 = round(x0 / RESOLUTION), round(x1 / RESOLUTION)
        grid[r0:r1, c0:c1] = True

    block(0, 0, 8, 0.1)
    block(0, 4.9, 8, 5)
    block(0, 0, 0.1, 5)
    block(7.9, 0, 8, 5)
    block(3.95, 0, 4.05, 2.0)
    block(3.95, 2.9, 4.05, 5)
    block(0.1, 3.9, 1.0, 4.9)
    block(2.0, 0.1, 2.1, 1.2)
    block(5.5, 3.5, 7.9, 3.6)
    block(5.5, 2.4, 5.6, 3.6)
    return grid


def twin_flat() -> np.ndarray:
    """An 8 x 5 m flat in two identical rooms, so one place cannot be told from the other.

    `flat()` is repeated rotated 180 degrees about the flat's centre, which is the one symmetry a
    pose can express: a mirrored room would need a reflection, not a rotation, and the search
    would never propose it. The two look-alikes are (1.5, 2.5) and (6.5, 2.45).
    """
    grid = flat()
    return grid | np.rot90(grid, 2)


def nav_map(
    grid: np.ndarray, places: dict[str, tuple[float, float]] | None = None
) -> NavigationMap:
    """The navigation map of a grid: clearance in centimetres to the nearest blocked cell."""
    rows, cols = np.nonzero(grid)
    blocked = np.column_stack((rows, cols)).astype(float)
    height, width = grid.shape
    yy, xx = np.mgrid[0:height, 0:width]
    cells = np.column_stack((yy.ravel(), xx.ravel())).astype(float)
    best = np.full(len(cells), np.inf)
    for start in range(0, len(blocked), 512):
        chunk = blocked[start : start + 512]
        d = np.hypot(cells[:, None, 0] - chunk[None, :, 0], cells[:, None, 1] - chunk[None, :, 1])
        best = np.minimum(best, d.min(axis=1))
    clearance = np.where(best == 0, 0, np.clip(np.rint((best - 0.5) * RESOLUTION * 100), 0, 255))
    return NavigationMap(
        clearance.astype(np.uint8).tobytes(),
        width,
        height,
        (0.0, 0.0),
        RESOLUTION,
        0.12,
        places or {"salon": (1.5, 1.5), "cuisine": (6.5, 1.5), "coin": (6.8, 4.3)},
    )


@dataclass
class Box:
    """Something on the floor the map does not hold: a chair, a bag, a closed door."""

    x0: float
    y0: float
    x1: float
    y1: float
    height: float = 0.8


@dataclass
class World:
    grid: np.ndarray
    boxes: list[Box] = field(default_factory=list)

    def blocked(self, x: np.ndarray, y: np.ndarray, z: np.ndarray) -> np.ndarray:
        col = np.floor(x / RESOLUTION).astype(int)
        row = np.floor(y / RESOLUTION).astype(int)
        outside = (row < 0) | (row >= self.grid.shape[0]) | (col < 0) | (col >= self.grid.shape[1])
        hit = outside.copy()
        inside = ~outside
        hit[inside] = self.grid[row[inside], col[inside]]
        for box in self.boxes:
            hit |= (x >= box.x0) & (x <= box.x1) & (y >= box.y0) & (y <= box.y1) & (z <= box.height)
        return hit | (z <= 0)

    def render(
        self, x: float, y: float, yaw: float, head_yaw: float, z: float, t_ns: int, seq: int
    ) -> TofFrame:
        """Ray-cast the 64 zones from the head's sensor at a true pose."""
        sensor_yaw = yaw + head_yaw
        sx = x + math.cos(yaw) * NECK[0] + math.cos(sensor_yaw) * EYE
        sy = y + math.sin(yaw) * NECK[0] + math.sin(sensor_yaw) * EYE
        sz = z + NECK[2]
        c, s = math.cos(sensor_yaw), math.sin(sensor_yaw)
        dx = c * BEAMS[:, 0] - s * BEAMS[:, 1]
        dy = s * BEAMS[:, 0] + c * BEAMS[:, 1]
        dz = BEAMS[:, 2]
        steps = np.arange(0.01, TOF_MAX_M + 0.2, 0.01)
        px = sx + dx[:, None] * steps[None, :]
        py = sy + dy[:, None] * steps[None, :]
        pz = sz + dz[:, None] * steps[None, :]
        hit = self.blocked(px.ravel(), py.ravel(), pz.ravel()).reshape(px.shape)
        first = np.where(hit.any(axis=1), hit.argmax(axis=1), -1)
        distance = np.where(first >= 0, steps[first], 0.0)
        status = np.where((first >= 0) & (distance <= TOF_MAX_M), 5, 255)
        distance = np.where(status == 5, distance, 0.0)
        return parse_tof_frame(
            {
                "rows": 8,
                "cols": 8,
                "distance_mm": np.rint(distance * 1000).astype(int).tolist(),
                "status": status.tolist(),
                "t_ns": t_ns,
                "seq": seq,
            }
        )


class TofRobotLink(FakeRobotLink):
    """FakeRobotLink with a true pose, drifting odometry, an IMU and a ToF frame in its state.

    `position`/`yaw` (what FakeRobotLink integrates) are the truth. Reported odometry integrates
    the same commands with a scale and heading error, starting at the origin like robotd's.
    """

    def __init__(
        self,
        clock: Clock,
        world: World,
        *,
        odom_scale: float = 1.0,
        odom_yaw_scale: float = 1.0,
    ) -> None:
        super().__init__(clock)
        self.world = world
        self.odom_scale = odom_scale
        self.odom_yaw_scale = odom_yaw_scale
        self.odom = [0.0, 0.0, 0.0]
        self.policies["simulated"] = True

    def place_at(self, x: float, y: float, yaw: float) -> None:
        self.position[:2] = [x, y]
        self.yaw = yaw

    def _integrate(self) -> None:
        elapsed = self.clock.now() - self._last_tick
        heading = self.odom[2]
        distance = self.twist[0] * elapsed * self.odom_scale
        self.odom[0] += distance * math.cos(heading)
        self.odom[1] += distance * math.sin(heading)
        self.odom[2] += self.twist[2] * elapsed * self.odom_yaw_scale
        super()._integrate()

    def head_yaw(self) -> float:
        return self.head[2]

    def state(self) -> dict[str, Any] | None:
        state = super().state()
        if state is None:
            return None
        head = self.head_yaw()
        state["t_ns"] = round(self.clock.now() * 1e9)
        state["odom"] = {
            "position": [self.odom[0], self.odom[1], self.position[2]],
            "yaw": math.atan2(math.sin(self.odom[2]), math.cos(self.odom[2])),
        }
        state["imu"] = {"quat": [math.cos(self.odom[2] / 2), 0.0, 0.0, math.sin(self.odom[2] / 2)]}
        state["frames"] = {
            "tof": {
                "pos": [NECK[0] + EYE * math.cos(head), EYE * math.sin(head), NECK[2]],
                "quat": [math.cos(head / 2), 0.0, 0.0, math.sin(head / 2)],
            }
        }
        return state

    def frame_now(self, seq: int) -> TofFrame:
        self._integrate()
        return self.world.render(
            self.position[0],
            self.position[1],
            self.yaw,
            self.head_yaw(),
            self.position[2],
            round(self.clock.now() * 1e9),
            seq,
        )


class ClockedTof:
    """A DepthSource that renders a frame every `period` of the test clock."""

    def __init__(
        self,
        link: TofRobotLink,
        clock: Clock,
        period: float = 1 / 15,
        before: Callable[[], None] | None = None,
    ) -> None:
        self.link = link
        self.clock = clock
        self.period = period
        self.before = before
        self.closed = False

    async def frames(self) -> AsyncIterator[TofFrame]:
        seq = 0
        while not self.closed:
            await self.clock.sleep(self.period)
            if self.before is not None:
                self.before()
            seq += 1
            yield self.link.frame_now(seq)

    async def close(self) -> None:
        self.closed = True


async def feed(localizer: Any, link: TofRobotLink, depth: ClockedTof) -> None:
    """Drive a localizer from the fake clock (its own poller sleeps in real time)."""
    async for frame in depth.frames():
        localizer.observe_state(link.state())
        localizer.observe_frame(frame)
        await asyncio.sleep(0)
