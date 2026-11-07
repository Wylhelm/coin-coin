"""Reconnectable WebRTC robot transport for mediad's LAN signalling protocol."""

import asyncio
import contextlib
import io
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription
from aiortc.exceptions import InvalidStateError
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import candidate_from_sdp
from av import VideoFrame
from PIL import Image

from coincoin.clock import Clock, SystemClock
from coincoin.robot.frames import FrameError
from coincoin.robot.jsonrpc import API_VERSION, JsonRpcConnection, JsonRpcError
from coincoin.robot.link import Frame, LinkDown, RobotRefused
from coincoin.robot.methods import ROBOT_METHODS

LOGGER = logging.getLogger(__name__)
LinkCallback = Callable[[], Awaitable[None] | None]
MAX_MOVE_BUFFERED_BYTES = 0
# A camera frame older than this is refused rather than shown as the present.
FRAME_STALE_S = 2.0
# The robot or mediad restarting takes seconds; retry soon enough to be back promptly.
MAX_RECONNECT_S = 10.0


def _encode_video_frame(video_frame: VideoFrame, captured_at: float) -> Frame:
    image = Image.fromarray(video_frame.to_ndarray(format="rgb24"))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=80)
    return Frame(output.getvalue(), image.width, image.height, captured_at)


def signalling_url(url: str) -> str:
    """Validate a mediad URL and translate it to the WebSocket signalling endpoint."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Expected webrtc://host:port") from error
    if (
        parsed.scheme != "webrtc"
        or parsed.hostname is None
        or port is None
        or not 0 < port < 65536
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected webrtc://host:port without credentials, path or query")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"ws://{host}:{port}"


class _DataChannelStream:
    """Adapt one aiortc datachannel to JsonRpcConnection's line stream."""

    def __init__(self, channel: Any) -> None:
        self.channel = channel
        self.reader = asyncio.StreamReader()
        self._latest_move: str | None = None
        self._move_task: asyncio.Task[None] | None = None

    async def readline(self) -> bytes:
        return await self.reader.readline()

    def write(self, data: bytes) -> None:
        if self.channel.readyState != "open":
            raise OSError("WebRTC control channel is not open")
        text = data.decode("utf-8")
        try:
            message = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = None
        if isinstance(message, dict) and message.get("method") not in ROBOT_METHODS:
            LOGGER.warning("WebRTC method refused: %r", message.get("method"))
            raise JsonRpcError(-32601, "refusee par WebRtcLink (S1 method policy)")
        if isinstance(message, dict) and message.get("method") == "robot.stop":
            # STOP is a barrier: never replay an earlier deferred move after it.
            self._latest_move = None
        is_move = isinstance(message, dict) and message.get("method") == "robot.move"
        if is_move and (
            self._latest_move is not None or self.channel.bufferedAmount > MAX_MOVE_BUFFERED_BYTES
        ):
            self._latest_move = text.rstrip("\n")
            if self._move_task is None or self._move_task.done():
                self._move_task = asyncio.create_task(self._flush_latest_move())
            return
        self.channel.send(text.rstrip("\n"))

    async def _flush_latest_move(self) -> None:
        try:
            while self.channel.readyState == "open":
                await asyncio.sleep(0.01)
                if self.channel.bufferedAmount > MAX_MOVE_BUFFERED_BYTES:
                    continue
                message, self._latest_move = self._latest_move, None
                if message is not None:
                    self.channel.send(message)
                if self._latest_move is None:
                    return
        finally:
            self._move_task = None
            self._latest_move = None

    async def drain(self) -> None:
        if self.channel.readyState != "open":
            raise OSError("WebRTC control channel is not open")

    def close(self) -> None:
        if self.channel.readyState != "closed":
            self.channel.close()
        self.reader.feed_eof()

    async def wait_closed(self) -> None:
        return

    def feed_message(self, message: str | bytes) -> None:
        data = message.encode("utf-8") if isinstance(message, str) else message
        if not data.endswith(b"\n"):
            data += b"\n"
        self.reader.feed_data(data)


