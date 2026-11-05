import asyncio
import json
import time
from pathlib import Path

from pydantic import SecretStr

from coincoin.app import App
from coincoin.clock import FakeClock
from coincoin.config import Settings
from coincoin.journal import Journal
from coincoin.realtime.events import AudioDelta, ResponseDone, ToolCall
from coincoin.realtime.session import RealtimeConnectionError, RealtimeSession
from coincoin.statemachine import Event, Mode, State
from tests.fakes.fake_realtime import FakeRealtimeServer


def _settings() -> Settings:
    return Settings.model_construct(
        openai_api_key=SecretStr("test-key"),
        openai_realtime_model="gpt-realtime-2.1",
        openai_realtime_voice="marin",
    )


async def test_fake_realtime_replays_real_trace_and_tool_turn(tmp_path: Path) -> None:
    fake = FakeRealtimeServer()
    await fake.start()
    session = RealtimeSession(_settings(), base_url=fake.websocket_url)
    audio = _FakeAudio()
    controls: list[dict[str, object]] = []
    journal = Journal(log_dir=tmp_path / "logs", session_id="test")

    async def control(message: dict[str, object]) -> None:
        controls.append(message)

    app = App(_settings(), audio, journal, control=control)
    app.state = State(Mode.AWAKE)

    async def ping(_app: App, _arguments: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "detail": "pong"}

    app.register_tool(
        "ping",
        "Return the fixture-backed test response.",
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        ping,
    )
    app._session = session
    app._spawn(app._playback_loop())
    try:
        await session.open("Test instructions", app.tools.definitions())
        await session.send_audio(bytes(960))
        observed: list[object] = []
        saw_tool = False
        interrupted = False
        async for event in session.events():
            observed.append(event)
            await app._handle_realtime_event(event)
            if isinstance(event, ToolCall):
                saw_tool = True
            elif isinstance(event, AudioDelta) and saw_tool and not interrupted:
                interrupted = True
                await session.cancel()
                await session.truncate(event.item_id, 0)
            elif isinstance(event, ResponseDone) and event.status == "cancelled":
                break

        assert saw_tool
        assert interrupted
        assert any(isinstance(event, ResponseDone) and event.usage for event in observed)
        assert fake.audio_input == bytes(960)
        await asyncio.wait_for(fake.truncated.wait(), timeout=1)
        assert any(event.get("type") == "response.cancel" for event in fake.received)
        assert any(
            event.get("type") == "conversation.item.truncate" and event.get("audio_end_ms") == 0
            for event in fake.received
        )
        update = next(event for event in fake.received if event.get("type") == "session.update")
        assert update["session"]["model"] == "gpt-realtime-2.1"
        assert update["session"]["audio"]["output"]["voice"] == "marin"
        assert any(event.get("type") == "conversation.item.create" for event in fake.received)
        await app._play_queue.join()
        await asyncio.sleep(0)
        entries = [
            json.loads(line)
            for path in journal.log_dir.glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert any(entry["type"] == "user_transcript" for entry in entries)
        assert any(entry["type"] == "assistant_transcript" for entry in entries)
        assert any(entry["type"] == "usage" and entry["usage"] for entry in entries)
        assert any(message.get("type") == "transcript" for message in controls)
    finally:
        await app.stop()
        await fake.close()


async def test_sleeping_app_does_not_send_microphone_audio(tmp_path: Path) -> None:
    fake = FakeRealtimeServer()
    await fake.start()
    session = RealtimeSession(_settings(), base_url=fake.websocket_url)
    audio = _FakeAudio()

    audio = _SingleFrameAudio()
    app = App(_settings(), audio, Journal(log_dir=tmp_path / "logs"))
    app.state = State(Mode.SLEEPING, wake_enabled=False)
    assert not app.active
    app._session = session
    try:
        await session.open("Test instructions", [])
        await app._audio_loop()
        assert fake.audio_input == b""
        assert not any(event.get("type") == "input_audio_buffer.append" for event in fake.received)
    finally:
        await app.stop()
        await fake.close()


async def test_realtime_session_loss_reconnects_and_is_journaled(tmp_path: Path) -> None:
    attempts = 0
    journal = Journal(log_dir=tmp_path / "logs")
    app = App(
        _settings(),
        _FakeAudio(),
        journal,
        session_factory=lambda settings: _ReconnectSession(settings, app, next_attempt()),
    )
    app.state = State(Mode.AWAKE)

    def next_attempt() -> int:
        nonlocal attempts
        attempts += 1
        return attempts

    await app._session_loop()
    await app.stop()
    entries = [
        json.loads(line)
        for path in journal.log_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert attempts == 2
    assert any(entry["type"] == "error" for entry in entries)


async def test_session_renews_one_minute_before_expiry(tmp_path: Path) -> None:
    app = App(_settings(), _FakeAudio(), Journal(log_dir=tmp_path / "logs"))
    session = _RecordingSession()
    session.session_expires_at = int(time.time()) + 60
    app._session = session
    app.state = State(Mode.AWAKE)
    await app._refresh_session(session)
    assert session.closed


async def test_first_played_audio_records_vad_to_audio_latency(tmp_path: Path) -> None:
    journal = Journal(log_dir=tmp_path / "logs")
    audio = _FakeAudio()
    audio.progress = 1
    app = App(_settings(), audio, journal, clock=FakeClock())
    app._speech_end_at = app.clock.now()
    app._latency_target_ms = 1
    await app._record_audio_latency(0)
    entry = json.loads(next(journal.log_dir.glob("*.jsonl")).read_text(encoding="utf-8"))
    assert entry["type"] == "latency"
    assert entry["mode"] == "realtime"


async def test_sleep_tool_returns_to_sleep_and_emits_feature_hook(tmp_path: Path) -> None:
    app = App(_settings(), _FakeAudio(), Journal(log_dir=tmp_path / "logs"))
    app.state = State(Mode.AWAKE)
    hooks: list[str] = []
    app.on("sleep", lambda summary: hooks.append(summary))

    result = await app.tools.dispatch("sleep", "{}", app)

    assert result == {"ok": True, "detail": "Conversation ended"}
    assert app.mode == Mode.SLEEPING
    assert not app.active
    assert hooks == [""]


async def test_followup_timeout_updates_journal_and_ui(tmp_path: Path) -> None:
    controls: list[dict[str, object]] = []

    async def control(message: dict[str, object]) -> None:
        controls.append(message)

    journal = Journal(log_dir=tmp_path / "logs")
    app = App(_settings(), _FakeAudio(), journal, clock=FakeClock(10), control=control)
    app.state = State(Mode.FOLLOWUP, followup_deadline=10)
    slept: list[str] = []
    app.on("sleep", lambda summary: slept.append(summary))

    await app._apply(Event("tick"))

    assert app.mode == Mode.SLEEPING
    assert any(
        message.get("type") == "state" and message.get("state") == "SLEEPING"
        for message in controls
    )
    assert slept == [""]
    entries = [
        json.loads(line)
        for path in journal.log_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(entry["type"] == "state" and entry["state"] == "SLEEPING" for entry in entries)


class _FakeAudio:
    def __init__(self) -> None:
        self.progress = 250
        self.flushed = False

    def mic_frames(self):
        async def frames():
            await asyncio.Future()
            yield b""

        return frames()

    async def play(self, pcm: bytes) -> None:
        self.progress += len(pcm) // 48

    async def flush(self) -> None:
        self.flushed = True
        self.progress = 0

    @property
    def playing(self) -> bool:
        return self.progress > 0

    @property
    def played_ms(self) -> int:
        return self.progress


class _SingleFrameAudio(_FakeAudio):
    def mic_frames(self):
        async def frames():
            yield bytes(960)

        return frames()


class _RecordingSession(RealtimeSession):
    def __init__(self) -> None:
        super().__init__(_settings())
        self.cancelled = False
        self.truncation: tuple[str, int] | None = None
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def cancel(self) -> None:
        self.cancelled = True

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        self.truncation = item_id, audio_end_ms


class _ReconnectSession(RealtimeSession):
    def __init__(self, settings: Settings, app: App, attempt: int) -> None:
        super().__init__(settings)
        self.app = app
        self.attempt = attempt

    async def open(self, instructions: str, tools: list[dict[str, object]]) -> None:
        _ = instructions, tools

    async def events(self):
        if self.attempt == 1:
            raise RealtimeConnectionError("fixture disconnected")
        self.app.state = State(Mode.SLEEPING)
        if False:
            yield ResponseDone(status="completed")


async def test_barge_in_truncates_only_current_item_at_pre_flush_playback(tmp_path: Path) -> None:
    audio = _FakeAudio()
    audio.progress = 50
    app = App(_settings(), audio, Journal(log_dir=tmp_path / "logs"))
    session = _RecordingSession()
    app._session = session
    app.state = State(Mode.AWAKE, speaking=True, wake_enabled=False)
    app._active_response_id = "response"
    app._ignore_audio_until_response = False
    await app._handle_realtime_event(
        AudioDelta(item_id="first-item", pcm=bytes(2400), response_id="response")
    )
    await app._handle_realtime_event(
        AudioDelta(item_id="second-item", pcm=bytes(2400), response_id="response")
    )

    await app._apply(Event("user_speech_start"))
    await asyncio.sleep(0)

    assert audio.flushed
    assert session.cancelled
    assert session.truncation == ("first-item", 50)
    assert app._output_enqueued_samples == 0
