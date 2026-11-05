"""Conservative move-to-applied telemetry over the cached RobotLink state."""

import math
import sys
from typing import Any

from coincoin.clock import Clock
from coincoin.journal import Journal
from coincoin.robot.link import RobotLink


def _vectors(state: dict[str, Any] | None) -> tuple[list[float], list[float]] | None:
    move = state.get("move") if state is not None else None
    if not isinstance(move, dict):
        return None
    vectors: list[list[float]] = []
    for key in ("requested", "applied"):
        vector = move.get(key)
        if not (
            isinstance(vector, list)
            and len(vector) == 3
            and all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and abs(value) <= sys.float_info.max
                for value in vector
            )
        ):
            return None
        vectors.append([float(value) for value in vector])
    return vectors[0], vectors[1]


class MoveLatency:
    """Observe at command ticks, without adding tasks or delaying actuation."""

    def __init__(
        self,
        link: RobotLink,
        clock: Clock,
        journal: Journal,
        *,
        move_id: int,
        requested_at: float,
        params: dict[str, float],
        duration: float,
        replacing: bool,
        baseline_after: float,
    ) -> None:
        self.link = link
        self.clock = clock
        self.journal = journal
        self.move_id = move_id
        self.requested_at = requested_at
        self.expected = [params[key] for key in ("vx", "vy", "vyaw")]
        self.sent_at = clock.now()
        self.deadline = self.sent_at + min(1.0, duration)
        self.pending = True
        vectors = _vectors(link.state())
        age = link.state_age()
        if replacing:
            self.finish("motion_replacement")
        elif not any(self.expected):
            self.finish("zero_command")
        elif vectors is None or not math.isfinite(age) or not 0 <= age <= 0.2:
            self.finish("baseline_unavailable")
        elif self.sent_at - age <= baseline_after:
            self.finish("baseline_predates_motion")
        elif any(value != 0 for vector in vectors for value in vector):
            self.finish("already_active")

    def observe(self) -> None:
        if not self.pending:
            return
        if not self.link.connected:
            self.finish("link_down")
            return
        now = self.clock.now()
        vectors = _vectors(self.link.state())
        age = self.link.state_age()
        received_at = now - age
        if (
            vectors is not None
            and math.isfinite(age)
            and 0 <= age <= 0.2
            and self.sent_at < received_at <= self.deadline
        ):
            requested, applied = vectors
            # Reject residual/opposite-axis actuation, not just another requested vector.
            if (
                requested == self.expected
                and any(applied)
                and all(
                    value == 0 or value * expected > 0
                    for value, expected in zip(applied, self.expected, strict=True)
                )
            ):
                self.pending = False
                self.journal.write(
                    "latency",
                    method="move",
                    move_id=self.move_id,
                    applied_status="observed",
                    move_to_first_applied_ms=round((received_at - self.requested_at) * 1000, 3),
                )
                return
        if now >= self.deadline:
            self.finish("observation_timeout")

    def finish(self, reason: str) -> None:
        if self.pending:
            self.pending = False
            self.journal.write(
                "latency",
                method="move",
                move_id=self.move_id,
                applied_status="unavailable",
                reason=reason,
            )
