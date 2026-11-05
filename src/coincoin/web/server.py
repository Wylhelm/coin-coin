"""HTTP and paired WebSocket endpoints for browser audio."""

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from aiohttp import WSMsgType, web

from coincoin.audio.browser import BrowserAudio
from coincoin.audio.resample import pcm16_to_float32
from coincoin.audio.tone import find_tone_offset, make_tone
from coincoin.config import Settings
from coincoin.journal import Journal

_LOGGER = logging.getLogger(__name__)
_STATIC = Path(__file__).parent / "static"
_INDEX = _STATIC / "index.html"
_AUDIO = web.AppKey("browser_audio", BrowserAudio)
_ECHO = web.AppKey("echo", bool)
_TONE = web.AppKey("tone", bool)
_TASKS = web.AppKey("background_tasks", set[asyncio.Task[None]])


class Conversation(Protocol):
    @property
    def active(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def stop_pressed(self) -> None: ...

    async def ui_connected(self) -> None: ...


class _OfflineConversation:
    active = False

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return

    async def stop_pressed(self) -> None:
        return

    async def ui_connected(self) -> None:
        return


_CONVERSATION = web.AppKey("conversation", Conversation)


class _JournalState:
    def __init__(self, journal: Journal | None) -> None:
        self.journal = journal


_JOURNAL = web.AppKey("journal", _JournalState)


class _ToneProbe:
    def __init__(self) -> None:
        self.reference = make_tone()
        self.armed_at: float | None = None
        self._samples = np.empty(0, dtype=np.float32)

    def arm(self) -> None:
        self.armed_at = time.monotonic()
        self._samples = np.empty(0, dtype=np.float32)

    def observe(self, frame: bytes) -> float | None:
        if self.armed_at is None:
            return None
        self._samples = np.concatenate((self._samples, pcm16_to_float32(frame)))
        self._samples = self._samples[-36_000:]
        offset = find_tone_offset(self._samples, self.reference)
        if offset is None:
            return None
        detected_at = time.monotonic() - (len(self._samples) - offset) / 24_000
        latency_ms = (detected_at - self.armed_at) * 1_000
        self.armed_at = None
        return latency_ms if latency_ms >= 0 else None


async def _index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(_INDEX)


async def _healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _static_file(request: web.Request) -> web.FileResponse:
    name = request.match_info["name"]
    if name not in {
        "app.js",
        "audio-protocol.js",
        "mic-worklet.js",
        "play-worklet.js",
        "style.css",
    }:
        raise web.HTTPNotFound()
    return web.FileResponse(_STATIC / name)


async def _audio_ws(request: web.Request) -> web.WebSocketResponse:
    websocket = web.WebSocketResponse(max_msg_size=64 * 1024, heartbeat=20)
    await websocket.prepare(request)
    audio = request.app[_AUDIO]
    page = await audio.connect_audio(websocket)
    async for message in websocket:
        if message.type == WSMsgType.BINARY:
            try:
                await audio.receive_mic(page, message.data)
            except ValueError as exc:
                await websocket.close(code=1003, message=str(exc).encode())
                break
        elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED}:
            break
    await audio.disconnect(page)
    return websocket


async def _ui_ws(request: web.Request) -> web.WebSocketResponse:
    websocket = web.WebSocketResponse(max_msg_size=16 * 1024, heartbeat=20)
    await websocket.prepare(request)
    audio = request.app[_AUDIO]
    page = await audio.connect_ui(websocket)
    if page is None:
        await websocket.send_json({"type": "error", "message": "Audio WebSocket not connected"})
        await websocket.close(code=4000, message=b"Audio WebSocket required")
        return websocket

    conversation = request.app[_CONVERSATION]
    ui_connected = getattr(conversation, "ui_connected", None)
    if ui_connected is not None:
        result = ui_connected()
        if inspect.isawaitable(result):
            await result

    async for message in websocket:
        if message.type != WSMsgType.TEXT:
            continue
        try:
            event = message.json()
        except (json.JSONDecodeError, TypeError):
            await websocket.close(code=1003, message=b"JSON control messages required")
            break
        if not isinstance(event, dict):
            await websocket.close(code=1003, message=b"JSON object required")
            break
        kind = event.get("type")
        if kind == "played":
            ms = event.get("ms")
            generation = event.get("generation", 0)
            playing = event.get("playing", False)
            samples = event.get("samples")
            if (
                isinstance(ms, int)
                and not isinstance(ms, bool)
                and ms >= 0
                and isinstance(generation, int)
                and isinstance(playing, bool)
                and (samples is None or (type(samples) is int and samples >= 0))
            ):
                await audio.update_playback(
                    page, ms, playing=playing, generation=generation, played_samples=samples
                )
        elif kind == "flushed":
            generation = event.get("generation")
            if isinstance(generation, int):
                audio.acknowledge_flush(page, generation)
                await websocket.send_json({"type": "flush_complete", "generation": generation})
        elif kind == "stop":
            if (
                request.app[_ECHO]
                or request.app[_TONE]
                or isinstance(request.app[_CONVERSATION], _OfflineConversation)
            ):
                _spawn(request.app, audio.flush())
            else:
                _spawn(request.app, request.app[_CONVERSATION].stop_pressed())
        else:
            await websocket.send_json({"type": "error", "message": "Unknown UI event"})
    await audio.disconnect_ui(page, websocket)
    return websocket


