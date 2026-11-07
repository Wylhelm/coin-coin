"""The delivered robot's path, end to end: mediad WebRTC route, capability probe, doctor and
link loss. Uses the local aiortc mediad stand-in; no simulator, no network beyond loopback."""

import asyncio
import json
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from coincoin.app import App
from coincoin.clock import SystemClock
from coincoin.config import Settings
from coincoin.doctor import FAIL, OK, Doctor
from coincoin.journal import Journal
from coincoin.robot.capabilities import probe
from coincoin.robot.feature import LINK_LOST, RobotFeature
from coincoin.robot.frames import FrameError
from coincoin.robot.webrtc_link import FRAME_STALE_S, WebRtcLink
from tests.integration.test_s4_regressions import _Audio
from tests.integration.test_webrtc_link import LocalMediad, until
from tests.integration.test_webrtc_link import mediad as mediad  # noqa: F401 - fixture


async def test_probe_over_webrtc_sees_the_route_refusal_and_producer(mediad: LocalMediad) -> None:
    link = WebRtcLink(mediad.url)
    try:
        await link.connect()
        assert link.producer_meta["simulated"] == "true"
        assert link.hello is not None and link.hello["api_version"] == 47
        capabilities = await probe(link)
    finally:
        await link.close()
    assert capabilities.transport == "webrtc"
    assert capabilities.remote_mode_switch is False
    assert capabilities.simulated and capabilities.api_compatible
    assert capabilities.producer["name"] == "test"
    assert capabilities.skills == ("bow",)
    probes = [record for record in mediad.records if record.get("method") == "robot.setMode"]
    assert probes == [
        {
            "jsonrpc": "2.0",
            "id": probes[0]["id"],
            "method": "robot.setMode",
            "params": {"mode": "__coincoin_probe__"},
        }
    ]


async def test_stale_camera_frame_is_refused(mediad: LocalMediad) -> None:
    link = WebRtcLink(mediad.url)
    try:
        await link.connect()
        assert (await link.frame()).jpeg
        link._latest_video_at = time.time() - FRAME_STALE_S - 1
        with pytest.raises(FrameError, match="stalled"):
            await link.frame()
    finally:
        await link.close()


def _feature_app(tmp_path: Path, url: str) -> tuple[App, RobotFeature]:
    settings = Settings(robot_link=url, person_mode_timeout_s=5)
    app = App(settings, _Audio(), Journal(tmp_path), clock=SystemClock(), features=())
    return app, RobotFeature()


def _entries(app: App) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in app.journal.log_dir.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]


async def test_feature_probes_at_connect_and_gates_tools(
    mediad: LocalMediad, tmp_path: Path
) -> None:
    app, feature = _feature_app(tmp_path, mediad.url)
    try:
        await feature.install(app)
        runtime = feature.runtime
        assert runtime is not None
        # Before any probe, sim-only tools are never offered.
        assert "go_to" not in {tool["name"] for tool in app.tools.definitions()}
        await until(lambda: runtime.governor.capabilities is not None)
        capabilities = runtime.governor.capabilities
        assert capabilities is not None and capabilities.remote_mode_switch is False
        context = await runtime.robot_context()
        assert "simulated" in context and "person" in context
        definitions = {tool["name"]: tool for tool in app.tools.definitions()}
        assert "CANNOT CHANGE MODE BY ITSELF" in str(definitions["set_mode"]["description"])
        assert "go_to" not in definitions  # no navigation map in this run
        assert any(entry["type"] == "robot_capabilities" for entry in _entries(app))
        result = await app.tools.dispatch("set_mode", json.dumps({"mode": "roller"}), app)
        assert result["ok"] and result["detail"].startswith("PENDING")
        assert runtime.governor.awaiting_person == "roller"
        moved = await app.tools.dispatch("move", '{"vx":0.1,"vy":0,"vyaw":0,"duration_s":0.5}', app)
        assert not moved["ok"]
        assert not any(
            record.get("method") == "robot.move" and record["params"]["vx"] != 0
            for record in mediad.records
        )
        mediad.mode = "roller"  # a person selected roller on the robot
        await until(lambda: runtime.governor.awaiting_person is None)
        stop = await app.tools.dispatch("stop", "{}", app)
        assert stop["ok"]
    finally:
        await app.stop()


