"""End-to-end WebRTC signalling, control JSON-RPC and video tests against a local mediad peer."""

import asyncio
import json
import math
from io import BytesIO
from typing import Any

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame
from PIL import Image

from coincoin.robot import webrtc_link as webrtc_module
from coincoin.robot.jsonrpc import API_VERSION
from coincoin.robot.link import LinkDown, RobotLink, RobotRefused
from coincoin.robot.webrtc_link import (
    MAX_MOVE_BUFFERED_BYTES,
    WebRtcLink,
    _DataChannelStream,
    signalling_url,
)


async def until(predicate) -> None:
    async with asyncio.timeout(15):
        while not predicate():  # noqa: ASYNC110 - bounded network observation
            await asyncio.sleep(0.01)


class PatternVideoTrack(VideoStreamTrack):
    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()
        frame = VideoFrame.from_ndarray(
            np.full((24, 32, 3), (10, 180, 30), dtype=np.uint8), format="rgb24"
        )
        frame.pts = pts
        frame.time_base = time_base
        return frame


class LocalMediad:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.answer_sdps: list[str] = []
        self.sessions = 0
        self.disconnect_once = False
        self._peers: set[RTCPeerConnection] = set()
        self._server = TestServer(web.Application())
        self._server.app.router.add_get("/", self._handle)

    @property
    def url(self) -> str:
        return f"webrtc://127.0.0.1:{self._server.port}"

    async def start(self) -> None:
        await self._server.start_server()

    async def close(self) -> None:
        await asyncio.gather(
            *(peer.close() for peer in self._peers if peer.connectionState != "closed"),
            return_exceptions=True,
        )
        await self._server.close()

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self._peers.add(peer)
        channel = None
        await ws.send_json({"type": "welcome", "peerId": "local-consumer"})
        try:
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    continue
                signal = json.loads(message.data)
                match signal.get("type"):
                    case "list":
                        await ws.send_json(
                            {
                                "type": "list",
                                "producers": [{"id": "local-robot", "meta": {"name": "test"}}],
                            }
                        )
                    case "startSession":
                        self.sessions += 1
                        session_id = f"session-{self.sessions}"
                        await ws.send_json({"type": "sessionStarted", "sessionId": session_id})
                        channel = peer.createDataChannel("control")
                        peer.addTrack(PatternVideoTrack())

                        @channel.on("message")
                        def on_message(raw: str | bytes, control: Any = channel) -> None:
                            request_data = json.loads(raw)
                            self.records.append(request_data)
                            request_id = request_data.get("id")
                            method = request_data.get("method")
                            if request_id is None:
                                return
                            result: Any = {"accepted": True}
                            if method == "hello":
                                result = {"api_version": API_VERSION}
                            elif method == "robot.health":
                                result = {"healthy": True}
                            elif method in ("robot.subscribe", "robot.stop"):
                                result = {"accepted": True}
                            elif method == "robot.do":
                                result = {"accepted": False, "reason": "test refusal"}
                            control.send(
                                json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
                            )
                            if method == "robot.subscribe":
                                control.send(
                                    json.dumps(
                                        {
                                            "jsonrpc": "2.0",
                                            "method": "robot.state",
                                            "params": {
                                                "safety": {"fallen": False, "picked_up": False},
                                                "move": {
                                                    "requested": [0, 0, 0],
                                                    "applied": [0, 0, 0],
                                                },
                                            },
                                        }
                                    )
                                )
                            if method == "robot.health" and self.disconnect_once:
                                self.disconnect_once = False
                                asyncio.create_task(peer.close())

                        offer = await peer.createOffer()
                        await peer.setLocalDescription(offer)
                        local = peer.localDescription
                        assert local is not None
                        await ws.send_json(
                            {
                                "type": "peer",
                                "sessionId": session_id,
                                "sdp": {"type": "offer", "sdp": local.sdp},
                            }
                        )
                    case "peer" if isinstance(signal.get("sdp"), dict):
                        description = signal["sdp"]
                        if description["type"] == "answer":
                            self.answer_sdps.append(description["sdp"])
                        await peer.setRemoteDescription(
                            RTCSessionDescription(sdp=description["sdp"], type=description["type"])
                        )
        finally:
            if peer.connectionState != "closed":
                await peer.close()
            self._peers.discard(peer)
        return ws


