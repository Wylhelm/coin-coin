import asyncio
import json
from io import BytesIO

import numpy as np
import pytest
from PIL import Image

from coincoin.robot.frames import FrameError, MediaFrameHeader, decode_uyvy, fetch_frame
from coincoin.robot.jsonrpc import JsonRpcError


def header(**overrides):
    return {
        "width": 16,
        "height": 8,
        "format": "UYVY",
        "bytes": 16 * 8 * 2,
        "captured_at_unix_us": 1_700_000_000_123_456,
        "rotate": 0,
        **overrides,
    }


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_synthetic_uyvy_color_and_rotation(rotate):
    metadata = MediaFrameHeader.parse(header(rotate=rotate))
    raw = bytes([54, 145, 34, 145]) * (16 * 8 // 2)
    frame = decode_uyvy(metadata, raw)
    image = Image.open(BytesIO(frame.jpeg))
    assert frame.jpeg.startswith(b"\xff\xd8")
    assert frame.captured_at == pytest.approx(1_700_000_000.123456)
    assert (frame.width, frame.height) == ((16, 8) if rotate in (0, 180) else (8, 16))
    pixel = np.asarray(image)[0, 0]
    assert pixel[0] < 10 and pixel[1] > 245 and pixel[2] < 10


def test_clockwise_rotation_and_black_white():
    raw = bytes([128, 16, 128, 16]) * 64 + bytes([128, 235, 128, 235]) * 64
    metadata = MediaFrameHeader.parse(header(height=16, bytes=512, rotate=90))
    frame = decode_uyvy(metadata, raw)
    pixels = np.asarray(Image.open(BytesIO(frame.jpeg)))
    assert pixels[0, 0, 0] > 240 and pixels[0, -1, 0] < 15
    with pytest.raises(FrameError, match="Incomplete"):
        decode_uyvy(metadata, raw[:-1])


@pytest.mark.parametrize(
    "overrides",
    [
        {"width": 0},
        {"width": 1},
        {"height": 0},
        {"width": True},
        {"bytes": 3},
        {"bytes": 16 * 1024 * 1024 + 1},
        {"format": "RGB"},
        {"rotate": 45},
        {"captured_at_unix_us": -1},
        {"rotate": None},
    ],
)
def test_reject_invalid_geometry_before_allocation(overrides):
    with pytest.raises(FrameError):
        MediaFrameHeader.parse(header(**overrides))
    with pytest.raises(FrameError):
        MediaFrameHeader.parse(None)


@pytest.mark.parametrize(
    "fault",
    ["none", "short", "oversized", "invalid_json", "wrong_id", "bool_id", "error", "bad_size"],
)
async def test_frame_wire_header_and_binary(fault):
    seen = []

    async def serve(reader, writer):
        try:
            seen.append(await reader.readline())
            hello = json.loads(await reader.readline())
            writer.write(
                json.dumps({"jsonrpc": "2.0", "id": hello["id"], "result": {}}).encode() + b"\n"
            )
            await writer.drain()
            request = json.loads(await reader.readline())
            metadata = header(width=2, height=1, bytes=4)
            if fault == "bad_size":
                metadata["bytes"] = 999
            response = {"jsonrpc": "2.0", "id": request["id"], "result": metadata}
            if fault == "wrong_id":
                response["id"] = 4
            if fault == "bool_id":
                response["id"] = True
            if fault == "error":
                response = {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "error": {"code": -32601, "message": "no camera"},
                }
            wire = json.dumps(response).encode() + b"\n"
            if fault == "oversized":
                wire = b"x" * 4097 + b"\n"
            if fault == "invalid_json":
                wire = b"not-json\n"
            writer.write(wire + (b"\x80\x10\x80\xeb" if fault != "short" else b"\x80"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        if fault == "none":
            frame = await fetch_frame("127.0.0.1", port, token="fake-frame-token")
            assert frame.width == 2 and frame.height == 1
            assert seen == [b"fake-frame-token\n"]
        else:
            with pytest.raises((FrameError, JsonRpcError)):
                await fetch_frame("127.0.0.1", port, token="fake-frame-token")
    finally:
        server.close()
        await server.wait_closed()


async def test_camera_missing_and_invalid_token():
    with pytest.raises(ValueError):
        await fetch_frame("127.0.0.1", 1, token="bad\n")
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    with pytest.raises(FrameError, match="Camera unavailable"):
        await fetch_frame("127.0.0.1", port)
