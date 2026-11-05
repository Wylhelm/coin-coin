"""S5 wire-level robot tools, lifecycle and Realtime continuation regressions."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import WSMsgType, web

from coincoin.app import App
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.realtime.events import ResponseDone, ResponseStarted, ToolCall
from coincoin.realtime.session import RealtimeConnectionError, RealtimeSession
from coincoin.robot.feature import LINK_LOST, RobotFeature
from coincoin.statemachine import Mode, State
from tests.fakes.fake_realtime import FakeRealtimeServer
from tests.fakes.fake_robotd import FakeRobotd, FakeRobotLink
from tests.integration.test_s4_regressions import _Audio, _settings


async def _until(predicate) -> None:
    async with asyncio.timeout(3):
        while not predicate():  # noqa: ASYNC110 - observe the actual TCP/WebSocket peers
            await asyncio.sleep(0.005)


def _entries(app: App) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in app.journal.log_dir.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]


class ScriptedRealtime(FakeRealtimeServer):
    """Keep the real GA handshake fixture; script tool-only responses on the wire."""

    def __init__(self, turns: list[list[tuple[str, dict[str, Any]]]]) -> None:
        self.turns = turns
        self.turn = 0
        super().__init__()

    async def _emit_turn(self, websocket: web.WebSocketResponse) -> None:
        if self.turn >= len(self.turns):
            return
        response_id = f"response-{self.turn}"
        await websocket.send_json({"type": "response.created", "response": {"id": response_id}})
        for index, (name, arguments) in enumerate(self.turns[self.turn]):
            await websocket.send_json(
                {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "item": {
                        "type": "function_call",
                        "name": name,
                        "call_id": f"call-{self.turn}-{index}",
                        "arguments": json.dumps(arguments),
                    },
                }
            )
        await websocket.send_json(
            {
                "type": "response.done",
                "response": {"id": response_id, "status": "completed", "usage": {}},
            }
        )
        self.turn += 1

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        await websocket.send_json(self.events[0])
        async for message in websocket:
            if message.type != WSMsgType.TEXT:
                continue
            event = json.loads(message.data)
            self.received.append(event)
            if event["type"] == "session.update":
                await websocket.send_json(self.events[1])
            elif event["type"] == "input_audio_buffer.append" or event["type"] == "response.create":
                await self._emit_turn(websocket)
        return websocket


@pytest.fixture
async def rig(tmp_path: Path):
    robot = FakeRobotd()
    await robot.start()
    settings = _settings()
    settings.robot_link = f"tcp://127.0.0.1:{robot.port}"
    feature = RobotFeature()
    controls: list[dict[str, object]] = []

    async def control(message: dict[str, object]) -> None:
        controls.append(message)

    app = App(settings, _Audio(), Journal(tmp_path), features=(), control=control)
    await feature.install(app)
    assert feature.runtime is not None
    runtime = feature.runtime
    await _until(lambda: runtime.link.connected and runtime.link.state() is not None)
    app.state = State(Mode.AWAKE)
    try:
        yield app, runtime, robot, controls
    finally:
        await app.stop()
        await robot.close_server()


async def test_scripted_move_then_turn_over_both_real_transports(rig) -> None:
    app, feature, robot, controls = rig
    fake = ScriptedRealtime(
        [
            [("move", {"vx": 0.2, "vy": 0, "vyaw": 0, "duration_s": 0.1})],
            [("turn", {"degrees": 90})],
        ]
    )
    await fake.start()
    session = RealtimeSession(app.settings, base_url=fake.websocket_url)
    app._session = session

    async def events() -> None:
        async for event in session.events():
            await app._handle_realtime_event(event)

    consumer = None
    try:
        context = await feature.skills_context()
        await session.open(context, app.tools.definitions())
        consumer = asyncio.create_task(events())
        await session.send_audio(bytes(960))
        await _until(lambda: sum(e["type"] == "response.create" for e in fake.received) == 2)
        await _until(
            lambda: any(c.method == "robot.move" and c.params["vyaw"] > 0 for c in robot.records)
        )
        outputs = [
            json.loads(e["item"]["output"])
            for e in fake.received
            if e["type"] == "conversation.item.create"
        ]
        assert len(outputs) == 2 and all(r["ok"] for r in outputs)
        assert any(c.method == "robot.move" and c.params["vx"] == 0.2 for c in robot.records)
        completed = await feature.governor.wait_motion()
        assert completed is not None and completed.ok
        await _until(lambda: any(c.method == "robot.stop" for c in robot.records))
        assert any(c.method == "robot.stop" and not c.notification for c in robot.records)
        assert any(e.get("name") == "move" and e["type"] == "tool_call" for e in _entries(app))
        assert any(e.get("name") == "turn" and e["type"] == "tool_result" for e in _entries(app))
        assert any(e.get("type") == "action" for e in controls)
    finally:
        if consumer is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await session.close()
        app._session = None
        await fake.close()


async def test_wire_refusal_preserved_and_unknown_skill_lists_names(rig) -> None:
    app, _, robot, controls = rig
    robot.refusals["robot.look"] = "motor refuses this angle"
    result = await app.tools.dispatch("look", '{"direction":"haut"}', app)
    assert result == {"ok": False, "reason": "motor refuses this angle"}
    unknown = await app.tools.dispatch("do_skill", '{"name":"fly"}', app)
    assert not unknown["ok"] and "roulade" in unknown["reason"]
    assert "sit_toggle" not in unknown["reason"]
    assert any(e.get("ok") is False for e in controls)


async def test_dynamic_skill_schema_refreshes_and_excludes_toggle(rig) -> None:
    app, feature, robot, _ = rig
    assert "roulade" in await feature.skills_context()
    robot.responses["robot.skills"] = {
        "skills": [{"name": "new_skill"}],
        "built_in": ["sit_toggle", "ground_pick"],
    }
    assert await feature.skills_context() == "new_skill, ground_pick"
    definition = next(d for d in app.tools.definitions() if d["name"] == "do_skill")
    assert definition["parameters"]["properties"]["name"]["enum"] == ["new_skill", "ground_pick"]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("move", '{"vx":true,"vy":0,"vyaw":0,"duration_s":1}'),
        ("move", '{"vx":NaN,"vy":0,"vyaw":0,"duration_s":1}'),
        ("move", '{"vx":0.1}'),
        ("turn", '{"degrees":90,"surprise":1}'),
        ("look", '{"direction":"haut","x":1,"y":0,"z":1}'),
        ("look", '{"x":1}'),
        ("look", '{"direction":"ailleurs"}'),
        ("quack", '{"tag":"greet","hold_s":1}'),
        ("quack", '{"tag":"wheee","hold_s":4}'),
        ("quack", '{"tag":"bad"}'),
        ("set_pose", '{"z":0,"roll":"bad","pitch":0}'),
    ],
)
async def test_bad_arguments_refused_before_transport(rig, name: str, arguments: str) -> None:
    app, _, robot, _ = rig
    before = len(robot.records)
    assert not (await app.tools.dispatch(name, arguments, app))["ok"]
    assert len(robot.records) == before


async def test_governor_clamps_even_when_caller_ignores_schema(rig) -> None:
    app, _, _, _ = rig
    result = await app.tools.dispatch("move", '{"vx":2,"vy":0,"vyaw":0,"duration_s":10}', app)
    assert result["ok"] and result["clamped"] == {"vx": 0.25, "duration_s": 5}
    pose = await app.tools.dispatch("set_pose", '{"z":1,"roll":1,"pitch":-1}', app)
    assert pose["ok"] and pose["clamped"] == {"z": 0.01, "roll": 0.26, "pitch": -0.26}


async def test_pose_sitting_look_status_and_stub_quack(rig) -> None:
    app, feature, robot, _ = rig
    for name, arguments in [
        ("set_pose", '{"z":0,"roll":0.1,"pitch":0}'),
        ("reset_pose", "{}"),
        ("sit", "{}"),
        ("sit", "{}"),
        ("stand", "{}"),
        ("look", '{"x":1,"y":0,"z":0.3,"hold_s":0.1}'),
        ("quack", '{"tag":"wheee","hold_s":2}'),
    ]:
        assert (await app.tools.dispatch(name, arguments, app))["ok"]
    assert sum(c.method == "robot.do" for c in robot.records) == 2
    assert "debout" in (await app.tools.dispatch("status", "{}", app))["detail"]
    robot.safety["fallen"] = True
    await _until(lambda: feature.link.state()["safety"]["fallen"])
    assert "tombe" in (await app.tools.dispatch("status", "{}", app))["detail"]
    assert "tout va bien" not in (await app.tools.dispatch("status", "{}", app))["detail"]
    assert not any(c.method == "robot.sound" for c in robot.records)
    assert any(e["type"] == "quack_stub" for e in _entries(app))


async def test_page_stop_precedes_blocked_flush_and_cancels_motion(rig) -> None:
    app, feature, robot, _ = rig
    assert (await app.tools.dispatch("move", '{"vx":0.2,"vy":0,"vyaw":0,"duration_s":5}', app))[
        "ok"
    ]
    await _until(lambda: any(c.method == "robot.move" for c in robot.records))
    app.audio.release_flush.clear()
    before = len(robot.records)
    task = asyncio.create_task(app.stop_pressed())
    try:
        await _until(lambda: any(c.method == "robot.stop" for c in robot.records[before:]))
        assert not task.done()
        await _until(lambda: not feature.governor.moving)
        assert robot.twist == [0, 0, 0]
    finally:
        app.audio.release_flush.set()
        await task


async def test_sleep_resets_pose_and_closes_mouth_and_shutdown_closes_transport(rig) -> None:
    app, feature, robot, _ = rig
    await app.request_sleep()
    await _until(lambda: not app._stop_tasks)
    assert any(c.method == "robot.pose" and c.params == {"active": False} for c in robot.records)
    assert any(c.method == "robot.mouth" and c.params == {"open": 0} for c in robot.records)
    await app.stop()
    assert not feature.link.connected
    assert feature.governor._monitor is None


class AnnouncementSession(RealtimeSession):
    def __init__(self) -> None:
        super().__init__(_settings())
        self.notices: list[str] = []
        self.results: list[tuple[str, str]] = []
        self.resumes = 0
        self.cancelled = False

    async def announce(self, message: str) -> None:
        self.notices.append(message)
        self.resumes += 1

    async def create_response(self) -> None:
        self.resumes += 1

    async def send_tool_result(self, call_id: str, result: str) -> None:
        self.results.append((call_id, result))

    async def close(self) -> None:
        pass

    async def cancel(self) -> None:
        self.cancelled = True


async def test_link_loss_announced_once_per_outage_and_tools_refuse(rig) -> None:
    app, feature, robot, _ = rig
    session = AnnouncementSession()
    app._session = session
    await robot.disconnect_clients()
    await _until(lambda: not feature.link.connected and bool(session.notices))
    for _ in range(2):
        assert await app.tools.dispatch("status", "{}", app) == {"ok": False, "reason": LINK_LOST}
    assert session.notices == [LINK_LOST]
    await _until(lambda: feature.link.connected)
    assert (await app.tools.dispatch("status", "{}", app))["ok"]
    assert not feature._lost


async def test_notice_uses_single_batch_resume_after_all_results_and_parent(rig) -> None:
    app, _, _, _ = rig
    session = AnnouncementSession()
    app._session = session
    release = asyncio.Event()

    async def delayed(_app: App, arguments: dict) -> dict:
        await release.wait()
        return {"ok": True, "detail": str(arguments)}

    app.register_tool("delayed", "Delay", {"type": "object"}, delayed)
    await app._handle_realtime_event(ResponseStarted(response_id="parent"))
    await app._handle_realtime_event(
        ToolCall(name="delayed", call_id="slow", arguments="{}", response_id="parent")
    )
    await app._handle_realtime_event(
        ToolCall(name="status", call_id="fast", arguments="{}", response_id="parent")
    )
    await app.announce(LINK_LOST)
    await app._handle_realtime_event(ResponseDone(response_id="parent", status="completed"))
    await asyncio.sleep(0.02)
    assert session.resumes == 0
    release.set()
    await _until(lambda: session.resumes == 1)
    assert [call_id for call_id, _ in session.results] == ["slow", "fast"]
    assert session.notices == [LINK_LOST]


async def test_stop_discards_pending_notice_and_batch(rig) -> None:
    app, _, _, _ = rig
    session = AnnouncementSession()
    app._session = session
    await app._handle_realtime_event(ResponseStarted(response_id="parent"))
    await app.announce(LINK_LOST)
    await app.stop_pressed()
    await app._handle_realtime_event(ResponseDone(response_id="parent", status="completed"))
    assert session.resumes == 0
    assert app._announcement is None


async def test_initial_connection_failure_does_not_prevent_app_cleanup(tmp_path: Path) -> None:
    settings = Settings(robot_link="tcp://127.0.0.1:1")
    feature = RobotFeature()
    app = App(settings, _Audio(), Journal(tmp_path), features=())
    await feature.install(app)
    assert feature.runtime is not None
    runtime = feature.runtime
    await _until(lambda: runtime._lost)
    assert await app.tools.dispatch("status", "{}", app) == {"ok": False, "reason": LINK_LOST}
    await app.stop()
    assert not runtime.link.connected


async def test_shutdown_hook_runs_after_priority_stop_and_even_with_slow_audio(
    tmp_path: Path,
) -> None:
    robot = FakeRobotLink()
    feature = RobotFeature(robot)
    audio = _Audio()
    app = App(_settings(), audio, Journal(tmp_path), features=())
    await feature.install(app)
    audio.release_flush.clear()
    await app.stop()
    assert not robot.connected
    assert any(c.method == "robot.stop" for c in robot.records)


async def test_announcement_wire_contract_does_not_send_function_outputs() -> None:
    fake = FakeRealtimeServer()
    await fake.start()
    session = RealtimeSession(_settings(), base_url=fake.websocket_url)
    try:
        await session.open("Test", [])
        await session.announce(LINK_LOST)
        await _until(lambda: any(e["type"] == "response.create" for e in fake.received))
        response = next(e["response"] for e in fake.received if e["type"] == "response.create")
        assert response["tool_choice"] == "none"
        assert LINK_LOST in response["instructions"]
        assert not any(e["type"] == "conversation.item.create" for e in fake.received)
    finally:
        await session.close()
        await fake.close()


async def test_refusals_reach_realtime_in_order_with_one_resume(rig) -> None:
    app, feature, robot, _ = rig
    robot.refusals["robot.look"] = "motor refused"
    fake = ScriptedRealtime([[("look", {"direction": "haut"}), ("do_skill", {"name": "fly"})]])
    await fake.start()
    session = RealtimeSession(app.settings, base_url=fake.websocket_url)
    app._session = session

    async def consume() -> None:
        async for event in session.events():
            await app._handle_realtime_event(event)

    consumer = None
    try:
        await session.open(await feature.skills_context(), app.tools.definitions())
        consumer = asyncio.create_task(consume())
        await session.send_audio(bytes(960))
        await _until(lambda: any(e["type"] == "response.create" for e in fake.received))
        outputs = [
            json.loads(e["item"]["output"])
            for e in fake.received
            if e["type"] == "conversation.item.create"
        ]
        assert outputs[0] == {"ok": False, "reason": "motor refused"}
        assert outputs[1]["ok"] is False and "roulade" in outputs[1]["reason"]
        assert sum(e["type"] == "response.create" for e in fake.received) == 1
    finally:
        if consumer is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await session.close()
        app._session = None
        await fake.close()


async def test_notice_after_normal_response_finishes_playback(rig) -> None:
    app, _, _, _ = rig
    session = AnnouncementSession()
    app._session = session
    await app._handle_realtime_event(ResponseStarted(response_id="spoken"))
    await app.announce(LINK_LOST)
    assert not session.notices
    await app._handle_realtime_event(ResponseDone(response_id="spoken", status="completed"))
    await _until(lambda: bool(session.notices))
    assert session.resumes == 1


async def test_notice_after_failed_parent_does_not_resume_cancelled_tools(rig) -> None:
    app, _, _, _ = rig
    session = AnnouncementSession()
    app._session = session
    await app._handle_realtime_event(ResponseStarted(response_id="failed"))
    await app.announce(LINK_LOST)
    await app._handle_realtime_event(ResponseDone(response_id="failed", status="failed"))
    await _until(lambda: bool(session.notices))
    assert session.resumes == 1
    assert session.results == []


async def test_default_installer_keeps_app_resources_independent(rig, tmp_path: Path) -> None:
    app, _, _, _ = rig
    installer = RobotFeature()
    first = App(app.settings, _Audio(), Journal(tmp_path / "first"), features=())
    second = App(app.settings, _Audio(), Journal(tmp_path / "second"), features=())
    try:
        await installer.install(first)
        first_runtime = installer.runtime
        assert first_runtime is not None
        await installer.install(second)
        second_runtime = installer.runtime
        assert second_runtime is not None
        await _until(lambda: first_runtime.link.connected and second_runtime.link.connected)
        assert first_runtime.link is not second_runtime.link
        await first.stop()
        assert not first_runtime.link.connected
        assert second_runtime.link.connected
    finally:
        await first.stop()
        await second.stop()


async def test_stop_cancels_an_idle_notice_already_waiting_in_transport(rig) -> None:
    app, _, _, _ = rig
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockedSession(AnnouncementSession):
        async def announce(self, message: str) -> None:
            started.set()
            await release.wait()
            await super().announce(message)

    session = BlockedSession()
    app._session = session
    task = asyncio.create_task(app.announce(LINK_LOST))
    await asyncio.wait_for(started.wait(), 1)
    await app.stop_pressed()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert session.resumes == 0


async def test_notice_transport_failure_is_explicit_and_ends_speaking(rig) -> None:
    app, _, _, _ = rig

    class FailedSession(AnnouncementSession):
        async def announce(self, message: str) -> None:
            raise RealtimeConnectionError("notice socket closed")

    app._session = FailedSession()
    await app.announce(LINK_LOST)
    assert not app.state.response_active
    assert not app.state.speaking
    assert any(e.get("operation") == "robot announcement" for e in _entries(app))