@pytest.fixture
async def mediad(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        webrtc_module,
        "RTCPeerConnection",
        lambda: RTCPeerConnection(RTCConfiguration(iceServers=[])),
    )
    server = LocalMediad()
    await server.start()
    yield server
    await server.close()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("webrtc://robot.example:8443", "ws://robot.example:8443"),
        ("webrtc://[::1]:8443", "ws://[::1]:8443"),
    ],
)
def test_signalling_url(url: str, expected: str) -> None:
    assert signalling_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "tcp://robot.example:8443",
        "webrtc://robot.example",
        "webrtc://user:password@robot.example:8443",
        "webrtc://robot.example:8443/path",
        "webrtc://robot.example:8443?token=secret",
    ],
)
def test_invalid_signalling_url(url: str) -> None:
    with pytest.raises(ValueError):
        WebRtcLink(url)


async def test_control_jsonrpc_video_and_api47_stop_request(mediad: LocalMediad) -> None:
    link = WebRtcLink(mediad.url)
    assert isinstance(link, RobotLink)
    assert link.state() is None and math.isinf(link.state_age())
    try:
        await link.connect()
        await until(lambda: link.state() is not None)
        assert link.connected and link.state_age() < 1
        assert len(mediad.answer_sdps) == 1
        assert "a=setup:passive" in mediad.answer_sdps[0]
        assert await link.call("robot.health") == {"healthy": True}
        with pytest.raises(RobotRefused, match="test refusal"):
            await link.call("robot.do", {"skill": "not-allowed"})
        assert await link.call("robot.stop", {}) == {"accepted": True}
        await link.notify("robot.move", {"vx": 0.1, "vy": 0, "vyaw": 0})

        frame = await link.frame()
        image = Image.open(BytesIO(frame.jpeg))
        assert frame.jpeg.startswith(b"\xff\xd8")
        assert (frame.width, frame.height) == (32, 24)
        assert image.size == (32, 24)
        assert frame.captured_at > 0

        await until(lambda: any(item.get("method") == "robot.move" for item in mediad.records))
        hello = next(item for item in mediad.records if item.get("method") == "hello")
        subscribe = next(item for item in mediad.records if item.get("method") == "robot.subscribe")
        stop = next(item for item in mediad.records if item.get("method") == "robot.stop")
        move = next(item for item in mediad.records if item.get("method") == "robot.move")
        assert hello["params"] == {"api_version": 47}
        assert isinstance(hello.get("id"), int)
        assert subscribe["params"] == {"hz": 10}
        assert isinstance(subscribe.get("id"), int)
        assert isinstance(stop.get("id"), int)
        assert "id" not in move
    finally:
        await link.close()
    assert not link.connected
    with pytest.raises(LinkDown):
        await link.call("robot.health")


async def test_backpressure_sends_only_the_latest_move() -> None:
    class Channel:
        readyState = "open"

        def __init__(self) -> None:
            self.bufferedAmount = MAX_MOVE_BUFFERED_BYTES + 1
            self.sent: list[str] = []

        def send(self, message: str) -> None:
            self.sent.append(message)

        def close(self) -> None:
            self.readyState = "closed"

    channel = Channel()
    stream = _DataChannelStream(channel)
    stream.write(b'{"jsonrpc":"2.0","method":"robot.move","params":{"vx":0.1}}\n')
    stream.write(b'{"jsonrpc":"2.0","method":"robot.move","params":{"vx":0.2}}\n')
    assert channel.sent == []

    channel.bufferedAmount = 0
    await until(lambda: len(channel.sent) == 1)
    assert json.loads(channel.sent[0])["params"]["vx"] == 0.2
    stream.close()


async def test_reconnects_when_peer_datachannel_fails(mediad: LocalMediad) -> None:
    events: list[str] = []
    link = WebRtcLink(
        mediad.url,
        on_up=lambda: events.append("up"),
        on_down=lambda: events.append("down"),
    )
    mediad.disconnect_once = True
    try:
        await link.connect()
        assert await link.call("robot.health") == {"healthy": True}
        await until(lambda: mediad.sessions >= 2 and link.connected)
        await until(lambda: events.count("down") >= 1 and events.count("up") >= 2)
        assert events[:3] == ["up", "down", "up"]
    finally:
        await link.close()
