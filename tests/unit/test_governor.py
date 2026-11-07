import asyncio
import json
import math
from pathlib import Path

import pytest

from coincoin.clock import FakeClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.robot.governor import LOOK_POINTS, Governor, person_wait_reason
from coincoin.robot.link import LinkDown, RobotRefused
from tests.fakes.fake_robotd import FakeRobotLink


async def pump() -> None:
    for _ in range(12):
        await asyncio.sleep(0)


async def advance(clock: FakeClock, seconds: float, step: float = 0.05) -> None:
    for _ in range(round(seconds / step)):
        clock.advance(step)
        await pump()


@pytest.fixture
async def rig(tmp_path: Path):
    clock = FakeClock()
    link = FakeRobotLink(clock)
    governor = Governor(link, Settings(), clock, Journal(tmp_path))
    await governor.start()
    await pump()
    link.records.clear()
    yield governor, link, clock
    await governor.close()


def commands(link: FakeRobotLink, method: str):
    return [record for record in link.records if record.method == method]


def parameters(link: FakeRobotLink, method: str):
    result = []
    for record in commands(link, method):
        assert record.params is not None
        result.append(record.params)
    return result


async def test_move_rate_duration_and_completion(rig):
    governor, link, clock = rig
    assert (await governor.move(0.2, 0, 0, 2)).ok
    await pump()
    await advance(clock, 2.1)
    moves = commands(link, "robot.move")
    assert 36 <= len(moves) <= 44
    assert (moves[-1].t - moves[0].t) / (len(moves) - 1) == pytest.approx(0.05, rel=0.1)
    assert all(record.notification for record in moves)
    stops = commands(link, "robot.stop")
    assert len(stops) == 1
    assert all(not record.notification for record in stops)
    assert stops[0].t == pytest.approx(2, abs=0.05)
    assert link.twist == [0, 0, 0]
    assert (await governor.wait_motion()).ok
    assert not governor.moving
    assert commands(link, "robot.mouth")[-1].params == {"open": 0}


@pytest.mark.parametrize(
    ("mode", "actual"),
    [
        ("walk", {"vx": 0.25, "vy": -0.2, "vyaw": 1.0, "duration_s": 5}),
        ("roller", {"vx": 0.3, "vy": 0.0, "vyaw": 0.3, "duration_s": 5}),
    ],
)
async def test_bounds_by_mode(rig, mode, actual):
    governor, link, clock = rig
    link.mode = mode
    await advance(clock, 30)
    result = await governor.move(50, -50, 50, 50)
    assert result.ok and result.clamped == actual
    await pump()
    assert commands(link, "robot.move")[-1].params == {
        key: actual[key] for key in ("vx", "vy", "vyaw")
    }
    await advance(clock, 5.1)
    assert not governor.moving
    assert len(commands(link, "robot.mode")) == 1


async def test_configurable_walk_bounds(rig):
    governor, link, _ = rig
    governor.settings = Settings(max_vx=0.1, max_vy=0.05, max_vyaw=0.2, max_move_s=1)
    assert (await governor.move(-3, 4, -5, 9)).clamped == {
        "vx": -0.1,
        "vy": 0.05,
        "vyaw": -0.2,
        "duration_s": 1,
    }


@pytest.mark.parametrize(
    ("request_", "starter", "sustained", "raised"),
    [
        ((0.05, 0, 0), {"vx": 0.25, "vy": 0, "vyaw": 0}, {"vx": 0.25, "vy": 0, "vyaw": 0}, "vx"),
        ((-0.1, 0, 0), {"vx": -0.4, "vy": 0, "vyaw": 0}, {"vx": -0.25, "vy": 0, "vyaw": 0}, "vx"),
        ((0, -0.2, 0), {"vx": 0, "vy": -0.3, "vyaw": -0.6}, {"vx": 0, "vy": -0.2, "vyaw": 0}, ""),
        ((0, 0, 0.1), {"vx": 0, "vy": 0, "vyaw": 1.0}, {"vx": 0, "vy": 0, "vyaw": 1.0}, "vyaw"),
    ],
)
async def test_walk_assist_starts_the_gait_then_sustains_it(
    rig, request_, starter, sustained, raised
):
    governor, link, clock = rig
    governor.settings = Settings(walk_assist=True)
    result = await governor.move(*request_, 2)
    assert result.ok
    assert result.clamped is None
    assert result.raised == ({raised: sustained[raised]} if raised else None)
    await advance(clock, 2.1)
    sent = parameters(link, "robot.move")
    assert sent[0] == starter
    assert sent[-1] == sustained
    assert all(params == starter for params in sent[:9])


