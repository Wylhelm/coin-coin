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


@pytest.mark.parametrize("target", ["walk", "roller"])
async def test_simulation_can_recover_after_a_fall_but_waits_until_upright(rig, target):
    governor, link, clock = rig
    link.policies["simulated"] = True
    link.mode = "roller"
    link.safety["fallen"] = True
    pending = asyncio.create_task(governor.set_mode(target))
    await pump()
    assert commands(link, "robot.setMode")[-1].params == {"mode": target}
    assert not pending.done()
    await advance(clock, 0.5)
    assert not pending.done()
    link.safety["fallen"] = False
    await advance(clock, 0.8)
    assert (await pending).ok
    assert not commands(link, "robot.move")


async def test_stand_recovers_a_fallen_simulated_robot_in_its_current_mode(rig):
    governor, link, clock = rig
    link.policies["simulated"] = True
    link.mode = "roller"
    link.safety["fallen"] = True
    await advance(clock, 0.2)
    pending = asyncio.create_task(governor.stand())
    await pump()
    assert commands(link, "robot.setMode")[-1].params == {"mode": "roller"}
    link.safety["fallen"] = False
    await advance(clock, 0.8)
    assert (await pending).ok


async def test_simulated_recovery_does_not_override_pickup_staleness_or_stop(rig):
    governor, link, clock = rig
    link.policies["simulated"] = True
    link.safety.update(fallen=True, picked_up=True)
    assert not (await governor.set_mode("walk")).ok
    assert not commands(link, "robot.setMode")
    link.safety["picked_up"] = False
    await advance(clock, 0.2)
    pending = asyncio.create_task(governor.set_mode("roller"))
    await pump()
    await governor.stop()
    await advance(clock, 0.2)
    assert not (await pending).ok
    link.streaming = False
    clock.advance(2)
    count = len(commands(link, "robot.setMode"))
    assert not (await governor.set_mode("walk")).ok
    assert len(commands(link, "robot.setMode")) == count


async def test_simulated_recovery_times_out_if_the_robot_stays_down(rig):
    governor, link, clock = rig
    link.policies["simulated"] = True
    link.safety["fallen"] = True
    await advance(clock, 0.2)
    pending = asyncio.create_task(governor.set_mode("roller"))
    await pump()
    await advance(clock, 10.2)
    outcome = await pending
    assert not outcome.ok and "within 10 s" in outcome.reason


@pytest.mark.parametrize("simulated", [False, None, "true", 1])
async def test_recovery_requires_an_explicit_simulation_capability(rig, simulated):
    governor, link, _ = rig
    link.policies["simulated"] = simulated
    link.safety["fallen"] = True
    assert not (await governor.set_mode("walk")).ok
    assert not (await governor.stand()).ok
    assert not commands(link, "robot.setMode")
    assert not commands(link, "robot.do")


async def test_simulation_recovery_does_not_allow_moving_while_fallen(rig):
    governor, link, _ = rig
    link.policies["simulated"] = True
    link.safety["fallen"] = True
    assert not (await governor.move(0.2, 0, 0, 1)).ok
    assert not commands(link, "robot.move")


@pytest.mark.parametrize("mode", ["roller", "walk"])
async def test_automatic_sim_equipment_waits_for_mode_confirmation(rig, monkeypatch, mode):
    from coincoin.robot import governor as module
    from coincoin.robot.capabilities import Capabilities

    governor, link, clock = rig
    link.mode = "walk" if mode == "roller" else "roller"
    link.policies["simulated"] = True
    governor.settings.sim_equipment_socket = "/owned/duck-a.sock"
    governor.capabilities = Capabilities("webrtc", remote_mode_switch=False)
    selected = []

    async def select(socket, target):
        selected.append((str(socket), target))
        link.mode = target
        link.policies["homed"] = False

    monkeypatch.setattr(module, "select_mode", select)
    result = await governor.set_mode(mode)
    assert result.ok and "simulation equipment" in result.detail
    await pump()
    await advance(clock, 0.6)
    assert selected == [("/owned/duck-a.sock", mode)]
    assert governor.awaiting_person == mode
    assert not (await governor.move(0.1, 0, 0, 1)).ok
    link.policies["homed"] = True
    await advance(clock, 0.6)
    assert governor.awaiting_person is None
    assert not commands(link, "robot.setMode")


async def test_automatic_equipment_never_runs_on_hardware(rig, monkeypatch):
    from coincoin.robot import governor as module
    from coincoin.robot.capabilities import Capabilities

    governor, link, _ = rig
    link.policies["simulated"] = False
    governor.settings.sim_equipment_socket = "/owned/duck-a.sock"
    governor.capabilities = Capabilities("webrtc", remote_mode_switch=False)

    async def forbidden(*args):
        pytest.fail("equipment assistant reached hardware")

    monkeypatch.setattr(module, "select_mode", forbidden)
    assert (await governor.set_mode("roller")).ok
    await pump()
    assert governor.awaiting_person is None
    assert link.mode == "walk"
