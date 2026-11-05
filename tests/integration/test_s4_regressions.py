import asyncio
import json
import socket
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from coincoin.app import App
from coincoin.audio.browser import BrowserAudio
from coincoin.clock import FakeClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.realtime.events import (
    AudioDelta,
    ResponseDone,
    ResponseStarted,
    SpeechStarted,
    ToolCall,
)
from coincoin.realtime.session import RealtimeConnectionError, RealtimeSession
from coincoin.statemachine import Event, Mode, State
from coincoin.wake.engine import WakeHit
from coincoin.wake.vad import VadEvent
from coincoin.web.server import create_app


def _settings() -> Settings:
    return Settings.model_construct(
        openai_api_key=SecretStr("local-test-key"),
        openai_realtime_model="gpt-realtime-2.1",
        openai_realtime_voice="marin",
    )


class _Audio:
    def __init__(self) -> None:
        self.progress = 0
        self.flush_started = asyncio.Event()
        self.release_flush = asyncio.Event()
        self.release_flush.set()

    def mic_frames(self):
        async def frames():
            await asyncio.Future()
            yield b""

        return frames()

    async def play(self, pcm: bytes) -> None:
        self.progress += len(pcm) // 48

    async def flush(self) -> None:
        self.flush_started.set()
        await self.release_flush.wait()
        self.progress = 0

    @property
    def played_ms(self) -> int:
        return self.progress

    @property
    def playing(self) -> bool:
        return self.progress > 0


class _FrameAudio(_Audio):
    def __init__(self, frames: list[bytes]) -> None:
        super().__init__()
        self.frames = frames

    def mic_frames(self):
        async def frames():
            for frame in self.frames:
                yield frame

        return frames()


class _RecordingVad:
    def __init__(self) -> None:
        self.reset_count = 0

    def feed(self, pcm16_16k: bytes) -> list[VadEvent]:
        _ = pcm16_16k
        return []

    def reset(self) -> None:
        self.reset_count += 1


class _Session:
    def __init__(
        self,
        *,
        open_started: asyncio.Event | None = None,
        open_release: asyncio.Event | None = None,
        cancel_release: asyncio.Event | None = None,
    ) -> None:
        self.open_started = open_started or asyncio.Event()
        self.open_release = open_release or asyncio.Event()
        if open_release is None:
            self.open_release.set()
        self.cancel_started = asyncio.Event()
        self.cancel_release = cancel_release or asyncio.Event()
        if cancel_release is None:
            self.cancel_release.set()
        self.truncated = asyncio.Event()
        self.events_closed = asyncio.Event()
        self.closed = False
        self.instructions = ""
        self.audio: list[bytes] = []
        self.tool_results: list[tuple[str, str]] = []
        self.responses_created = 0
        self.truncation: tuple[str, int] | None = None
        self.session_expires_at = None

    async def open(self, instructions: str, _tools: list[dict[str, object]]) -> None:
        self.instructions = instructions
        self.open_started.set()
        await self.open_release.wait()

    async def events(self):
        await self.events_closed.wait()
        if False:
            yield ResponseDone(status="completed")

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def cancel(self) -> None:
        self.cancel_started.set()
        await self.cancel_release.wait()

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        self.truncation = item_id, audio_end_ms
        self.truncated.set()

    async def send_tool_result(self, call_id: str, result: str) -> None:
        self.tool_results.append((call_id, result))

    async def create_response(self) -> None:
        self.responses_created += 1

    async def close(self) -> None:
        self.closed = True
        self.events_closed.set()


async def _cancel_app_tasks(app: App) -> None:
    for task in tuple(app._tasks):
        task.cancel()
    if app._tasks:
        await asyncio.gather(*tuple(app._tasks), return_exceptions=True)


