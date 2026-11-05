"""Opt-in physical checks through the transport configured by this session."""

import asyncio
import math
import os
from pathlib import Path
from typing import Any

import pytest

from coincoin.clock import SystemClock
from coincoin.journal import Journal
from coincoin.robot.factory import create_robot_link
from coincoin.robot.governor import Governor
from coincoin.robot.link import LinkDown, RobotLink
from tests.conftest import load_sim_settings

pytestmark = pytest.mark.sim
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


async def _wait_for_settled_odom(link: RobotLink, clock: SystemClock) -> dict[str, Any]:
    previous_state: dict[str, Any] | None = None
    previous_position: list[float] | None = None
    stable_updates = 0
    async with asyncio.timeout(1):
        while stable_updates < 2:
            state = link.state()
            if state is not None and state is not previous_state:
                previous_state = state
                position = state["odom"]["position"]
                applied = state["move"]["applied"]
                if link.state_age() <= 0.2 and all(abs(value) < 0.01 for value in applied):
                    if previous_position is not None:
                        displacement = math.hypot(
                            position[0] - previous_position[0],
                            position[1] - previous_position[1],
                        )
                        stable_updates = stable_updates + 1 if displacement <= 0.005 else 0
                    previous_position = position
                else:
                    previous_position = None
                    stable_updates = 0
            await clock.sleep(0.02)
    assert state is not None
    return state


def sim_state(link: RobotLink) -> dict[str, Any]:
    state = link.state()
    assert state is not None, "simulator state unavailable"
    return state


@pytest.fixture
async def sim(tmp_path: Path):
    if os.environ.get("COINCOIN_TEST_SIM") != "1":
        pytest.skip("Set COINCOIN_TEST_SIM=1 with the assigned simulator configured in .env")
    settings = load_sim_settings(ENV_FILE)
    clock = SystemClock()
    link = create_robot_link(
        settings.robot_link,
        frame_url=settings.frame_link,
        token=settings.relay_token.get_secret_value(),
        clock=clock,
    )
    governor = Governor(link, settings, clock, Journal(tmp_path))
    try:
        try:
            await link.connect()
        except LinkDown:
            if settings.robot_link.startswith("webrtc://"):
                raise
            pytest.skip("The explicitly assigned simulator relay is unavailable")
        async with asyncio.timeout(5):
            while link.state() is None:
                await clock.sleep(0.05)
        await governor.start()
        await governor.stop("sim test setup")
        await clock.sleep(0.3)
        yield governor, link, clock
    finally:
        await governor.close()
        await link.close()


async def test_move_advances_more_than_fifteen_cm(sim):
    governor, link, clock = sim
    before_state = await _wait_for_settled_odom(link, clock)
    before = before_state["odom"]
    assert (await governor.move(0.2, 0, 0, 2)).ok
    completed = await governor.wait_motion()
    assert completed is not None and completed.ok, completed
    after_state = await _wait_for_settled_odom(link, clock)
    after = after_state["odom"]
    delta = [after["position"][i] - before["position"][i] for i in range(2)]
    forward = delta[0] * math.cos(before["yaw"]) + delta[1] * math.sin(before["yaw"])
    print(
        f"move: before={before['position']} after={after['position']} "
        f"forward_m={forward:.3f} applied={after_state['move']['applied']}"
    )
    assert forward > 0.15
    assert after_state["move"]["requested"] == [0, 0, 0]
    assert all(abs(value) < 0.01 for value in after_state["move"]["applied"])


async def test_turn_ninety_degrees_within_fifteen(sim):
    governor, link, clock = sim
    before = sim_state(link)["odom"]["yaw"]
    assert (await governor.turn(90)).ok
    completed = await governor.wait_motion()
    assert completed is not None and completed.ok, completed
    await clock.sleep(0.3)
    after = sim_state(link)["odom"]["yaw"]
    delta = math.degrees(math.atan2(math.sin(after - before), math.cos(after - before)))
    print(f"turn: delta_degrees={delta:.3f}")
    assert abs(delta - 90) < 15


async def test_look_left_has_positive_yaw(sim):
    governor, link, clock = sim
    assert (await governor.look("gauche", hold_s=1)).ok
    await clock.sleep(0.6)
    yaw = sim_state(link)["head"][2]
    print(f"look gauche: head_yaw={yaw:.3f}")
    assert yaw > 0.2
    assert (await governor.wait_look()).ok
