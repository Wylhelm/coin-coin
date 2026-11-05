"""Offline protocol regression, not an acoustic/browser or physical robot test."""

import asyncio
import base64
import json
import struct
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientWebSocketResponse, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from pydantic import SecretStr

import coincoin.app as app_module
from coincoin.app import App
from coincoin.audio.browser import BrowserAudio
from coincoin.audio.mixer import OutputMixer
from coincoin.brain.feature import BrainFeature
from coincoin.features import FEATURES
from coincoin.journal import Journal
from coincoin.realtime.session import RealtimeSession
from coincoin.robot.feature import RobotFeature
from coincoin.robot.link import Frame
from coincoin.robot.quack import StreamQuack
from coincoin.robot.tcp_link import TcpLink
from coincoin.robot.voice_feature import VoiceFeature
from coincoin.statemachine import Mode
from coincoin.wake.engine import WakeHit
from coincoin.web.server import create_app
from tests.fakes.fake_openclaw import FakeOpenClaw
from tests.fakes.fake_realtime import FakeRealtimeServer
from tests.fakes.fake_robotd import FakeRobotd
from tests.integration.test_s4_regressions import _RecordingVad, _settings
from tests.integration.test_see_tool import CameraLink
from tests.integration.test_tools_e2e_fakes import _entries, _until
from tests.integration.test_voice_feature import _sound_bank


class _RobotPeer(FakeRobotd):
    def __init__(self) -> None:
        super().__init__()
        self.final_close: Callable[[], bool] = lambda: False
        self.close_pending = asyncio.Event()
        self.release_close = asyncio.Event()
        self.hold_close = False

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if (
            self.hold_close
            and method == "robot.mouth"
            and params == {"open": 0.0}
            and self.final_close()
        ):
            self.hold_close = False
            self.close_pending.set()
            await self.release_close.wait()
        await super().notify(method, params)


class _ControlledWake:
    def __init__(self) -> None:
        self.armed = False
        self.frames: list[bytes] = []

    def feed(self, pcm16_16k: bytes) -> WakeHit | None:
        self.frames.append(pcm16_16k)
        if self.armed:
            self.armed = False
            return WakeHit(1.0, 0, "fr")
        return None

    def reset(self) -> None:
        pass


class _RealtimePeer(FakeRealtimeServer):
    """Use the shared GA handshake, with test-controlled server events."""

    def __init__(self) -> None:
        super().__init__()
        for event in self.events[:2]:
            event["session"]["expires_at"] = int(time.time()) + 3_600
        self.peer: web.WebSocketResponse | None = None
        self.disconnected = asyncio.Event()

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        peer = web.WebSocketResponse()
        await peer.prepare(request)
        self.peer = peer
        try:
            await peer.send_json(self.events[0])
            async for message in peer:
                if message.type != WSMsgType.TEXT:
                    continue
                event = message.json()
                self.received.append(event)
                if event["type"] == "session.update":
                    await peer.send_json(self.events[1])
                elif event["type"] == "input_audio_buffer.append":
                    self.audio_input.extend(base64.b64decode(event["audio"]))
        finally:
            self.disconnected.set()
        return peer

    async def send(self, *events: dict[str, Any]) -> None:
        assert self.peer is not None
        for event in events:
            await self.peer.send_json(event)

    async def tools(self, turn: str, calls: list[tuple[str, dict[str, Any]]]) -> None:
        await self.send({"type": "response.created", "response": {"id": turn}})
        for index, (name, arguments) in enumerate(calls):
            await self.send(
                {
                    "type": "response.output_item.done",
                    "response_id": turn,
                    "item": {
                        "type": "function_call",
                        "name": name,
                        "call_id": f"{turn}-{index}",
                        "arguments": json.dumps(arguments),
                    },
                }
            )
        await self.send({"type": "response.done", "response": {"id": turn, "status": "completed"}})


