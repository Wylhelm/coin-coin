import asyncio
import json
import struct
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

import coincoin.app as app_module
import coincoin.realtime.session as session_module
from coincoin.app import App
from coincoin.audio.browser import BrowserAudio
from coincoin.clock import FakeClock
from coincoin.journal import Journal
from coincoin.realtime.events import (
    AudioDelta,
    ResponseDone,
    ResponseStarted,
    SpeechStarted,
    ToolCall,
    parse_event,
)
from coincoin.realtime.session import RealtimeConnectionError, RealtimeSession
from coincoin.statemachine import Event, Mode, State
from coincoin.web.server import create_app
from tests.fakes.fake_realtime import FakeRealtimeServer
from tests.integration.test_s4_regressions import (
    _Audio,
    _cancel_app_tasks,
    _ReconnectSession,
    _Session,
    _settings,
)


def _entries(app: App) -> list[dict]:
    return [
        json.loads(line)
        for path in app.journal.log_dir.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]


@pytest.mark.parametrize("mode", [Mode.AWAKE, Mode.SLEEPING])
async def test_stop_rearms_wake_after_safety_interval_without_sound(
    tmp_path: Path, mode: Mode
) -> None:
    clock = FakeClock()
    session = _Session()
    app = App(
        _settings(),
        _Audio(),
        Journal(log_dir=tmp_path),
        clock=clock,
        session_factory=lambda _: session,  # type: ignore[arg-type]
    )
    sounds: list[str] = []
    app.on("quack", lambda tag: sounds.append(tag))
    app.state = State(mode)
    await app.stop_pressed()
    assert app.mode == Mode.SLEEPING
    assert not app.state.wake_enabled
    assert sounds == []
    clock.advance(0.499)
    await app._apply(Event("tick"))
    assert not app.state.wake_enabled
    clock.advance(0.001)
    await app._apply(Event("tick"))
    assert app.state.wake_enabled
    await app._apply(Event("wake"))
    assert app.mode == Mode.AWAKE
    await _cancel_app_tasks(app)


@pytest.mark.parametrize("blocked", [False, True])
async def test_shutdown_allows_priority_stop_to_complete_or_reports_deadline(
    tmp_path: Path, monkeypatch, blocked: bool
) -> None:
    monkeypatch.setattr(app_module, "_STOP_TIMEOUT_S", 0.1)
    started = asyncio.Event()
    completed = asyncio.Event()
    ui_started = asyncio.Event()
    release = asyncio.Event()
    app = App(_settings(), _Audio(), Journal(log_dir=tmp_path))
    app.state = State(Mode.AWAKE)

    async def robot_stop() -> None:
        started.set()
        if blocked:
            await release.wait()
        else:
            await asyncio.sleep(0.01)
            completed.set()

    async def ui(_message: dict[str, object]) -> None:
        ui_started.set()
        await release.wait()

    app.on("stop", robot_stop)
    app.control = ui
    shutdown = asyncio.create_task(app.stop())
    await asyncio.wait_for(started.wait(), 0.2)
    await asyncio.wait_for(shutdown, 0.5)
    if blocked:
        assert any(e.get("operation") == "robot stop hook" for e in _entries(app))
    else:
        assert completed.is_set()
    assert not app._tasks


async def test_sleep_tool_event_completes_hooks_result_and_next_wake(tmp_path: Path) -> None:
    clock = FakeClock()
    app = App(_settings(), _Audio(), Journal(log_dir=tmp_path), clock=clock)
    app.state = State(Mode.AWAKE)
    app._session = _Session()  # type: ignore[assignment]
    slept: list[str] = []
    app.on("sleep", lambda summary: slept.append(summary))
    await app._handle_realtime_event(ResponseStarted(response_id="sleep-response"))
    await app._handle_realtime_event(
        ToolCall(name="sleep", call_id="sleep-call", arguments="{}", response_id="sleep-response")
    )
    tasks = tuple(app._tool_tasks)
    await asyncio.wait_for(asyncio.gather(*tasks), 0.5)
    assert slept == [""]
    assert app.mode == Mode.SLEEPING
    assert not app.state.speaking
    assert any(e.get("type") == "tool_result" and e["result"]["ok"] for e in _entries(app))
    clock.advance(0.5)
    await app._apply(Event("tick"))
    assert app.state.wake_enabled
    app.session_factory = lambda _: _Session()  # type: ignore[assignment]
    await app._apply(Event("wake"))
    assert app.mode == Mode.AWAKE
    await _cancel_app_tasks(app)