async def test_stop_starts_robot_hook_before_flush_cancel_and_blocked_ui(
    tmp_path: Path,
) -> None:
    order: list[str] = []
    audio = _Audio()
    audio.release_flush.clear()
    cancel_release = asyncio.Event()
    session = _Session(cancel_release=cancel_release)
    control_started = asyncio.Event()

    async def control(_message: dict[str, object]) -> None:
        order.append("ui")
        control_started.set()
        await asyncio.Future()

    app = App(_settings(), audio, Journal(log_dir=tmp_path / "logs"), control=control)
    app._session = session  # type: ignore[assignment]
    app._active_response_id = "response"
    app.state = State(Mode.AWAKE, speaking=True, response_active=True)
    robot_started = asyncio.Event()
    robot_release = asyncio.Event()

    async def stop_hook() -> None:
        order.append("robot")
        robot_started.set()
        await robot_release.wait()

    app.on("stop", stop_hook)
    stop_task = asyncio.create_task(app.stop_pressed())
    try:
        await asyncio.wait_for(robot_started.wait(), timeout=0.2)
        await asyncio.wait_for(audio.flush_started.wait(), timeout=0.2)
        assert order[0] == "robot"
        assert not robot_release.is_set()

        audio.release_flush.set()
        await asyncio.wait_for(session.cancel_started.wait(), timeout=0.2)
        await asyncio.wait_for(control_started.wait(), timeout=0.2)
        await asyncio.wait_for(stop_task, timeout=0.5)
        assert order.index("robot") < order.index("ui")
        assert app.mode == Mode.SLEEPING
        assert not app.state.speaking
    finally:
        robot_release.set()
        cancel_release.set()
        audio.release_flush.set()
        if not stop_task.done():
            stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        await _cancel_app_tasks(app)


async def test_stop_invalidates_opening_session_and_discards_old_microphone_audio(
    tmp_path: Path,
) -> None:
    opening = asyncio.Event()
    release_open = asyncio.Event()
    session = _Session(open_started=opening, open_release=release_open)
    audio = _Audio()
    vad = _RecordingVad()
    app = App(
        _settings(),
        audio,
        Journal(log_dir=tmp_path / "logs"),
        session_factory=lambda _settings: session,  # type: ignore[arg-type]
        vad=vad,
    )
    app.audio_gate.process(b"old-microphone", Mode.AWAKE)
    assert app.audio_gate.buffered_bytes > 0

    await app._apply(Event("wake"))
    await asyncio.wait_for(opening.wait(), timeout=0.2)
    await app.stop_pressed()
    assert app.audio_gate.buffered_bytes == 0
    assert vad.reset_count == 2
    assert app._session is None

    release_open.set()
    for _ in range(100):
        if session.closed:
            break
        await asyncio.sleep(0)
    assert session.closed
    assert app._session is None

    new_session = _Session()
    app.session_factory = lambda _settings: new_session  # type: ignore[assignment]
    app.state = State(Mode.SLEEPING, wake_enabled=True)
    await app._apply(Event("wake"))
    await asyncio.wait_for(new_session.open_started.wait(), timeout=0.2)
    for _ in range(100):
        if app._session is new_session:
            break
        await asyncio.sleep(0)
    new_frame = b"new-microphone"
    await app._send_audio(new_frame)
    assert new_session.audio == [new_frame]
    await app.stop_pressed()
    await _cancel_app_tasks(app)


async def test_audio_gate_preserves_order_while_open_and_microphone_race(
    tmp_path: Path,
) -> None:
    aa, bb, cc = b"aa", b"bb", b"cc"
    allow_cc = asyncio.Event()

    class _OrderedAudio(_Audio):
        def mic_frames(self):
            async def frames():
                yield aa
                yield bb
                await allow_cc.wait()
                yield cc

            return frames()

    audio = _OrderedAudio()
    open_release = asyncio.Event()
    session = _Session(open_release=open_release)
    app = App(
        _settings(),
        audio,
        Journal(log_dir=tmp_path / "logs"),
        session_factory=lambda _settings: session,  # type: ignore[arg-type]
    )
    app.state = State(Mode.AWAKE)
    mic_task = asyncio.create_task(app._audio_loop())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    open_task = asyncio.create_task(app._session_loop())
    await asyncio.wait_for(session.open_started.wait(), timeout=0.2)
    allow_cc.set()
    open_release.set()
    await asyncio.wait_for(mic_task, timeout=0.2)
    for _ in range(100):
        if len(session.audio) == 3:
            break
        await asyncio.sleep(0)
    assert session.audio == [aa, bb, cc]
    open_task.cancel()
    await asyncio.gather(open_task, return_exceptions=True)
    await _cancel_app_tasks(app)