class _BrowserPeer:
    """Acknowledge playback/flush over the real UI route, without a real speaker."""

    def __init__(self, audio: ClientWebSocketResponse, ui: ClientWebSocketResponse) -> None:
        self.audio = audio
        self.ui = ui
        self.controls: list[dict[str, Any]] = []
        self.packets: list[bytes] = []
        self.generation = 0
        self.samples = 0
        self.discard_through = 0
        self.ack_playback = True
        self.hold_samples: int | None = None
        self.playback_pending = asyncio.Event()
        self.release_playback = asyncio.Event()
        self.tasks = [asyncio.create_task(self._audio()), asyncio.create_task(self._ui())]

    async def _audio(self) -> None:
        async for message in self.audio:
            assert message.type == WSMsgType.BINARY
            packet = message.data
            self.packets.append(packet)
            if struct.unpack_from("<Q", packet)[0] <= self.discard_through:
                continue
            self.samples += (len(packet) - 8) // 2
            if self.ack_playback:
                if self.samples == self.hold_samples:
                    self.playback_pending.set()
                    await self.release_playback.wait()
                await self.ui.send_json(
                    {
                        "type": "played",
                        "generation": self.generation,
                        "samples": self.samples,
                        "ms": self.samples // 24,
                        "playing": False,
                    }
                )

    async def _ui(self) -> None:
        async for message in self.ui:
            assert message.type == WSMsgType.TEXT
            event = message.json()
            self.controls.append(event)
            if event["type"] == "flush":
                self.generation = event["generation"]
                self.discard_through = int(event["through_sequence"])
                self.samples = 0
                await self.ui.send_json({"type": "flushed", "generation": self.generation})

    async def close(self) -> None:
        await self.audio.close()
        await self.ui.close()
        await asyncio.gather(*self.tasks)