def _tool_payload(response_id: str, call_id: str) -> dict:
    return {
        "type": "response.output_item.done",
        "response_id": response_id,
        "item": {
            "type": "function_call",
            "name": "local_move",
            "call_id": call_id,
            "arguments": "{}",
        },
    }


async def test_interruption_rejects_late_tool_but_allows_new_response(tmp_path: Path) -> None:
    app = App(_settings(), _Audio(), Journal(log_dir=tmp_path))
    app.state = State(Mode.AWAKE)
    session = _Session()
    app._session = session  # type: ignore[assignment]
    invoked: list[str] = []
    running = asyncio.Event()
    cancelled = asyncio.Event()

    async def move(_app: App, _args: dict) -> dict:
        invoked.append("move")
        if len(invoked) == 1:
            running.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        return {"ok": True}

    app.register_tool("local_move", "Local fake movement", {"type": "object"}, move)
    await app._handle_realtime_event(ResponseStarted(response_id="old"))
    event = parse_event(_tool_payload("old", "first"))
    assert isinstance(event, ToolCall) and event.response_id == "old"
    await app._handle_realtime_event(event)
    await asyncio.wait_for(running.wait(), 0.2)
    await app._handle_realtime_event(SpeechStarted())
    await asyncio.wait_for(cancelled.wait(), 0.2)
    late = parse_event(_tool_payload("old", "late"))
    assert isinstance(late, ToolCall)
    await app._handle_realtime_event(late)
    await app._handle_realtime_event(ResponseDone(response_id="old", status="completed"))
    await asyncio.sleep(0)
    assert invoked == ["move"]
    assert session.responses_created == 0
    await app._handle_realtime_event(ResponseStarted(response_id="new"))
    fresh = parse_event(_tool_payload("new", "new-call"))
    assert isinstance(fresh, ToolCall)
    await app._handle_realtime_event(fresh)
    await app._handle_realtime_event(ResponseDone(response_id="new", status="completed"))
    await asyncio.gather(*tuple(app._tool_tasks))
    assert invoked == ["move", "move"]
    assert session.responses_created == 1
    await _cancel_app_tasks(app)


async def test_failed_tool_output_ends_speaking_without_starting_a_response(tmp_path: Path) -> None:
    class BrokenOutput(_Session):
        async def send_tool_result(self, call_id: str, output: str) -> None:
            raise RealtimeConnectionError("Local fake output failure")

    app = App(_settings(), _Audio(), Journal(log_dir=tmp_path))
    app.state = State(Mode.AWAKE)
    session = BrokenOutput()
    app._session = session  # type: ignore[assignment]

    async def ping(_app: App, _args: dict) -> dict:
        return {"ok": True}

    app.register_tool("ping", "Local fake ping", {"type": "object"}, ping)
    await app._handle_realtime_event(ResponseStarted(response_id="r"))
    await app._handle_realtime_event(
        ToolCall(name="ping", call_id="ping", arguments="{}", response_id="r")
    )
    await app._handle_realtime_event(ResponseDone(response_id="r", status="completed"))
    await asyncio.gather(*tuple(app._tool_tasks))
    assert not app.state.speaking
    assert not app.state.response_active
    assert session.responses_created == 0
    assert any(e.get("operation") == "tool result send" for e in _entries(app))
    await _cancel_app_tasks(app)


class _Socket:
    def __init__(self) -> None:
        self.closed = False

    async def send_bytes(self, _data: bytes) -> None:
        return

    async def send_json(self, _data: dict) -> None:
        return

    async def close(self, **_kwargs) -> None:
        self.closed = True


async def _connect_page(audio: BrowserAudio):
    page = await audio.connect_audio(_Socket())  # type: ignore[arg-type]
    await audio.connect_ui(_Socket())  # type: ignore[arg-type]
    return page


async def _finish_task(app: App) -> None:
    assert app._response_finish_task is not None
    await asyncio.wait_for(asyncio.shield(app._response_finish_task), 0.5)


