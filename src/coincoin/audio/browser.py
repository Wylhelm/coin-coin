"""Browser-backed AudioIO transport over paired audio and UI WebSockets."""

import asyncio
import logging
import struct
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from aiohttp import web

_LOGGER = logging.getLogger(__name__)
_FRAME_SAMPLES = 480
_FRAME_BYTES = _FRAME_SAMPLES * 2
_MAX_MIC_FRAMES = 100
_MAX_AHEAD_SAMPLES = 24_000 * 300 // 1_000
_SEQUENCE_HEADER = struct.Struct("<Q")
_FLUSH_TIMEOUT = 0.1


@dataclass(slots=True)
class _Page:
    audio_ws: web.WebSocketResponse
    ui_ws: web.WebSocketResponse | None = None
    sent_samples: int = 0
    played_ms: int = 0
    last_sequence: int = 0
    is_playing: bool = False
    generation: int = 0
    closed: bool = False
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    flush_complete: asyncio.Event = field(default_factory=asyncio.Event)
    flushed: asyncio.Event = field(default_factory=asyncio.Event)
    flush_error: str | None = None

    def __post_init__(self) -> None:
        self.flush_complete.set()


class BrowserAudio:
    """Implement AudioIO using the one currently connected browser page."""

    def __init__(self) -> None:
        self._page: _Page | None = None
        self._page_lock = asyncio.Lock()
        self._mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_MAX_MIC_FRAMES)
        self._last_sequence = 0
        self.dropped_mic_frames = 0

    async def connect_audio(self, websocket: web.WebSocketResponse) -> _Page:
        async with self._page_lock:
            previous = self._page
            if previous is not None:
                await self._replace(previous)
            self._drain_mic_queue()
            page = _Page(websocket)
            self._page = page
            return page

    async def connect_ui(self, websocket: web.WebSocketResponse) -> _Page | None:
        async with self._page_lock:
            page = self._page
            if page is None or page.closed:
                return None
            if page.ui_ws is not None and not page.ui_ws.closed:
                await self._send_control(page.ui_ws, {"type": "replaced"})
                await page.ui_ws.close(code=4001, message=b"Page replaced")
            page.ui_ws = websocket
            return page

    async def disconnect_ui(self, page: _Page, websocket: web.WebSocketResponse) -> None:
        async with self._page_lock:
            if page.ui_ws is websocket:
                page.ui_ws = None
                async with page.condition:
                    page.condition.notify_all()

    async def disconnect(self, page: _Page) -> None:
        async with self._page_lock:
            page.closed = True
            page.flushed.set()
            async with page.condition:
                page.condition.notify_all()
            if self._page is page:
                self._page = None
                self._drain_mic_queue()

    async def receive_mic(self, page: _Page, pcm: bytes) -> None:
        if page.closed:
            return
        if len(pcm) % 2:
            raise ValueError("Audio WebSocket expects little-endian PCM16")
        for offset in range(0, len(pcm), _FRAME_BYTES):
            frame = pcm[offset : offset + _FRAME_BYTES]
            if len(frame) < _FRAME_BYTES:
                frame += bytes(_FRAME_BYTES - len(frame))
            if self._mic_queue.full():
                self._mic_queue.get_nowait()
                self.dropped_mic_frames += 1
            self._mic_queue.put_nowait(frame)

    async def mic_frames(self) -> AsyncGenerator[bytes, None]:
        """Yield mic frames, dropping the oldest frame when the 2s queue fills."""
        while True:
            yield await self._mic_queue.get()

    async def play(self, pcm: bytes) -> None:
        """Send unpadded PCM chunks while keeping no more than 300 ms queued."""
        if len(pcm) % 2:
            raise ValueError("Playback expects little-endian PCM16")
        page = self._page
        if page is None or page.closed or page.ui_ws is None:
            raise ConnectionError("No browser audio page is connected")
        while True:
            await page.flush_complete.wait()
            async with page.condition:
                if page.closed:
                    raise ConnectionError("Browser audio page disconnected")
                if page.flush_error is not None:
                    raise ConnectionError(
                        f"Browser audio playback is unavailable: {page.flush_error}"
                    )
                if page.flush_complete.is_set():
                    generation = page.generation
                    break
        for offset in range(0, len(pcm), _FRAME_BYTES):
            frame = pcm[offset : offset + _FRAME_BYTES]
            frame_samples = len(frame) // 2
            while True:
                await page.flush_complete.wait()
                async with page.condition:
                    await page.condition.wait_for(
                        lambda frame_samples=frame_samples: (
                            page.closed
                            or page.flush_error is not None
                            or not page.flush_complete.is_set()
                            or page.generation != generation
                            or page.ui_ws is None
                            or page.ui_ws.closed
                            or page.sent_samples - page.played_ms * 24 + frame_samples
                            <= _MAX_AHEAD_SAMPLES
                        )
                    )
                    if page.closed:
                        raise ConnectionError("Browser audio page disconnected")
                    if page.flush_error is not None:
                        raise ConnectionError(
                            f"Browser audio playback is unavailable: {page.flush_error}"
                        )
                    if page.ui_ws is None or page.ui_ws.closed:
                        raise ConnectionError("Browser UI WebSocket disconnected")
                    if page.generation != generation:
                        return
                    if not page.flush_complete.is_set():
                        continue
                    self._last_sequence += 1
                    sequence = self._last_sequence
                    page.last_sequence = sequence
                    page.sent_samples += frame_samples
                    break
            async with page.send_lock:
                if page.generation != generation:
                    return
                try:
                    await page.audio_ws.send_bytes(_SEQUENCE_HEADER.pack(sequence) + frame)
                except (ConnectionError, RuntimeError) as exc:
                    raise ConnectionError("Browser audio page disconnected") from exc

    async def flush(self) -> None:
        """Flush browser playback within 100 ms or raise an explicit failure."""
        page = self._page
        if page is None or page.closed:
            return
        deadline = asyncio.get_running_loop().time() + _FLUSH_TIMEOUT
        gated = False
        phase = "waiting for flush lock"
        try:
            async with asyncio.timeout_at(deadline):
                async with page.flush_lock:
                    if page.closed or self._page is not page:
                        raise ConnectionError("Browser audio page was replaced")
                    page.flush_complete.clear()
                    page.flush_error = None
                    gated = True
                    phase = "updating playback generation"
                    async with page.condition:
                        page.generation += 1
                        page.sent_samples = 0
                        page.played_ms = 0
                        page.is_playing = False
                        page.flushed.clear()
                        page.condition.notify_all()
                        generation = page.generation
                        through_sequence = page.last_sequence
                    ui_ws = page.ui_ws
                    if ui_ws is None or ui_ws.closed:
                        raise ConnectionError("Browser UI WebSocket is disconnected")
                    phase = "sending flush command"
                    try:
                        await self._send_control(
                            ui_ws,
                            {
                                "type": "flush",
                                "generation": generation,
                                "through_sequence": str(through_sequence),
                            },
                        )
                    except (ConnectionError, RuntimeError) as exc:
                        raise ConnectionError("Could not send browser audio flush command") from exc
                    phase = "waiting for flush acknowledgement"
                    await page.flushed.wait()
                    if page.closed or self._page is not page:
                        raise ConnectionError("Browser audio page was replaced during flush")
                    if page.generation != generation:
                        raise ConnectionError("Browser audio flush generation changed")
                    page.flush_error = None
                    page.flush_complete.set()
                    gated = False
        except TimeoutError as exc:
            if gated:
                self._fail_flush(page, f"flush timed out while {phase}")
            _LOGGER.warning("Browser audio flush exceeded its 100 ms deadline while %s", phase)
            raise TimeoutError(f"Browser audio flush exceeded 100 ms while {phase}") from exc
        except asyncio.CancelledError:
            if gated:
                self._fail_flush(page, "flush was cancelled")
            raise
        except Exception as exc:
            if gated:
                self._fail_flush(page, str(exc))
            raise

    @staticmethod
    def _fail_flush(page: _Page, reason: str) -> None:
        page.flush_error = reason
        page.flush_complete.set()
        asyncio.create_task(BrowserAudio._notify_condition(page))

    @staticmethod
    async def _notify_condition(page: _Page) -> None:
        async with page.condition:
            page.condition.notify_all()

    async def send_control(self, message: dict[str, object]) -> None:
        """Send a UI event to the active browser, if one is connected."""
        page = self._page
        if page is not None and page.ui_ws is not None and not page.ui_ws.closed:
            await self._send_control(page.ui_ws, message)

    async def update_playback(
        self, page: _Page, played_ms: int, *, playing: bool, generation: int
    ) -> None:
        if generation != page.generation or page.closed:
            return
        async with page.condition:
            page.played_ms = max(page.played_ms, min(played_ms, page.sent_samples // 24))
            page.is_playing = playing
            page.condition.notify_all()

    def acknowledge_flush(self, page: _Page, generation: int) -> None:
        if generation == page.generation:
            page.flushed.set()

    @property
    def playing(self) -> bool:
        page = self._page
        return page is not None and not page.closed and page.is_playing

    @property
    def played_ms(self) -> int:
        page = self._page
        return 0 if page is None or page.closed else page.played_ms

    async def _replace(self, page: _Page) -> None:
        page.closed = True
        page.flushed.set()
        async with page.condition:
            page.condition.notify_all()
        if page.ui_ws is not None and not page.ui_ws.closed:
            await self._send_control(page.ui_ws, {"type": "replaced"})
            await page.ui_ws.close(code=4001, message=b"Page replaced")
        if not page.audio_ws.closed:
            await page.audio_ws.close(code=4001, message=b"Page replaced")

    @staticmethod
    async def _send_control(websocket: web.WebSocketResponse, message: dict[str, object]) -> None:
        await websocket.send_json(message)

    def _drain_mic_queue(self) -> None:
        while not self._mic_queue.empty():
            self._mic_queue.get_nowait()
