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
from collections import deque
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

    def segment_clear(self, start: Point, end: Point) -> bool:
        """Whether the footprint can follow this segment without cutting a corner."""
        return self._visible(self.cell(start), self.cell(end))

    def segment_traversable(self, start: Point, end: Point) -> bool:
        """Check a live route with a little less margin than the planner's shortcut.

        The bar cannot be raised above the room the duck already has where it stands:
        an estimate that drifts a few centimetres into the inflation band is ordinary,
        and a check the duck's own cell already fails would condemn every route it
        could plan, leaving it to replan forever from a spot it is not allowed to leave.
        """
        a, b = self.cell(start), self.cell(end)
        margin = math.ceil((self.robot_radius + COMFORT_MARGIN_M / 3) * 100)
        needed = max(
            min(margin, self.clearance_at(a), self.clearance_at(b)),
            min(self._needed_cm, self.clearance_at(a)),
        )
        return self._visible(a, b, minimum_cm=needed)

    def segment_unobstructed(self, start: Point, end: Point) -> bool:
        """Whether a pose correction crosses an occupied cell, ignoring footprint clearance."""
        return self._visible(self.cell(start), self.cell(end), minimum_cm=1)

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

    def _visible(
        self, a: tuple[int, int], b: tuple[int, int], *, minimum_cm: int | None = None
    ) -> bool:
        if minimum_cm is None:
            comfort = math.ceil((self.robot_radius + COMFORT_MARGIN_M / 2) * 100)
            needed = min(comfort, self.clearance_at(a), self.clearance_at(b))
            needed = max(needed, self._needed_cm)
        else:
            needed = minimum_cm
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
    """Turn a route and a pose into velocity commands, on foot or on skates.

    Walking stops almost at once, so it pivots toward each waypoint and then walks with
    proportional steering. Skates glide: the skating policy cannot spin in place but turns in a
    slow arc, small forward commands alone do nothing, and a reverse command brakes. The skating
    branch therefore follows the current path segment, controls measured speed (push, coast or
    brake), turns in slow arcs and only reports arrival once the duck has stopped near the goal.
    """

    WAYPOINT_M = 0.3
    ROLLER_WAYPOINT_M = 0.15
    ROLLER_LOOKAHEAD_M = 0.6
    ESCAPE_WAYPOINT_M = 0.03
    # Leave room for localization error and the duck settling after the stop command.
    ARRIVAL_M = 0.1
    ROLLER_ARRIVAL_M = 0.3
    TURN_RAD = 0.6
    ALIGNED_RAD = 0.2
    STEER_GAIN = 1.5
    WALK_MIN_VX = 0.15
    WALK_PIVOT_STALL_S = 2.0
    WALK_PIVOT_PROGRESS_RAD = 0.1
    WALK_PIVOT_CLEARANCE_M = 0.1
    # The roller command is a throttle, not a speed. Measured in the simulator: a push below
    # 0.22 leaves the skates slipping in place and moves nothing at all, 0.25 glides at 0.38 m/s,
    # 0.30 at 0.51 m/s, and everything above saturates near 0.56 m/s. The speeds the skates can
    # hold are therefore not a continuum but a cliff and a plateau, which is why this branch does
    # not regulate speed by feedback: asking for 0.26 m/s got either a stopped duck or a 0.5 m/s
    # one, and the band around it flipped the command at 7 Hz. There is one gear worth using, the
    # 0.38 m/s the lowest usable push delivers: above it the ToF glide match degrades, and the
    # duck starts to tip -- measured 0.44 m/s down the salon, it fell over.
    ROLLER_CRUISE = 0.38
    ROLLER_PUSH = 0.25
    ROLLER_BRAKE = 0.45
    ROLLER_STOPPED = 0.06
    # The wheels only push or brake, and the body answers a few hundred milliseconds later.
    # Re-deciding the mode on every frame rectifies that lag into an acceleration nobody
    # asked for: measured in the corridor between the salon and the bedroom, a duck told to
    # hold 0.1 m/s ran at 0.3 m/s while its command flipped sign at 7 Hz. Hold each mode for
    # a dwell and require the speed to leave a band before switching. The band is narrower
    # than the step between the speeds the skates can hold, so a push that lands on the fast
    # plateau is braked rather than ridden; the dwell is what keeps that from becoming a
    # limit cycle.
    #
    # The dwell is also the coast between two pushes, and it has to stay short. Standing at
    # the start of the salon leg the duck pushed in 0.3 s bursts every 0.65 s and covered
    # 0.11 m in 40 s: a burst is too short to break the static friction the skates sit in,
    # so the glide never started and the leg was abandoned as stalled. Measured on free
    # floor, a single push reaches 0.33 m/s in 2 s and 0.44 m/s in 3 s, so the push has to be
    # seconds long to be worth anything. Holding it is what tips the duck over -- 7 s of
    # unbroken push reared it to 86 degrees -- hence the short coast that lets the pitch
    # oscillation die between two pushes rather than accumulate.
    ROLLER_DWELL_S = 0.4
    ROLLER_SPEED_BAND_MPS = 0.08
    # The heading loop has the same trouble, and worse consequences. Walking damps a full-gain
    # proportional yaw against contact friction; a glide keeps whatever speed it is given, so a
    # yaw command that saturates back and forth makes the skating policy push through every arc
    # and the body gathers speed nobody asked for (measured: 0.4 m/s while 0.15 was commanded,
    # weaving +/-0.3 m down a straight corridor). A gentler gain and a dead band let the duck
    # glide straight, which is also what keeps the commanded-speed odometry honest.
    #
    # The skating policy does not take a yaw rate: `twist[2]` is a heading error in radians and
    # the policy answers it with a body yaw rate several times larger (measured 1.5-1.9 rad/s
    # for a 0.3 rad error). Feeding it a rate made every correction that many times too strong:
    # the duck spun in place while gliding at 0.4 m/s, its estimate fell behind the spin, and
    # the speed loop kept pushing on the stale reading until it fell over. So the loop below
    # works in rad/s and divides by the measured response before it leaves.
    ROLLER_YAW_RESPONSE = 5.5
    ROLLER_YAW_RATE = 0.5
    ROLLER_YAW_GAIN = 1.2
    ROLLER_YAW_BAND_RAD = 0.05
    # Pivoting on the spot is a third regime, and its break-away is not the same both ways
    # round. Measured with the wheels coasting: a 0.30 heading-error command turns the body at
    # 0.78 rad/s counter-clockwise and not at all clockwise -- it kicks through about 0.3 rad
    # and then stalls, which is what froze the route out of the salon at (0.63, 1.33) with the
    # command still asking. Clockwise the same 0.30 moves nothing, 0.50 turns at 0.72 rad/s and
    # 0.80 spins at 4.2 rad/s. There is no range to be proportional in, so the pivot is bang-bang
    # on one magnitude, and that magnitude starts where the skates usually break away and climbs
    # while the body does not answer. Climbing only on a stall is what keeps the fast side from
    # ever being asked for a command it would answer at three radians per second.
    ROLLER_PIVOT = 0.3
    ROLLER_PIVOT_MAX = 0.5
    ROLLER_PIVOT_STEP = 0.1
    ROLLER_PIVOT_STALL_S = 0.4
    ROLLER_PIVOT_PROGRESS_RAD = 0.05
    # Whether the glide is over is read off the estimated speed, and while the body pivots that
    # estimate is noisy: measured down the salon, a duck sitting still and turning reported
    # 0.06 to 0.17 m/s from frame to frame. Judging the pivot against the 0.06 m/s that counts
    # as stopped on a straight line put the brake back on every third frame -- 381 frames of
    # the 0.30 pivot in one leg, each one cutting the turn it had just started, and the leg
    # was abandoned as stuck after 87 s. So the pivot starts below a threshold above that noise
    # and, once started, is only interrupted by a speed that is a real glide.
    ROLLER_PIVOT_STOPPED = 0.2
    ROLLER_PIVOT_GLIDE = 0.3
    # A reverse push decelerates the glide slowly, about 0.12 m/s² measured in the simulator,
    # and a push swings the body enough that the glide match rejects those frames: brake by the
    # predicted stopping distance, and push in measured bursts. From cruise that is 0.67 m, so
    # the approach has to start most of a metre out rather than at the goal.
    ROLLER_BRAKE_DECEL = 0.12
    ROLLER_BRAKE_MARGIN_M = 0.05
    # A reverse push is not a brake that ends at rest. Measured in the simulator, the skates
    # keep rolling backwards once the command is released, and a long reverse push leaves the
    # body reared up: 26 degrees of pitch on a full 0.45 for a second, 29 when it was held right
    # through the stop. At zero throttle nothing damps that pitch any more -- the duck stood at
    # 1.6 degrees for three seconds from a clean standstill, but released at 10 degrees it kept
    # rearing and tipped over backwards inside a second and a half, 79 to 87 degrees, on free
    # floor, with no contact and at two different places in the apartment. Pulsing the same
    # 0.45 in 0.25 s bursts with a 0.20 s coast between them never let the body bank past 8.4
    # degrees, and the duck came to a true standstill and stayed there, roll -1.0 degree.
    ROLLER_BRAKE_PULSE_S = 0.25
    ROLLER_BRAKE_REST_S = 0.2
    # Long enough to break away and reach the plateau, short enough that the pitch the push
    # builds up has not started to run away: 3 s of unbroken push measured 9.8 to 11.6
    # degrees of pitch and stayed upright, 5 s reached 26.8 and rolled 15.7 degrees.
    #
    # Only a push that has to break the skates loose needs all of it. Pushing for two seconds
    # from an already rolling 0.30 m/s overshoots the band to 0.5 m/s, and there the depth
    # odometry gives up: the frame-to-frame match needs a stable viewpoint, and past about
    # 0.45 m/s the fit fell to 0.4-0.7 and was rejected. The controller reads its speed off
    # the estimated pose, so a lagging estimate read "slow" and pushed again -- in the f21
    # bedroom leg the true glide sat at 0.5 m/s while the estimate crawled at 0.11 and the
    # error grew 0.2 -> 1.8 m in six seconds. A rolling push is therefore short enough that
    # the glide answers before the speed leaves the band.
    ROLLER_BLIND_S = 2.0
    ROLLER_BLIND_ROLLING_S = 0.5
    # Capping the push was not enough: the controller judged the next push on a speed read
    # off the estimated pose, and that estimate lags the glide by up to 0.36 m, so it read
    # "slow" while the duck was already fast and pushed again. On the seed 0 bedroom leg the
    # true glide climbed 0.34 -> 0.46 m/s while `forward` never left the band, the frame match
    # fell to 0.32-0.62 and the error ran 0.67 -> 1.40 m until a wall stopped it; seed 2 lost
    # the way home the same way, 2.9 m off with 31 contacts. A mandatory coast after every
    # push bounds the speed without needing a trustworthy measurement: one 2 s push reaches
    # 0.33 m/s, two seconds of coast loses 0.12 at the 0.06 m/s^2 measured on the apartment,
    # so the glide stays under the 0.45 m/s where the depth match gives up and averages about
    # 0.25 m/s. The dwell alone was too short to serve as this brake: it is 0.4 s, and the
    # estimate has not caught up in 0.4 s.
    ROLLER_COAST_S = 2.0
    # The speed is a difference of two poses, and the estimated pose is a particle filter's:
    # frame to frame it wanders by a couple of centimetres, which at 20 Hz reads as
    # +-0.45 m/s. Every consumer of the speed -- the brake distance, the arrival latch, the
    # "is the glide over" test -- believed that noise: down the salon a duck standing still
    # with a zero command was reported at 0.39 m/s, the approach to the goal latched its
    # brake 0.46 m early, and the leg was abandoned a hand's width from where it was going.
    # Measuring over a window instead makes the filter's slow wander cancel against itself
    # while the glide's real motion does not. Walking keeps a short window: there the loop
    # is closed on contact friction and can take the delay.
    ROLLER_SPEED_WINDOW_S = 0.45
    WALK_SPEED_WINDOW_S = 0.2
    # What is left of the filter's wander over a window still has to be gated out, or the
    # arrival test -- which asks for less than 0.06 m/s -- is never satisfied and the duck
    # brakes and creeps alternately at its goal. Measured on the salon pivot, the frame-to-frame
    # speed of a duck that is not translating is 0.06 m/s median but 0.34 at the 90th percentile
    # and 0.55 at worst; over a 0.45 s window that is 0.06 / 0.09 / 0.10, and the 0.10 is the
    # filter drifting while it turns. A real glide covers 0.17 m in the window, so the gate sits
    # an order of magnitude above what a pose that is not moving can fake and well below that.
    ROLLER_STOP_NOISE_M = 0.035

    def __init__(
        self,
        route: Sequence[Point],
        limits: tuple[float, float, float],
        mode: str | None,
        room: Callable[[Point], float] | None = None,
        segment_clear: Callable[[Point, Point], bool] | None = None,
        *,
        route_traversable: Callable[[Point, Point], bool] | None = None,
        recover_stalled_pivot: bool = False,
    ) -> None:
        self.route = list(route)
        self.limits = limits
        self.roller = mode == "roller"
        self.room = room
        self.segment_clear = segment_clear
        self.route_traversable = route_traversable
        self.recover_stalled_pivot = recover_stalled_pivot and not self.roller
        self.index = 0
        self.turning = False
        self._pivot_anchor: tuple[float, float] | None = None
        self._pivot_direction = 0.0
        self._pivot_gain = self.ROLLER_PIVOT
        self._pivot_sign = 0.0
        self._pivoting = False
        self._reverse_pivot = False
        self._start: Point | None = None
        self._escaping = False
        self._track: deque[tuple[float, float, float]] = deque()
        self._velocity = (0.0, 0.0)
        self._arriving = False
        self._brake_at: float | None = None
        self._push_started: float | None = None
        self._push_ended_at: float | None = None
        self._push_rest = True
        self._drive = 0
        self._drive_at: float | None = None
        self._drive_brake_at: float | None = None

    @property
    def reversing_pivot(self) -> bool:
        return self._reverse_pivot

    def remaining(self, pose: Pose) -> float:
        points = [(pose.x, pose.y), *self.route[self.index :]]
        return sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))

    def route_clear(self, pose: Pose) -> bool:
        """Check that the current pose can still reach the next waypoint safely."""
        return (
            self._escaping
            or self.route_traversable is None
            or self.route_traversable((pose.x, pose.y), self.route[self.index])
        )

    def _observe(self, pose: Pose, now: float) -> None:
        window = self.ROLLER_SPEED_WINDOW_S if self.roller else self.WALK_SPEED_WINDOW_S
        track = self._track
        track.append((now, pose.x, pose.y))
        # Keep exactly one sample older than the window as the anchor, so the span always
        # covers the window rather than shrinking to nothing when a tick lands just inside it.
        while len(track) >= 3 and now - track[1][0] >= window:
            track.popleft()
        at, x, y = track[0]
        span = now - at
        if span > 1e-3:
            travelled = math.hypot(pose.x - x, pose.y - y)
            if self.roller and span >= 0.5 * window and travelled < self.ROLLER_STOP_NOISE_M:
                self._velocity = (0.0, 0.0)
            else:
                self._velocity = (
                    max(-1.0, min(1.0, (pose.x - x) / span)),
                    max(-1.0, min(1.0, (pose.y - y) / span)),
                )
        # Motion must not re-arm the push timer. Doing so inverted it: the timer only ran while
        # the duck was stuck and never cut a push that was actually working, so the f17 salon leg
        # pushed without interruption, reached 0.47 m/s -- well over the 0.378 m/s the throttle
        # curve caps at -- and tipped over. The burst has to end on its own clock.

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

    def _overshot(self, here: Point) -> bool:
        """Whether the duck has gone past its final waypoint along the approach axis."""
        assert self._start is not None
        target = self.route[self.index]
        start = self._start if self.index == 0 else self.route[self.index - 1]
        dx, dy = target[0] - start[0], target[1] - start[1]
        length = math.hypot(dx, dy)
        if length < 1e-6:
            return False
        return ((here[0] - target[0]) * dx + (here[1] - target[1]) * dy) / length > 0

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
            if self.segment_clear is not None and not self.segment_clear(
                here, self.route[self.index + 1]
            ):
                break
            self._escaping = False
            self.index += 1
        target = self.route[self.index]
        distance = math.dist(here, target)
        final = self.index == last
        brake = {"vx": -math.copysign(self.ROLLER_BRAKE, forward), "vy": 0.0, "vyaw": 0.0}
        arrival_m = self.ROLLER_ARRIVAL_M if self.roller else self.ARRIVAL_M
        if final and self.roller:
            # Brake by the predicted stopping distance, and once braking begins never
            # re-aim: the glide overshoots the goal while slowing, and turning back
            # toward it at speed tips the duck over. The measured speed is capped near
            # cruise: a glide that froze through a push catches up in one jump, and that
            # spike is fiction the brakes must not answer half a map early.
            stopping = min(speed, self.ROLLER_CRUISE + 0.1) ** 2 / (2.0 * self.ROLLER_BRAKE_DECEL)
            if self._arriving or distance <= max(arrival_m, stopping + self.ROLLER_BRAKE_MARGIN_M):
                if speed < self.ROLLER_STOPPED:
                    goal_error = abs(
                        _wrap(math.atan2(target[1] - pose.y, target[0] - pose.x) - pose.yaw)
                    )
                    if distance <= arrival_m or (
                        goal_error > self.TURN_RAD and self._overshot(here)
                    ):
                        # Arrived — or slid past the goal, where turning back is what
                        # tips the duck over: report and let the caller judge. Braking
                        # short and merely facing away is not that case: the duck still
                        # has ground to cover and must turn and creep in, or a trip would
                        # be abandoned a hand's width from its goal.
                        return None
                    # Braked short on a speed spike: release and creep in again.
                    self._arriving = False
                else:
                    self._arriving = True
                    self._push_started = None
                    return (
                        self._pulsed_brake(brake, now)
                        if abs(forward) >= self.ROLLER_STOPPED
                        else _idle()
                    )
        if final and distance <= arrival_m:
            return None
        aim = self._roller_target(here) if self.roller else target
        heading = math.atan2(aim[1] - pose.y, aim[0] - pose.x)
        error = _wrap(heading - pose.yaw)
        self.turning = abs(error) > (self.ALIGNED_RAD if self.turning else self.TURN_RAD)
        yaw_rate_cap = min(max_vyaw, self.ROLLER_YAW_RATE) if self.roller else max_vyaw
        if self.turning:
            vyaw = math.copysign(max_vyaw, error)
            if not self.roller:
                if self.recover_stalled_pivot:
                    if self._pivot_anchor is None:
                        self._pivot_anchor = (pose.yaw, now)
                        self._pivot_direction = math.copysign(1, error)
                    anchor_yaw, anchor_time = self._pivot_anchor
                    progress = self._pivot_direction * _wrap(pose.yaw - anchor_yaw)
                    if progress >= self.WALK_PIVOT_PROGRESS_RAD:
                        self._pivot_anchor = (pose.yaw, now)
                    elif (
                        not self._reverse_pivot
                        and now - anchor_time >= self.WALK_PIVOT_STALL_S
                        and self.room is not None
                        and self.room(here) >= self.WALK_PIVOT_CLEARANCE_M
                    ):
                        self._reverse_pivot = True
                    if self._reverse_pivot:
                        if self.room is None or self.room(here) < self.WALK_PIVOT_CLEARANCE_M:
                            return _idle()
                        vyaw = -self._pivot_direction * max_vyaw
                return {"vx": 0.0, "vy": 0.0, "vyaw": vyaw}
            # Gliding through a turn sweeps an arc the room does not have: the skates answer a
            # 0.3 rad heading error with 1.7 rad/s, so at cruise the body carves a 0.22 m radius
            # and a 135 degree turn drags it most of a metre sideways into the first wall -- on
            # the way out of the salon it ended up pinned against the wall at (-0.67, 1.52).
            # The governor's `turn` primitive shows the body pivots cleanly when it is not
            # pushing, so brake the glide out and pivot from rest.
            if abs(forward) >= (
                self.ROLLER_PIVOT_GLIDE if self._pivoting else self.ROLLER_PIVOT_STOPPED
            ):
                self._push_started = None
                self._pivot_anchor = None
                self._pivoting = False
                return self._pulsed_brake(brake, now)
            self._pivoting = True
            sign = math.copysign(1.0, error)
            if sign != self._pivot_sign:
                # A fresh pivot, or the error changed sides: start again from the break-away
                # magnitude the skates usually answer rather than from what the last direction
                # needed.
                self._pivot_sign = sign
                self._pivot_gain = self.ROLLER_PIVOT
                self._pivot_anchor = None
            if self._pivot_anchor is None:
                self._pivot_anchor = (pose.yaw, now)
            anchor_yaw, anchor_time = self._pivot_anchor
            progress = sign * _wrap(pose.yaw - anchor_yaw)
            if progress >= self.ROLLER_PIVOT_PROGRESS_RAD:
                # The body answered: give the magnitude back to the break-away value so a pivot
                # that needed a bigger kick does not keep spinning at that rate once it turns.
                self._pivot_gain = max(self.ROLLER_PIVOT, self._pivot_gain - self.ROLLER_PIVOT_STEP)
                self._pivot_anchor = (pose.yaw, now)
            elif now - anchor_time >= self.ROLLER_PIVOT_STALL_S:
                self._pivot_gain = min(
                    self.ROLLER_PIVOT_MAX, self._pivot_gain + self.ROLLER_PIVOT_STEP
                )
                self._pivot_anchor = (pose.yaw, now)
            return {"vx": 0.0, "vy": 0.0, "vyaw": math.copysign(self._pivot_gain, error)}
        else:
            self._pivot_anchor = None
            self._pivot_direction = 0.0
            self._reverse_pivot = False
            self._pivot_gain = self.ROLLER_PIVOT
            self._pivot_sign = 0.0
            self._pivoting = False
            if self.roller:
                self._brake_at = None
                if abs(error) <= self.ROLLER_YAW_BAND_RAD:
                    vyaw = 0.0
                else:
                    vyaw = self._yaw_command(error, yaw_rate_cap)
            else:
                vyaw = max(-max_vyaw, min(max_vyaw, self.STEER_GAIN * error))
        if not self.roller:
            vx = max(self.WALK_MIN_VX, min(max_vx, distance))
            return {"vx": min(vx, max_vx), "vy": 0.0, "vyaw": vyaw}
        # Cruise is a throttle that the wheels actually answer, and the only slow gear the
        # skates have is the brake. Lowering the target instead, as this branch used to where
        # walls were close or a corner was near, asked for 0.14 m/s and got a duck gliding at
        # 0.06: the wheels were below their dead band, so they pushed nothing at all. The route
        # is planned with clearance and the ToF guards the front, so the target stays cruise and
        # a tight corner is taken as an arc, which the yaw command can hold.
        desired = self.ROLLER_CRUISE
        vx = self._drive_command(forward, desired, self.ROLLER_PUSH, self.ROLLER_BRAKE, now)
        return {"vx": vx, "vy": 0.0, "vyaw": vyaw}

    def _pulsed_brake(self, brake: dict[str, float], now: float) -> dict[str, float]:
        """Pulse the reverse push instead of holding it, so the body never rears up.

        Held, the brake leaves the skates with a standing nose-up attitude that nothing damps
        once the command drops to zero; the coast between bursts lets the body fall back flat,
        and the duck reaches a standstill it can pivot from.
        """
        if self._brake_at is None:
            self._brake_at = now
        elapsed = now - self._brake_at
        if elapsed < self.ROLLER_BRAKE_PULSE_S:
            return brake
        if elapsed < self.ROLLER_BRAKE_PULSE_S + self.ROLLER_BRAKE_REST_S:
            return _idle()
        self._brake_at = now
        return brake

    def _yaw_command(self, error: float, rate_cap: float) -> float:
        """Turn the heading error into the heading-error command the skating policy wants.

        The loop is proportional in rad/s -- `ROLLER_YAW_GAIN` of body yaw per rad of error,
        capped at `rate_cap` -- and the result is scaled back into the radians of heading error
        the policy answers at that rate.
        """
        rate = min(rate_cap, self.ROLLER_YAW_GAIN * abs(error))
        return math.copysign(rate / self.ROLLER_YAW_RESPONSE, error)

    def _set_drive(self, mode: int, now: float) -> None:
        if mode != self._drive:
            self._drive = mode
        self._drive_at = now

    def _drive_command(
        self, forward: float, desired: float, push_vx: float, brake_vx: float, now: float
    ) -> float:
        """Push, coast or brake, holding each choice long enough for the glide to answer."""
        blind = self.ROLLER_BLIND_S if self._push_rest else self.ROLLER_BLIND_ROLLING_S
        if self._push_started is not None and now - self._push_started >= blind:
            # The glide match rejects the frames of a push, so the measured speed freezes
            # while the real one climbs. A push has to last long enough to break the skates
            # loose: 0.3 s bursts every 0.65 s moved the duck 0.11 m in 40 s, because static
            # friction swallowed every start and the coast between bursts only lost the
            # little speed that was gained. Two seconds of push reaches 0.33 m/s, three
            # seconds 0.44 m/s, five seconds rears the pitch up to 26.8 degrees and seven
            # seconds rolls the duck over, so the push is capped at two seconds and followed
            # by ROLLER_DWELL_S of coast before the speed is judged again.
            self._set_drive(0, now)
            self._push_started = None
            self._push_ended_at = now
        elif self._drive_at is None or now - self._drive_at >= self.ROLLER_DWELL_S:
            coasting = (
                self._push_ended_at is not None and now - self._push_ended_at < self.ROLLER_COAST_S
            )
            if forward < desired - self.ROLLER_SPEED_BAND_MPS and not coasting:
                self._set_drive(1, now)
            elif forward > desired + self.ROLLER_SPEED_BAND_MPS:
                self._set_drive(-1, now)
            else:
                self._set_drive(0, now)
        if self._drive > 0:
            if self._push_started is None:
                self._push_started = now
                self._push_rest = forward < self.ROLLER_STOPPED
            self._drive_brake_at = None
            return push_vx
        if self._push_started is not None:
            self._push_ended_at = now
        self._push_started = None
        if self._drive < 0:
            # The brake rears the skates up if it is held: the f17 cruise ran the overspeed brake
            # at -0.45 for well under a second and the pitch went 9 -> 15 -> 23 -> 27 degrees
            # before the duck rolled over. Pulse it exactly like the arrival brake, which the
            # bench measures at 8.4 degrees or less and a standstill it keeps.
            if self._drive_brake_at is None:
                self._drive_brake_at = now
            elapsed = now - self._drive_brake_at
            if elapsed >= self.ROLLER_BRAKE_PULSE_S + self.ROLLER_BRAKE_REST_S:
                self._drive_brake_at = now
                elapsed = 0.0
            if elapsed < self.ROLLER_BRAKE_PULSE_S:
                return -math.copysign(brake_vx, forward)
            return 0.0
        self._drive_brake_at = None
        return 0.0


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
        """Remember what the ToF found in the way for planning."""
        self.set_obstacles([*self.obstacles, *points])

    def clear_obstacles(self) -> None:
        """Forget them: a chair moves and a door opens, so each go_to looks afresh."""
        if self.obstacles:
            self.set_obstacles([])

    def set_obstacles(self, points: Sequence[Point]) -> None:
        """Replace what the ToF has added to the map (to undo marks that proved doubtful)."""
        self.obstacles = list(points)[-OBSTACLE_MEMORY:]

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
