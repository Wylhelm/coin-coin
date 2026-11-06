import asyncio

import pytest

from coincoin.clock import FakeClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.governor import Governor
from tests.fakes.fake_robotd import FakeRobotLink
from tests.unit.test_governor import advance, commands, pump


@pytest.fixture
async def rig(tmp_path):
    clock = FakeClock()
    link = FakeRobotLink(clock)
    governor = Governor(link, Settings(), clock, Journal(tmp_path))
    await governor.start()
    await pump()
    link.records.clear()
    yield governor, link, clock
    await governor.close()


async def test_switches_both_ways_and_updates_motion_limits_immediately(rig):
    governor, link, clock = rig
    for mode, lateral in (("roller", 0), ("walk", 0.2), ("roller", 0)):
        assert (await governor.set_mode(mode)).ok
        assert link.mode == mode
        assert (await governor.move(0.1, 0.2, 0, 0.1)).ok
        await pump()
        params = commands(link, "robot.move")[-1].params
        assert params is not None and params["vy"] == lateral
        await advance(clock, 0.2)
    assert [c.params for c in commands(link, "robot.setMode")] == [
        {"mode": "roller"},
        {"mode": "walk"},
        {"mode": "roller"},
    ]


async def test_same_mode_is_idempotent_and_still_stops_motion(rig):
    governor, link, clock = rig
    assert (await governor.move(0.1, 0, 0, 5)).ok
    await pump()
    assert (await governor.set_mode("walk")).ok
    assert not governor.moving and link.twist == [0, 0, 0]
    assert not commands(link, "robot.setMode")
    count = len(commands(link, "robot.move"))
    await advance(clock, 1)
    assert len(commands(link, "robot.move")) == count


@pytest.mark.parametrize("reason", ["robot fallen", "robot picked up", "robot state stale"])
async def test_unsafe_switch_is_refused(rig, reason):
    governor, link, clock = rig
    if reason == "robot fallen":
        link.safety["fallen"] = True
    elif reason == "robot picked up":
        link.safety["picked_up"] = True
    else:
        link.streaming = False
        clock.advance(2)
    outcome = await governor.set_mode("roller")
    assert not outcome.ok and outcome.reason == reason
    assert not commands(link, "robot.setMode")


async def test_invalid_mode_and_robot_refusal_are_explicit(rig):
    governor, link, _ = rig
    assert not (await governor.set_mode("ice")).ok
    assert not commands(link, "robot.setMode")
    link.refusals["robot.setMode"] = "policy unavailable"
    outcome = await governor.set_mode("roller")
    assert not outcome.ok and outcome.reason == "policy unavailable"


async def test_waits_for_confirmed_mode_and_homing(rig):
    governor, link, clock = rig
    link.responses["robot.mode"] = {"mode": "walk"}
    pending = asyncio.create_task(governor.set_mode("roller"))
    await pump()
    assert commands(link, "robot.setMode")
    assert not pending.done()
    link.responses["robot.mode"] = {"mode": "roller"}
    link.policies["homed"] = False
    await advance(clock, 0.2)
    assert not pending.done()
    link.policies["homed"] = True
    await advance(clock, 0.2)
    assert (await pending).ok


async def test_stop_cancels_pending_switch_and_no_movement_follows(rig):
    governor, link, clock = rig
    link.responses["robot.mode"] = {"mode": "walk"}
    pending = asyncio.create_task(governor.set_mode("roller"))
    await pump()
    await governor.stop()
    await advance(clock, 0.2)
    assert not (await pending).ok
    assert not commands(link, "robot.move")


async def test_unconfirmed_or_unhealthy_switch_is_not_success(rig):
    governor, link, clock = rig
    link.responses["robot.mode"] = {"mode": "walk"}
    pending = asyncio.create_task(governor.set_mode("roller"))
    await pump()
    await advance(clock, 10.2)
    outcome = await pending
    assert not outcome.ok and "within 10 s" in outcome.reason
    link.responses.pop("robot.mode")
    link.mode = "walk"
    link.responses["robot.health"] = {"healthy": False}
    outcome = await governor.set_mode("roller")
    assert not outcome.ok and "unhealthy" in outcome.reason
