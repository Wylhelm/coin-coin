"""Opt-in movement checks through WebRTC against this session's camera-enabled simulator."""

import asyncio
import math
import os
import statistics
from pathlib import Path

import pytest

from coincoin.clock import SystemClock
from coincoin.journal import Journal
from coincoin.robot.governor import Governor
from coincoin.robot.webrtc_link import WebRtcLink
from tests.conftest import load_sim_settings

pytestmark = pytest.mark.sim
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


@pytest.fixture
async def sim(tmp_path: Path):
    if os.environ.get("COINCOIN_TEST_SIM") != "1":
        pytest.skip("Set COINCOIN_TEST_SIM=1 with the assigned camera-enabled simulator")
    settings = load_sim_settings(ENV_FILE)
    clock = SystemClock()
    link = WebRtcLink(settings.robot_link, clock=clock)
    governor = Governor(link, settings, clock, Journal(tmp_path))
    try:
        await link.connect()
        async with asyncio.timeout(5):
            while link.state() is None:
                await clock.sleep(0.05)
        await governor.start()
        await governor.stop("WebRTC sim test setup")
        await clock.sleep(0.3)
        yield governor, link, clock
    finally:
        await governor.close()
        await link.close()


async def test_webrtc_move_advances_more_than_fifteen_cm(sim):
    governor, link, clock = sim
    before = link.state()["odom"]
    assert (await governor.move(0.2, 0, 0, 2)).ok
    completed = await governor.wait_motion()
    assert completed is not None and completed.ok, completed
    await clock.sleep(0.3)
    after = link.state()["odom"]
    delta = [after["position"][i] - before["position"][i] for i in range(2)]
    forward = delta[0] * math.cos(before["yaw"]) + delta[1] * math.sin(before["yaw"])
    print(f"WebRTC move: forward_m={forward:.3f}")
    assert forward > 0.15
    assert link.state()["move"]["requested"] == [0, 0, 0]
    async with asyncio.timeout(1):
        while any(abs(value) >= 0.01 for value in link.state()["move"]["applied"]):
            await clock.sleep(0.02)


async def test_webrtc_turn_ninety_degrees_within_fifteen(sim):
    governor, link, clock = sim
    before = link.state()["odom"]["yaw"]
    assert (await governor.turn(90)).ok
    completed = await governor.wait_motion()
    assert completed is not None and completed.ok, completed
    await clock.sleep(0.3)
    after = link.state()["odom"]["yaw"]
    delta = math.degrees(math.atan2(math.sin(after - before), math.cos(after - before)))
    print(f"WebRTC turn: delta_degrees={delta:.3f}")
    assert abs(delta - 90) < 15


async def test_webrtc_look_left_has_positive_yaw(sim):
    governor, link, clock = sim
    assert (await governor.look("gauche", hold_s=1)).ok
    await clock.sleep(0.6)
    yaw = link.state()["head"][2]
    print(f"WebRTC look gauche: head_yaw={yaw:.3f}")
    assert yaw > 0.2
    assert (await governor.wait_look()).ok


async def test_webrtc_health_rtt_and_twenty_hz_move_jitter(sim):
    _, link, _ = sim
    rtts = []
    for _ in range(25):
        started = asyncio.get_running_loop().time()
        assert (await link.call("robot.health"))["healthy"]
        rtts.append((asyncio.get_running_loop().time() - started) * 1000)
    rtts.sort()
    p95_rtt = rtts[math.ceil(0.95 * len(rtts)) - 1]

    await link.call("robot.subscribe", {"hz": 50})
    await asyncio.sleep(0.2)
    done = asyncio.Event()
    transitions = []
    deadman = False

    async def send_moves():
        try:
            for index in range(50):
                await link.notify(
                    "robot.move",
                    {"vx": 0.04 + (index % 10) * 0.001, "vy": 0.0, "vyaw": 0.0},
                )
                await asyncio.sleep(0.05)
        finally:
            done.set()

    async def observe_state():
        nonlocal deadman
        state = link.state()
        last_requested = tuple(state.get("move", {}).get("requested", [])) if state else None
        while not done.is_set():
            state = link.state()
            if state is not None:
                move = state.get("move", {})
                requested = tuple(move.get("requested", []))
                if requested != last_requested:
                    transitions.append(asyncio.get_running_loop().time())
                    last_requested = requested
                if any(abs(value) > 0.001 for value in requested):
                    deadman |= "deadman" in move.get("limited_by", [])
            await asyncio.sleep(0.005)

    try:
        await asyncio.gather(send_moves(), observe_state())
    finally:
        await link.call("robot.stop", {})

    intervals_ms = [
        (right - left) * 1000 for left, right in zip(transitions, transitions[1:], strict=False)
    ]
    assert len(intervals_ms) >= 10, f"only observed {len(intervals_ms)} move updates"
    jitter_p95 = sorted(abs(interval - 50) for interval in intervals_ms)[
        math.ceil(0.95 * len(intervals_ms)) - 1
    ]
    observed_duration = transitions[-1] - transitions[0]
    observed_hz = (len(transitions) - 1) / observed_duration
    print(
        "WebRTC metrics: "
        f"robot.health_rtt_ms_p50={statistics.median(rtts):.2f} "
        f"p95={p95_rtt:.2f}; robot.move_nominal_hz=20 "
        f"robot.move_observed_hz={observed_hz:.2f} "
        f"observed_updates={len(transitions)} observed_duration_s={observed_duration:.3f} "
        f"interval_ms_p50={statistics.median(intervals_ms):.2f} "
        f"jitter_abs_ms_p95={jitter_p95:.2f}; deadman_during_move={deadman}"
    )
    assert not deadman