@pytest.mark.parametrize("ending", ["sleep", "stop"])
async def test_joint_features_browser_protocol_and_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    baseline = asyncio.all_tasks()
    robot = _RobotPeer()
    realtime = _RealtimePeer()
    memory = FakeOpenClaw(content="Offline memory context.")
    camera = CameraLink([0])
    wake = _ControlledWake()
    physical = BrowserAudio()
    brain, robot_feature = BrainFeature(), RobotFeature()
    bank = tmp_path / "sounds"
    _sound_bank(bank, "greet", "coo", "inquire", "wheee")
    features = [brain, robot_feature, VoiceFeature(bank)]
    # Restore the actual production composition locally; conftest stays isolated.
    assert [type(feature) for feature in FEATURES] == [type(feature) for feature in features]
    monkeypatch.setattr(app_module, "FEATURES", features)

    async def frame(_link: TcpLink) -> Frame:
        return await camera.frame()

    monkeypatch.setattr(TcpLink, "frame", frame)
    async with AsyncExitStack() as stack:
        await robot.start()
        stack.push_async_callback(robot.close_server)
        await realtime.start()
        stack.push_async_callback(realtime.close)
        memory_client = await stack.enter_async_context(TestClient(TestServer(memory.app)))
        settings = _settings()
        settings.robot_link = f"tcp://127.0.0.1:{robot.port}"
        settings.frame_link = "tcp://127.0.0.1:1"  # Camera is injected at the Frame boundary.
        settings.openclaw_url = str(memory_client.make_url("")).rstrip("/")
        settings.openclaw_token = SecretStr("loopback-assembly-token")
        app = App(
            settings,
            physical,
            Journal(tmp_path / "logs"),
            session_factory=lambda config: RealtimeSession(config, base_url=realtime.websocket_url),
            wake_engine=wake,
            vad=_RecordingVad(),
            control=physical.send_control,
        )
        stack.push_async_callback(brain.aclose)
        stack.push_async_callback(app.stop)
        client = await stack.enter_async_context(
            TestClient(TestServer(create_app(physical, conversation=app)))
        )
        browser = _BrowserPeer(
            await client.ws_connect("/ws/audio"), await client.ws_connect("/ws/ui")
        )
        stack.push_async_callback(browser.close)
        # Stop while the protocol peer can still acknowledge the final flush.
        stack.push_async_callback(app.stop)
        runtime = robot_feature.runtime
        assert runtime is not None
        final_mouth_closed = asyncio.Event()

        async def mouth_closed() -> None:
            if app.mode == Mode.SLEEPING and not app.state.speaking:
                # Notifications are unacknowledged. Fence them on the same TCP stream.
                await runtime.link.call("robot.health")
                final_mouth_closed.set()

        app.on("mouth_close", mouth_closed)
        robot.final_close = lambda: app.mode == Mode.SLEEPING and not app.state.speaking
        stack.callback(robot.release_close.set)
        stack.callback(browser.release_playback.set)
        await _until(lambda: runtime.link.connected and runtime.link.state() is not None)
        assert isinstance(runtime.link, TcpLink)
        assert isinstance(runtime.quack, StreamQuack)
        mixer = app.audio
        assert isinstance(mixer, OutputMixer) and mixer.audio is physical
        assert (await client.get("/")).status == 200
        assert await (await client.get("/healthz")).json() == {"status": "ok"}
        assert (await client.get("/static/app.js")).status == 200

        await browser.audio.send_bytes(bytes(960))
        await _until(lambda: bool(wake.frames))
        assert app.mode == Mode.SLEEPING and realtime.peer is None and not memory.requests
        wake.armed = True
        await browser.audio.send_bytes(bytes(960))
        await _until(lambda: app._session is not None and not app.state.quack_active)
        await _until(lambda: physical.playback_progress.played_samples == 4_800)
        update = next(e["session"] for e in realtime.received if e["type"] == "session.update")
        assert "Offline memory context." in update["instructions"]
        assert "roulade" in update["instructions"]
        assert {"move", "quack", "see", "remember", "ask_openclaw", "sleep"} <= {
            tool["name"] for tool in update["tools"]
        }
        assert mixer.playback_progress.played_samples == 0  # Greet is not model speech.
        microphone = struct.pack("<h", 1024) * 480
        await browser.audio.send_bytes(microphone)
        await _until(lambda: len(realtime.audio_input) > 0)
        assert realtime.audio_input == microphone

        await realtime.tools(
            "tools",
            [
                ("move", {"vx": 0.1, "vy": 0, "vyaw": 0, "duration_s": 0.1}),
                ("see", {"question": "What is green?"}),
                ("remember", {"fact": "Assembly test fact."}),
                ("ask_openclaw", {"question": "What do you remember?"}),
                ("quack", {"tag": "wheee"}),
            ],
        )
        await _until(lambda: any(e["type"] == "response.create" for e in realtime.received))
        items = [e["item"] for e in realtime.received if e["type"] == "conversation.item.create"]
        outputs = [item for item in items if item["type"] == "function_call_output"]
        assert [item["call_id"] for item in outputs] == [f"tools-{index}" for index in range(5)]
        assert all(json.loads(item["output"])["ok"] for item in outputs)
        assert sum(e["type"] == "response.create" for e in realtime.received) == 1
        image = next(item for item in items if item["type"] == "message")
        encoded = image["content"][0]["image_url"].split(",", 1)[1]
        assert Image.open(BytesIO(base64.b64decode(encoded))).size == (4, 4)
        assert image["content"][1] == {"type": "input_text", "text": "What is green?"}
        assert items.index(image) < items.index(outputs[1])
        assert camera.calls == 1 and memory.memories == ["Assembly test fact."]
        assert any(
            c.method == "robot.move" and c.params and c.params["vx"] == 0.1 for c in robot.records
        )
        await _until(
            lambda: any(
                c.method == "robot.mouth" and c.params and c.params["open"] > 0
                for c in robot.records
            )
        )
        assert not any(c.method == "robot.sound" for c in robot.records)

        browser.ack_playback = False
        before = len(browser.packets)
        pcm = struct.pack("<h", 4096) * 4_800
        await realtime.send(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "user-item",
                "transcript": "Assembly test user transcript.",
            },
            {"type": "response.created", "response": {"id": "speech"}},
            {
                "type": "response.output_audio_transcript.done",
                "item_id": "spoken-item",
                "transcript": "Assembly test assistant transcript.",
            },
            {
                "type": "response.output_audio.delta",
                "response_id": "speech",
                "item_id": "spoken-item",
                "delta": base64.b64encode(pcm).decode(),
            },
        )
        await _until(lambda: len(browser.packets) == before + 10)
        assert b"".join(packet[8:] for packet in browser.packets[before:]) == pcm
        assert app.state.speaking
        # Report only 40 ms of model speech after the two 200 ms quacks.
        await browser.ui.send_json(
            {"type": "played", "generation": 0, "samples": 10_560, "ms": 440, "playing": True}
        )
        await _until(lambda: mixer.playback_progress.played_samples == 960)
        await realtime.send({"type": "input_audio_buffer.speech_started"})
        await _until(
            lambda: any(e["type"] == "conversation.item.truncate" for e in realtime.received)
        )
        truncate = next(e for e in realtime.received if e["type"] == "conversation.item.truncate")
        assert truncate["item_id"] == "spoken-item" and truncate["audio_end_ms"] == 40
        assert any(e["type"] == "response.cancel" for e in realtime.received)
        await _until(lambda: any(e["type"] == "flush_complete" for e in browser.controls))
        assert not app.state.speaking and app.mode == Mode.AWAKE
        assert mixer.playback_progress.sent_samples == 0
        await realtime.send(
            {
                "type": "response.output_audio.delta",
                "response_id": "speech",
                "item_id": "spoken-item",
                "delta": base64.b64encode(pcm).decode(),
            }
        )

        browser.ack_playback = True
        await realtime.tools("moving", [("move", {"vx": 0.1, "vy": 0, "vyaw": 0, "duration_s": 5})])
        await _until(lambda: sum(e["type"] == "response.create" for e in realtime.received) == 2)
        assert len(browser.packets) == before + 10  # Interrupted audio did not escape.
        await _until(lambda: robot.twist[0] == 0.1)
        stop_at = len(robot.records)
        packets_at_sleep = len(browser.packets)
        flushes_at_sleep = browser.generation
        if ending == "stop":
            await browser.ui.send_json({"type": "stop"})
        else:
            browser.hold_samples = 4_800
            robot.hold_close = True
            await realtime.tools("sleeping", [("sleep", {})])
        await _until(lambda: app.mode == Mode.SLEEPING and app._session is None)
        if ending == "sleep":
            async with asyncio.timeout(3):
                await browser.playback_pending.wait()
                await _until(
                    lambda: any(
                        c.method == "robot.mouth" and c.params and c.params["open"] > 0
                        for c in robot.records[stop_at:]
                    )
                )
                browser.release_playback.set()
                await robot.close_pending.wait()
            # The old readiness predicate is true while the final close is still in flight.
            assert not app.state.speaking and not app._stop_tasks
            assert [c for c in robot.records if c.method == "robot.mouth"][-1].params != {
                "open": 0.0
            }
            assert not final_mouth_closed.is_set()
            robot.release_close.set()
        async with asyncio.timeout(3):
            await final_mouth_closed.wait()
        await _until(lambda: not app.state.speaking and not app._stop_tasks)
        await brain.drain()
        await _until(lambda: realtime.disconnected.is_set())
        assert len(browser.packets) == packets_at_sleep + (10 if ending == "sleep" else 0)
        assert browser.generation == flushes_at_sleep + 1
        assert robot.twist == [0.0, 0.0, 0.0]
        assert any(c.method == "robot.stop" for c in robot.records[stop_at:])
        assert [c for c in robot.records if c.method == "robot.mouth"][-1].params == {"open": 0.0}
        assert any(e["type"] == "state" and e["state"] == "SLEEPING" for e in browser.controls)
        prompts = [request["body"]["messages"][0]["content"] for request in memory.requests]
        assert len(prompts) == 4 and prompts[-1].startswith("[Fin de conversation]")
        assert "move" in prompts[-1] and "remember" in prompts[-1]
        assert "Assembly test user transcript." in prompts[-1]
        assert any(
            e["type"] == "transcript" and e["text"] == "Assembly test assistant transcript."
            for e in browser.controls
        )
        audio_before = len(realtime.audio_input)
        awake_frames = len(wake.frames)
        await _until(lambda: app.state.wake_enabled)
        await runtime.link.call("robot.health")
        assert [c for c in robot.records if c.method == "robot.mouth"][-1].params == {"open": 0.0}
        await browser.audio.send_bytes(bytes(960))
        await _until(lambda: len(wake.frames) > awake_frames)
        assert len(realtime.audio_input) == audio_before
        await app.stop()
        assert not app._tasks and not runtime.link.connected
        assert not brain._tasks
        entries = _entries(app)
        assert not any(e["type"] in {"error", "quack_stub", "tool_error"} for e in entries)
        assert encoded not in json.dumps(entries) and "loopback-assembly-token" not in json.dumps(
            entries
        )

    await _until(lambda: not (asyncio.all_tasks() - baseline))
    assert physical._page is None and not robot.clients and not robot.handlers
    assert robot.server is not None and not robot.server.is_serving()
    assert all(task.done() for task in browser.tasks)
