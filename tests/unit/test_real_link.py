"""Real-route behaviour: capability probe, person-assisted mode change and tool gating."""

import asyncio
import json

import pytest

from coincoin.clock import FakeClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.realtime.tools import ToolRegistry
from coincoin.realtime.tools.robot import tool_availability, tool_description
from coincoin.robot.capabilities import PROBE_MODE, Capabilities, probe, probe_mode_switch
from coincoin.robot.governor import Governor
from tests.fakes.fake_robotd import FakeRobotLink
from tests.unit.test_governor import advance, commands, pump

WEBRTC = Capabilities("webrtc", api_version=47, remote_mode_switch=False, mode="walk")


@pytest.fixture
async def rig(tmp_path):
    clock = FakeClock()
    link = FakeRobotLink(clock)
    notices: list[str] = []
    governor = Governor(link, Settings(person_mode_timeout_s=5), clock, Journal(tmp_path))

    async def notice(text: str) -> None:
        notices.append(text)

    governor.on_notice = notice
    await governor.start()
    await pump()
    link.records.clear()
    yield governor, link, clock, notices
    await governor.close()


def mode_changes(link: FakeRobotLink) -> list[object]:
    return [
        record.params
        for record in commands(link, "robot.setMode")
        if record.params != {"mode": PROBE_MODE}
    ]


async def test_probe_reads_identity_and_remote_mode_switch():
    link = FakeRobotLink(FakeClock())
    link.policies.update(simulated=True, skills=["bow"])
    capabilities = await probe(link)
    assert capabilities.transport == "FakeRobotLink"
    assert capabilities.api_version == 47 and capabilities.api_compatible
    assert capabilities.simulated and capabilities.skills == ("bow",)
    assert capabilities.remote_mode_switch is True
    assert capabilities.mode == "walk" and link.mode == "walk"
    assert not capabilities.problems


async def test_probe_detects_the_webrtc_route_refusal_without_changing_mode():
    link = FakeRobotLink(FakeClock())
    link.remote_mode_switch = False
    capabilities = await probe(link)
    assert capabilities.remote_mode_switch is False
    assert capabilities.mode_switch_needs_person
    assert not capabilities.simulated
    assert link.mode == "walk"


async def test_probe_is_undecided_when_the_link_is_down():
    link = FakeRobotLink(FakeClock())
    link.connected = False
    assert await probe_mode_switch(link) is None
    capabilities = await probe(link)
    assert capabilities.remote_mode_switch is None and capabilities.mode_switch_needs_person
    assert len(capabilities.problems) == 3


async def test_person_flow_never_moves_until_the_robot_reports_the_mode(rig):
    governor, link, clock, notices = rig
    link.remote_mode_switch = False
    governor.capabilities = WEBRTC
    result = await governor.set_mode("roller")
    assert result.ok and result.detail.startswith("PENDING")
    assert "fit the skates" in result.detail
    assert governor.awaiting_person == "roller"
    assert not mode_changes(link)
    assert commands(link, "robot.stop")
    refused = await governor.move(0.1, 0, 0, 0.5)
    assert not refused.ok and "person" in refused.reason
    assert not [r for r in link.records if r.method == "robot.move" and r.params["vx"] != 0]
    status = await governor.status()
    assert "roller" in (status.detail or "")
    await advance(clock, 1)
    assert governor.awaiting_person == "roller"
    link.mode = "roller"  # the person fitted the skates and selected roller on the robot
    await advance(clock, 1)
    assert governor.awaiting_person is None
    assert notices and "roller" in notices[-1]
    assert (await governor.move(0.1, 0, 0, 0.1)).ok


async def test_person_flow_times_out_and_keeps_motion_safe(rig):
    governor, link, clock, notices = rig
    link.remote_mode_switch = False
    governor.capabilities = WEBRTC
    assert (await governor.set_mode("roller")).ok
    await advance(clock, 6)
    assert governor.awaiting_person is None
    assert notices and notices[-1].startswith("Personne")
    assert link.mode == "walk"


