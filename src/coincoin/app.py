"""Browser audio service entry point and conversation orchestration."""

import argparse
import asyncio
import inspect
import json
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any, Protocol

from aiohttp import web

from coincoin.audio.gate import AudioGate
from coincoin.audio.io import AudioIO
from coincoin.audio.resample import resample_pcm16
from coincoin.clock import Clock, SystemClock
from coincoin.config import Settings
from coincoin.features import FEATURES, Feature
from coincoin.journal import Journal
from coincoin.realtime.events import (
    AssistantTranscript,
    AudioDelta,
    Error,
    RealtimeEvent,
    ResponseDone,
    ResponseStarted,
    SpeechStarted,
    SpeechStopped,
    ToolCall,
    UserTranscript,
)
from coincoin.realtime.instructions import build_instructions
from coincoin.realtime.session import RealtimeApiError, RealtimeConnectionError, RealtimeSession
from coincoin.realtime.tools import ToolRegistry, default_tools
from coincoin.statemachine import Effect, Event, Mode, State, transition
from coincoin.wake.engine import WakeEngine, create_wake_engine
from coincoin.wake.vad import VadEvent, VoiceActivityDetector
from coincoin.web.server import create_app

_LOGGER = logging.getLogger(__name__)
_SECRET_TEXT = re.compile(r"sk-[A-Za-z0-9_-]+|(?i:Bearer)\s+[^\s,;]+")
_MAX_RECONNECT_S = 30.0
_MIN_RECONNECT_S = 0.5
_PLAYBACK_TIMEOUT_S = 60.0
_SESSION_RENEW_BEFORE_S = 60

EventHandler = Callable[..., Any]
SessionFactory = Callable[[Settings], RealtimeSession]
ControlSender = Callable[[dict[str, object]], Awaitable[None]]


async def _discard_control(message: dict[str, object]) -> None:
    _ = message


class Vad(Protocol):
    def feed(self, pcm16_16k: bytes) -> list[VadEvent]: ...

    def reset(self) -> None: ...


