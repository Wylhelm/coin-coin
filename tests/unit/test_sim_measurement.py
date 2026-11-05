"""SIMWALK's shared measurement protocol, without a simulator or network."""

import asyncio
from copy import deepcopy
from typing import Any

import pytest

from coincoin.clock import SystemClock
from coincoin.robot.governor import ToolResult
from tests.fakes.fake_robotd import FakeRobotLink
from tests.sim import test_sim_motion as s1
from tests.sim import test_sim_webrtc as s9


def snapshot(x: float, *, applied: float = 0.0) -> dict[str, Any]:
    return {
        "odom": {"position": [x, 0.0, 0.12], "yaw": 0.0},
        "move": {"requested": [0.0, 0.0, 0.0], "applied": [applied, 0.0, 0.0]},
    }


async def test_settling_requires_distinct_fresh_stopped_stable_updates() -> None:
    first = snapshot(0.0)
    observations = [
        (first, 0.0),
        (first, 0.0),  # A repeated snapshot must not count as another update.
        (snapshot(0.001), 0.0),
        (snapshot(0.002), 0.3),  # Staleness resets accumulated stability.
        (snapshot(0.003), 0.0),
        (snapshot(0.004, applied=0.1), 0.0),  # Deceleration is not settled.
        (snapshot(0.005), 0.0),
        (snapshot(0.020), 0.0),  # More than 5 mm resets stable_updates.
        (snapshot(0.024), 0.0),
        (snapshot(0.028), 0.0),
    ]

    class Link(FakeRobotLink):
        index = 0

        def state(self) -> dict[str, Any]:
            return observations[self.index][0]

        def state_age(self) -> float:
            return observations[self.index][1]

    link = Link()

    class Clock(SystemClock):
        async def sleep(self, seconds: float) -> None:
            assert seconds == 0.02
            link.index = min(link.index + 1, len(observations) - 1)
            await asyncio.sleep(0)

    result = await s1._wait_for_settled_odom(link, Clock())
    assert result is observations[-1][0]
    assert link.index == len(observations) - 1
    assert s9._wait_for_settled_odom is s1._wait_for_settled_odom


async def test_settling_timeout_does_not_accept_a_cached_snapshot() -> None:
    state = snapshot(0.0)

    class CachedLink(FakeRobotLink):
        def state(self) -> dict[str, Any]:
            return state

        def state_age(self) -> float:
            return 0.0

    with pytest.raises(TimeoutError):
        await s1._wait_for_settled_odom(CachedLink(), SystemClock())


@pytest.mark.parametrize(
    ("module", "test_name"),
    [
        (s1, "test_move_advances_more_than_fifteen_cm"),
        (s9, "test_webrtc_move_advances_more_than_fifteen_cm"),
    ],
)
async def test_both_motion_tests_measure_settled_states_without_changing_command(
    monkeypatch: pytest.MonkeyPatch, module: Any, test_name: str
) -> None:
    link = FakeRobotLink()
    reads = []
    states = [snapshot(0.0), snapshot(0.16)]

    async def settled(actual_link, actual_clock):
        assert actual_link is link and actual_clock is clock
        reads.append(len(reads))
        return deepcopy(states[len(reads) - 1])

    class Governor:
        async def move(self, *args):
            assert reads == [0]
            assert args == (0.2, 0, 0, 2)
            return ToolResult(True)

        async def wait_motion(self):
            return ToolResult(True)

    class Clock(SystemClock):
        async def sleep(self, seconds: float) -> None:
            raise AssertionError("The fixed measurement delay must not be used")

    clock = Clock()
    monkeypatch.setattr(module, "_wait_for_settled_odom", settled)
    await getattr(module, test_name)((Governor(), link, clock))
    assert reads == [0, 1]
