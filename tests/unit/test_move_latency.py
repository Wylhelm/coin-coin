import asyncio
import copy
import json
import math
from pathlib import Path
from typing import Any

import pytest

from coincoin.clock import FakeClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.governor import Governor
from tests.fakes.fake_robotd import FakeRobotLink


class CachedRobotLink(FakeRobotLink):
    """Push states explicitly; notifications never manufacture telemetry."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.cached: dict[str, Any] | None = None
        self.received_at = -math.inf
        self.push()

    def push(
        self,
        requested: Any = None,
        applied: Any = None,
        *,
        received_at: float | None = None,
    ) -> None:
        self.cached = {
            "safety": {"fallen": False, "picked_up": False},
            "move": {
                "requested": [0, 0, 0] if requested is None else requested,
                "applied": [0, 0, 0] if applied is None else applied,
            },
        }
        self.received_at = self.clock.now() if received_at is None else received_at

    def state(self) -> dict[str, Any] | None:
        return copy.deepcopy(self.cached)

    def state_age(self) -> float:
        return self.clock.now() - self.received_at


async def pump() -> None:
    for _ in range(12):
        await asyncio.sleep(0)


async def advance(clock: FakeClock, seconds: float) -> None:
    for _ in range(round(seconds / 0.05)):
        clock.advance(0.05)
        await pump()


def latency(journal: Journal) -> list[dict[str, Any]]:
    entries = [
        json.loads(line)
        for path in journal.log_dir.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    return [entry for entry in entries if entry["type"] == "latency"]


def outcomes(journal: Journal) -> list[dict[str, Any]]:
    return [entry for entry in latency(journal) if "applied_status" in entry]


@pytest.fixture
async def rig(tmp_path: Path):
    clock = FakeClock()
    link = CachedRobotLink(clock)
    journal = Journal(tmp_path)
    governor = Governor(link, Settings(), clock, journal)
    await governor.start()
    await pump()
    yield governor, link, clock, journal
    await governor.close()
    await pump()


async def test_delayed_applied_uses_receipt_time_and_includes_preparation(rig):
    governor, link, clock, journal = rig
    link.policies.update(enabled=False, homed=False)
    link.auto_home = False
    pending = asyncio.create_task(governor.move(0.2, 0, 0, 1))
    await pump()
    await advance(clock, 0.1)
    link.policies["homed"] = True
    await advance(clock, 0.1)
    assert (await pending).ok
    sent = next(entry for entry in latency(journal) if "move_to_first_command_ms" in entry)
    assert sent["move_to_first_command_ms"] > 0
    assert not outcomes(journal)
    clock.advance(0.02)
    received_at = clock.now()
    link.push([0.2, 0, 0], [0.03, 0, 0])
    await advance(clock, 0.05)
    observed = outcomes(journal)
    assert len(observed) == 1
    assert observed[0]["applied_status"] == "observed"
    assert observed[0]["move_to_first_applied_ms"] == pytest.approx(received_at * 1000)
    assert observed[0]["move_id"] == sent["move_id"]
    await advance(clock, 0.1)
    assert len(outcomes(journal)) == 1


async def test_received_before_send_or_different_command_cannot_succeed(rig):
    governor, link, clock, journal = rig
    assert (await governor.move(0.2, 0, 0, 1)).ok
    await pump()
    link.push([0.2, 0, 0], [0.1, 0, 0], received_at=0)
    await advance(clock, 0.1)
    assert not outcomes(journal)
    link.push([-0.2, 0, 0], [-0.1, 0, 0])
    await advance(clock, 0.1)
    assert not outcomes(journal)
    link.push([0.2, 0, 0], [0.02, 0, 0])
    await advance(clock, 0.05)
    assert outcomes(journal)[0]["applied_status"] == "observed"


@pytest.mark.parametrize(
    "applied",
    [
        [0, 0, 0],
        [-0.1, 0, 0],
        [0.1, 0.1, 0],
        [math.nan, 0, 0],
        [math.inf, 0, 0],
        [10**400, 0, 0],
        [True, 0, 0],
        [1],
        {},
    ],
)
async def test_zero_residual_or_invalid_applied_never_succeeds(rig, applied):
    governor, link, clock, journal = rig
    await governor.move(0.2, 0, 0, 0.3)
    await pump()
    await advance(clock, 0.05)
    link.push([0.2, 0, 0], applied)
    await advance(clock, 0.35)
    assert not governor.moving
    assert len(outcomes(journal)) == 1
    assert outcomes(journal)[0]["applied_status"] == "unavailable"
    assert "move_to_first_applied_ms" not in outcomes(journal)[0]


async def test_stale_matching_state_is_not_a_measurement(rig):
    governor, link, clock, journal = rig
    await governor.move(0.2, 0, 0, 1)
    await pump()
    clock.advance(0.4)
    link.push([0.2, 0, 0], [0.1, 0, 0], received_at=0.1)
    await pump()
    assert not outcomes(journal)
    await governor.stop()
    assert outcomes(journal)[0]["reason"] == "cancelled"


@pytest.mark.parametrize("baseline", ["active", "requested", "stale", "malformed", "zero"])
async def test_ineligible_baseline_is_explicit(rig, baseline):
    governor, link, clock, journal = rig
    if baseline == "active":
        link.push([0.2, 0, 0], [0.01, 0, 0])
    elif baseline == "requested":
        link.push([0.2, 0, 0], [0, 0, 0])
    elif baseline == "stale":
        clock.advance(0.3)
    elif baseline == "malformed":
        link.cached = {"safety": link.safety}
    await governor.move(0 if baseline == "zero" else 0.2, 0, 0, 1)
    await pump()
    entry = outcomes(journal)[0]
    assert entry["applied_status"] == "unavailable"
    assert (
        entry["reason"]
        == {
            "active": "already_active",
            "requested": "already_active",
            "stale": "baseline_unavailable",
            "malformed": "baseline_unavailable",
            "zero": "zero_command",
        }[baseline]
    )
    link.push([0.2, 0, 0], [0.1, 0, 0])
    await advance(clock, 0.1)
    assert len(outcomes(journal)) == 1


async def test_observation_is_bounded_even_while_motion_continues(rig):
    governor, link, clock, journal = rig
    await governor.move(0.2, 0, 0, 5)
    await pump()
    for _ in range(11):
        link.push()
        await advance(clock, 0.1)
    assert governor.moving
    assert outcomes(journal)[0]["reason"] == "observation_timeout"
    link.push([0.2, 0, 0], [0.1, 0, 0])
    await advance(clock, 0.1)
    assert len(outcomes(journal)) == 1


async def test_state_received_after_deadline_never_succeeds(rig):
    governor, link, clock, journal = rig
    await governor.move(0.2, 0, 0, 5)
    await pump()
    clock.advance(1.01)
    link.push([0.2, 0, 0], [0.1, 0, 0])
    await pump()
    assert outcomes(journal)[0]["reason"] == "observation_timeout"


@pytest.mark.parametrize("ending", ["stop", "replace", "turn", "down", "missing", "close"])
async def test_invalidation_cannot_be_followed_by_late_success(rig, ending):
    governor, link, clock, journal = rig
    await governor.move(0.2, 0, 0, 5)
    await pump()
    motion = governor._motion
    await advance(clock, 0.05)
    if ending == "stop":
        await governor.stop()
    elif ending == "replace":
        await governor.move(-0.2, 0, 0, 5)
    elif ending == "turn":
        assert link.cached is not None
        link.cached["odom"] = {"yaw": 0}
        await governor.turn(90)
    elif ending == "down":
        link.connected = False
    elif ending == "missing":
        link.cached = None
    else:
        await governor.close()
    await advance(clock, 0.1)
    assert motion is not None and motion.done()
    first = [entry for entry in outcomes(journal) if entry["move_id"] == 1]
    assert len(first) == 1 and first[0]["applied_status"] == "unavailable"
    link.push([0.2, 0, 0], [0.1, 0, 0])
    await advance(clock, 0.2)
    assert not any(entry["applied_status"] == "observed" for entry in outcomes(journal))
    await governor.close()
    assert governor._motion is None
    assert governor._monitor is None
    assert governor._mode_task is None
    assert not governor._action_calls
    assert not clock._sleepers


async def test_missing_state_cannot_start_motion(rig):
    governor, link, _, journal = rig
    link.cached = None
    assert not (await governor.move(0.2, 0, 0, 1)).ok
    await pump()
    assert not latency(journal)
    assert not any(record.method == "robot.move" for record in link.records)


async def test_identical_replacement_cannot_claim_delayed_state_from_previous_move(rig):
    governor, link, clock, journal = rig
    await governor.move(0.2, 0, 0, 5)
    await pump()
    await advance(clock, 0.05)
    await governor.move(0.2, 0, 0, 5)
    await pump()
    assert [entry["reason"] for entry in outcomes(journal)] == [
        "cancelled",
        "motion_replacement",
    ]
    link.push([0.2, 0, 0], [0.1, 0, 0])
    await advance(clock, 0.1)
    assert not any(entry["applied_status"] == "observed" for entry in outcomes(journal))
    assert [entry["move_id"] for entry in outcomes(journal)] == [1, 2]


async def test_stop_while_notify_is_pending_cannot_claim_received_state(rig):
    governor, link, clock, journal = rig
    entered, release = asyncio.Event(), asyncio.Event()
    original = link.notify

    async def delayed_notify(method, params=None):
        if method == "robot.move":
            entered.set()
            await release.wait()
        await original(method, params)

    link.notify = delayed_notify
    await governor.move(0.2, 0, 0, 5)
    await entered.wait()
    await advance(clock, 0.05)
    link.push([0.2, 0, 0], [0.1, 0, 0])
    await governor.stop()
    release.set()
    await pump()
    assert outcomes(journal)[0]["reason"] == "cancelled"
    assert not any(entry["applied_status"] == "observed" for entry in outcomes(journal))


async def test_clamped_multiaxis_command_is_correlated(rig):
    governor, link, clock, journal = rig
    assert (await governor.move(10, -10, 10, 1)).ok
    await pump()
    await advance(clock, 0.05)
    link.push([0.25, -0.2, 1], [0.02, -0.01, 0.03])
    await advance(clock, 0.05)
    assert outcomes(journal)[0]["move_to_first_applied_ms"] == 50


async def test_notification_failure_never_reports_applied(rig):
    governor, link, _, journal = rig
    link.fail_notify = "robot.move"
    await governor.move(0.2, 0, 0, 1)
    await pump()
    assert outcomes(journal)[0]["reason"] == "robot_error"
    assert not any(entry["applied_status"] == "observed" for entry in outcomes(journal))
    assert not governor.moving


async def test_restart_after_stop_requires_idle_state_after_previous_command(rig):
    governor, link, clock, journal = rig
    await governor.move(0.2, 0, 0, 1)
    await pump()
    await governor.stop()
    await advance(clock, 0.05)
    await governor.move(0.2, 0, 0, 1)
    await pump()
    assert outcomes(journal)[-1]["reason"] == "baseline_predates_motion"
    await governor.stop()
    await advance(clock, 0.05)
    link.push()
    await governor.move(0.2, 0, 0, 1)
    await pump()
    assert len(outcomes(journal)) == 2
    await advance(clock, 0.05)
    link.push([0.2, 0, 0], [0.1, 0, 0])
    await advance(clock, 0.05)
    assert outcomes(journal)[-1]["applied_status"] == "observed"
    assert outcomes(journal)[-1]["move_id"] == 3