async def test_stop_cancels_the_person_wait(rig):
    governor, link, clock, notices = rig
    governor.capabilities = WEBRTC
    link.remote_mode_switch = False
    assert (await governor.set_mode("roller")).ok
    await governor.cancel_person_wait("bouton")
    assert governor.awaiting_person is None
    link.mode = "roller"
    await advance(clock, 1)
    assert not notices


async def test_mode_already_selected_needs_nobody(rig):
    governor, link, _, _ = rig
    governor.capabilities = WEBRTC
    link.remote_mode_switch = False
    result = await governor.set_mode("walk")
    assert result.ok and result.detail == "locomotion mode: walk"
    assert governor.awaiting_person is None


async def test_route_refusal_without_probe_falls_back_to_the_person(rig):
    governor, link, _, _ = rig
    link.remote_mode_switch = False
    assert governor.capabilities is None
    result = await governor.set_mode("roller")
    assert result.ok and result.detail.startswith("PENDING")
    assert governor.capabilities is not None
    assert governor.capabilities.remote_mode_switch is False
    await governor.cancel_person_wait("test")


async def test_person_wait_survives_link_outage(rig):
    governor, link, clock, notices = rig
    governor.capabilities = WEBRTC
    link.remote_mode_switch = False
    assert (await governor.set_mode("roller")).ok
    link.connected = False
    await advance(clock, 1)
    assert governor.awaiting_person == "roller"
    link.connected = True
    link.mode = "roller"
    await advance(clock, 1)
    assert governor.awaiting_person is None and "roller" in notices[-1]


async def test_fallen_robot_without_remote_mode_switch_asks_a_person(rig):
    governor, link, _, _ = rig
    governor.capabilities = WEBRTC
    link.policies["simulated"] = True
    link.safety["fallen"] = True
    result = await governor.stand()
    assert not result.ok and "person" in result.reason
    assert not mode_changes(link)


def test_tool_descriptions_follow_capabilities():
    assert "PERSON" not in tool_description("set_mode")
    assert "cannot change mode by itself".upper() in tool_description("set_mode", WEBRTC).upper()
    remote = Capabilities("tcp", remote_mode_switch=True, simulated=True)
    assert tool_description("set_mode", remote) == tool_description("set_mode")
    assert tool_description("stand", remote) == tool_description("stand")
    assert "person" in tool_description("stand", WEBRTC)
    assert tool_availability("go_to", None)
    assert tool_availability("go_to", WEBRTC)
    assert tool_availability("go_to", remote) is None
    assert tool_availability("move", WEBRTC) is None


async def test_registry_hides_unavailable_tools_and_refuses_them():
    registry = ToolRegistry()
    calls: list[str] = []

    async def handler(arguments, context):
        calls.append("called")
        return {"ok": True}

    registry.register("go_to", "Go.", {"type": "object", "properties": {}}, handler)
    registry.configure("go_to", available=False, reason="not simulated", description="Go!")
    assert registry.definitions() == []
    assert not registry.available("go_to") and registry.description("go_to") == "Go!"
    result = await registry.dispatch("go_to", json.dumps({}), None)
    assert result == {"ok": False, "reason": "go_to: not simulated"}
    assert not calls
    registry.configure("go_to", available=True)
    assert [tool["name"] for tool in registry.definitions()] == ["go_to"]
    with pytest.raises(ValueError, match="Unknown tool"):
        registry.configure("fly", available=False)


async def test_settings_timeout_is_used(rig):
    governor, link, clock, _ = rig
    governor.capabilities = WEBRTC
    link.remote_mode_switch = False
    result = await governor.set_mode("roller")
    assert "5 s" in result.detail
    await asyncio.sleep(0)
    await governor.cancel_person_wait("test")