class WebRtcLink:
    """RobotLink using mediad's producer-offer WebRTC session and `control` channel."""

    def __init__(
        self,
        url: str,
        *,
        clock: Clock | None = None,
        on_up: LinkCallback | None = None,
        on_down: LinkCallback | None = None,
    ) -> None:
        self._signalling_url = signalling_url(url)
        self._clock = clock if clock is not None else SystemClock()
        self.on_up = on_up
        self.on_down = on_down
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._peer: RTCPeerConnection | None = None
        self._channel: Any | None = None
        self._stream: _DataChannelStream | None = None
        self._connection: JsonRpcConnection | None = None
        self._video_task: asyncio.Task[None] | None = None
        self._latest_frame: Frame | None = None
        self._latest_video_frame: VideoFrame | None = None
        self._latest_video_at = 0.0
        self._frame_lock = asyncio.Lock()
        self._pending_ice: list[RTCIceCandidate | None] = []
        self._frame_available = asyncio.Event()
        self._channel_open = asyncio.Event()
        self._peer_connected = asyncio.Event()
        self._peer_lost = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._up = asyncio.Event()
        self.producer_meta: dict[str, Any] = {}
        self.hello: dict[str, Any] | None = None
        self.last_error = ""

    @property
    def connected(self) -> bool:
        return (
            self._connection is not None
            and self._connection.connected
            and self._peer is not None
            and self._peer.connectionState == "connected"
            and self._channel is not None
            and self._channel.readyState == "open"
        )

    async def connect(self) -> None:
        if self.connected:
            return
        if self._runner is None:
            self._ready = asyncio.get_running_loop().create_future()
            self._runner = asyncio.create_task(self._run())
            await asyncio.shield(self._ready)
        else:
            try:
                async with asyncio.timeout(10):
                    while not self.connected:
                        self._up.clear()
                        await self._up.wait()
            except TimeoutError as error:
                raise LinkDown("WebRTC robot link has not reconnected") from error

    async def close(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await runner
        if self._ready is not None and not self._ready.done():
            self._ready.cancel()
        self._ready = None
        self._up.clear()

    def state(self) -> dict[str, Any] | None:
        return self._connection.state() if self._connection is not None else None

    def state_age(self) -> float:
        return self._connection.state_age() if self._connection is not None else float("inf")

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,  # noqa: ASYNC109 - RobotLink contract
    ) -> Any:
        return await self._require_connection().call(method, params, timeout)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._require_connection().notify(method, params)

    async def frame(self) -> Frame:
        if self._latest_frame is not None:
            self._check_fresh(self._latest_video_at)
            return self._latest_frame
        try:
            async with asyncio.timeout(3):
                await self._frame_available.wait()
        except TimeoutError as error:
            raise FrameError("No video frame received from WebRTC peer") from error
        async with self._frame_lock:
            if self._latest_frame is not None:
                self._check_fresh(self._latest_video_at)
                return self._latest_frame
            video_frame = self._latest_video_frame
            if video_frame is None:
                raise FrameError("No video frame received from WebRTC peer")
            captured_at = self._latest_video_at
            self._check_fresh(captured_at)
            frame = await asyncio.to_thread(_encode_video_frame, video_frame, captured_at)
            if video_frame is self._latest_video_frame and captured_at == self._latest_video_at:
                self._latest_frame = frame
            return frame

    @staticmethod
    def _check_fresh(captured_at: float) -> None:
        age = time.time() - captured_at
        if age > FRAME_STALE_S:
            raise FrameError(f"WebRTC camera stalled: last frame is {age:.1f} s old")

    def _require_connection(self) -> JsonRpcConnection:
        if not self.connected or self._connection is None:
            raise LinkDown("WebRTC robot link is down")
        return self._connection

    async def _callback(self, callback: LinkCallback | None) -> None:
        if callback is not None:
            result = callback()
            if result is not None:
                await result

    async def _send_signal(self, message: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise LinkDown("WebRTC signalling socket is closed")
        await self._ws.send_json(
            message, dumps=lambda value: json.dumps(value, separators=(",", ":"))
        )

    async def _receive_signal(self) -> dict[str, Any]:
        if self._ws is None:
            raise LinkDown("WebRTC signalling socket is closed")
        message = await self._ws.receive()
        if message.type != aiohttp.WSMsgType.TEXT:
            raise LinkDown("WebRTC signalling socket closed")
        try:
            result = json.loads(message.data)
        except (json.JSONDecodeError, TypeError) as error:
            raise LinkDown("Invalid WebRTC signalling message") from error
        if not isinstance(result, dict) or not isinstance(result.get("type"), str):
            raise LinkDown("Invalid WebRTC signalling message")
        return result

    async def _open(self) -> JsonRpcConnection:
        try:
            async with asyncio.timeout(20):
                self._session = aiohttp.ClientSession()
                self._ws = await self._session.ws_connect(self._signalling_url, heartbeat=20)
                welcome = await self._receive_signal()
                if welcome["type"] != "welcome":
                    raise LinkDown("mediad did not send a signalling welcome")
                LOGGER.debug("Received mediad signalling welcome")

                await self._send_signal({"type": "list"})
                listing = await self._receive_until({"list"})
                producers = listing.get("producers")
                LOGGER.debug("Received mediad producer listing")
                if not isinstance(producers, list) or not producers:
                    raise LinkDown("mediad has no WebRTC producer")
                producer = next(
                    (
                        item
                        for item in producers
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    ),
                    None,
                )
                if producer is None:
                    raise LinkDown("mediad returned an invalid producer list")
                meta = producer.get("meta")
                self.producer_meta = dict(meta) if isinstance(meta, dict) else {}
                await self._send_signal({"type": "startSession", "peerId": producer["id"]})
                started = await self._receive_until({"sessionStarted"})
                session_id = started.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    raise LinkDown("mediad returned an invalid WebRTC session")
                LOGGER.debug("Started mediad WebRTC session")

                self._peer = RTCPeerConnection()
                self._channel_open.clear()
                self._peer_connected.clear()
                self._peer_lost.clear()
                self._frame_available.clear()
                self._latest_frame = None
                self._latest_video_frame = None
                self._pending_ice.clear()
                self._peer.on("connectionstatechange", self._on_peer_state)
                self._peer.on("track", self._on_track)
                self._peer.on("datachannel", self._on_datachannel)

                async with asyncio.timeout(15):
                    ready = asyncio.create_task(self._wait_transport_ready())
                    signal_task = asyncio.create_task(self._receive_signal())
                    try:
                        while not ready.done():
                            done, _ = await asyncio.wait(
                                (ready, signal_task), return_when=asyncio.FIRST_COMPLETED
                            )
                            if ready in done:
                                ready.result()
                                break
                            signal = signal_task.result()
                            if (
                                signal.get("type") == "peer"
                                and signal.get("sessionId") == session_id
                            ):
                                await self._handle_peer_signal(signal, session_id)
                            elif signal.get("type") in ("endSession", "error"):
                                raise LinkDown("mediad ended or rejected the WebRTC session")
                            signal_task = asyncio.create_task(self._receive_signal())
                    finally:
                        for task in (ready, signal_task):
                            task.cancel()
                        await asyncio.gather(ready, signal_task, return_exceptions=True)
                LOGGER.debug("mediad control channel and ICE connection are ready")

                stream = self._stream
                if stream is None:
                    raise LinkDown("mediad did not open the control datachannel")
                connection = JsonRpcConnection(stream, stream, clock=self._clock)
                self._connection = connection
                self.hello = None
                try:
                    hello = await connection.call("hello", {"api_version": API_VERSION})
                    self.hello = hello if isinstance(hello, dict) else None
                except JsonRpcError:
                    LOGGER.info("mediad hello rejected; trying the state subscription")
                await connection.call("robot.subscribe", {"hz": 10})
                await connection.notify("robot.mouth", {"open": 0})
                return connection
        except BaseException:
            await self._cleanup()
            raise

    async def _receive_until(self, kinds: set[str]) -> dict[str, Any]:
        while True:
            message = await self._receive_signal()
            if message["type"] in kinds:
                return message
            if message["type"] == "error":
                raise LinkDown("mediad rejected the WebRTC signalling request")

    async def _wait_transport_ready(self) -> None:
        async def wait_ready() -> None:
            await asyncio.gather(self._channel_open.wait(), self._peer_connected.wait())

        ready = asyncio.create_task(wait_ready())
        lost = asyncio.create_task(self._peer_lost.wait())
        try:
            done, _ = await asyncio.wait((ready, lost), return_when=asyncio.FIRST_COMPLETED)
            if lost in done:
                raise LinkDown("WebRTC ICE or control channel disconnected during setup")
            await ready
        finally:
            ready.cancel()
            lost.cancel()
            await asyncio.gather(ready, lost, return_exceptions=True)

    async def _handle_peer_signal(self, message: dict[str, Any], session_id: str) -> None:
        if self._peer is None:
            raise LinkDown("WebRTC peer is not initialized")
        description = message.get("sdp")
        if isinstance(description, dict):
            kind, sdp = description.get("type"), description.get("sdp")
            if kind not in ("offer", "answer") or not isinstance(sdp, str):
                raise LinkDown("Invalid WebRTC session description")
            LOGGER.debug("Received WebRTC SDP %s", kind)
            await self._peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=kind))
            if kind == "offer":
                for candidate in self._pending_ice:
                    await self._peer.addIceCandidate(candidate)
                self._pending_ice.clear()
                answer = await self._peer.createAnswer()
                # mediad's webrtcsink consumer session fails DTLS with aiortc's default active role.
                passive_answer = RTCSessionDescription(
                    sdp=answer.sdp.replace("a=setup:active", "a=setup:passive"),
                    type=answer.type,
                )
                await self._peer.setLocalDescription(passive_answer)
                local = self._peer.localDescription
                if local is None:
                    raise LinkDown("WebRTC did not create an answer")
                await self._send_signal(
                    {
                        "type": "peer",
                        "sessionId": session_id,
                        "sdp": {"type": "answer", "sdp": local.sdp},
                    }
                )
            return

        ice = message.get("ice")
        if isinstance(ice, dict):
            candidate_text = ice.get("candidate")
            mline = ice.get("sdpMLineIndex")
            if not isinstance(candidate_text, str) or type(mline) is not int:
                raise LinkDown("Invalid WebRTC ICE candidate")
            candidate = (
                None
                if not candidate_text
                else candidate_from_sdp(candidate_text.removeprefix("candidate:"))
            )
            if candidate is not None:
                candidate.sdpMLineIndex = mline
                candidate.sdpMid = None
            if self._peer.remoteDescription is None:
                self._pending_ice.append(candidate)
            else:
                await self._peer.addIceCandidate(candidate)

    def _on_peer_state(self) -> None:
        if self._peer is None:
            return
        LOGGER.debug("WebRTC ICE connection state: %s", self._peer.connectionState)
        if self._peer.connectionState == "connected":
            self._peer_connected.set()
            self._up.set()
        elif self._peer.connectionState in ("failed", "closed", "disconnected"):
            self._peer_connected.clear()
            self._peer_lost.set()
            self._up.clear()

    def _on_datachannel(self, channel: Any) -> None:
        LOGGER.debug("Received WebRTC datachannel %s", channel.label)
        if channel.label != "control" or self._stream is not None:
            channel.close()
            return
        stream = _DataChannelStream(channel)
        self._channel = channel
        self._stream = stream

        @channel.on("message")
        def on_message(message: str | bytes) -> None:
            stream.feed_message(message)

        @channel.on("close")
        def on_close() -> None:
            stream.reader.feed_eof()

        @channel.on("open")
        def on_open() -> None:
            LOGGER.debug("WebRTC datachannel state: %s", channel.readyState)
            if channel.ordered:
                self._channel_open.set()
                self._up.set()
            else:
                self._peer_lost.set()

        if channel.readyState == "open":
            on_open()

    def _on_track(self, track: Any) -> None:
        if track.kind == "video":
            self._video_task = asyncio.create_task(self._read_video(track))
        else:
            track.stop()

    async def _read_video(self, track: Any) -> None:
        try:
            while True:
                video_frame = await track.recv()
                self._latest_video_frame = video_frame
                self._latest_video_at = time.time()
                self._latest_frame = None
                self._frame_available.set()
        except MediaStreamError:
            return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOGGER.warning("WebRTC video receiver failed (%s)", type(error).__name__)

    async def _wait_link_down(self, connection: JsonRpcConnection) -> None:
        closed = asyncio.create_task(connection.wait_closed())
        peer_lost = asyncio.create_task(self._peer_lost.wait())
        try:
            await asyncio.wait((closed, peer_lost), return_when=asyncio.FIRST_COMPLETED)
        finally:
            closed.cancel()
            peer_lost.cancel()
            await asyncio.gather(closed, peer_lost, return_exceptions=True)

    async def _cleanup(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()
        elif self._stream is not None:
            self._stream.close()
        self._stream = None
        self._channel = None
        if self._video_task is not None:
            self._video_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, MediaStreamError):
                await self._video_task
            self._video_task = None
        if self._peer is not None:
            peer, self._peer = self._peer, None
            await peer.close()
        if self._ws is not None:
            ws, self._ws = self._ws, None
            await ws.close()
        if self._session is not None:
            session, self._session = self._session, None
            await session.close()
        self._latest_frame = None
        self._latest_video_frame = None
        self._latest_video_at = 0.0
        self._pending_ice.clear()
        self._frame_available.clear()
        self._channel_open.clear()
        self._peer_connected.clear()
        self._peer_lost.clear()

    async def _run(self) -> None:
        delay = 0.5
        try:
            while True:
                try:
                    connection = await self._open()
                except (
                    OSError,
                    TimeoutError,
                    LinkDown,
                    JsonRpcError,
                    RobotRefused,
                    aiohttp.ClientError,
                    InvalidStateError,
                    ValueError,
                ) as error:
                    self.last_error = f"{type(error).__name__}: {error}"
                    LOGGER.warning("WebRTC robot connection failed (%s)", type(error).__name__)
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_exception(
                            LinkDown("Could not initialize WebRTC robot link")
                        )
                        self._ready.exception()
                else:
                    delay = 0.5
                    self.last_error = ""
                    await self._callback(self.on_up)
                    self._up.set()
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result(None)
                    await self._wait_link_down(connection)
                    was_up = self._connection is not None
                    await self._cleanup()
                    self._up.clear()
                    if was_up:
                        await self._callback(self.on_down)
                await self._clock.sleep(min(MAX_RECONNECT_S, delay * random.uniform(0.8, 1.2)))
                delay = min(MAX_RECONNECT_S, delay * 2)
        finally:
            was_up = self._connection is not None
            await self._cleanup()
            self._up.clear()
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(LinkDown("WebRTC initialization stopped"))
                self._ready.exception()
            if was_up:
                await self._callback(self.on_down)
