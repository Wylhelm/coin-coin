"""Recovery of a fallen simulated robot through the equipment assistant (WebRTC link)."""

import pytest

from coincoin.clock import FakeClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.realtime.tools.robot import tool_description
from coincoin.robot import governor as module
from coincoin.robot.capabilities import Capabilities
from coincoin.robot.governor import Governor
from tests.fakes.fake_robotd import FakeRobotLink
from tests.unit.test_governor import advance, commands, pump

SOCKET = "/owned/duck-a.sock"
WEBRTC_SIM = Capabilities("webrtc", remote_mode_switch=False, simulated=True)


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


def assisted_rig(governor, link):
    """A simulated robot on a link that refuses robot.setMode, with the assistant configured."""
    link.policies["simulated"] = True
    link.remote_mode_switch = False
    governor.settings.sim_equipment_socket = SOCKET
    governor.capabilities = WEBRTC_SIM


def stand_up(link: FakeRobotLink, selected: list, *, homed: bool = False):
    """Script the assistant's hand: robotd turns the mode re-selection into a stand-up."""

    async def select(socket, mode):
        selected.append((str(socket), mode))
        link.safety["fallen"] = False
        link.mode = mode
        link.policies["homed"] = homed

    return select


async def test_fallen_robot_on_webrtc_is_stood_up_by_the_equipment_assistant(rig, monkeypatch):
    governor, link, clock = rig
    link.mode = "walk"
    link.safety["fallen"] = True
    assisted_rig(governor, link)
    selected = []
    monkeypatch.setattr(module, "select_mode", stand_up(link, selected))
    notices = []

    async def notice(text):
        notices.append(text)

    governor.on_notice = notice
    result = await governor.set_mode("roller")
    assert result.ok and "simulation equipment" in result.detail
    await pump()
    await advance(clock, 0.6)
    assert selected == [(SOCKET, "roller")]
    assert not any("failed" in text for text in notices)
    assert not commands(link, "robot.setMode")
    link.policies["homed"] = True
    await advance(clock, 0.6)
    assert governor.awaiting_person is None
    assert governor._mode == "roller"
    assert "roller" in notices[-1]


async def test_stand_on_a_fallen_simulated_robot_recovers_instead_of_asking_a_person(
    rig, monkeypatch
):
    governor, link, clock = rig
    link.mode = "roller"
    link.safety["fallen"] = True
    assisted_rig(governor, link)
    selected = []
    monkeypatch.setattr(module, "select_mode", stand_up(link, selected, homed=True))
    result = await governor.stand()
    assert result.ok and "simulation equipment" in result.detail
    assert "person must put the duck back" not in result.detail
    await pump()
    await advance(clock, 0.6)
    assert selected == [(SOCKET, "roller")]
    assert governor.awaiting_person is None
    assert link.safety["fallen"] is False
    assert not commands(link, "robot.setMode")


async def test_reselecting_the_current_mode_while_fallen_is_not_short_circuited(rig, monkeypatch):
    governor, link, clock = rig
    link.mode = "roller"
    link.safety["fallen"] = True
    assisted_rig(governor, link)
    selected = []
    monkeypatch.setattr(module, "select_mode", stand_up(link, selected, homed=True))
    result = await governor.set_mode("roller")
    assert result.ok and "simulation equipment" in result.detail
    await pump()
    await advance(clock, 0.6)
    assert selected == [(SOCKET, "roller")]
    assert governor.awaiting_person is None
    assert not commands(link, "robot.setMode")


async def test_reselecting_the_current_mode_when_upright_stays_a_no_op(rig, monkeypatch):
    governor, link, _ = rig
    link.mode = "roller"
    assisted_rig(governor, link)

    async def forbidden(*args):
        pytest.fail("equipment assistant reached an upright robot")

    monkeypatch.setattr(module, "select_mode", forbidden)
    result = await governor.set_mode("roller")
    assert result.ok and result.detail == "locomotion mode: roller"
    assert governor.awaiting_person is None


@pytest.mark.parametrize("reason", ["robot picked up", "robot state stale"])
async def test_equipment_assistance_still_refuses_other_safety_reasons(rig, monkeypatch, reason):
    governor, link, clock = rig
    link.mode = "walk"
    assisted_rig(governor, link)
    if reason == "robot picked up":
        link.safety["picked_up"] = True
    else:
        link.streaming = False
        clock.advance(2)
    notices = []

    async def notice(text):
        notices.append(text)

    governor.on_notice = notice

    async def forbidden(*args):
        pytest.fail("equipment assistant ran while the robot was unsafe")

    monkeypatch.setattr(module, "select_mode", forbidden)
    result = await governor.set_mode("roller")
    assert result.ok and "PENDING" in result.detail
    await pump()
    await advance(clock, 0.6)
    assert governor.awaiting_person is None
    assert notices and reason in notices[-1]
    assert not commands(link, "robot.setMode")


async def test_fallen_robot_without_an_equipment_assistant_still_needs_a_person(rig):
    governor, link, _ = rig
    link.policies["simulated"] = True
    link.mode = "roller"
    link.safety["fallen"] = True
    link.remote_mode_switch = False
    governor.capabilities = WEBRTC_SIM
    result = await governor.stand()
    assert not result.ok and "person must put the duck back" in result.reason
    pending = await governor.set_mode("walk")
    assert pending.ok and "a person must" in pending.detail
    assert governor.awaiting_person == "walk"
    await governor.cancel_person_wait("test")
    assert not commands(link, "robot.setMode")


def test_stand_description_tells_the_model_the_simulator_gets_it_up():
    assisted = tool_description("stand", WEBRTC_SIM, equipment_assisted=True)
    assert "put back on its feet automatically" in assisted
    assert "ask a person to put it back on its feet" not in assisted
    manual = tool_description("stand", WEBRTC_SIM)
    assert "ask a person to put it back on its feet" in manual
    # The note never claims assistance for a robot that is not simulated.
    hardware = Capabilities("tcp", remote_mode_switch=True, simulated=False)
    assert "ask a person to put it back on its feet" in tool_description(
        "stand", hardware, equipment_assisted=True
    )