async def test_mediad_restart_stops_motion_and_reconnects(
    mediad: LocalMediad, tmp_path: Path
) -> None:
    app, feature = _feature_app(tmp_path, mediad.url)
    try:
        await feature.install(app)
        runtime = feature.runtime
        assert runtime is not None
        await until(lambda: runtime.link.connected and runtime.governor.capabilities is not None)
        moving = asyncio.create_task(
            app.tools.dispatch("move", '{"vx":0.1,"vy":0,"vyaw":0,"duration_s":4}', app)
        )
        await until(
            lambda: any(
                record.get("method") == "robot.move" and record["params"]["vx"] > 0
                for record in mediad.records
            )
        )
        assert runtime.governor.moving
        before = len(mediad.records)
        await mediad.restart()
        result = await asyncio.wait_for(moving, 3)
        assert not result["ok"]
        await until(lambda: any(entry.get("reason") == LINK_LOST for entry in _entries(app)))
        assert not runtime.governor.moving
        await until(lambda: runtime.link.connected)
        assert mediad.sessions >= 1
        assert any(record.get("method") == "robot.subscribe" for record in mediad.records[before:])
        # The probe ran again on the new connection.
        await until(
            lambda: sum(entry["type"] == "robot_capabilities" for entry in _entries(app)) >= 2
        )
        resumed = [
            record
            for record in mediad.records[before:]
            if record.get("method") == "robot.move" and record["params"]["vx"] != 0
        ]
        assert not resumed, "motion must not resume by itself after reconnection"
    finally:
        await app.stop()


async def test_doctor_walks_the_whole_path(mediad: LocalMediad, monkeypatch) -> None:
    monkeypatch.setenv("ROBOT_TARGET", "")
    report = await Doctor(Settings(robot_link=mediad.url)).run()
    steps = {step.name: step for step in report.steps}
    assert report.ok, report.text()
    assert [step.name for step in report.steps] == [
        "config",
        "resolve host",
        "reach signalling",
        "signalling",
        "open WebRTC",
        "api version",
        "policies",
        "target",
        "capabilities",
        "health",
        "state stream",
        "command latency",
        "camera",
    ]
    assert "refused by this link" in steps["capabilities"].detail
    assert steps["camera"].status == OK and "32x24" in steps["camera"].detail
    assert steps["signalling"].data["simulated"] == "true"
    assert "ready" in report.text().splitlines()[-1]


async def test_doctor_explains_unreachable_signalling() -> None:
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    settings = Settings(robot_target="robot", robot_host="127.0.0.1", robot_signalling_port=port)
    report = await Doctor(settings).run()
    assert not report.ok
    failed = report.steps[-1]
    assert failed.name == "reach signalling" and failed.status == FAIL
    assert "mediad" in failed.fix and "duckctl" in failed.fix


async def test_doctor_explains_unknown_host() -> None:
    report = await Doctor(Settings(robot_target="robot", robot_host="no-such-duck.invalid")).run()
    assert report.steps[-1].name == "resolve host" and report.steps[-1].status == FAIL
    assert "duckctl ip" in report.steps[-1].fix


async def test_doctor_refuses_sim_target_on_a_real_robot(mediad: LocalMediad) -> None:
    mediad.simulated = False
    mediad.meta = {"name": "real"}
    report = await Doctor(Settings(robot_target="sim"), mediad.url).run()
    steps = {step.name: step for step in report.steps}
    assert not report.ok and steps["target"].status == FAIL
    assert "sim-only tools (go_to, assisted get-up) disabled" in steps["capabilities"].detail