async def test_browser_fractional_tail_waits_for_exact_samples_not_rounded_ms(
    tmp_path: Path,
) -> None:
    audio = BrowserAudio()
    app = App(_settings(), audio, Journal(log_dir=tmp_path))
    app.state = State(Mode.AWAKE)
    app._spawn(app._playback_loop())
    async with TestClient(TestServer(create_app(audio=audio))) as client:
        audio_ws = await client.ws_connect("/ws/audio")
        ui = await client.ws_connect("/ws/ui")
        await app._handle_realtime_event(ResponseStarted(response_id="tiny"))
        await app._handle_realtime_event(
            AudioDelta(item_id="tiny-item", pcm=bytes(50), response_id="tiny")
        )
        await app._play_queue.join()
        assert len((await audio_ws.receive()).data) == 58
        await ui.send_json(
            {"type": "played", "ms": 1, "samples": 24, "playing": False, "generation": 0}
        )
        await app._handle_realtime_event(ResponseDone(response_id="tiny", status="completed"))
        await asyncio.sleep(0.03)
        assert app.state.speaking
        assert audio.playback_progress.played_samples == 24
        await ui.send_json(
            {"type": "played", "ms": 1, "samples": 25, "playing": False, "generation": 0}
        )
        await _finish_task(app)
        assert not app.state.speaking
        await ui.close()
        await audio_ws.close()
    await _cancel_app_tasks(app)


async def test_browser_replacement_uses_new_page_progress_for_new_response(tmp_path: Path) -> None:
    audio = BrowserAudio()
    app = App(_settings(), audio, Journal(log_dir=tmp_path))
    app.state = State(Mode.AWAKE)
    app._spawn(app._playback_loop())
    for number in (1, 2):
        page = await _connect_page(audio)
        response_id = f"r{number}"
        await app._handle_realtime_event(ResponseStarted(response_id=response_id))
        for _ in range(50):
            await app._handle_realtime_event(
                AudioDelta(item_id=response_id, pcm=bytes(960), response_id=response_id)
            )
            # Progress reports follow actual sends and release BrowserAudio flow control.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await audio.update_playback(
                page,
                page.sent_samples // 24,
                playing=False,
                generation=0,
                played_samples=page.sent_samples,
            )
        while app._play_queue.qsize():
            await audio.update_playback(
                page,
                page.sent_samples // 24,
                playing=False,
                generation=0,
                played_samples=page.sent_samples,
            )
            await asyncio.sleep(0)
        await app._play_queue.join()
        await audio.update_playback(page, 1000, playing=False, generation=0, played_samples=24000)
        await app._handle_realtime_event(ResponseDone(response_id=response_id, status="completed"))
        await _finish_task(app)
        assert not app.state.speaking
        assert app._output_enqueued_samples == 24000
    await _cancel_app_tasks(app)


async def test_browser_replaced_during_unfinished_response_reports_failure(tmp_path: Path) -> None:
    audio = BrowserAudio()
    await _connect_page(audio)
    app = App(_settings(), audio, Journal(log_dir=tmp_path))
    app.state = State(Mode.AWAKE)
    app._spawn(app._playback_loop())
    await app._handle_realtime_event(ResponseStarted(response_id="r"))
    await app._handle_realtime_event(AudioDelta(item_id="item", pcm=bytes(960), response_id="r"))
    await app._play_queue.join()
    await app._handle_realtime_event(ResponseDone(status="completed", response_id="r"))
    await asyncio.sleep(0)
    await _connect_page(audio)
    await _finish_task(app)
    assert not app.state.speaking
    assert any(e.get("operation") == "audio playback" for e in _entries(app))
    await _cancel_app_tasks(app)


@pytest.mark.parametrize("already_sent", [False, True])
async def test_page_replacement_drops_old_queued_and_late_audio_but_allows_next_response(
    tmp_path: Path, already_sent: bool
) -> None:
    audio = BrowserAudio()
    await _connect_page(audio)
    app = App(_settings(), audio, Journal(log_dir=tmp_path))
    app.state = State(Mode.AWAKE)
    successes: list[str] = []
    app.on("response_done", lambda event: successes.append(event.response_id))
    await app._handle_realtime_event(ResponseStarted(response_id="old"))
    await app._handle_realtime_event(
        AudioDelta(item_id="old-item", pcm=bytes(960), response_id="old")
    )
    if already_sent:
        app._spawn(app._playback_loop())
        await app._play_queue.join()
    page = await _connect_page(audio)
    await app._handle_realtime_event(
        AudioDelta(item_id="old-item", pcm=bytes(960), response_id="old")
    )
    if not already_sent:
        app._spawn(app._playback_loop())
    await app._handle_realtime_event(ResponseDone(response_id="old", status="completed"))
    await _finish_task(app)
    assert page.sent_samples == 0
    assert successes == []
    assert any(e.get("operation") == "audio playback" for e in _entries(app))
    await app._handle_realtime_event(ResponseStarted(response_id="fresh"))
    await app._handle_realtime_event(
        AudioDelta(item_id="fresh-item", pcm=bytes(50), response_id="fresh")
    )
    await app._play_queue.join()
    await audio.update_playback(page, 1, playing=False, generation=0, played_samples=25)
    await app._handle_realtime_event(ResponseDone(response_id="fresh", status="completed"))
    await _finish_task(app)
    assert successes == ["fresh"]
    await _cancel_app_tasks(app)


