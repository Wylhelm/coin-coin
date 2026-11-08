"""Named-place navigation: map loading, path planning, steering and the pose it steers by.

The map is a clearance grid in the apartment's frame, built from a scanned scene and the Roborock
map. The pose comes from a pose source (see `coincoin.robot.localization`): by default the
`estimated` source, Monte-Carlo localization from contact odometry and the ToF depth frames, which
is all a real duck has. The `oracle` source reads the MuJoCo body server's true pose and exists for
evaluation; steering by it is a simulator shortcut. The map is not a safety map: obstacles it does
not hold are only seen by the ToF while the duck walks.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import heapq
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from coincoin.robot.link import RobotRefused
from coincoin.robot.tcp_link import tcp_address

if TYPE_CHECKING:
    from coincoin.robot.localization import EstimatedPose, PoseEstimate, PoseSource

Point = tuple[float, float]
# Paths keep this much extra room beyond the robot radius where the floor allows it.
COMFORT_MARGIN_M = 0.15
SNAP_RADIUS_M = 0.6
# Obstacle points the ToF reported and navigation keeps blocking, newest last.
OBSTACLE_MEMORY = 400


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    yaw: float


def yaw_from_quat(w: float, x: float, y: float, z: float) -> float:
    return math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))


class NavigationMap:
    def __init__(
        self,
        clearance_cm: bytes,
        width: int,
        height: int,
        origin: Point,
        resolution: float,
        robot_radius: float,
        places: dict[str, Point],
    ) -> None:
        if len(clearance_cm) != width * height or width <= 0 or height <= 0:
            raise ValueError("navigation grid size does not match its dimensions")
        if resolution <= 0 or robot_radius <= 0:
            raise ValueError("navigation resolution and robot radius must be positive")
        self.clearance = clearance_cm
        self.width = width
        self.height = height
        self.origin = origin
        self.resolution = resolution
        self.robot_radius = robot_radius
        self.places = places
        self._needed_cm = math.ceil(robot_radius * 100)

    @classmethod
    def load(cls, path: Path) -> NavigationMap:
        data = json.loads(path.read_text(encoding="utf-8"))
        places = {
            str(name): (float(place["x"]), float(place["y"]))
            for name, place in data["places"].items()
        }
        if not places:
            raise ValueError(f"no places in {path}")
        return cls(
            base64.b64decode(data["clearance_cm_uint8_base64"]),
            int(data["width"]),
            int(data["height"]),
            (float(data["origin_xy_m"][0]), float(data["origin_xy_m"][1])),
            float(data["resolution_m"]),
            float(data["robot_radius_m"]),
            places,
        )

    def with_obstacles(self, points: Sequence[Point]) -> NavigationMap:
        """A copy with extra point obstacles (seen by the ToF, not in the map) blocked."""
        if not points:
            return self
        clearance = bytearray(self.clearance)
        reach = math.ceil((self.robot_radius + COMFORT_MARGIN_M + 0.1) / self.resolution)
        for cell in {self.cell(point) for point in points}:
            for row in range(max(0, cell[0] - reach), min(self.height, cell[0] + reach + 1)):
                for col in range(max(0, cell[1] - reach), min(self.width, cell[1] + reach + 1)):
                    distance = math.hypot(row - cell[0], col - cell[1]) * self.resolution
                    centimetres = max(0, round((distance - self.resolution / 2) * 100))
                    index = row * self.width + col
                    if centimetres < clearance[index]:
                        clearance[index] = centimetres
        return NavigationMap(
            bytes(clearance),
            self.width,
            self.height,
            self.origin,
            self.resolution,
            self.robot_radius,
            self.places,
        )

    def cell(self, point: Point) -> tuple[int, int]:
        return (
            round((point[1] - self.origin[1]) / self.resolution),
            round((point[0] - self.origin[0]) / self.resolution),
        )

    def centre(self, cell: tuple[int, int]) -> Point:
        return (
            self.origin[0] + cell[1] * self.resolution,
            self.origin[1] + cell[0] * self.resolution,
        )

    def clearance_at(self, cell: tuple[int, int]) -> int:
        row, col = cell
        if not (0 <= row < self.height and 0 <= col < self.width):
            return 0
        return self.clearance[row * self.width + col]

    def free(self, cell: tuple[int, int]) -> bool:
        return self.clearance_at(cell) >= self._needed_cm

    def clearance_m(self, point: Point) -> float:
        """Room around the duck's footprint at a world point, in metres."""
        return max(0, self.clearance_at(self.cell(point)) - self._needed_cm) / 100

    def _snap(self, point: Point) -> tuple[int, int] | None:
        start = self.cell(point)
        if self.free(start):
            return start
        reach = math.ceil(SNAP_RADIUS_M / self.resolution)
        best: tuple[float, tuple[int, int]] | None = None
        for dr in range(-reach, reach + 1):
            for dc in range(-reach, reach + 1):
                candidate = (start[0] + dr, start[1] + dc)
                distance = math.hypot(dr, dc)
                if (
                    distance <= reach
                    and self.free(candidate)
                    and (best is None or distance < best[0])
                ):
                    best = (distance, candidate)
        return best[1] if best else None

    def _step_cost(self, cell: tuple[int, int]) -> float:
        comfort = (self.robot_radius + COMFORT_MARGIN_M) * 100
        shortfall = max(0.0, comfort - self.clearance_at(cell)) / comfort
        return 1 + 4 * shortfall

    def plan(self, start: Point, goal: Point) -> list[Point] | None:
        """Return waypoints from start to goal, excluding start, or None if unreachable."""
        first, last = self._snap(start), self._snap(goal)
        if first is None or last is None:
            return None
        frontier: list[tuple[float, float, tuple[int, int]]] = [(0.0, 0.0, first)]
        came: dict[tuple[int, int], tuple[int, int] | None] = {first: None}
        cost = {first: 0.0}
        steps = [(dr, dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1) if dr or dc]
        while frontier:
            _, spent, current = heapq.heappop(frontier)
            if current == last:
                break
            if spent > cost[current]:
                continue
            for dr, dc in steps:
                neighbour = (current[0] + dr, current[1] + dc)
                if not self.free(neighbour):
                    continue
                # No corner cutting between two blocked orthogonal neighbours.
                if (
                    dr
                    and dc
                    and not (
                        self.free((current[0] + dr, current[1]))
                        and self.free((current[0], current[1] + dc))
                    )
                ):
                    continue
                total = spent + math.hypot(dr, dc) * self._step_cost(neighbour)
                if total < cost.get(neighbour, math.inf):
                    cost[neighbour] = total
                    came[neighbour] = current
                    estimate = math.hypot(neighbour[0] - last[0], neighbour[1] - last[1])
                    heapq.heappush(frontier, (total + estimate, total, neighbour))
        if last not in came:
            return None
        cells = [last]
        while (previous := came[cells[-1]]) is not None:
            cells.append(previous)
        cells.reverse()
        waypoints = [self.centre(cell) for cell in self._shortcut(cells)[1:]]
        if (
            waypoints
            and self.free(self.cell(goal))
            and self._visible(self.cell(waypoints[-1]), self.cell(goal))
        ):
            waypoints[-1] = goal
        if not waypoints:
            waypoints = [goal if self.free(self.cell(goal)) else self.centre(last)]
        if first != self.cell(start):
            # A route from a snapped start must first lead out of the blocked cell.
            waypoints.insert(0, self.centre(first))
        return waypoints

    def _visible(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        comfort = math.ceil((self.robot_radius + COMFORT_MARGIN_M / 2) * 100)
        needed = min(comfort, self.clearance_at(a), self.clearance_at(b))
        needed = max(needed, self._needed_cm)
        samples = max(1, math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]) * 2))
        previous = a
        for i in range(samples + 1):
            t = i / samples
            cell = (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t))
            if self.clearance_at(cell) < needed:
                return False
            dr, dc = cell[0] - previous[0], cell[1] - previous[1]
            if (
                dr
                and dc
                and (
                    self.clearance_at((previous[0] + dr, previous[1])) < needed
                    or self.clearance_at((previous[0], previous[1] + dc)) < needed
                )
            ):
                return False
            previous = cell
        return True

    def _shortcut(self, cells: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
        kept = [cells[0]]
        index = 0
        while index < len(cells) - 1:
            furthest = index + 1
            for candidate in range(len(cells) - 1, index + 1, -1):
                if self._visible(cells[index], cells[candidate]):
                    furthest = candidate
                    break
            kept.append(cells[furthest])
            index = furthest
        return kept


class SimPose:
    """Read the duck's true pose from the MuJoCo body server over a persistent connection.

    Newer body servers name it for what it is, `oracle.pose`; older ones only carry it in `read`
    (`trunk` plus the IMU quaternion). The first answer decides which one is asked from then on.
    """

    def __init__(self, url: str) -> None:
        self._address = tcp_address(url)
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._oracle_op: bool | None = None

    async def read(self) -> Pose:
        async with self._lock:
            for attempt in range(2):
                try:
                    if self._writer is None:
                        self._reader, self._writer = await asyncio.wait_for(
                            asyncio.open_connection(*self._address), 2
                        )
                    assert self._reader is not None
                    pose = await self._ask(self._reader, self._writer)
                    if not all(map(math.isfinite, (pose.x, pose.y, pose.yaw))):
                        raise ValueError("non-finite pose")
                    return pose
                except (
                    OSError,
                    TimeoutError,
                    ValueError,
                    KeyError,
                    TypeError,
                    IndexError,
                ) as error:
                    await self._drop()
                    if attempt:
                        raise RobotRefused(f"simulated position unavailable: {error}") from None
        raise AssertionError("unreachable")

    async def _ask(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> Pose:
        if self._oracle_op is not False:
            writer.write(b'{"op": "oracle.pose"}\n')
            await writer.drain()
            answer = json.loads(await asyncio.wait_for(reader.readline(), 1))
            if "error" not in answer:
                self._oracle_op = True
                return Pose(float(answer["x"]), float(answer["y"]), float(answer["yaw"]))
            if self._oracle_op:
                raise ValueError(str(answer["error"]))
            self._oracle_op = False
        writer.write(b'{"op": "read"}\n')
        await writer.drain()
        answer = json.loads(await asyncio.wait_for(reader.readline(), 1))
        trunk, quat = answer["trunk"], answer["imu"]["quat"]
        return Pose(float(trunk[0]), float(trunk[1]), yaw_from_quat(*quat))

    async def _drop(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()


class Steering:
    """Turn a route and the true pose into velocity commands, on foot or on skates.

    Walking stops almost at once, so it pivots toward each waypoint and then walks with
    proportional steering. Skates glide: the skating policy cannot spin in place but turns in a
    slow arc, small forward commands alone do nothing, and a reverse command brakes. The skating
    branch therefore follows the current path segment, controls measured speed (push, coast or
    brake), turns in slow arcs and only reports arrival once the duck has stopped near the goal.
    """

    WAYPOINT_M = 0.3
    ROLLER_WAYPOINT_M = 0.15
    ROLLER_LOOKAHEAD_M = 0.45
    ESCAPE_WAYPOINT_M = 0.03
    ARRIVAL_M = 0.2
    ROLLER_ARRIVAL_M = 0.3
    TURN_RAD = 0.6
    ALIGNED_RAD = 0.2
    STEER_GAIN = 1.5
    WALK_MIN_VX = 0.15
    ROLLER_CRUISE = 0.26
    ROLLER_CORNER = 0.08
    ROLLER_STOPPED = 0.06
    ROLLER_ARC_VX = 0.15
    ROLLER_ARC_MAX = 0.15

    def __init__(
        self,
        route: Sequence[Point],
        limits: tuple[float, float, float],
        mode: str | None,
        room: Callable[[Point], float] | None = None,
    ) -> None:
        self.route = list(route)
        self.limits = limits
        self.roller = mode == "roller"
        self.room = room
        self.index = 0
        self.turning = False
        self._start: Point | None = None
        self._escaping = False
        self._last: tuple[Pose, float] | None = None
        self._velocity = (0.0, 0.0)

    def remaining(self, pose: Pose) -> float:
        points = [(pose.x, pose.y), *self.route[self.index :]]
        return sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))

    def _observe(self, pose: Pose, now: float) -> None:
        if self._last is not None:
            previous, at = self._last
            dt = now - at
            if dt > 1e-3:
                vx = (pose.x - previous.x) / dt
                vy = (pose.y - previous.y) / dt
                self._velocity = (
                    0.5 * self._velocity[0] + 0.5 * vx,
                    0.5 * self._velocity[1] + 0.5 * vy,
                )
        self._last = (pose, now)

    def _roller_target(self, here: Point) -> Point:
        assert self._start is not None
        start = self._start if self.index == 0 else self.route[self.index - 1]
        end = self.route[self.index]
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length < 1e-6:
            return end
        along = ((here[0] - start[0]) * dx + (here[1] - start[1]) * dy) / length
        along = min(length, max(0.0, along) + self.ROLLER_LOOKAHEAD_M)
        return start[0] + dx * along / length, start[1] + dy * along / length

    def update(self, pose: Pose, now: float) -> dict[str, float] | None:
        """Return the next command, or None once arrived."""
        self._observe(pose, now)
        max_vx, _, max_vyaw = self.limits
        speed = math.hypot(*self._velocity)
        forward = self._velocity[0] * math.cos(pose.yaw) + self._velocity[1] * math.sin(pose.yaw)
        here = (pose.x, pose.y)
        if self._start is None:
            self._start = here
            self._escaping = self.room is not None and self.room(here) <= 0.0
        last = len(self.route) - 1
        waypoint_radius = self.ROLLER_WAYPOINT_M if self.roller else self.WAYPOINT_M
        while self.index < last and math.dist(here, self.route[self.index]) < waypoint_radius:
            if self._escaping and math.dist(here, self.route[self.index]) > self.ESCAPE_WAYPOINT_M:
                break
            self._escaping = False
            self.index += 1
        target = self.route[self.index]
        distance = math.dist(here, target)
        final = self.index == last
        brake = {"vx": -math.copysign(max_vx, forward), "vy": 0.0, "vyaw": 0.0}
        if final and distance <= (self.ROLLER_ARRIVAL_M if self.roller else self.ARRIVAL_M):
            if not self.roller or speed < self.ROLLER_STOPPED:
                return None
            return brake if abs(forward) >= self.ROLLER_STOPPED else _idle()
        aim = self._roller_target(here) if self.roller else target
        heading = math.atan2(aim[1] - pose.y, aim[0] - pose.x)
        error = _wrap(heading - pose.yaw)
        self.turning = abs(error) > (self.ALIGNED_RAD if self.turning else self.TURN_RAD)
        if self.turning:
            vyaw = math.copysign(max_vyaw, error)
            if not self.roller:
                return {"vx": 0.0, "vy": 0.0, "vyaw": vyaw}
            # The skating policy was never trained to spin in place: it turns while gliding
            # slowly, so it arcs with a gentle push and brakes whenever it gathers speed.
            if abs(forward) >= self.ROLLER_ARC_MAX:
                return brake
            return {"vx": self.ROLLER_ARC_VX, "vy": 0.0, "vyaw": vyaw}
        vyaw = max(-max_vyaw, min(max_vyaw, self.STEER_GAIN * error))
        if not self.roller:
            vx = max(self.WALK_MIN_VX, min(max_vx, distance))
            return {"vx": min(vx, max_vx), "vy": 0.0, "vyaw": vyaw}
        desired = self.ROLLER_CRUISE
        if final:
            desired = min(desired, 0.6 * distance)
        else:
            following = self.route[self.index + 1]
            corner = abs(
                _wrap(math.atan2(following[1] - target[1], following[0] - target[0]) - heading)
            )
            if corner > self.TURN_RAD:
                desired = min(desired, self.ROLLER_CORNER + 0.3 * distance)
        if self.room is not None:
            # Glide slowly where walls are close: braking takes a few tens of centimetres.
            desired = min(desired, self.ROLLER_CORNER + 0.6 * self.room(here))
        if forward < desired - 0.05:
            vx = max_vx
        elif forward > desired + 0.05:
            vx = -max_vx
        else:
            vx = 0.0
        return {"vx": vx, "vy": 0.0, "vyaw": vyaw}


def _wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def _idle() -> dict[str, float]:
    return {"vx": 0.0, "vy": 0.0, "vyaw": 0.0}


class Navigator:
    """The map, the pose source navigation steers by, and what the ToF has added to the map.

    `source` is the controller input: an `estimated` source by default, or `oracle` (the simulator's
    true pose) when asked for. `oracle`, when given separately, is only ever read to score the
    estimate. `localizer` is the MCL instance behind an estimated source (or running alongside the
    oracle), which also carries the latest depth scans for obstacle checks and head sweeps.
    """

    def __init__(
        self,
        nav_map: NavigationMap,
        source: PoseSource | SimPose,
        *,
        oracle: SimPose | None = None,
        localizer: EstimatedPose | None = None,
    ) -> None:
        if isinstance(source, SimPose):
            from coincoin.robot.localization import OraclePose

            source = OraclePose(source)
        self.map = nav_map
        self.source = source
        self.oracle = oracle
        self.localizer = localizer
        self.obstacles: list[Point] = []

    @property
    def places(self) -> dict[str, Point]:
        return self.map.places

    @property
    def honest(self) -> bool:
        """Whether the controller steers by what a real duck could know."""
        return self.source.name != "oracle"

    async def estimate(self) -> PoseEstimate:
        return await self.source.estimate()

    async def pose(self) -> Pose:
        return (await self.estimate()).pose

    async def truth(self) -> Pose | None:
        """The simulator's true pose, for scoring only; None when there is no oracle."""
        if self.oracle is None:
            return None
        try:
            return await self.oracle.read()
        except RobotRefused:
            return None

    def mark_obstacles(self, points: Sequence[Point]) -> None:
        """Remember what the ToF found in the way, for planning and for the localizer's map."""
        self.set_obstacles([*self.obstacles, *points])

    def clear_obstacles(self) -> None:
        """Forget them: a chair moves and a door opens, so each go_to looks afresh."""
        if self.obstacles:
            self.set_obstacles([])

    def set_obstacles(self, points: Sequence[Point]) -> None:
        """Replace what the ToF has added to the map (to undo marks that proved doubtful)."""
        self.obstacles = list(points)[-OBSTACLE_MEMORY:]
        if self.localizer is not None:
            self.localizer.use_map(
                self.map.with_obstacles(self.obstacles) if self.obstacles else self.map
            )

    def plan(self, start: Point, goal: Point) -> list[Point] | None:
        return self.map.with_obstacles(self.obstacles).plan(start, goal)

    def room(self, point: Point) -> float:
        return self.map.clearance_m(point)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.source.close()
        if self.localizer is not None and self.localizer is not self.source:
            with contextlib.suppress(Exception):
                await self.localizer.close()
        if self.oracle is not None:
            with contextlib.suppress(Exception):
                await self.oracle.close()