async def test_walk_assist_is_off_by_default_and_in_roller_mode(rig):
    governor, link, clock = rig
    assert not governor.settings.walk_assist
    assert (await governor.move(0.25, 0, 0, 1)).raised is None
    await pump()
    assert parameters(link, "robot.move")[0] == {"vx": 0.25, "vy": 0, "vyaw": 0}
    await advance(clock, 1.1)
    governor.settings = Settings(walk_assist=True)
    link.mode = "roller"
    await advance(clock, 30)
    link.records.clear()
    result = await governor.move(0.05, 0, 0, 1)
    assert result.clamped is None and result.raised is None
    await pump()
    assert parameters(link, "robot.move")[0] == {"vx": 0.05, "vy": 0, "vyaw": 0}


@pytest.mark.parametrize(
    ("request_", "sent", "raised"),
    [
        ((0.05, 0, 0), {"vx": 0.25, "vy": 0, "vyaw": 0}, {"vx": 0.25}),
        ((-0.1, 0, 0), {"vx": -0.25, "vy": 0, "vyaw": 0}, {"vx": -0.25}),
        ((0, 0.05, 0), {"vx": 0, "vy": 0.15, "vyaw": 0}, {"vy": 0.15}),
        ((0, 0, -0.2), {"vx": 0, "vy": 0, "vyaw": -1.0}, {"vyaw": -1.0}),
        ((0.1, 0, 0.5), {"vx": 0.25, "vy": 0, "vyaw": 1.0}, {"vx": 0.25, "vyaw": 1.0}),
        ((0.25, -0.2, 1.0), {"vx": 0.25, "vy": -0.2, "vyaw": 1.0}, None),
    ],
)
async def test_slow_walking_speeds_are_raised_to_the_gait_minimum(rig, request_, sent, raised):
    governor, link, clock = rig
    result = await governor.move(*request_, 1)
    assert result.ok and result.clamped is None
    assert result.raised == raised
    if raised:
        assert "speed raised to the minimum" in result.detail
        for key, value in raised.items():
            assert f"{key} {abs(value):g}" in result.detail
    else:
        assert "raised" not in result.detail
    await pump()
    assert parameters(link, "robot.move")[0] == sent
    entries = [
        json.loads(line)
        for path in governor.journal.log_dir.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    entry = [e for e in entries if e.get("method") == "move" and e["type"] == "tool_call"][-1]
    assert entry["raised"] == raised
    await advance(clock, 1.1)


async def test_floors_never_exceed_the_caps_and_are_configurable(rig):
    governor, link, clock = rig
    # The default vx floor (0.3) is above the default cap (0.25): the cap wins.
    governor.settings = Settings(max_vx=0.4, min_vx=0.3)
    assert (await governor.move(0.1, 0, 0, 1)).raised == {"vx": 0.3}
    await advance(clock, 1.1)
    governor.settings = Settings(max_vx=0.4, min_vx=0.3)
    assert (await governor.move(0.6, 0, 0, 1)).clamped == {"vx": 0.4}
    await advance(clock, 1.1)
    governor.settings = Settings(min_vx=0, min_vy=0, min_vyaw=0)
    result = await governor.move(0.05, 0.01, 0.1, 1)
    assert result.raised is None
    await pump()
    assert parameters(link, "robot.move")[-1] == {"vx": 0.05, "vy": 0.01, "vyaw": 0.1}
    await advance(clock, 1.1)
    with pytest.raises(ValueError):
        Settings(min_vx=-0.1)


@pytest.mark.parametrize("duration", [10, 30, 45])
async def test_long_moves_respect_configured_duration(rig, duration):
    governor, link, clock = rig
    governor.settings = Settings(max_move_s=30)
    result = await governor.move(0.1, 0, 0, duration)
    assert result.ok
    assert result.clamped == ({"duration_s": 30} if duration > 30 else None)
    await pump()
    await advance(clock, 5.2)
    assert governor.moving
    await advance(clock, min(duration, 30) - 5)
    assert not governor.moving
    assert commands(link, "robot.stop")[-1].t == pytest.approx(min(duration, 30), abs=0.1)
    assert link.twist == [0, 0, 0]


async def test_stop_interrupts_a_long_move_without_resuming(rig):
    governor, link, clock = rig
    governor.settings = Settings(max_move_s=30)
    assert (await governor.move(0.1, 0, 0, 30)).ok
    await pump()
    await advance(clock, 6)
    assert governor.moving
    assert (await governor.stop()).ok
    count = len(commands(link, "robot.move"))
    await advance(clock, 25)
    assert len(commands(link, "robot.move")) == count
    assert not governor.moving
    assert link.twist == [0, 0, 0]


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True])
async def test_non_finite_input_refused(rig, value):
    governor, link, _ = rig
    assert not (await governor.move(value, 0, 0, 1)).ok
    assert not (await governor.turn(value)).ok
    assert not (await governor.set_pose(value, 0, 0)).ok
    assert not (await governor.look(x=value, y=0, z=0)).ok
    assert not (await governor.look("centre", hold_s=value)).ok
    with pytest.raises(ValueError, match="finite"):
        await governor.mouth(value)
    assert not commands(link, "robot.move")


