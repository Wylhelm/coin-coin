"""Vision across the actual relay binary framing and Realtime WebSocket."""

import asyncio
import base64
import json
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from coincoin.app import App
from coincoin.journal import Journal
from coincoin.realtime.session import RealtimeSession
from coincoin.realtime.tools.vision import NO_CAMERA, install_tools
from coincoin.robot.frames import FrameError, fetch_frame
from coincoin.robot.link import Frame
from coincoin.robot.tcp_link import TcpLink
from coincoin.statemachine import Mode, State
from relay.duck_relay import DuckRelay
from tests.fakes.fake_robotd import FakeRobotLink
from tests.integration.test_s4_regressions import _Audio, _settings
from tests.integration.test_tools_e2e_fakes import ScriptedRealtime, _entries, _until


@pytest.fixture
async def camera(tmp_path: Path):
    seen = []
    malformed = [False]

    async def serve(reader, writer):
        try:
            while line := await reader.readline():
                request = json.loads(line)
                seen.append(request["method"])
                result = {}
                raw = b""
                if request["method"] == "media.frame":
                    raw = bytes([54, 145, 34, 145]) * (64 * 32 // 2)
                    result = {
                        "width": 64,
                        "height": 32,
                        "format": "UYVY",
                        "bytes": len(raw) if not malformed[0] else 7,
                        "captured_at_unix_us": int(time.time() * 1_000_000),
                        "rotate": 90,
                    }
                writer.write(
                    json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
                    + b"\n"
                    + raw
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(serve, tmp_path / "duck-a-frame.sock")
    relay = DuckRelay(tmp_path, robot_port=0, frame_port=0, token="fake-vision-token")
    await relay.start()
    port = relay.servers[1].sockets[0].getsockname()[1]
    try:
        yield port, seen, malformed
    finally:
        await relay.close()
        server.close()
        await server.wait_closed()


async def test_frame_through_relay_validates_geometry(camera):
    port, seen, malformed = camera
    frame = await fetch_frame("127.0.0.1", port, token="fake-vision-token")
    assert (frame.width, frame.height) == (32, 64)
    assert time.time() - frame.captured_at < 2
    assert Image.open(BytesIO(frame.jpeg)).size == (32, 64)
    malformed[0] = True
    with pytest.raises(FrameError):
        await fetch_frame("127.0.0.1", port, token="fake-vision-token")
    assert seen == ["hello", "media.frame", "hello", "media.frame"]


@pytest.mark.parametrize("question", [None, "What is green?"])
async def test_see_sends_user_image_then_output_then_one_response(camera, tmp_path, question):
    port, _, _ = camera
    settings = _settings()
    link = TcpLink(
        "tcp://127.0.0.1:1",
        frame_url=f"tcp://127.0.0.1:{port}",
        token="fake-vision-token",
    )
    arguments = {} if question is None else {"question": question}
    fake = ScriptedRealtime([[("see", arguments)]])
    await fake.start()
    app = App(settings, _Audio(), Journal(tmp_path / "logs"), features=())
    install_tools(app, link)
    app.state = State(Mode.AWAKE)
    session = RealtimeSession(settings, base_url=fake.websocket_url)
    app._session = session

    async def consume():
        async for event in session.events():
            await app._handle_realtime_event(event)

    consumer = None
    try:
        await session.open("", app.tools.definitions())
        consumer = asyncio.create_task(consume())
        await session.send_audio(bytes(960))
        await _until(lambda: any(e["type"] == "response.create" for e in fake.received))
        items = [e["item"] for e in fake.received if e["type"] == "conversation.item.create"]
        assert len(items) == 2
        image, output = items
        assert image["type"] == "message" and image["role"] == "user"
        content = image["content"]
        assert content[0]["type"] == "input_image"
        prefix, encoded = content[0]["image_url"].split(",", 1)
        assert prefix == "data:image/jpeg;base64"
        jpeg = base64.b64decode(encoded, validate=True)
        assert len(jpeg) <= 150_000
        assert Image.open(BytesIO(jpeg)).size == (32, 64)
        assert content[1:] == (
            [] if question is None else [{"type": "input_text", "text": question}]
        )
        assert json.loads(output["output"]) == {"ok": True, "detail": "image ajoutée"}
        assert output["type"] == "function_call_output"
        assert sum(e["type"] == "response.create" for e in fake.received) == 1
        entries = _entries(app)
        assert any(e["type"] == "tool_call" and e["name"] == "see" for e in entries)
        assert any(e["type"] == "vision" and e["image_bytes"] == len(jpeg) for e in entries)
        journal = json.dumps(entries)
        assert encoded not in journal and "image_url" not in journal
    finally:
        if consumer is not None:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
        await app.stop()
        await fake.close()


class CameraLink(FakeRobotLink):
    def __init__(self, ages=()):
        super().__init__()
        self.ages = iter(ages)
        self.calls = 0
        self.blocked = asyncio.Event()
        self.release = asyncio.Event()
        self.wait = False

    async def frame(self):
        self.calls += 1
        if self.wait:
            self.blocked.set()
            await self.release.wait()
        age = next(self.ages, None)
        if age is None:
            raise FrameError("no camera")
        output = BytesIO()
        Image.new("RGB", (4, 4), "green").save(output, "JPEG")
        return Frame(output.getvalue(), 4, 4, time.time() - age)


@pytest.fixture
def offline(tmp_path):
    app = App(_settings(), _Audio(), Journal(tmp_path), features=())
    app.state = State(Mode.AWAKE)
    session = RealtimeSession(app.settings)
    app._session = session
    return app


@pytest.mark.parametrize(
    ("ages", "ok", "calls"),
    [([4, 0], True, 2), ([4, 4], False, 2), ([], False, 1), ([0], True, 1), ([-10, 0], True, 2)],
)
async def test_camera_freshness_and_missing(offline, monkeypatch, ages, ok, calls):
    images = []

    async def add_image(self, jpeg, question=None):
        images.append(jpeg)

    monkeypatch.setattr(RealtimeSession, "add_image", add_image)
    link = CameraLink(ages)
    install_tools(offline, link)
    result = await offline.tools.dispatch("see", "{}", offline)
    assert result["ok"] is ok
    assert len(images) == int(ok) and link.calls == calls
    if not ages:
        assert result["reason"] == NO_CAMERA


@pytest.mark.parametrize(
    "arguments", ['{"question": null}', '{"question": 7}', '{"x":1}', '{"question":""}']
)
async def test_invalid_arguments_do_not_capture(offline, arguments):
    link = CameraLink([0])
    install_tools(offline, link)
    assert not (await offline.tools.dispatch("see", arguments, offline))["ok"]
    assert link.calls == 0


@pytest.mark.parametrize("change", ["sleep", "session", "cancel", "timeout"])
async def test_no_late_image_after_interruption(offline, change, monkeypatch):
    images = []

    async def add_image(self, jpeg, question=None):
        images.append(jpeg)

    monkeypatch.setattr(RealtimeSession, "add_image", add_image)
    link = CameraLink([0])
    link.wait = True
    install_tools(offline, link)
    task = asyncio.create_task(offline.tools.dispatch("see", "{}", offline))
    await link.blocked.wait()
    if change == "sleep":
        offline.state = State(Mode.SLEEPING)
    elif change == "session":
        offline._session = RealtimeSession(offline.settings)
    elif change == "cancel":
        task.cancel()
    if change != "timeout":
        link.release.set()
    if change == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert not result["ok"]
        if change == "timeout":
            assert result["reason"] == NO_CAMERA
    assert not images