async def test_playback_progress_timeout_is_not_reported_as_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(app_module, "_PLAYBACK_TIMEOUT_S", 0.03)
    app = App(_settings(), _Audio(), Journal(log_dir=tmp_path))
    app.state = State(Mode.AWAKE, speaking=True, response_active=True)
    successes: list[object] = []
    app.on("response_done", lambda event: successes.append(event))
    await app._finish_response(ResponseDone(status="completed"), 24000)
    assert not app.state.speaking
    assert successes == []
    assert any(e.get("operation") == "audio playback" for e in _entries(app))
    await _cancel_app_tasks(app)


async def test_exact_item_boundary_truncates_start_of_next_item(tmp_path: Path) -> None:
    audio = _Audio()
    audio.progress = 100
    app = App(_settings(), audio, Journal(log_dir=tmp_path))
    session = _Session()
    app._session = session  # type: ignore[assignment]
    app.state = State(Mode.AWAKE, speaking=True)
    app._audio_segments = [("first", 0, 2400), ("second", 2400, 4800)]
    await app._handle_realtime_event(SpeechStarted())
    await asyncio.wait_for(session.truncated.wait(), 0.2)
    assert session.truncation == ("second", 0)
    await _cancel_app_tasks(app)


async def test_audio_hook_follows_actual_play_and_cannot_block_interruption(tmp_path: Path) -> None:
    play_started = asyncio.Event()
    hook_started = asyncio.Event()
    calls: list[bytes] = []
    hooks: list[tuple[bytes, int]] = []

    class BlockedAudio(_Audio):
        async def play(self, pcm: bytes) -> None:
            calls.append(pcm)
            play_started.set()
            await asyncio.Future()

    audio = BlockedAudio()
    app = App(_settings(), audio, Journal(log_dir=tmp_path))
    app.state = State(Mode.AWAKE)

    async def hook(pcm: bytes, play_at_ms: int) -> None:
        assert play_started.is_set()
        hooks.append((pcm, play_at_ms))
        hook_started.set()
        await asyncio.Future()

    app.on("audio_out", hook)
    app._spawn(app._playback_loop())
    await app._handle_realtime_event(ResponseStarted(response_id="r"))
    first, second = bytes(960), struct.pack("<h", 1) * 480
    await app._handle_realtime_event(AudioDelta(item_id="one", pcm=first, response_id="r"))
    await app._handle_realtime_event(AudioDelta(item_id="two", pcm=second, response_id="r"))
    await asyncio.wait_for(hook_started.wait(), 0.2)
    assert calls == [first]
    assert hooks == [(first, 0)]
    await asyncio.wait_for(app._handle_realtime_event(SpeechStarted()), 0.2)
    assert audio.flush_started.is_set()
    assert not app.state.speaking
    await _cancel_app_tasks(app)