async def test_barge_in_flushes_before_hook_and_truncates_heard_item_in_samples(
    tmp_path: Path,
) -> None:
    audio = _Audio()
    audio.progress = 50
    session = _Session()
    app = App(_settings(), audio, Journal(log_dir=tmp_path / "logs"))
    app._session = session  # type: ignore[assignment]
    app._active_response_id = "r1"
    app.state = State(Mode.AWAKE, speaking=True, response_active=True)
    app._ignore_audio_until_response = False
    await app._handle_realtime_event(AudioDelta(item_id="first", pcm=bytes(4800), response_id="r1"))
    await app._handle_realtime_event(
        AudioDelta(item_id="second", pcm=bytes(2400), response_id="r1")
    )
    hook_started = asyncio.Event()
    hook_release = asyncio.Event()
    app.on("barge_in", lambda: _block_hook(hook_started, hook_release))

    barge_task = asyncio.create_task(app._apply(Event("user_speech_start")))
    await asyncio.wait_for(hook_started.wait(), timeout=0.2)
    assert audio.flush_started.is_set()
    await app._handle_realtime_event(AudioDelta(item_id="stale", pcm=bytes(480), response_id="r1"))
    assert app._play_queue.empty()
    hook_release.set()
    await asyncio.wait_for(barge_task, timeout=0.2)
    await asyncio.wait_for(session.truncated.wait(), timeout=0.2)
    assert session.truncation == ("first", 50)
    await _cancel_app_tasks(app)


async def _block_hook(started: asyncio.Event, release: asyncio.Event) -> None:
    started.set()
    await release.wait()


async def test_barge_in_counts_fragmented_audio_without_rounding_each_delta(
    tmp_path: Path,
) -> None:
    audio = _Audio()
    audio.progress = 2
    session = _Session()
    app = App(_settings(), audio, Journal(log_dir=tmp_path / "logs"))
    app._session = session  # type: ignore[assignment]
    app._active_response_id = "r1"
    app._ignore_audio_until_response = False
    app.state = State(Mode.AWAKE, speaking=True, response_active=True)
    for _ in range(48):
        await app._handle_realtime_event(
            AudioDelta(item_id="item", pcm=b"\x00\x00", response_id="r1")
        )

    await app._apply(Event("user_speech_start"))
    await asyncio.wait_for(session.truncated.wait(), timeout=0.2)
    assert session.truncation == ("item", 2)
    await _cancel_app_tasks(app)