async def test_invalid_duration_and_direction(rig):
    governor, _, _ = rig
    assert not (await governor.move(0, 0, 0, 0)).ok
    assert not (await governor.move(0, 0, 0, -1)).ok
    stopped = await governor.move(0, 0, 0, 1)
    assert not stopped.ok and "vx" in stopped.reason
    assert not (await governor.look("inconnue")).ok
    assert not (await governor.look("centre", x=1)).ok
    assert not (await governor.look(x=1, y=0)).ok
    assert not (await governor.look(hold_s=-1)).ok


async def test_replacement_does_not_send_stop(rig):
    governor, link, clock = rig
    await governor.move(0.25, 0, 0, 5)
    await pump()
    await advance(clock, 0.2)
    await governor.move(-0.25, 0, 0, 1)
    await pump()
    assert not commands(link, "robot.stop")
    assert parameters(link, "robot.move")[-1]["vx"] == -0.25
    await advance(clock, 1.1)
    assert len(commands(link, "robot.stop")) == 1


@pytest.mark.parametrize("hazard", ["fallen", "picked_up", "stale", "down", "missing_safety"])
@pytest.mark.parametrize("duration", [5, 30])
async def test_each_watchdog_stop(rig, hazard, duration):
    governor, link, clock = rig
    governor.settings = Settings(max_move_s=duration)
    await governor.move(0.2, 0, 0, duration)
    await pump()
    if hazard in ("fallen", "picked_up"):
        link.safety[hazard] = True
    elif hazard == "stale":
        link.streaming = False
        clock.advance(1.01)
    elif hazard == "down":
        link.connected = False
    else:
        link.safety = {}
    await advance(clock, 0.2)
    assert not governor.moving
    if hazard != "down":
        assert commands(link, "robot.stop")
        assert link.twist == [0, 0, 0]
    assert len(commands(link, "robot.move")) == 1


@pytest.mark.parametrize("hazard", ["fallen", "picked_up", "stale", "down", "no_state"])
async def test_preconditions_refuse_without_actuation(rig, hazard):
    governor, link, clock = rig
    if hazard in ("fallen", "picked_up"):
        link.safety[hazard] = True
    elif hazard == "stale":
        link.streaming = False
        clock.advance(1.1)
    elif hazard == "down":
        link.connected = False
    else:
        link.have_state = False
    assert not (await governor.move(0.1, 0, 0, 1)).ok
    assert not (await governor.do_skill("roulade")).ok
    assert not commands(link, "robot.move")
    assert not commands(link, "robot.do")


async def test_enable_on_not_toggle_then_home(rig):
    governor, link, clock = rig
    link.policies.update(enabled=False, homed=False)
    link.auto_home = False
    pending = asyncio.create_task(governor.move(0.1, 0, 0, 1))
    await pump()
    assert commands(link, "robot.enable")[0].params == {"on": True}
    assert not commands(link, "robot.move")
    link.policies["homed"] = True
    await advance(clock, 0.1)
    assert (await pending).ok
    await pump()
    assert commands(link, "robot.move")
    assert not commands(link, "robot.init")


