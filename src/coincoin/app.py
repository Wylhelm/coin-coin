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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from aiohttp import web

from coincoin.audio.gate import AudioGate
from coincoin.audio.io import AudioIO, PlaybackProgress, TrackedPlayback
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

if TYPE_CHECKING:
    from coincoin.robot.feature import _RobotRuntime

_LOGGER = logging.getLogger(__name__)
_SECRET_TEXT = re.compile(r"sk-[A-Za-z0-9_-]+|(?i:Bearer)\s+[^\s,;]+")
_MAX_RECONNECT_S = 30.0
_MIN_RECONNECT_S = 0.5
_PLAYBACK_TIMEOUT_S = 60.0
_SESSION_RENEW_BEFORE_S = 60
_STOP_TIMEOUT_S = 1.0

EventHandler = Callable[..., Any]
SessionFactory = Callable[[Settings], RealtimeSession]
ControlSender = Callable[[dict[str, object]], Awaitable[None]]


async def _discard_control(message: dict[str, object]) -> None:
    _ = message


class Vad(Protocol):
    def feed(self, pcm16_16k: bytes) -> list[VadEvent]: ...

    def reset(self) -> None: ...


@dataclass
class _ToolTurn:
    response_id: str | None
    session: RealtimeSession | None
    generation: int
    calls: list[ToolCall] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)
    status: str | None = None
    sending: bool = False


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
        features: Sequence[Feature] | None = None,
        control: ControlSender = _discard_control,
    ) -> None:
        self.settings = settings
        self.robot: _RobotRuntime | None = None
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
        self._features = tuple(FEATURES if features is None else features)
        self._announcement: str | None = None
        self._announcement_version = 0
        self._session: RealtimeSession | None = None
        self._session_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._stop_tasks: set[asyncio.Task[None]] = set()
        self._audio_hook_tasks: set[asyncio.Task[None]] = set()
        self._play_task: asyncio.Task[None] | None = None
        self._playback_target: PlaybackProgress | None = None
        self._playback_epoch = (
            audio.playback_progress.epoch if isinstance(audio, TrackedPlayback) else None
        )
        self._play_queue: asyncio.Queue[tuple[str, bytes, int, int, int | None] | None] = (
            asyncio.Queue()
        )
        self._response_audio_epoch: int | None = None
        self._play_error: str | None = None
        self._started = False
        self._stopping = False
        self._audio_lock = asyncio.Lock()
        self._audio_generation = 0
        self._audio_send_tasks: set[asyncio.Task[None]] = set()
        self._output_enqueued_samples = 0
        self._audio_segments: list[tuple[str, int, int]] = []
        self._ignore_audio_until_response = False
        self._active_response_id: str | None = None
        self._cancelled_response_ids: set[str] = set()
        self._pending_truncation: tuple[str, int] | None = None
        self._assistant_text = ""
        self._conversation_history: list[str] = []
        self._session_generation = 0
        self._session_task_generation: int | None = None
        self._tool_tasks: set[asyncio.Task[None]] = set()
        # Robot motions outlive an interrupted turn: their outcome is announced when they end.
        self._motion_tool_tasks: set[asyncio.Task[None]] = set()
        self._tool_turns: dict[str | None, _ToolTurn] = {}
        self._speech_end_at: float | None = None
        self._latency_target_ms: int | None = None
        self._response_finish_task: asyncio.Task[None] | None = None
        for definition in default_tools():
            self.register_tool(
                definition.name,
                definition.description,
                definition.schema,
                definition.handler,
                manages_timeout=definition.manages_timeout,
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
        *,
        manages_timeout: bool = False,
    ) -> None:
        """Register a tool; handlers managing timeouts must bound their own execution."""
        self.tools.register(name, description, schema, handler, manages_timeout=manages_timeout)

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
        self._start_robot_stop()
        cleanup = asyncio.create_task(self.stop_pressed())
        await asyncio.gather(*tuple(self._stop_tasks))
        try:
            await asyncio.wait_for(cleanup, timeout=_STOP_TIMEOUT_S)
        except TimeoutError as exc:
            self._report_error("conversation shutdown", exc)
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._tasks.clear()
        if self._session is not None:
            await self._session.close()
            self._session = None
        await self._emit("shutdown")
        self._started = False

    async def notify_action(self, text: str, *, ok: bool) -> None:
        """Publish a bounded, text-only action/refusal to the browser."""
        await self._send_control_safely({"type": "action", "text": text, "ok": ok})

    async def announce(self, message: str) -> None:
        """Defer a robot notice to the current response's single continuation."""
        if self._stopping or self._session is None or not self.active:
            return
        self._announcement = message
        self._announcement_version += 1
        if not self.state.response_active:
            task = self._spawn(self._resume_response(self._session))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)
            await task

    async def _resume_response(
        self, session: RealtimeSession, *, tool_turn: _ToolTurn | None = None
    ) -> None:
        if self._stopping or self._session is not session or not self.active:
            return
        if tool_turn is None:
            # Deferred notice tasks may race; only a tool turn can resume without a notice.
            if self._announcement is None or self.state.response_active:
                return
        elif not self._tool_turn_valid(tool_turn):
            return
        notice = self._announcement
        version = self._announcement_version
        if notice is None:
            await session.create_response()
        else:
            generation = self._session_generation
            response_id = self._active_response_id
            await self._apply(Event("response_started"))
            if (
                self._stopping
                or self._session is not session
                or self._session_generation != generation
                or self._active_response_id != response_id
                or not self.state.response_active
            ):
                return
            try:
                await session.announce(notice)
            except RealtimeConnectionError as exc:
                if self._announcement_version == version:
                    self._announcement = None
                self._report_error("robot announcement", exc)
                await self._apply(Event("response_failed"))
            else:
                # Cancellation retains an unsent notice; a newer outage must survive this send.
                if self._announcement_version == version:
                    self._announcement = None

    def _start_robot_stop(self) -> None:
        if self._stop_tasks:
            return
        task = self._spawn(self._run_robot_stop())
        self._stop_tasks.add(task)
        task.add_done_callback(self._stop_tasks.discard)

    async def _run_robot_stop(self) -> None:
        try:
            async with asyncio.timeout(_STOP_TIMEOUT_S):
                await self._emit("stop")
        except Exception as exc:
            self._report_error("robot stop hook", exc)

    async def request_sleep(self) -> None:
        """Handle the model's sleep tool."""
        await self._apply(Event("sleep_requested"))

    async def stop_pressed(self) -> None:
        """Handle the page's priority STOP control."""
        await self._apply(Event("stop_pressed"))

    async def ui_connected(self) -> None:
        """Synchronize current mode and speech state on every UI connection."""
        await self._send_status()

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
                async with self._audio_lock:
                    if self.state.mode != Mode.AWAKE:
                        continue
                    generation = self._audio_generation
                    outgoing = self.audio_gate.process(
                        pcm24k,
                        self.state.mode,
                        session_ready=self._session is not None,
                    )
                    for frame in outgoing:
                        if generation != self._audio_generation:
                            break
                        await self._send_audio_locked(frame, Mode.AWAKE, generation)
                continue
            if self.vad is None:
                raise RuntimeError("Follow-up audio requires a configured local VAD")
            for event in self.vad.feed(resample_pcm16(pcm24k, 24_000, 16_000)):
                await self._handle_vad_event(event)

    async def _handle_vad_event(self, event: VadEvent) -> None:
        if event.kind == "speech_start":
            await self._apply(Event("user_speech_start"))
        if event.kind in {"speech_start", "speech_frame"} and event.pcm16:
            async with self._audio_lock:
                if self.state.mode != Mode.FOLLOWUP:
                    return
                pcm24k = resample_pcm16(event.pcm16, 16_000, 24_000)
                for frame in self.audio_gate.process(
                    pcm24k, Mode.FOLLOWUP, session_ready=self._session is not None, speech=True
                ):
                    await self._send_audio_locked(frame, Mode.FOLLOWUP, self._audio_generation)
        if event.kind == "speech_end":
            await self._apply(Event("user_speech_end"))

    async def _send_audio(self, pcm24k: bytes) -> None:
        async with self._audio_lock:
            await self._send_audio_locked(pcm24k, self.state.mode, self._audio_generation)

    async def _send_audio_locked(self, pcm24k: bytes, mode: Mode, generation: int) -> None:
        session = self._session
        if (
            session is None
            or self.state.mode == Mode.SLEEPING
            or generation != self._audio_generation
        ):
            return
        task = asyncio.create_task(session.send_audio(pcm24k))
        self._audio_send_tasks.add(task)
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except RealtimeConnectionError as exc:
            self._report_error("audio send", exc)
        else:
            if generation == self._audio_generation and self.state.mode != Mode.SLEEPING:
                self.audio_gate.record_sent(mode, len(pcm24k))
                self.journal.write(
                    "audio_bytes",
                    state=mode.value,
                    bytes=len(pcm24k),
                    totals=self.audio_gate.bytes_sent,
                )
        finally:
            self._audio_send_tasks.discard(task)

    async def _tick_loop(self) -> None:
        while not self._stopping:
            await self.clock.sleep(0.1)
            await self._apply(Event("tick"))

    async def _apply(self, event: Event) -> None:
        was_speaking = self.state.speaking
        old_mode = self.state.mode
        self.state, effects = transition(self.state, event, self.clock.now())
        entering_sleep = old_mode != Mode.SLEEPING and self.state.mode == Mode.SLEEPING
        priority_effects = {effect.kind for effect in effects}
        if event.kind == "wake" and self.state.mode != old_mode:
            self._start_session_after_brain_start()
        if entering_sleep or event.kind == "stop_pressed":
            self._invalidate_conversation(keep_playback="FlushPlayback" in priority_effects)
        elif event.kind == "wake" and self.state.mode != old_mode:
            self.audio_gate.reset()
            if self.vad is not None:
                self.vad.reset()

        if "RobotStop" in priority_effects:
            self._start_robot_stop()
            await asyncio.sleep(0)
        flush_done = False
        cancel_done = False
        if "FlushPlayback" in priority_effects:
            await self._flush_for_barge_in()
            flush_done = True
            await self._apply_effect(
                next(effect for effect in effects if effect.kind == "CancelResponse")
            )
            cancel_done = True
        if event.kind == "wake" and self.state.mode != old_mode:
            await self._emit("wake")

        if event.kind == "user_speech_start" and was_speaking:
            if not flush_done:
                await self._flush_for_barge_in()
                flush_done = True
            if not cancel_done:
                await self._apply_effect(
                    next(effect for effect in effects if effect.kind == "CancelResponse")
                )
                cancel_done = True
            self._cancel_stale_tool_tasks()

        if entering_sleep:
            await self._emit("sleep", summary=event.summary)
        if event.kind == "user_speech_start":
            self._speech_end_at = None
            self._latency_target_ms = None
        elif event.kind == "user_speech_end":
            self._speech_end_at = self.clock.now()
        if event.kind == "user_speech_start" and was_speaking:
            await self._emit("barge_in")
        if self.state.mode != old_mode:
            self.journal.write("state", state=self.state.mode.value)
            await self._send_status()
        elif self.state.speaking != was_speaking or event.kind in {
            "response_started",
            "response_done",
            "response_failed",
        }:
            await self._send_status()

        for effect in effects:
            if effect.kind == "RobotStop":
                continue
            if effect.kind == "FlushPlayback" and flush_done:
                continue
            if effect.kind == "CancelResponse" and cancel_done:
                continue
            if effect.kind == "BrainSessionStart" and event.kind == "wake":
                continue
            if effect.kind == "OpenSession" and event.kind == "wake":
                continue
            if effect.kind == "Quack" and entering_sleep:
                await asyncio.gather(*tuple(self._stop_tasks), return_exceptions=True)
            await self._apply_effect(effect)
        if (
            event.kind == "response_failed"
            and self._announcement is not None
            and self._session is not None
            and not self._stopping
        ):
            task = self._spawn(self._resume_response(self._session))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)

    def _invalidate_conversation(self, *, keep_playback: bool = False) -> None:
        self._announcement = None
        self._session_generation += 1
        self._audio_generation += 1
        self._ignore_audio_until_response = True
        if self._response_finish_task is not None:
            self._response_finish_task.cancel()
            self._response_finish_task = None
        self.audio_gate.reset()
        if self.vad is not None:
            self.vad.reset()
        if not keep_playback:
            self._discard_queued_audio()
            self._audio_segments.clear()
            self._output_enqueued_samples = 0
        if self._active_response_id is not None:
            self._cancelled_response_ids.add(self._active_response_id)
        self._active_response_id = None
        for task in tuple(self._audio_send_tasks):
            task.cancel()
        self._cancel_stale_tool_tasks()
        session_task = self._session_task
        if session_task is not None and session_task is not asyncio.current_task():
            session_task.cancel()

    def _cancel_stale_tool_tasks(self) -> None:
        for turn in self._tool_turns.values():
            turn.status = "cancelled"
        for task in tuple(self._tool_tasks):
            if task is not asyncio.current_task() and task not in self._motion_tool_tasks:
                task.cancel()

    def _start_session_after_brain_start(self) -> None:
        generation = self._session_generation
        if (
            self._session_task is not None
            and not self._session_task.done()
            and self._session_task_generation == generation
        ):
            return
        self._session_task_generation = generation
        self._session_task = self._spawn(self._open_after_brain_start(generation))

    async def _open_after_brain_start(self, generation: int) -> None:
        await self._emit("brain_session_start")
        if generation == self._session_generation and self.state.mode != Mode.SLEEPING:
            await self._session_loop(generation)

    async def _apply_effect(self, effect: Effect) -> None:
        if effect.kind == "OpenSession":
            if self._session_task is None or self._session_task.done():
                self._session_task = self._spawn(self._session_loop())
        elif effect.kind == "CloseSession":
            session = self._session
            self._session = None
            if session is not None:
                await session.close()
        elif effect.kind == "FlushPlayback":
            await self._flush_for_barge_in()
        elif effect.kind == "CancelResponse":
            session = self._session
            truncation = self._pending_truncation
            self._pending_truncation = None
            if session is not None:
                self._spawn(self._cancel_and_truncate(session, truncation))
        elif effect.kind == "SetWakeEnabled":
            if effect.value and self.wake_engine is not None:
                self.wake_engine.reset()
        elif effect.kind == "Quack":
            await self._emit("quack", tag=effect.value)
            if not self._handlers.get("quack"):
                await self._apply(Event("quack_done"))
        elif effect.kind == "RobotStop":
            return
        elif effect.kind == "ResetPose":
            await self._emit("reset_pose")
        elif effect.kind == "MouthClose":
            await self._emit("mouth_close")
        elif effect.kind == "BrainSessionStart":
            await self._emit("brain_session_start")
        elif effect.kind == "BrainSessionEnd":
            await self._emit("brain_session_end", summary=effect.value)

    async def _cancel_and_truncate(
        self, session: RealtimeSession, truncation: tuple[str, int] | None
    ) -> None:
        try:
            await asyncio.wait_for(session.cancel(), timeout=0.25)
        except (RealtimeConnectionError, TimeoutError) as exc:
            self._report_error("response cancellation", exc)
        if truncation is not None:
            try:
                await asyncio.wait_for(
                    session.truncate(*truncation),
                    timeout=0.25,
                )
            except (RealtimeConnectionError, TimeoutError) as exc:
                self._report_error("response truncation", exc)

    async def _flush_for_barge_in(self) -> None:
        played_samples = (
            self.audio.playback_progress.played_samples
            if isinstance(self.audio, TrackedPlayback)
            else self.audio.played_ms * 24
        )
        segment = next(
            (entry for entry in self._audio_segments if played_samples < entry[2]),
            self._audio_segments[-1] if self._audio_segments else None,
        )
        if segment is not None:
            item_id, item_start, item_end = segment
            heard_samples = min(item_end, max(item_start, played_samples)) - item_start
            self._pending_truncation = item_id, heard_samples // 24
        self._audio_generation += 1
        self._ignore_audio_until_response = True
        if self._active_response_id is not None:
            self._cancelled_response_ids.add(self._active_response_id)
        self._cancel_stale_tool_tasks()
        if self._response_finish_task is not None:
            self._response_finish_task.cancel()
            self._response_finish_task = None
        if self._play_task is not None:
            self._play_task.cancel()
        for task in tuple(self._audio_hook_tasks):
            task.cancel()
        for task in tuple(self._audio_send_tasks):
            task.cancel()
        try:
            await self.audio.flush()
        except (ConnectionError, TimeoutError, RuntimeError) as exc:
            self._report_error("audio flush", exc)
        self._discard_queued_audio()
        self._output_enqueued_samples = 0
        self._audio_segments.clear()
        self._playback_target = None
        if isinstance(self.audio, TrackedPlayback):
            self._playback_epoch = self.audio.playback_progress.epoch
        self._play_error = None

    async def _session_loop(self, generation: int | None = None) -> None:
        generation = self._session_generation if generation is None else generation
        delay = _MIN_RECONNECT_S
        while (
            not self._stopping
            and generation == self._session_generation
            and self.state.mode != Mode.SLEEPING
        ):
            session = self.session_factory(self.settings)
            refresh_task: asyncio.Task[None] | None = None
            try:
                instructions_context: dict[str, str] = {}
                for key, provider in self._context_providers.items():
                    value = provider()
                    instructions_context[key] = await value if inspect.isawaitable(value) else value
                instructions_context["conversation_summary"] = self._conversation_summary()
                await session.open(
                    build_instructions(
                        instructions_context, language=self.settings.response_language
                    ),
                    self.tools.definitions(),
                )
                async with self._audio_lock:
                    if generation != self._session_generation or self.state.mode == Mode.SLEEPING:
                        return
                    self._session = session
                    buffered = self.audio_gate.process(b"", self.state.mode, session_ready=True)
                    for frame in buffered:
                        await self._send_audio_locked(
                            frame, self.state.mode, generation=self._audio_generation
                        )
                delay = _MIN_RECONNECT_S
                await self._emit("session_open", session)
                self.journal.write("state", state=self.state.mode.value, realtime="connected")
                await self._send_status()
                if session.session_expires_at is not None:
                    refresh_task = asyncio.create_task(self._refresh_session(session))
                async for event in session.events():
                    if generation != self._session_generation:
                        return
                    await self._handle_realtime_event(event)
                if self.state.mode == Mode.SLEEPING:
                    return
                raise RealtimeConnectionError("Realtime event stream ended")
            except RealtimeApiError as exc:
                self._report_error("Realtime API", exc)
                return
            except RealtimeConnectionError as exc:
                self._report_error("Realtime connection", exc)
                if self.state.response_active:
                    await self._apply(Event("response_failed"))
                if exc.status is not None and 400 <= exc.status < 500:
                    return
                if (
                    not self._stopping
                    and generation == self._session_generation
                    and self.state.mode != Mode.SLEEPING
                ):
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
            if (
                not event.pcm
                or self._ignore_audio_until_response
                or (
                    event.response_id is not None
                    and event.response_id in self._cancelled_response_ids
                )
                or (
                    self._active_response_id is not None
                    and event.response_id != self._active_response_id
                )
            ):
                return
            if (
                isinstance(self.audio, TrackedPlayback)
                and self._response_audio_epoch is not None
                and self._response_audio_epoch != self.audio.playback_progress.epoch
            ):
                self._play_error = (
                    "Browser playback page or generation was replaced during response"
                )
                return
            self._sync_playback_epoch()
            self._response_audio_epoch = self._playback_epoch
            start_sample = self._output_enqueued_samples
            if self._speech_end_at is not None and self._latency_target_ms is None:
                self._latency_target_ms = start_sample // 24 + 1
            end_sample = start_sample + len(event.pcm) // 2
            if (
                self._audio_segments
                and self._audio_segments[-1][0] == event.item_id
                and self._audio_segments[-1][2] == start_sample
            ):
                previous = self._audio_segments[-1]
                self._audio_segments[-1] = (previous[0], previous[1], end_sample)
            else:
                self._audio_segments.append((event.item_id, start_sample, end_sample))
            self._output_enqueued_samples = end_sample
            await self._play_queue.put(
                (
                    event.item_id,
                    event.pcm,
                    start_sample,
                    self._audio_generation,
                    self._playback_epoch,
                )
            )
        elif isinstance(event, AssistantTranscript):
            if event.final:
                self._assistant_text = event.text
                self.journal.write("assistant_transcript", text=event.text)
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
            if event.response_id in self._cancelled_response_ids:
                return
            self._assistant_text = ""
            self._response_audio_epoch = None
            self._active_response_id = event.response_id
            self._ignore_audio_until_response = False
            if self._response_finish_task is not None:
                self._response_finish_task.cancel()
                self._response_finish_task = None
            await self._apply(Event("response_started"))
            await self._emit("response_started", event)
        elif isinstance(event, ToolCall):
            response_id = event.response_id or self._active_response_id
            if (
                self.state.mode == Mode.SLEEPING
                or response_id in self._cancelled_response_ids
                or (response_id is None and self._ignore_audio_until_response)
            ):
                self.journal.write(
                    "tool_rejected",
                    call_id=event.call_id,
                    response_id=response_id,
                    reason="Inactive or interrupted response",
                )
                return
            turn = self._tool_turns.setdefault(
                response_id, _ToolTurn(response_id, self._session, self._session_generation)
            )
            if turn.status is not None or any(call.call_id == event.call_id for call in turn.calls):
                return
            turn.calls.append(event)
            task = self._spawn(self._handle_tool_call(event, turn))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)
            if self.tools.manages_timeout(event.name):
                self._motion_tool_tasks.add(task)
                task.add_done_callback(self._motion_tool_tasks.discard)
        elif isinstance(event, ResponseDone):
            self.journal.write(
                "usage", status=event.status, usage=event.usage, response_id=event.response_id
            )
            if event.response_id in self._cancelled_response_ids:
                return
            turn = self._tool_turns.get(event.response_id)
            if turn is not None:
                turn.status = event.status
                if event.status == "completed":
                    task = self._spawn(self._complete_tool_turn(turn))
                    self._tool_tasks.add(task)
                    task.add_done_callback(self._tool_tasks.discard)
                else:
                    self._cancel_stale_tool_tasks()
            if event.status == "completed":
                if turn is not None:
                    return
                if self._response_finish_task is not None:
                    self._response_finish_task.cancel()
                self._response_finish_task = self._spawn(
                    self._finish_response(event, self._output_enqueued_samples)
                )
            else:
                if event.response_id is not None:
                    self._cancelled_response_ids.add(event.response_id)
                self._ignore_audio_until_response = True
                self._active_response_id = None
                await self._apply(Event("response_failed"))
        elif isinstance(event, Error):
            message = f"{event.error_type}: {event.message}"
            self._report_error("Realtime API event", RuntimeError(message))

    def _tool_turn_valid(self, turn: _ToolTurn) -> bool:
        return (
            turn.session is not None
            and self._session is turn.session
            and turn.generation == self._session_generation
            and turn.response_id not in self._cancelled_response_ids
            and turn.status in {None, "completed"}
            and (turn.response_id is None or turn.response_id == self._active_response_id)
            and self.state.mode != Mode.SLEEPING
        )

    async def _handle_tool_call(self, event: ToolCall, turn: _ToolTurn) -> None:
        if not self._tool_turn_valid(turn):
            return
        started = self.clock.now()
        self.journal.write("tool_call", name=event.name, call_id=event.call_id)
        result = await self.tools.dispatch(event.name, event.arguments, self)
        result_json = json.dumps(result, ensure_ascii=False, allow_nan=False)
        self.journal.write(
            "tool_result",
            name=event.name,
            call_id=event.call_id,
            result=result,
            latency_ms=round((self.clock.now() - started) * 1000, 3),
        )
        turn.results[event.call_id] = result_json
        if not self._tool_turn_valid(turn) and self.tools.manages_timeout(event.name):
            await self._announce_late_result(event, result)
            return
        await self._complete_tool_turn(turn)

    async def _announce_late_result(self, event: ToolCall, result: dict[str, Any]) -> None:
        """Tell the conversation how an interrupted robot action ended (arrived, refused...)."""
        if self._stopping or self.state.mode == Mode.SLEEPING:
            return
        outcome = (
            f"it succeeded: {result.get('detail') or 'done'}"
            if result.get("ok")
            else f"it failed: {result.get('reason')}"
        )
        message = (
            f"the earlier {event.name} request ({event.arguments}) was interrupted in the "
            f"conversation but the robot kept going; it has now ended and {outcome}"
        )
        self.journal.write("tool_result_late", name=event.name, call_id=event.call_id)
        await self.announce(message)

    async def _complete_tool_turn(self, turn: _ToolTurn) -> None:
        if (
            not self._tool_turn_valid(turn)
            or turn.status != "completed"
            or turn.sending
            or len(turn.results) != len(turn.calls)
        ):
            return
        session = turn.session
        if session is None:
            return
        turn.sending = True
        try:
            for call in turn.calls:
                if not self._tool_turn_valid(turn):
                    return
                await session.send_tool_result(call.call_id, turn.results[call.call_id])
            if self._tool_turn_valid(turn):
                await self._resume_response(session, tool_turn=turn)
        except RealtimeConnectionError as exc:
            self._report_error("tool result send", exc)
            if self._tool_turn_valid(turn):
                turn.status = "failed"
                await self._apply(Event("response_failed"))

    def _sync_playback_epoch(self) -> None:
        if not isinstance(self.audio, TrackedPlayback):
            return
        epoch = self.audio.playback_progress.epoch
        if epoch != self._playback_epoch:
            self._playback_epoch = epoch
            self._audio_generation += 1
            self._discard_queued_audio()
            self._output_enqueued_samples = 0
            self._audio_segments.clear()
            self._playback_target = None
            self._play_error = None

    async def _finish_response(self, event: ResponseDone, expected_samples: int) -> None:
        failed = False
        try:
            await asyncio.wait_for(self._play_queue.join(), timeout=_PLAYBACK_TIMEOUT_S)
            deadline = self.clock.now() + _PLAYBACK_TIMEOUT_S
            target = self._playback_target
            while True:
                if self._play_error is not None:
                    raise RuntimeError(self._play_error)
                if isinstance(self.audio, TrackedPlayback):
                    progress = self.audio.playback_progress
                    if (
                        self._response_audio_epoch is not None
                        and progress.epoch != self._response_audio_epoch
                    ):
                        raise ConnectionError("Browser playback page or generation was replaced")
                    if target is None:
                        break
                    if progress.epoch != target.epoch:
                        raise ConnectionError("Browser playback page or generation was replaced")
                    finished = progress.played_samples >= target.sent_samples
                else:
                    finished = self.audio.played_ms * 24 >= expected_samples or (
                        not self.audio.playing and self.audio.played_ms >= expected_samples // 24
                    )
                if finished:
                    break
                if self.clock.now() >= deadline:
                    raise TimeoutError("Response playback progress timed out")
                await self.clock.sleep(0.02)
        except TimeoutError:
            failed = True
            self._report_error("audio playback", TimeoutError("Response playback did not finish"))
        except asyncio.CancelledError:
            raise
        except (ConnectionError, RuntimeError) as exc:
            failed = True
            self._report_error("audio playback", exc)
        if not self._stopping and self.state.mode != Mode.SLEEPING:
            if failed:
                await self._apply(Event("response_failed"))
                return
            await self._apply(
                Event("response_done", question=self._assistant_text.rstrip().endswith("?"))
            )
            await self._emit("response_done", event)
            self._remember_turn("Coin-Coin", self._assistant_text)
            if self._announcement is not None and self._session is not None:
                task = self._spawn(self._resume_response(self._session))
                self._tool_tasks.add(task)
                task.add_done_callback(self._tool_tasks.discard)

    async def _playback_loop(self) -> None:
        while True:
            item = await self._play_queue.get()
            try:
                if item is None:
                    return
                _, pcm, start_sample, generation, epoch = item
                if generation != self._audio_generation or self._ignore_audio_until_response:
                    continue
                if (
                    isinstance(self.audio, TrackedPlayback)
                    and epoch != self.audio.playback_progress.epoch
                ):
                    raise ConnectionError("Browser playback page or generation was replaced")
                self._play_task = asyncio.create_task(self.audio.play(pcm))
                await asyncio.sleep(0)
                if generation == self._audio_generation and not self._play_task.cancelled():
                    hook = self._spawn(self._emit("audio_out", pcm, start_sample // 24))
                    self._audio_hook_tasks.add(hook)
                    hook.add_done_callback(self._audio_hook_tasks.discard)
                await self._play_task
                if isinstance(self.audio, TrackedPlayback):
                    self._playback_target = self.audio.playback_progress
                await self._record_audio_latency(start_sample // 24)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    if self._play_task is not None:
                        self._play_task.cancel()
                    raise
            except (ConnectionError, ValueError, RuntimeError) as exc:
                self._play_error = str(exc)
                self._report_error("audio playback", exc)
            finally:
                self._play_task = None
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
        await self._send_control_safely(
            {
                "type": "transcript",
                "item_id": item_id,
                "speaker": speaker,
                "text": text,
                "final": final,
            }
        )

    async def _send_status(self) -> None:
        await self._send_control_safely(
            {"type": "state", "state": self.state.mode.value, "speaking": self.state.speaking}
        )

    def _report_error(self, operation: str, error: BaseException) -> None:
        message = _SECRET_TEXT.sub("[REDACTED]", str(error))
        self.journal.write("error", operation=operation, message=message)
        _LOGGER.warning("%s failed: %s", operation, type(error).__name__)
        self._spawn(self._deliver_error(message))

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

    async def _send_control_safely(self, message: dict[str, object]) -> None:
        try:
            await asyncio.wait_for(self.control(message), timeout=0.1)
        except Exception as exc:
            _LOGGER.warning("UI control delivery failed: %s", type(exc).__name__)

    async def _deliver_error(self, message: str) -> None:
        try:
            await asyncio.wait_for(self.control({"type": "error", "message": message}), timeout=0.1)
        except Exception as exc:
            _LOGGER.warning("UI error delivery failed: %s", type(exc).__name__)


def main(argv: list[str] | None = None) -> None:
    """Load configuration and start the local browser audio/conversation service."""
    parser = argparse.ArgumentParser(prog="coincoin")
    parser.add_argument("--echo", action="store_true", help="echo microphone audio to playback")
    parser.add_argument(
        "--host", default="0.0.0.0", help="HTTP bind address (default: all interfaces)"
    )
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
        host=args.host,
        port=8765,
    )


if __name__ == "__main__":
    main()