async def test_slow_tool_does_not_block_server_speech_interruption(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    audio = _Audio()
    session = _Session()
    app = App(_settings(), audio, Journal(log_dir=tmp_path / "logs"))
    app._session = session  # type: ignore[assignment]
    app.state = State(Mode.AWAKE, speaking=True, response_active=True)

    async def slow(_app: App, _args: dict[str, object]) -> dict[str, object]:
        started.set()
        await release.wait()
        return {"ok": True}

    app.register_tool("slow_test", "Slow test", {"type": "object"}, slow)
    await app._handle_realtime_event(ToolCall(call_id="call-1", name="slow_test", arguments="{}"))
    await asyncio.wait_for(started.wait(), timeout=0.2)
    await asyncio.wait_for(app._handle_realtime_event(SpeechStarted()), timeout=0.2)
    assert audio.flush_started.is_set()
    assert not app.state.speaking
    assert session.tool_results == []
    release.set()
    await _cancel_app_tasks(app)


async def test_tool_refusal_is_sent_unchanged_to_realtime_and_journal(tmp_path: Path) -> None:
    session = _Session()
    journal = Journal(log_dir=tmp_path / "logs")
    app = App(_settings(), _Audio(), journal)
    app._session = session  # type: ignore[assignment]
    app.state = State(Mode.AWAKE)

    async def refuse(_app: App, _args: dict[str, object]) -> dict[str, object]:
        return {"ok": False, "reason": "fallen"}

    app.register_tool("refuse_test", "Refusal test", {"type": "object"}, refuse)
    await app._handle_realtime_event(ToolCall(call_id="call-2", name="refuse_test", arguments="{}"))
    await app._handle_realtime_event(ResponseDone(status="completed"))
    for _ in range(100):
        if session.tool_results:
            break
        await asyncio.sleep(0)
    assert session.tool_results == [("call-2", json.dumps({"ok": False, "reason": "fallen"}))]
    entries = [
        json.loads(line)
        for path in journal.log_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        entry.get("type") == "tool_result" and entry["result"]["reason"] == "fallen"
        for entry in entries
    )
    await _cancel_app_tasks(app)


async def test_response_failure_clears_speaking_state_and_updates_ui(tmp_path: Path) -> None:
    controls: list[dict[str, object]] = []

    async def control(message: dict[str, object]) -> None:
        controls.append(message)

    app = App(_settings(), _Audio(), Journal(log_dir=tmp_path / "logs"), control=control)
    app.state = State(Mode.AWAKE)
    await app._handle_realtime_event(ResponseStarted(response_id="failed-response"))
    assert app.state.speaking and app.state.response_active
    await app._handle_realtime_event(ResponseDone(status="failed", response_id="failed-response"))
    assert not app.state.speaking
    assert not app.state.response_active
    assert controls[-1] == {"type": "state", "state": "AWAKE", "speaking": False}
    await _cancel_app_tasks(app)


async def test_followup_timeout_emits_sleep_hook(tmp_path: Path) -> None:
    app = App(_settings(), _Audio(), Journal(log_dir=tmp_path / "logs"), clock=FakeClock(10))
    hooks: list[str] = []
    app.on("sleep", lambda summary: hooks.append(summary))
    app.state = State(Mode.FOLLOWUP, followup_deadline=10)

    await app._apply(Event("tick"))

    assert app.mode == Mode.SLEEPING
    assert hooks == [""]
    await _cancel_app_tasks(app)


async def test_memory_hook_finishes_before_open_while_microphone_is_buffered(
    tmp_path: Path,
) -> None:
    hook_started = asyncio.Event()
    hook_release = asyncio.Event()
    memory = {"value": ""}
    audio_frame = bytes(960)
    audio = _FrameAudio([audio_frame])
    session = _Session()
    app = App(
        _settings(),
        audio,
        Journal(log_dir=tmp_path / "logs"),
        session_factory=lambda _settings: session,  # type: ignore[arg-type]
    )
    app.instructions_context("memory_context", lambda: memory["value"])

    async def brain_start() -> None:
        hook_started.set()
        await hook_release.wait()
        memory["value"] = "loaded-memory"

    app.on("brain_session_start", brain_start)
    await app._apply(Event("wake"))
    await asyncio.wait_for(hook_started.wait(), timeout=0.2)
    await app._audio_loop()
    assert app.audio_gate.buffered_bytes == len(audio_frame)
    assert not session.open_started.is_set()

    hook_release.set()
    await asyncio.wait_for(session.open_started.wait(), timeout=0.2)
    for _ in range(100):
        if app._session is session and session.audio:
            break
        await asyncio.sleep(0)
    assert "loaded-memory" in session.instructions
    assert session.audio == [audio_frame]
    await app.stop_pressed()
    await _cancel_app_tasks(app)


async def test_followup_audio_logs_transmitted_bytes_only_after_transport_accepts(
    tmp_path: Path,
) -> None:
    journal = Journal(log_dir=tmp_path / "logs")
    session = _Session()
    app = App(_settings(), _Audio(), journal)
    app._session = session  # type: ignore[assignment]
    app.state = State(Mode.FOLLOWUP)
    await app._handle_vad_event(
        VadEvent(kind="speech_frame", timestamp_s=0.0, pcm16=bytes(512 * 2))
    )
    assert len(session.audio) == 1
    assert len(session.audio[0]) == 1536
    entries = [
        json.loads(line)
        for path in journal.log_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    audio_entry = next(entry for entry in entries if entry["type"] == "audio_bytes")
    assert audio_entry["type"] == "audio_bytes"
    assert audio_entry["state"] == "FOLLOWUP"
    assert audio_entry["bytes"] == 1536
    assert audio_entry["totals"] == {"FOLLOWUP": 1536}


def _closed_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def test_local_tcp_refusal_is_normalized_and_reconnects(tmp_path: Path) -> None:
    port = _closed_local_port()
    session = RealtimeSession(_settings(), base_url=f"ws://127.0.0.1:{port}/v1/realtime")
    with pytest.raises(RealtimeConnectionError, match="connection failed"):
        await session.open("local test", [])
    assert session._client is None
    assert not session.connected

    attempts = 0
    app_holder: dict[str, App] = {}

    def factory(settings: Settings):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return RealtimeSession(settings, base_url=f"ws://127.0.0.1:{port}/v1/realtime")
        return _ReconnectSession(app_holder["app"])

    app = App(
        _settings(),
        _Audio(),
        Journal(log_dir=tmp_path / "logs"),
        session_factory=factory,  # type: ignore[arg-type]
    )
    app_holder["app"] = app
    app.state = State(Mode.AWAKE)
    await app._session_loop()
    assert attempts == 2
    assert app.mode == Mode.SLEEPING
    await _cancel_app_tasks(app)


async def test_response_state_is_cleared_when_connection_drops_during_reconnect(
    tmp_path: Path,
) -> None:
    attempts = 0
    app_holder: dict[str, App] = {}

    def factory(_settings: Settings):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _DroppedResponseSession()
        return _ReconnectSession(app_holder["app"])

    app = App(
        _settings(),
        _Audio(),
        Journal(log_dir=tmp_path / "logs"),
        session_factory=factory,  # type: ignore[arg-type]
    )
    app_holder["app"] = app
    app.state = State(Mode.AWAKE, speaking=True, response_active=True)
    await app._session_loop()

    assert attempts == 2
    assert not app.state.response_active
    assert not app.state.speaking
    await _cancel_app_tasks(app)


class _ReconnectSession(_Session):
    def __init__(self, app: App) -> None:
        super().__init__()
        self.app = app

    async def events(self):
        self.app.state = State(Mode.SLEEPING)
        if False:
            yield ResponseDone(status="completed")


class _DroppedResponseSession(_Session):
    async def events(self):
        raise RealtimeConnectionError("fixture disconnected during response")
        yield ResponseDone(status="completed")


async def test_ui_connection_syncs_mode_and_speaking_and_stop_works_while_sleeping(
    tmp_path: Path,
) -> None:
    audio = BrowserAudio()
    app = App(
        _settings(),
        audio,
        Journal(log_dir=tmp_path / "logs"),
        features=(),
        wake_engine=_NoWake(),
        vad=_NoVad(),
        control=audio.send_control,
    )
    stop_hook = asyncio.Event()
    app.on("stop", lambda: stop_hook.set())
    async with TestClient(TestServer(create_app(audio, conversation=app))) as client:
        audio_ws = await client.ws_connect("/ws/audio")
        app.state = State(Mode.SLEEPING, speaking=True, wake_enabled=False)
        ui_ws = await client.ws_connect("/ws/ui")
        assert await ui_ws.receive_json() == {
            "type": "state",
            "state": "SLEEPING",
            "speaking": True,
        }

        await ui_ws.send_json({"type": "stop"})
        flush = await asyncio.wait_for(ui_ws.receive_json(), timeout=0.2)
        assert flush["type"] == "flush"
        await ui_ws.send_json({"type": "flushed", "generation": flush["generation"]})
        assert await asyncio.wait_for(ui_ws.receive_json(), timeout=0.2) == {
            "type": "flush_complete",
            "generation": flush["generation"],
        }
        state = await asyncio.wait_for(ui_ws.receive_json(), timeout=0.2)
        assert state == {"type": "state", "state": "SLEEPING", "speaking": False}
        await asyncio.wait_for(stop_hook.wait(), timeout=0.2)
        await ui_ws.close()
        await audio_ws.close()


class _NoWake:
    def feed(self, pcm16_16k: bytes) -> WakeHit | None:
        _ = pcm16_16k
        return None

    def reset(self) -> None:
        return


class _NoVad:
    def feed(self, pcm16_16k: bytes) -> list[VadEvent]:
        _ = pcm16_16k
        return []

    def reset(self) -> None:
        return


async def test_ui_error_delivery_failure_is_bounded_and_not_recursive(tmp_path: Path) -> None:
    calls = 0

    async def broken_control(_message: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        raise ConnectionError("UI disconnected")

    app = App(_settings(), _Audio(), Journal(log_dir=tmp_path / "logs"), control=broken_control)
    app._report_error("test", RuntimeError("single incident"))
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert calls == 1
    await _cancel_app_tasks(app)