async def test_homing_timeout_and_robot_refusal(rig):
    governor, link, clock = rig
    link.policies.update(enabled=False, homed=False)
    link.auto_home = False
    pending = asyncio.create_task(governor.move(0.1, 0, 0, 1))
    await pump()
    await advance(clock, 5.2)
    assert "5 s" in (await pending).reason
    assert not commands(link, "robot.move")
    link.policies["enabled"] = False
    link.refusals["robot.enable"] = "exact daemon reason"
    assert (await governor.move(0.1, 0, 0, 1)).reason == "exact daemon reason"


async def test_stop_cancels_pending_home_and_head(rig):
    governor, link, clock = rig
    await governor.look("gauche", hold_s=10)
    link.policies.update(enabled=False, homed=False)
    link.auto_home = False
    pending = asyncio.create_task(governor.move(0.1, 0, 0, 1))
    await pump()
    assert (await governor.stop("priority STOP")).ok
    await advance(clock, 0.1)
    assert not (await pending).ok
    heads = len(commands(link, "robot.head"))
    await advance(clock, 0.3)
    assert len(commands(link, "robot.head")) == heads
    assert not commands(link, "robot.move")
    assert commands(link, "robot.mouth")[-1].params == {"open": 0}
    assert await governor.wait_motion() is None


async def test_notification_failure_interrupts_motion(rig):
    governor, link, _ = rig
    link.fail_notify = "robot.move"
    assert (await governor.move(0.1, 0, 0, 1)).ok
    await pump()
    assert not governor.moving
    assert not link.connected
    assert not (await governor.stop("link down")).ok


@pytest.mark.parametrize(("degrees", "start"), [(90, 0), (-90, -170), (360, 170)])
async def test_turn_closed_loop_wraps_and_slows(rig, degrees, start):
    governor, link, clock = rig
    governor.settings = Settings(min_vyaw=0)
    link.yaw = math.radians(start)
    result = await governor.turn(degrees)
    assert result.ok and result.raised is None
    await pump()
    await advance(clock, 10)
    completed = await governor.wait_motion()
    assert completed.ok
    assert abs(math.degrees(link.yaw) - start - degrees) <= 5
    rates = [abs(params["vyaw"]) for params in parameters(link, "robot.move")]
    assert 0.8 in rates and 0.3 in rates
    assert commands(link, "robot.stop")


@pytest.mark.parametrize("degrees", [30, -30, 90])
async def test_turn_holds_the_gait_yaw_floor_and_says_so(rig, degrees):
    governor, link, clock = rig
    result = await governor.turn(degrees)
    assert result.ok and result.raised == {"vyaw": 1.0}
    assert "speed raised to the minimum" in result.detail and "vyaw 1 rad/s" in result.detail
    await pump()
    await advance(clock, 5)
    completed = await governor.wait_motion()
    assert completed.ok, completed.reason
    rates = {abs(params["vyaw"]) for params in parameters(link, "robot.move")}
    assert rates == {1.0}
    assert abs(math.degrees(link.yaw) - degrees) <= 12
    assert commands(link, "robot.stop")


async def test_turn_floor_does_not_apply_in_roller_mode(rig):
    governor, link, clock = rig
    link.mode = "roller"
    await advance(clock, 30)
    link.records.clear()
    result = await governor.turn(30)
    assert result.ok and result.raised is None
    await pump()
    await advance(clock, 5)
    rates = {abs(params["vyaw"]) for params in parameters(link, "robot.move")}
    assert rates and max(rates) <= 0.3


async def test_turn_clamp_missing_yaw_and_timeout(rig):
    governor, link, clock = rig
    assert (await governor.turn(720)).clamped == {"degrees": 360}
    await governor.stop("test")
    link.yaw = math.nan
    assert not (await governor.turn(90)).ok
    link.yaw = 0
    link.responses["robot.mode"] = {"mode": "roller"}
    await advance(clock, 30)
    assert (await governor.turn(90)).ok
    await pump()
    link.twist = [0, 0, 0]
    # Freeze odometry without preventing velocity notifications.
    original = link._integrate
    link._integrate = lambda: None
    waiting = asyncio.create_task(governor.wait_motion())
    await pump()
    await advance(clock, 10)
    link._integrate = original
    assert (await waiting).reason == "turn timed out"
    assert not governor.moving


