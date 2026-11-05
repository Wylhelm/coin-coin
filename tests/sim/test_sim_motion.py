"""Opt-in physical checks against the session's explicitly configured relay."""

import asyncio
import math
import os
from pathlib import Path

import pytest
from pydantic_settings import DotEnvSettingsSource

from coincoin.clock import SystemClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.governor import Governor
from coincoin.robot.link import LinkDown
from coincoin.robot.tcp_link import TcpLink

pytestmark = pytest.mark.sim
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@pytest.fixture
async def sim(tmp_path: Path):
    if os.environ.get("COINCOIN_TEST_SIM") != "1":
        pytest.skip("Set COINCOIN_TEST_SIM=1 with your assigned relay configured in .env")
    source = DotEnvSettingsSource(Settings, env_file=ENV_FILE)
    settings = Settings.model_validate(source())
    clock = SystemClock()
    link = TcpLink(
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
    before = link.state()["odom"]
    assert (await governor.move(0.2, 0, 0, 2)).ok
    completed = await governor.wait_motion()
    assert completed is not None and completed.ok, completed
    await clock.sleep(0.3)
    after = link.state()["odom"]
    delta = [after["position"][i] - before["position"][i] for i in range(2)]
    forward = delta[0] * math.cos(before["yaw"]) + delta[1] * math.sin(before["yaw"])
    print(f"move: before={before['position']} after={after['position']} forward_m={forward:.3f}")
    assert forward > 0.15
    assert link.state()["move"]["requested"] == [0, 0, 0]
    async with asyncio.timeout(1):
        while any(abs(value) >= 0.01 for value in link.state()["move"]["applied"]):
            await clock.sleep(0.02)
    assert all(abs(value) < 0.01 for value in link.state()["move"]["applied"])


async def test_turn_ninety_degrees_within_fifteen(sim):
    governor, link, clock = sim
    before = link.state()["odom"]["yaw"]
    assert (await governor.turn(90)).ok
    completed = await governor.wait_motion()
    assert completed is not None and completed.ok, completed
    await clock.sleep(0.3)
    after = link.state()["odom"]["yaw"]
    delta = math.degrees(math.atan2(math.sin(after - before), math.cos(after - before)))
    print(f"turn: delta_degrees={delta:.3f}")
    assert abs(delta - 90) < 15


async def test_look_left_has_positive_yaw(sim):
    governor, link, clock = sim
    assert (await governor.look("gauche", hold_s=1)).ok
    await clock.sleep(0.6)
    yaw = link.state()["head"][2]
    print(f"look gauche: head_yaw={yaw:.3f}")
    assert yaw > 0.2
    assert (await governor.wait_look()).ok