async def _echo_loop(audio: BrowserAudio) -> None:
    async for frame in audio.mic_frames():
        try:
            await audio.play(frame)
        except ConnectionError:
            _LOGGER.debug("Echo frame discarded after browser disconnect")


async def _tone_sender(audio: BrowserAudio, probe: _ToneProbe) -> None:
    tone = probe.reference
    next_tone = time.monotonic()
    while True:
        await asyncio.sleep(max(0, next_tone - time.monotonic()))
        next_tone += 1.0
        probe.arm()
        try:
            await audio.play(tone)
        except ConnectionError:
            probe.armed_at = None
        if next_tone < time.monotonic():
            next_tone = time.monotonic() + 1.0


async def _tone_listener(audio: BrowserAudio, probe: _ToneProbe, journal: Journal | None) -> None:
    async for frame in audio.mic_frames():
        latency_ms = probe.observe(frame)
        if latency_ms is None:
            continue
        rounded = round(latency_ms, 1)
        if journal is not None:
            journal.write("latency", mode="tone", round_trip_ms=rounded)
        await audio.send_control({"type": "latency", "ms": rounded})


async def _startup(app: web.Application) -> None:
    conversation = app[_CONVERSATION]
    if conversation is not None:
        await conversation.start()
    if app[_ECHO]:
        _spawn(app, _echo_loop(app[_AUDIO]))
    if app[_TONE]:
        probe = _ToneProbe()
        _spawn(app, _tone_sender(app[_AUDIO], probe))
        _spawn(app, _tone_listener(app[_AUDIO], probe, app[_JOURNAL].journal))


async def _cleanup(app: web.Application) -> None:
    conversation = app[_CONVERSATION]
    if conversation is not None:
        await conversation.stop()
    tasks = tuple(app[_TASKS])
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _spawn(app: web.Application, coroutine: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coroutine)
    app[_TASKS].add(task)

    def completed(done: asyncio.Task[None]) -> None:
        app[_TASKS].discard(done)
        if done.cancelled():
            return
        error = done.exception()
        if error is not None:
            _LOGGER.error(
                "Browser audio background task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(completed)


def create_app(
    audio: BrowserAudio | None = None,
    *,
    echo: bool = False,
    tone: bool = False,
    journal: Journal | None = None,
    settings: Settings | None = None,
    conversation: Conversation | None = None,
) -> web.Application:
    """Create the HTTP/audio app; Realtime connects only after a local wake phrase."""
    audio_io = audio if audio is not None else BrowserAudio()
    effective_journal = journal
    active_conversation = conversation
    if conversation is None and settings is not None and not echo and not tone:
        from coincoin.app import App

        effective_journal = journal if journal is not None else Journal()
        active_conversation = App(
            settings,
            audio_io,
            effective_journal,
            control=audio_io.send_control,
        )
    app = web.Application()
    app[_AUDIO] = audio_io
    app[_ECHO] = echo
    app[_TONE] = tone
    app[_JOURNAL] = _JournalState(effective_journal)
    app[_TASKS] = set()
    app[_CONVERSATION] = active_conversation or _OfflineConversation()
    app.add_routes(
        [
            web.get("/", _index),
            web.get("/healthz", _healthz),
            web.get("/static/{name}", _static_file),
            web.get("/ws/audio", _audio_ws),
            web.get("/ws/ui", _ui_ws),
        ]
    )
    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)
    return app