class App:
    """Connect audio, local wake/VAD, the pure state machine, Realtime, and features."""

    def __init__(
        self,
        settings: Settings,
        audio: AudioIO,
        journal: Journal,
        *,
        session_factory: SessionFactory = RealtimeSession,
        wake_engine: WakeEngine | None = None,
        vad: Vad | None = None,
        clock: Clock | None = None,
        features: Sequence[Feature] = FEATURES,
        control: ControlSender = _discard_control,
    ) -> None:
        self.settings = settings
        self.audio = audio
        self.journal = journal
        self.session_factory = session_factory
        self.wake_engine = wake_engine
        self.vad = vad
        self.clock = clock or SystemClock()
        self.control = control
        self.state = State(followup_s=settings.followup_s)
        self.audio_gate = AudioGate()
        self.tools = ToolRegistry()
        self._handlers: dict[str, list[EventHandler]] = {}
        self._context_providers: dict[str, Callable[[], str | Awaitable[str]]] = {}
        self._features = tuple(features)
        self._session: RealtimeSession | None = None
        self._session_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._play_queue: asyncio.Queue[tuple[str, bytes, int] | None] = asyncio.Queue()
        self._play_error: str | None = None
        self._started = False
        self._stopping = False
        self._output_enqueued_ms = 0
        self._item_start_ms: dict[str, int] = {}
        self._item_queued_ms: dict[str, int] = {}
        self._active_audio_item: str | None = None
        self._pending_truncation: tuple[str, int] | None = None
        self._assistant_text = ""
        self._conversation_history: list[str] = []
        self._speech_end_at: float | None = None
        self._latency_target_ms: int | None = None
        self._response_finish_task: asyncio.Task[None] | None = None
        for definition in default_tools():
            self.register_tool(
                definition.name,
                definition.description,
                definition.schema,
                definition.handler,
            )

    @property
    def mode(self) -> Mode:
        return self.state.mode

    @property
    def active(self) -> bool:
        return self.state.mode != Mode.SLEEPING

    def register_tool(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        handler: Callable[["App", dict[str, Any]], Awaitable[dict[str, Any]]],
    ) -> None:
        """Register a model-callable feature tool."""
        self.tools.register(name, description, schema, handler)

    def on(self, event: str, handler: EventHandler) -> None:
        """Register a handler for a lifecycle event exposed to feature modules."""
        if not event:
            raise ValueError("Event name must not be empty")
        self._handlers.setdefault(event, []).append(handler)

    def instructions_context(self, key: str, provider: Callable[[], str | Awaitable[str]]) -> None:
        """Register a context value provider for the Realtime system instructions."""
        if not key:
            raise ValueError("Instruction context key must not be empty")
        self._context_providers[key] = provider

    async def start(self) -> None:
        """Install features and start local audio, playback, and state-timer tasks."""
        if self._started:
            return
        for feature in self._features:
            await feature.install(self)
        if self.wake_engine is None:
            self.wake_engine = create_wake_engine(self.settings)
        if self.vad is None:
            self.vad = VoiceActivityDetector()
        self._started = True
        self._stopping = False
        self._spawn(self._audio_loop())
        self._spawn(self._playback_loop())
        self._spawn(self._tick_loop())
        await self._send_status()

    async def stop(self) -> None:
        """Stop the conversation and release all owned tasks and the current session."""
        if self._stopping:
            return
        self._stopping = True
        if self._started:
            await self._apply(Event("stop_pressed"))
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._tasks.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._started = False

    async def request_sleep(self) -> None:
        """Handle the model's sleep tool."""
        await self._apply(Event("sleep_requested"))

    async def stop_pressed(self) -> None:
        """Handle the page's priority STOP control."""
        await self._apply(Event("stop_pressed"))

    async def _emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        for handler in tuple(self._handlers.get(event, ())):
            result = handler(*args, **kwargs)
            if inspect.isawaitable(result):
                await result

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                _LOGGER.error("Conversation task failed: %s", type(error).__name__)
                self._report_error("conversation task", error)

        task.add_done_callback(completed)
        return task

    async def _audio_loop(self) -> None:
        async for pcm24k in self.audio.mic_frames():
            if self._stopping:
                return
            if self.state.mode == Mode.SLEEPING:
                if self.state.wake_enabled and self.wake_engine is not None:
                    hit = self.wake_engine.feed(resample_pcm16(pcm24k, 24_000, 16_000))
                    if hit is not None:
                        self.journal.write("wake", score=hit.score, lang=hit.lang)
                        await self._apply(Event("wake"))
                continue
            if self.state.mode == Mode.AWAKE:
                outgoing = self.audio_gate.process(
                    pcm24k,
                    self.state.mode,
                    session_ready=self._session is not None,
                )
                for frame in outgoing:
                    await self._send_audio(frame)
                continue
            if self.vad is None:
                raise RuntimeError("Follow-up audio requires a configured local VAD")
            for event in self.vad.feed(resample_pcm16(pcm24k, 24_000, 16_000)):
                await self._handle_vad_event(event)

    async def _handle_vad_event(self, event: VadEvent) -> None:
        if event.kind == "speech_start":
            await self._apply(Event("user_speech_start"))
        if event.kind in {"speech_start", "speech_frame"} and event.pcm16:
            await self._send_audio(resample_pcm16(event.pcm16, 16_000, 24_000))
        if event.kind == "speech_end":
            await self._apply(Event("user_speech_end"))

    async def _send_audio(self, pcm24k: bytes) -> None:
        session = self._session
        if session is None:
            return
        try:
            await session.send_audio(pcm24k)
        except RealtimeConnectionError as exc:
            self._report_error("audio send", exc)

    async def _tick_loop(self) -> None:
        while not self._stopping:
            await self.clock.sleep(0.1)
            await self._apply(Event("tick"))

    async def _apply(self, event: Event) -> None:
        was_speaking = self.state.speaking
        old_mode = self.state.mode
        self.state, effects = transition(self.state, event, self.clock.now())
        if event.kind == "wake" and self.state.mode != old_mode:
            await self._emit("wake")
        elif event.kind == "sleep_requested" and old_mode != Mode.SLEEPING:
            await self._emit("sleep", summary=event.summary)
        if self.state.mode != old_mode:
            self.journal.write("state", state=self.state.mode.value)
            await self._send_status()
        elif event.kind in {"response_started", "response_done"}:
            await self._send_status()
        if event.kind == "user_speech_start":
            self._speech_end_at = None
            self._latency_target_ms = None
        elif event.kind == "user_speech_end":
            self._speech_end_at = self.clock.now()
        if event.kind == "user_speech_start" and was_speaking:
            await self._emit("barge_in")
        for effect in effects:
            await self._apply_effect(effect)

    async def _apply_effect(self, effect: Effect) -> None:
        if effect.kind == "OpenSession":
            if self._session_task is None or self._session_task.done():
                self._session_task = self._spawn(self._session_loop())
        elif effect.kind == "CloseSession":
            if self._session is not None:
                await self._session.close()
        elif effect.kind == "FlushPlayback":
            await self._flush_for_barge_in()
        elif effect.kind == "CancelResponse":
            session = self._session
            truncation = self._pending_truncation
            self._pending_truncation = None
            if session is not None:
                try:
                    await session.cancel()
                except RealtimeConnectionError as exc:
                    self._report_error("response cancellation", exc)
                if truncation is not None:
                    item_id, heard_ms = truncation
                    try:
                        await session.truncate(item_id, heard_ms)
                    except RealtimeConnectionError as exc:
                        self._report_error("response truncation", exc)
        elif effect.kind == "SetWakeEnabled":
            if effect.value and self.wake_engine is not None:
                self.wake_engine.reset()
        elif effect.kind == "Quack":
            await self._emit("quack", tag=effect.value)
            if not self._handlers.get("quack"):
                await self._apply(Event("quack_done"))
        elif effect.kind == "RobotStop":
            await self._emit("stop")
        elif effect.kind == "ResetPose":
            await self._emit("reset_pose")
        elif effect.kind == "MouthClose":
            await self._emit("mouth_close")
        elif effect.kind == "BrainSessionStart":
            await self._emit("brain_session_start")
        elif effect.kind == "BrainSessionEnd":
            await self._emit("brain_session_end", summary=effect.value)

    async def _flush_for_barge_in(self) -> None:
        item_id = self._active_audio_item
        heard_ms: int | None = None
        if item_id is not None:
            item_start = self._item_start_ms.get(item_id, self._output_enqueued_ms)
            queued_ms = self._item_queued_ms.get(item_id, 0)
            heard_ms = min(queued_ms, max(0, self.audio.played_ms - item_start))
            self._pending_truncation = item_id, heard_ms
        try:
            await self.audio.flush()
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            self._report_error("audio flush", exc)
        self._discard_queued_audio()
        self._output_enqueued_ms = 0
        self._item_start_ms.clear()
        self._item_queued_ms.clear()
        self._active_audio_item = None
        self._play_error = None

    async def _session_loop(self) -> None:
        delay = _MIN_RECONNECT_S
        while not self._stopping and self.state.mode != Mode.SLEEPING:
            session = self.session_factory(self.settings)
            refresh_task: asyncio.Task[None] | None = None
            try:
                instructions_context: dict[str, str] = {}
                for key, provider in self._context_providers.items():
                    value = provider()
                    instructions_context[key] = await value if inspect.isawaitable(value) else value
                instructions_context["conversation_summary"] = self._conversation_summary()
                await session.open(
                    build_instructions(instructions_context), self.tools.definitions()
                )
                self._session = session
                delay = _MIN_RECONNECT_S
                if self.state.mode == Mode.AWAKE:
                    buffered = self.audio_gate.process(b"", self.state.mode, session_ready=True)
                    for frame in buffered:
                        await session.send_audio(frame)
                await self._emit("session_open", session)
                self.journal.write("state", state=self.state.mode.value, realtime="connected")
                await self._send_status()
                if session.session_expires_at is not None:
                    refresh_task = asyncio.create_task(self._refresh_session(session))
                async for event in session.events():
                    await self._handle_realtime_event(event)
                if self.state.mode == Mode.SLEEPING:
                    return
                raise RealtimeConnectionError("Realtime event stream ended")
            except RealtimeApiError as exc:
                self._report_error("Realtime API", exc)
                return
            except RealtimeConnectionError as exc:
                self._report_error("Realtime connection", exc)
                if exc.status is not None and 400 <= exc.status < 500:
                    return
                if not self._stopping and self.state.mode != Mode.SLEEPING:
                    await self.clock.sleep(delay * random.uniform(0.8, 1.2))
                    delay = min(_MAX_RECONNECT_S, delay * 2)
            finally:
                if refresh_task is not None:
                    refresh_task.cancel()
                    await asyncio.gather(refresh_task, return_exceptions=True)
                if self._session is session:
                    self._session = None
                await session.close()

    async def _handle_realtime_event(self, event: RealtimeEvent) -> None:
        if isinstance(event, AudioDelta):
            if not event.pcm:
                return
            start_ms = self._output_enqueued_ms
            if self._speech_end_at is not None and self._latency_target_ms is None:
                self._latency_target_ms = start_ms + 1
            self._item_start_ms.setdefault(event.item_id, start_ms)
            duration_ms = len(event.pcm) // 48
            self._item_queued_ms[event.item_id] = (
                self._item_queued_ms.get(event.item_id, 0) + duration_ms
            )
            self._output_enqueued_ms += duration_ms
            self._active_audio_item = event.item_id
            await self._play_queue.put((event.item_id, event.pcm, start_ms))
            await self._emit("audio_out", event.pcm, start_ms)
        elif isinstance(event, AssistantTranscript):
            if event.final:
                self._assistant_text = event.text
            else:
                self._assistant_text += event.text
            await self._send_transcript(event.item_id, "assistant", event.text, event.final)
        elif isinstance(event, UserTranscript):
            await self._send_transcript(event.item_id, "user", event.text, event.final)
            if event.final:
                self._remember_turn("User", event.text)
                self.journal.write("user_transcript", text=event.text)
        elif isinstance(event, SpeechStarted):
            self.journal.write("state", input_speech="started")
            await self._apply(Event("user_speech_start"))
        elif isinstance(event, SpeechStopped):
            await self._apply(Event("user_speech_end"))
        elif isinstance(event, ResponseStarted):
            self._assistant_text = ""
            await self._emit("response_started", event)
            await self._apply(Event("response_started"))
        elif isinstance(event, ToolCall):
            await self._handle_tool_call(event)
        elif isinstance(event, ResponseDone):
            self.journal.write(
                "usage", status=event.status, usage=event.usage, response_id=event.response_id
            )
            if event.status == "completed":
                if self._response_finish_task is not None:
                    self._response_finish_task.cancel()
                self._response_finish_task = self._spawn(
                    self._finish_response(event, self._output_enqueued_ms)
                )
        elif isinstance(event, Error):
            message = f"{event.error_type}: {event.message}"
            self._report_error("Realtime API event", RuntimeError(message))

    async def _handle_tool_call(self, event: ToolCall) -> None:
        self.journal.write("tool_call", name=event.name, call_id=event.call_id)
        result = await self.tools.dispatch(event.name, event.arguments, self)
        result_json = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.journal.write("tool_result", name=event.name, call_id=event.call_id, result=result)
        session = self._session
        if session is not None and self.state.mode != Mode.SLEEPING:
            await session.send_tool_result(event.call_id, result_json)

    async def _finish_response(self, event: ResponseDone, expected_ms: int) -> None:
        try:
            await asyncio.wait_for(self._play_queue.join(), timeout=_PLAYBACK_TIMEOUT_S)
            deadline = self.clock.now() + _PLAYBACK_TIMEOUT_S
            while self.audio.played_ms < expected_ms and self.clock.now() < deadline:
                if self._play_error is not None:
                    break
                await self.clock.sleep(0.02)
        except TimeoutError:
            self._report_error("audio playback", TimeoutError("Response playback did not finish"))
        except asyncio.CancelledError:
            raise
        except (ConnectionError, RuntimeError) as exc:
            self._report_error("audio playback", exc)
        if not self._stopping and self.state.mode != Mode.SLEEPING:
            await self._apply(
                Event("response_done", question=self._assistant_text.rstrip().endswith("?"))
            )
            await self._emit("response_done", event)
            self._remember_turn("Coin-Coin", self._assistant_text)
            self.journal.write("assistant_transcript", text=self._assistant_text)

    async def _playback_loop(self) -> None:
        while True:
            item = await self._play_queue.get()
            try:
                if item is None:
                    return
                _, pcm, start_ms = item
                await self.audio.play(pcm)
                await self._record_audio_latency(start_ms)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, ValueError, RuntimeError) as exc:
                self._play_error = str(exc)
                self._report_error("audio playback", exc)
            finally:
                self._play_queue.task_done()

    def _discard_queued_audio(self) -> None:
        while not self._play_queue.empty():
            try:
                self._play_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._play_queue.task_done()

    async def _send_transcript(self, item_id: str, speaker: str, text: str, final: bool) -> None:
        await self.control(
            {
                "type": "transcript",
                "item_id": item_id,
                "speaker": speaker,
                "text": text,
                "final": final,
            }
        )

    async def _send_status(self) -> None:
        await self.control({"type": "state", "state": self.state.mode.value})

    def _report_error(self, operation: str, error: BaseException) -> None:
        message = _SECRET_TEXT.sub("[REDACTED]", str(error))
        self.journal.write("error", operation=operation, message=message)
        _LOGGER.warning("%s failed: %s", operation, type(error).__name__)
        self._spawn(self._send_control({"type": "error", "message": message}))

    async def _refresh_session(self, session: RealtimeSession) -> None:
        expires_at = session.session_expires_at
        if expires_at is None:
            return
        delay = max(0.0, expires_at - time.time() - _SESSION_RENEW_BEFORE_S)
        await self.clock.sleep(delay)
        if self._session is session and self.state.mode != Mode.SLEEPING:
            self.journal.write("state", realtime="renewing")
            await session.close()

    async def _record_audio_latency(self, start_ms: int) -> None:
        target_ms = self._latency_target_ms
        speech_end_at = self._speech_end_at
        if target_ms is None or speech_end_at is None or start_ms + 1 < target_ms:
            return
        deadline = self.clock.now() + 2.0
        while self.audio.played_ms < target_ms and self.clock.now() < deadline:
            await self.clock.sleep(0.01)
        if self.audio.played_ms >= target_ms:
            self.journal.write(
                "latency",
                mode="realtime",
                speech_to_audio_ms=round((self.clock.now() - speech_end_at) * 1_000, 1),
            )
        self._speech_end_at = None
        self._latency_target_ms = None

    def _remember_turn(self, speaker: str, text: str) -> None:
        value = text.strip()
        if not value:
            return
        self._conversation_history.append(f"{speaker}: {value}")
        self._conversation_history = self._conversation_history[-8:]
        while sum(map(len, self._conversation_history)) > 1_600:
            self._conversation_history.pop(0)

    def _conversation_summary(self) -> str:
        return " ".join(self._conversation_history)

    async def _send_control(self, message: dict[str, object]) -> None:
        await self.control(message)


def main(argv: list[str] | None = None) -> None:
    """Load configuration and start the local browser audio/conversation service."""
    parser = argparse.ArgumentParser(prog="coincoin")
    parser.add_argument("--echo", action="store_true", help="echo microphone audio to playback")
    parser.add_argument(
        "--tone", action="store_true", help="play a 1 kHz tone and measure round-trip latency"
    )
    args = parser.parse_args(argv)
    echo = args.echo or os.environ.get("ECHO", "").strip().lower() in {"1", "true", "yes", "on"}
    if echo and args.tone:
        parser.error("--echo and --tone are mutually exclusive")

    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    journal = Journal(retention_days=settings.log_retention_days)
    journal.write("state", service="starting")
    web.run_app(
        create_app(
            echo=echo,
            tone=args.tone,
            journal=journal,
            settings=None if echo or args.tone else settings,
        ),
        host="0.0.0.0",
        port=8765,
    )


if __name__ == "__main__":
    main()