async def test_zero_turn_no_velocity(rig):
    governor, link, _ = rig
    assert (await governor.turn(0)).ok
    await pump()
    assert not commands(link, "robot.move")
    assert (await governor.wait_motion()).ok


@pytest.mark.parametrize("direction", list(LOOK_POINTS))
async def test_look_mapping_and_hold_at_five_hz(rig, direction):
    governor, link, clock = rig
    assert (await governor.look(direction, hold_s=1)).ok
    await pump()
    await advance(clock, 1.1)
    look = commands(link, "robot.look")[0]
    assert look.params == dict(zip(("x", "y", "z"), LOOK_POINTS[direction], strict=True))
    heads = commands(link, "robot.head")
    assert 5 <= len(heads) <= 6
    assert all(record.params == heads[0].params for record in heads)
    await governor.wait_look()


async def test_look_point_clamping_replacement_and_failure(rig):
    governor, link, clock = rig
    assert (await governor.look(hold_s=50)).clamped == {"hold_s": 10}
    await pump()
    await governor.look(x=1, y=1, z=1, hold_s=0)
    await pump()
    before = len(commands(link, "robot.head"))
    await advance(clock, 1)
    assert len(commands(link, "robot.head")) == before
    link.responses["robot.look"] = {"head": {"neck_pitch": 0}}
    assert not (await governor.look("centre")).ok
    link.responses["robot.look"] = {
        "head": {"neck_pitch": 0, "head_pitch": 0, "head_yaw": 0.5, "head_roll": 0},
        "clamped": True,
    }
    assert "head" in (await governor.look("gauche")).clamped
    link.fail_notify = "robot.head"
    await pump()
    assert not link.connected


async def test_pose_reset_and_mouth_are_bounded(rig):
    governor, link, _ = rig
    assert (await governor.set_pose(-1, 1, -1)).clamped == {
        "z": -0.025,
        "roll": 0.26,
        "pitch": -0.26,
    }
    assert parameters(link, "robot.pose")[-1]["active"] is True
    assert (await governor.set_pose(0.005, 0, 0)).clamped is None
    assert (await governor.reset_pose()).ok
    assert commands(link, "robot.pose")[-1].params == {"active": False}
    await governor.mouth(10)
    await governor.mouth(-10)
    assert [params["open"] for params in parameters(link, "robot.mouth")][-2:] == [1, 0]
    link.connected = False
    assert not (await governor.reset_pose()).ok
    assert not (await governor.set_pose(0, 0, 0)).ok
    with pytest.raises(LinkDown):
        await governor.mouth(0)


async def test_dynamic_skills_and_original_daemon_reason(rig):
    governor, link, _ = rig
    assert await governor.skills() == ["roulade", "kick_left", "kick_right", "ground_pick"]
    assert not (await governor.do_skill("sit_toggle")).ok
    assert "roulade" in (await governor.do_skill("crouch")).reason
    assert (await governor.do_skill("ground_pick")).ok
    link.refusals["robot.do"] = "still travelling home"
    assert (await governor.do_skill("roulade")).reason == "still travelling home"
    link.responses["robot.skills"] = {
        "skills": [{"name": "new"}, {"name": "new"}],
        "built_in": ["new", "sit_toggle"],
    }
    assert await governor.skills() == ["new"]


@pytest.mark.parametrize(
    "response",
    [None, {"skills": None}, {"skills": [None]}, {"skills": [], "built_in": 1}],
)
async def test_invalid_skill_lists_refused(rig, response):
    governor, link, _ = rig
    link.responses["robot.skills"] = response
    with pytest.raises(RobotRefused):
        await governor.skills()
    assert not (await governor.do_skill("roulade")).ok


async def test_sit_stand_idempotence_and_missing_sitting(rig):
    governor, link, _ = rig
    assert (await governor.stand()).ok
    assert not commands(link, "robot.do")
    assert (await governor.sit()).ok
    assert (await governor.sit()).ok
    assert (await governor.stand()).ok
    assert (await governor.stand()).ok
    assert [record.params for record in commands(link, "robot.do")] == [
        {"skill": "sit_toggle"},
        {"skill": "sit_toggle"},
    ]
    link.policies.pop("sitting")
    assert (await governor.sit()).reason == "je ne sais pas si je suis assis"
    assert (await governor.stand()).reason == "je ne sais pas si je suis assis"
    assert not commands(link, "robot.init")