@pytest.mark.parametrize("first_finishes", ["slow", "fast"])
@pytest.mark.parametrize("interrupted", [False, True])
async def test_parallel_tool_outputs_precede_one_continuation_over_fake_websocket(
    tmp_path: Path, first_finishes: str, interrupted: bool
) -> None:
    fake = FakeRealtimeServer()
    fake._input_events = [
        {"type": "response.created", "response": {"id": "parent"}},
        _tool_payload("parent", "slow"),
        _tool_payload("parent", "fast"),
        {"type": "response.done", "response": {"id": "parent", "status": "completed"}},
    ]
    fake._followup_events = []
    await fake.start()
    session = RealtimeSession(_settings(), base_url=fake.websocket_url)
    app = App(_settings(), _Audio(), Journal(log_dir=tmp_path))
    app.state = State(Mode.AWAKE)
    app._session = session
    releases = {key: asyncio.Event() for key in ("slow", "fast")}
    starts = [asyncio.Event(), asyncio.Event()]
    invocation = 0

    async def local_move(_app: App, _arguments: dict) -> dict:
        nonlocal invocation
        index = invocation
        invocation += 1
        key = ("slow", "fast")[index]
        starts[index].set()
        await releases[key].wait()
        return {"ok": False, "reason": key}

    app.register_tool("local_move", "Local test", {"type": "object"}, local_move)

    async def consume() -> None:
        async for event in session.events():
            await app._handle_realtime_event(event)

    reader = None
    try:
        await session.open("Local fake tools", app.tools.definitions())
        reader = asyncio.create_task(consume())
        await session.send_audio(bytes(960))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in starts)), 0.5)
        releases[first_finishes].set()
        await asyncio.sleep(0.02)
        assert not any(e["type"] == "response.create" for e in fake.received)
        if interrupted:
            await app._handle_realtime_event(SpeechStarted())
            releases["fast" if first_finishes == "slow" else "slow"].set()
            await asyncio.sleep(0.02)
            assert not any(
                e["type"] in {"conversation.item.create", "response.create"} for e in fake.received
            )
            return
        releases["fast" if first_finishes == "slow" else "slow"].set()
        for _ in range(100):
            if any(e["type"] == "response.create" for e in fake.received):
                break
            await asyncio.sleep(0.005)
        relevant = [
            e for e in fake.received if e["type"] in {"conversation.item.create", "response.create"}
        ]
        assert [e["type"] for e in relevant] == [
            "conversation.item.create",
            "conversation.item.create",
            "response.create",
        ]
        assert [e["item"]["call_id"] for e in relevant[:2]] == ["slow", "fast"]
        assert [json.loads(e["item"]["output"])["reason"] for e in relevant[:2]] == ["slow", "fast"]
    finally:
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await _cancel_app_tasks(app)
        await session.close()
        await fake.close()


async def test_stalled_local_websocket_upgrade_times_out_reconnects_and_cleans_up(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(session_module, "_OPEN_TIMEOUT_S", 0.03)
    accepted = asyncio.Event()
    disconnected = asyncio.Event()
    handlers: set[asyncio.Task] = set()

    async def stalled(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        accepted.set()
        try:
            await reader.readuntil(b"\r\n\r\n")
            assert await reader.read() == b""
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(stalled, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    sessions: list[RealtimeSession] = []
    clock = FakeClock()
    app: App

    def factory(settings):
        if not sessions:
            session = RealtimeSession(settings, base_url=f"ws://127.0.0.1:{port}/v1/realtime")
            sessions.append(session)
            return session
        return _ReconnectSession(app)

    app = App(
        _settings(),
        _Audio(),
        Journal(log_dir=tmp_path),
        session_factory=factory,  # type: ignore[arg-type]
        clock=clock,
    )
    app.state = State(Mode.AWAKE)
    loop = asyncio.create_task(app._session_loop())
    try:
        await asyncio.wait_for(accepted.wait(), 0.2)
        await asyncio.wait_for(disconnected.wait(), 0.2)
        assert not loop.done()
        assert sessions[0]._client is None
        for _ in range(10):
            clock.advance(1)
            await asyncio.sleep(0.01)
            if loop.done():
                break
        await asyncio.wait_for(loop, 0.2)
        assert app.mode == Mode.SLEEPING
        assert any(
            e.get("operation") == "Realtime connection" and "TimeoutError" in e["message"]
            for e in _entries(app)
        )
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        await _cancel_app_tasks(app)
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*tuple(handlers))


@pytest.mark.parametrize("options", [{}, {"echo": True}, {"tone": True}])
async def test_offline_echo_and_tone_stop_keep_browser_flush(options: dict) -> None:
    async with TestClient(TestServer(create_app(audio=BrowserAudio(), **options))) as client:
        audio_ws = await client.ws_connect("/ws/audio")
        ui = await client.ws_connect("/ws/ui")
        await ui.send_json({"type": "stop"})
        flush = await asyncio.wait_for(ui.receive_json(), 0.2)
        assert flush["type"] == "flush"
        await ui.send_json({"type": "flushed", "generation": flush["generation"]})
        assert (await asyncio.wait_for(ui.receive_json(), 0.2))["type"] == "flush_complete"
        await ui.close()
        await audio_ws.close()
