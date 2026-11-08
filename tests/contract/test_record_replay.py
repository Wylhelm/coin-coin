"""Record and replay of robot sessions, entirely offline (fake robot, fake clocks, local TCP)."""

import asyncio
import base64
import io
import json
import math
from pathlib import Path
from typing import Any

import pytest

from coincoin.robot.frames import FrameError
from coincoin.robot.link import Frame, LinkDown, RobotLink, RobotRefused
from coincoin.robot.tcp_link import TcpLink
from scripts import record_session, replay_session
from scripts.session_log import (
    FORMAT,
    Event,
    RecordingLink,
    ReplayLink,
    SessionWriter,
    StateSampler,
    read_events,
    summarize,
)
from tests.fakes.fake_robotd import FakeRobotLink

JPEG = b"\xff\xd8fake-jpeg\xff\xd9"


class SteppingClock:
    """A clock whose sleeps advance time immediately, so a 2-minute recording runs instantly."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s
        await asyncio.sleep(0)


class CameraRobot(FakeRobotLink):
    def __init__(self, clock: SteppingClock) -> None:
        super().__init__(clock)
        self.responses = {
            "robot.policies": {"enabled": True, "homed": True, "sitting": True},
            "robot.mode": {"mode": "walk"},
            "robot.health": {"healthy": True, "battery": {"volts": 7.6, "percent": 80}},
            "robot.skills": {"skills": ["sit_toggle", "sad"]},
        }
        self.frames = 0

    def state(self) -> dict[str, Any] | None:
        state = super().state()
        if state is not None:
            state["t"] = round(self.clock.now(), 3)
            state["loop"] = {"hz": 50.0, "missed": 0}
        return state

    async def frame(self) -> Frame:
        self.frames += 1
        return Frame(JPEG, 640, 360, 1_700_000_000.0 + self.frames)


def _args(**overrides: Any) -> Any:
    args = record_session.parse_args(["--out", "unused.jsonl", "--link", "tcp://127.0.0.1:1"])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


async def _record(duration: float, **overrides: Any) -> tuple[list[Event], CameraRobot]:
    clock = SteppingClock()
    robot = CameraRobot(clock)
    handle = io.StringIO()
    writer = SessionWriter(handle, clock)
    await record_session.record(robot, writer, _args(duration=duration, **overrides), clock=clock)
    events = [Event.from_json(line) for line in handle.getvalue().splitlines() if line.strip()]
    return events, robot


async def test_recording_has_header_states_frames_health_and_no_motion() -> None:
    events, robot = await _record(12.0)
    header = events[0]
    assert header.kind == "header" and header.data["format"] == FORMAT
    assert header.data["probes"]["robot.mode"] == {"mode": "walk"}
    assert header.data["link"] == "tcp"
    kinds = [event.kind for event in events]
    assert kinds.count("state") >= 12 * 10
    assert kinds.count("frame") == 12
    health = [e for e in events if e.kind == "call" and e.data["method"] == "robot.health"]
    assert len(health) >= 2
    motion = {"robot.move", "robot.head", "robot.look", "robot.pose", "robot.do", "robot.setMode"}
    assert not {command.method for command in robot.records} & motion
    times = [event.t for event in events]
    assert times == sorted(times)


async def test_a_recording_replays_its_own_answers_states_and_frames() -> None:
    events, _ = await _record(5.0)
    clock = SteppingClock()
    link = ReplayLink(events, clock=clock)
    await link.connect()
    assert (await link.call("robot.policies"))["sitting"] is True
    assert (await link.call("robot.health"))["battery"]["percent"] == 80
    clock.t = 2.5
    state = link.state()
    assert state is not None and state["loop"]["hz"] == 50.0
    assert link.state_age() < 0.2
    assert (await link.frame()).jpeg == JPEG


async def test_unavailable_camera_is_noted_not_fatal() -> None:
    clock = SteppingClock()
    robot = FakeRobotLink(clock)
    handle = io.StringIO()
    counts = await record_session.record(
        robot, SessionWriter(handle, clock), _args(duration=1.0), clock=clock
    )
    assert counts.get("frame", 0) == 0
    assert "frame unavailable" in handle.getvalue()


async def test_recording_link_records_calls_refusals_and_notifications() -> None:
    clock = SteppingClock()
    robot = FakeRobotLink(clock)
    robot.refusals["robot.setMode"] = "robot.setMode is not available over WebRTC"
    handle = io.StringIO()
    link: RobotLink = RecordingLink(robot, SessionWriter(handle, clock))
    await link.notify("robot.move", {"vx": 0.1, "vy": 0, "vyaw": 0})
    with pytest.raises(RobotRefused):
        await link.call("robot.setMode", {"mode": "roller"})
    robot.connected = False
    with pytest.raises(LinkDown):
        await link.call("robot.stop")
    lines = [json.loads(line) for line in handle.getvalue().splitlines()]
    assert lines[0]["kind"] == "notify" and lines[0]["params"]["vx"] == 0.1
    assert lines[1]["refused"].endswith("over WebRTC")
    assert lines[2]["error"].startswith("LinkDown")


def _synthetic(path: Path) -> list[Event]:
    events = [
        Event(0.0, "header", {"format": FORMAT, "api_version": 51, "link": "webrtc"}),
        Event(0.05, "call", {"method": "robot.mode", "params": {}, "result": {"mode": "walk"}}),
        Event(0.06, "call", {"method": "robot.setMode", "refused": "not over WebRTC"}),
    ]
    for index in range(10):
        state = {
            "t": index * 0.1,
            "safety": {"fallen": index >= 8, "picked_up": False},
            "odom": {"yaw": 0.0, "position": [0.01 * index, 0, 0]},
            "loop": {"hz": 49.5},
        }
        events.append(Event(0.1 * (index + 1), "state", {"state": state}))
    frame = {
        "jpeg_b64": base64.b64encode(JPEG).decode(),
        "width": 1280,
        "height": 720,
        "captured_at": 1.5,
    }
    events.append(Event(0.5, "frame", frame))
    events.append(Event(0.55, "tof", {"frame": {"rows": 8, "cols": 8, "distance_mm": [300] * 64}}))
    events.append(Event(0.9, "notify", {"method": "robot.move", "params": {"vx": 0.1}}))
    events.sort(key=lambda event: event.t)
    path.write_text("".join(event.to_json() + "\n" for event in events), encoding="utf-8")
    return events


async def test_replay_link_follows_the_clock_and_goes_stale_at_the_end(tmp_path: Path) -> None:
    path = tmp_path / "real.jsonl"
    _synthetic(path)
    clock = SteppingClock()
    link = ReplayLink(path, clock=clock)
    assert link.state() is None and math.isinf(link.state_age())
    await link.connect()
    assert link.state() is None
    with pytest.raises(FrameError):
        await link.frame()
    clock.t = 0.35
    state = link.state()
    assert state is not None and state["t"] == pytest.approx(0.2)
    assert link.state_age() == pytest.approx(0.05)
    clock.t = 0.6
    frame = await link.frame()
    assert (frame.jpeg, frame.width, frame.height) == (JPEG, 1280, 720)
    assert link.tof() is not None
    clock.t = 0.95
    state = link.state()
    assert state is not None and state["safety"]["fallen"] is True
    clock.t = 3.0
    assert link.finished and link.state_age() > 1.0


async def test_replay_link_answers_refuses_and_records_commands(tmp_path: Path) -> None:
    path = tmp_path / "real.jsonl"
    _synthetic(path)
    link = ReplayLink(path, clock=SteppingClock())
    await link.connect()
    assert await link.call("robot.mode") == {"mode": "walk"}
    assert await link.call("robot.mode") == {"mode": "walk"}
    assert await link.call("hello", {"api_version": 47}) == {"api_version": 51}
    assert await link.call("robot.stop") is None
    with pytest.raises(RobotRefused, match="not over WebRTC"):
        await link.call("robot.setMode", {"mode": "roller"})
    with pytest.raises(RobotRefused, match="was not recorded"):
        await link.call("robot.policies")
    await link.notify("robot.move", {"vx": 0.2})
    assert [command.method for command in link.commands][-1] == "robot.move"
    await link.close()
    with pytest.raises(LinkDown):
        await link.notify("robot.stop")


async def test_replay_loop_and_speed(tmp_path: Path) -> None:
    path = tmp_path / "real.jsonl"
    _synthetic(path)
    clock = SteppingClock()
    link = ReplayLink(path, clock=clock, speed=2.0, loop=True)
    await link.connect()
    clock.t = 0.5
    assert link.elapsed() == pytest.approx(1.0 % link.duration)
    assert not link.finished
    assert link.state_age() >= 0
    with pytest.raises(ValueError):
        ReplayLink(path, speed=0)


def test_reader_rejects_foreign_and_out_of_order_files(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text('{"t": 0, "kind": "state"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not a"):
        read_events(foreign)
    backwards = tmp_path / "backwards.jsonl"
    backwards.write_text(
        Event(1.0, "header", {"format": FORMAT}).to_json()
        + "\n"
        + Event(0.5, "note", {"text": "x"}).to_json()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="backwards"):
        read_events(backwards)
    for bad in ('{"t": -1, "kind": "note"}', '{"t": 0, "kind": "nope"}', "[1]"):
        with pytest.raises(ValueError):
            Event.from_json(bad)
    with pytest.raises(ValueError):
        SessionWriter(io.StringIO()).write("nope")


def test_summary_and_compare_report_parity_differences(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real = tmp_path / "real.jsonl"
    _synthetic(real)
    sim = tmp_path / "sim.jsonl"
    sim.write_text(
        Event(0.0, "header", {"format": FORMAT, "api_version": 47, "link": "tcp"}).to_json()
        + "\n"
        + Event(0.1, "state", {"state": {"t": 0, "safety": {}, "loop": {"hz": 50}}}).to_json()
        + "\n",
        encoding="utf-8",
    )
    summary = summarize(read_events(real))
    assert summary["safety_true_samples"] == {"fallen": 2}
    assert summary["loop_hz_min"] == 49.5
    assert summary["commands"] == {"robot.mode": 1, "robot.setMode": 1, "robot.move": 1}
    result = replay_session.compare(read_events(sim), read_events(real))
    assert result["differences"]["api_version"] == [47, 51]
    assert result["differences"]["safety_true_samples.fallen"] == [0, 2]
    assert replay_session.main(["summary", str(real)]) == 0
    assert json.loads(capsys.readouterr().out)["api_version"] == 51
    assert replay_session.main(["compare", str(sim), str(real)]) == 0
    assert "differences" in json.loads(capsys.readouterr().out)


async def test_state_sampler_skips_unchanged_states() -> None:
    clock = SteppingClock()
    robot = FakeRobotLink(clock)
    robot.have_state = False
    handle = io.StringIO()
    sampler = StateSampler(robot, SessionWriter(handle, clock))
    assert not sampler.sample()
    robot.have_state = True
    assert sampler.sample()
    assert not sampler.sample()


async def test_tcp_link_runs_against_a_served_recording(tmp_path: Path) -> None:
    path = tmp_path / "real.jsonl"
    _synthetic(path)
    server = replay_session.ReplayServer(read_events(path), token="secret", loop=True)
    port = await server.start()
    link = TcpLink(f"tcp://127.0.0.1:{port}", token="secret")
    try:
        await link.connect()
        assert await link.call("robot.mode") == {"mode": "walk"}
        with pytest.raises(RobotRefused):
            await link.call("robot.setMode", {"mode": "roller"})
        for _ in range(100):
            if link.state() is not None:
                break
            await asyncio.sleep(0.02)
        assert link.state() is not None
        await link.notify("robot.stop")
        await asyncio.sleep(0.05)
        methods = [command.method for command in server.links[0].commands]
        assert methods[:2] == ["hello", "robot.subscribe"]
        assert "robot.stop" in methods
    finally:
        await link.close()
        await server.close()


def test_record_arguments_are_validated() -> None:
    with pytest.raises(SystemExit):
        record_session.parse_args(["--out", "x.jsonl", "--link", "http://duck"])
    with pytest.raises(SystemExit):
        record_session.parse_args(["--out", "x.jsonl", "--tof", "udp://duck:1"])
    with pytest.raises(SystemExit):
        record_session.parse_args(["--out", "x.jsonl", "--duration", "0"])
    with pytest.raises(SystemExit):
        record_session.parse_args(["--out", "x.jsonl", "--frame-every", "-1"])
    args = record_session.parse_args(["--out", "x.jsonl", "--link", "webrtc://duck:8443"])
    assert args.duration == 120.0