@pytest.mark.parametrize("action", ["move", "turn"])
async def test_motion_stands_a_seated_duck_up_first(rig, action):
    governor, link, clock = rig
    assert (await governor.sit()).ok
    started = asyncio.create_task(
        governor.move(0.2, 0, 0, 1) if action == "move" else governor.turn(30)
    )
    await advance(clock, 2)
    assert (await started).ok
    assert link.policies["sitting"] is False
    assert [record.params for record in commands(link, "robot.do")] == [
        {"skill": "sit_toggle"},
        {"skill": "sit_toggle"},
    ]
    await governor.stop()


async def test_skills_replace_motion(rig):
    governor, link, _ = rig
    await governor.move(0.1, 0, 0, 5)
    await pump()
    assert (await governor.do_skill("roulade")).ok
    assert not governor.moving
    assert commands(link, "robot.stop")
    await governor.move(0.1, 0, 0, 5)
    await pump()
    assert (await governor.sit()).ok
    assert not governor.moving


async def test_invalid_policies_mode_and_status(rig):
    governor, link, clock = rig
    assert "standing" in (await governor.status()).detail
    link.responses["robot.policies"] = None
    assert not (await governor.status()).ok
    assert not (await governor.move(0.1, 0, 0, 1)).ok
    link.responses.pop("robot.policies")
    link.mode = "unknown"
    await advance(clock, 30)
    assert not (await governor.move(0.1, 0, 0, 1)).ok
    link.have_state = False
    assert "unknown" in (await governor.status()).detail


async def test_reconnection_refreshes_mode_and_closes_mouth(rig):
    governor, link, clock = rig
    link.connected = False
    await advance(clock, 0.2)
    link.mode = "roller"
    link.connected = True
    await advance(clock, 0.2)
    assert commands(link, "robot.mouth")[-1].params == {"open": 0}
    result = await governor.move(1, 1, 1, 1)
    assert result.clamped["vy"] == 0
    await governor.close()
    assert not (await governor.move(0.1, 0, 0, 1)).ok


async def test_mode_change_stops_active_motion(rig):
    governor, link, clock = rig
    await advance(clock, 29)
    await governor.move(0.1, 0.1, 0, 5)
    await pump()
    link.mode = "roller"
    await advance(clock, 1.2)
    assert not governor.moving
    assert link.twist == [0, 0, 0]


async def test_request_timeout_and_stop_refusal_surface(rig):
    governor, link, _ = rig
    link.refusals["robot.stop"] = "exact stop refusal"
    assert (await governor.stop("test")).reason == "exact stop refusal"
    assert commands(link, "robot.mouth")[-1].params == {"open": 0}
    original = link.call

    async def timeout_call(method, params=None, timeout=2):  # noqa: ASYNC109 - fake contract
        if method == "robot.policies":
            raise TimeoutError
        return await original(method, params, timeout)

    link.call = timeout_call
    assert (await governor.move(0.1, 0, 0, 1)).reason == "robot request timed out"
    assert (await governor.status()).reason == "robot request timed out"


