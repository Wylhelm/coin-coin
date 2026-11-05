"""Server-side GA Realtime WebSocket client."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Sequence
from urllib.parse import quote

import aiohttp

from coincoin.config import Settings
from coincoin.realtime.events import RealtimeEvent, parse_event

_OPEN_TIMEOUT_S = 20


class RealtimeConnectionError(ConnectionError):
    """The Realtime WebSocket is unavailable or closed unexpectedly."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class RealtimeApiError(RuntimeError):
    """The Realtime API rejected a request or reported a session error."""

    def __init__(self, error: dict[str, object]) -> None:
        self.error_type = str(error.get("type", "unknown"))
        self.code = str(error["code"]) if error.get("code") is not None else None
        self.param = str(error["param"]) if error.get("param") is not None else None
        self.message = str(error.get("message", "Realtime API error"))
        super().__init__(f"{self.error_type}: {self.message}")


class RealtimeSession:
    """Own one GA Realtime WebSocket and normalize the server event stream."""

    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str = "wss://api.openai.com/v1/realtime",
    ) -> None:
        self.settings = settings
        self.base_url = base_url.rstrip("?")
        self._client: aiohttp.ClientSession | None = None
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[RealtimeEvent | RealtimeConnectionError | None] = (
            asyncio.Queue()
        )
        self._send_lock = asyncio.Lock()
        self.session_expires_at: int | None = None

    @property
    def connected(self) -> bool:
        return self._socket is not None and not self._socket.closed

    async def open(self, instructions: str, tools: Sequence[dict[str, object]]) -> None:
        """Connect to /v1/realtime and wait for the requested session configuration."""
        if self.connected or self._client is not None:
            raise RuntimeError("Realtime session is already open")
        key = self.settings.openai_api_key.get_secret_value()
        if not key:
            raise RealtimeConnectionError("OPENAI_API_KEY is empty")
        model = self.settings.openai_realtime_model or "gpt-realtime-2.1"
        if not model:
            raise RealtimeConnectionError("OPENAI_REALTIME_MODEL is empty")
        voice = self.settings.openai_realtime_voice or "marin"
        if not voice:
            raise RealtimeConnectionError("OPENAI_REALTIME_VOICE is empty")

        url = f"{self.base_url}?model={quote(model, safe='')}"
        self._client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        try:
            self._socket = await asyncio.wait_for(
                self._client.ws_connect(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    heartbeat=20,
                ),
                timeout=_OPEN_TIMEOUT_S,
            )
            created = await self._receive_handshake_event()
            if created.get("type") != "session.created":
                raise RealtimeConnectionError("Realtime did not create a session")
            await self._send(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "model": model,
                        "instructions": instructions,
                        "output_modalities": ["audio"],
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24_000},
                                "transcription": {
                                    "model": "gpt-4o-mini-transcribe",
                                },
                                "turn_detection": {
                                    "type": "server_vad",
                                    "threshold": 0.5,
                                    "prefix_padding_ms": 300,
                                    "silence_duration_ms": 500,
                                    "create_response": True,
                                    "interrupt_response": True,
                                },
                            },
                            "output": {
                                "format": {"type": "audio/pcm", "rate": 24_000},
                                "voice": voice,
                            },
                        },
                        "tools": list(tools),
                        "tool_choice": "auto",
                        "max_output_tokens": 2048,
                    },
                }
            )
            while True:
                event = await self._receive_handshake_event()
                if event.get("type") == "session.updated":
                    session = event.get("session")
                    if isinstance(session, dict):
                        expires_at = session.get("expires_at")
                        if isinstance(expires_at, int):
                            self.session_expires_at = expires_at
                    break
            self._reader_task = asyncio.create_task(self._read_events())
        except BaseException as exc:
            await self._close_transport()
            if isinstance(exc, aiohttp.WSServerHandshakeError):
                request_id = exc.headers.get("x-request-id") if exc.headers else None
                suffix = f" (request {request_id})" if request_id else ""
                raise RealtimeConnectionError(
                    f"Realtime WebSocket handshake failed: HTTP {exc.status}{suffix}",
                    status=exc.status,
                ) from None
            if isinstance(exc, (aiohttp.ClientError, TimeoutError)):
                raise RealtimeConnectionError(
                    f"Realtime WebSocket connection failed: {type(exc).__name__}"
                ) from None
            raise

    async def _receive_handshake_event(self) -> dict[str, object]:
        socket = self._socket
        if socket is None:
            raise RealtimeConnectionError("Realtime WebSocket is not connected")
        try:
            message = await asyncio.wait_for(socket.receive(), timeout=_OPEN_TIMEOUT_S)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise RealtimeConnectionError(
                f"Realtime handshake failed: {type(exc).__name__}"
            ) from None
        if message.type != aiohttp.WSMsgType.TEXT:
            raise RealtimeConnectionError("Realtime WebSocket closed during handshake")
        try:
            event = json.loads(message.data)
        except json.JSONDecodeError:
            raise RealtimeConnectionError("Realtime sent invalid JSON during handshake") from None
        if not isinstance(event, dict):
            raise RealtimeConnectionError("Realtime sent a non-object handshake event")
        if event.get("type") == "error":
            error = event.get("error")
            raise RealtimeApiError(error if isinstance(error, dict) else {})
        return event

    async def _read_events(self) -> None:
        socket = self._socket
        if socket is None:
            return
        try:
            async for message in socket:
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        await self._events.put(
                            RealtimeConnectionError("Realtime sent invalid JSON")
                        )
                        return
                    if isinstance(payload, dict):
                        try:
                            event = parse_event(payload)
                        except (KeyError, TypeError, ValueError) as exc:
                            kind = payload.get("type", "unknown")
                            await self._events.put(
                                RealtimeConnectionError(
                                    f"Realtime sent invalid {kind} event: {type(exc).__name__}"
                                )
                            )
                            return
                        if event is not None:
                            await self._events.put(event)
                elif message.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED}:
                    break
            error = socket.exception()
            detail = type(error).__name__ if error is not None else "WebSocket closed"
            await self._events.put(RealtimeConnectionError(f"Realtime connection lost: {detail}"))
        except asyncio.CancelledError:
            raise
        except aiohttp.ClientError as exc:
            await self._events.put(
                RealtimeConnectionError(f"Realtime connection lost: {type(exc).__name__}")
            )

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        """Yield normalized events until the socket closes."""
        while True:
            event = await self._events.get()
            if event is None:
                return
            if isinstance(event, RealtimeConnectionError):
                raise event
            yield event

    async def _send(self, event: dict[str, object]) -> None:
        socket = self._socket
        if socket is None or socket.closed:
            raise RealtimeConnectionError("Realtime WebSocket is not connected")
        async with self._send_lock:
            try:
                await socket.send_json(event)
            except (aiohttp.ClientError, RuntimeError) as exc:
                raise RealtimeConnectionError(
                    f"Realtime send failed: {type(exc).__name__}"
                ) from None

    async def send_audio(self, pcm24k: bytes) -> None:
        """Append PCM16 mono 24 kHz to the Realtime input buffer."""
        if len(pcm24k) % 2:
            raise ValueError("Realtime audio must contain complete PCM16 samples")
        if pcm24k:
            await self._send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm24k).decode("ascii"),
                }
            )

    async def cancel(self) -> None:
        await self._send({"type": "response.cancel"})

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        if not item_id or audio_end_ms < 0:
            raise ValueError("Truncation requires an item id and nonnegative played duration")
        await self._send(
            {
                "type": "conversation.item.truncate",
                "item_id": item_id,
                "content_index": 0,
                "audio_end_ms": audio_end_ms,
            }
        )

    async def send_tool_result(self, call_id: str, result: str) -> None:
        if not call_id:
            raise ValueError("Tool result requires a call id")
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                },
            }
        )

    async def create_response(self) -> None:
        """Resume once all function outputs from a completed parent have been sent."""
        await self._send({"type": "response.create"})

    async def add_image(self, jpeg: bytes) -> None:
        raise NotImplementedError("Realtime image input is reserved for S7")

    async def close(self) -> None:
        await self._close_transport()
        await self._events.put(None)

    async def _close_transport(self) -> None:
        task = self._reader_task
        self._reader_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        socket = self._socket
        self._socket = None
        if socket is not None and not socket.closed:
            await socket.close()
        client = self._client
        self._client = None
        if client is not None and not client.closed:
            await client.close()