@pytest.mark.parametrize("late_reply", [False, True])
@pytest.mark.parametrize(
    ("action", "method", "occurrence"),
    [
        ("look", "robot.look", 1),
        ("skill", "robot.skills", 1),
        ("skill", "robot.stop", 1),
        ("skill", "robot.do", 1),
        ("sit", "robot.policies", 3),
        ("sit", "robot.do", 1),
        ("stand", "robot.policies", 3),
        ("move", "robot.policies", 1),
        ("move", "robot.enable", 1),
    ],
)
async def test_stop_preempts_slow_action_rpc(rig, action, method, occurrence, late_reply):
    governor, link, clock = rig
    assert (await governor.move(0.25, 0, 0, 5)).ok
    await pump()
    assert link.twist[0] == 0.25
    if action == "stand":
        link.policies["sitting"] = True
    if method == "robot.enable":
        link.policies["enabled"] = False
    entered, release = asyncio.Event(), asyncio.Event()
    original = link.call
    seen = 0

    async def delayed(method_, params=None, timeout=2):  # noqa: ASYNC109 - fake contract
        nonlocal seen
        result = await original(method_, params, timeout)
        if method_ == method:
            seen += 1
            if seen == occurrence:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    if not late_reply:
                        raise
                    # Model a response already in flight despite cancellation.
                    await release.wait()
        return result

    link.call = delayed
    match action:
        case "look":
            pending = asyncio.create_task(governor.look("gauche"))
        case "skill":
            pending = asyncio.create_task(governor.do_skill("roulade"))
        case "sit":
            pending = asyncio.create_task(governor.sit())
        case "stand":
            pending = asyncio.create_task(governor.stand())
        case _:
            pending = asyncio.create_task(governor.move(-0.25, 0, 0, 5))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        start = len(link.records)
        assert (await asyncio.wait_for(governor.stop("priority STOP"), 0.1)).ok
        assert any(record.method == "robot.stop" for record in link.records[start:])
        assert link.twist == [0, 0, 0]
        assert not governor.moving
        release.set()
        result = await asyncio.wait_for(pending, 1)
        assert not result.ok and result.reason == "action cancelled by stop"
        await advance(clock, 0.3)
        assert not any(
            record.method in ("robot.move", "robot.head", "robot.enable", "robot.do")
            for record in link.records[start:]
        )
        assert (await governor.wait_look()).reason == "look interrupted"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_stop_invalidates_look_waiting_for_action_lock(rig):
    governor, link, _ = rig
    async with governor._action_lock:
        pending = asyncio.create_task(governor.look("gauche"))
        await pump()
        assert not pending.done()
        assert (await asyncio.wait_for(governor.stop("queued action"), 0.1)).ok
    assert (await pending).reason == "action cancelled by stop"
    assert not commands(link, "robot.look")
    assert not commands(link, "robot.head")
    assert (await governor.look("droite")).ok
    await pump()
    assert commands(link, "robot.head")


async def test_watchdog_preempts_slow_look_and_rejects_late_head(rig):
    governor, link, clock = rig
    assert (await governor.move(0.2, 0, 0, 5)).ok
    entered, release = asyncio.Event(), asyncio.Event()
    original = link.call

    async def delayed(method, params=None, timeout=2):  # noqa: ASYNC109 - fake contract
        result = await original(method, params, timeout)
        if method == "robot.look":
            entered.set()
            await release.wait()
        return result

    link.call = delayed
    pending = asyncio.create_task(governor.look("gauche"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        link.safety["fallen"] = True
        await advance(clock, 0.2)
        assert commands(link, "robot.stop")
        assert link.twist == [0, 0, 0]
        assert (await asyncio.wait_for(pending, 1)).reason == "action cancelled by stop"
        assert not commands(link, "robot.head")
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


def scripted_health(link: FakeRobotLink, health: dict[str, object]) -> None:
    real_call = link.call

    async def call(method, params=None, timeout=2.0):  # noqa: ASYNC109 - RobotLink contract
        if method == "robot.health":
            link._record(method, params, False)
            return dict(health)
        return await real_call(method, params, timeout)

    link.call = call  # type: ignore[method-assign]


async def test_degraded_health_refuses_and_stops_motion_until_healthy(rig):
    governor, link, clock = rig
    health: dict[str, object] = {"healthy": True}
    scripted_health(link, health)
    assert (await governor.move(0.25, 0, 0, 5)).ok
    health.update(healthy=False, degraded=True, reason="servo power off")
    await advance(clock, 5.5)
    assert commands(link, "robot.health")
    assert not governor.moving and link.twist == [0, 0, 0]
    assert commands(link, "robot.stop")
    refused = await governor.move(0.25, 0, 0, 1)
    assert not refused.ok
    assert "degraded" in refused.reason and "servo power off" in refused.reason
    assert "refused" in refused.reason
    health.clear()
    health["healthy"] = True
    await advance(clock, 5.5)
    assert (await governor.move(0.25, 0, 0, 1)).ok
    await advance(clock, 1.2)


async def test_unhealthy_verdict_and_unknown_poll_keep_last_verdict(rig):
    governor, link, clock = rig
    health: dict[str, object] = {"healthy": False, "reason": "control loop stalled"}
    scripted_health(link, health)
    await advance(clock, 5.5)
    refused = await governor.move(0.25, 0, 0, 1)
    assert not refused.ok and "unhealthy" in refused.reason


def test_person_message_stays_generic():
    for mode in ("walk", "roller"):
        reason = person_wait_reason(mode)
        assert mode in reason and "robotctl" not in reason and "configure" not in reason
