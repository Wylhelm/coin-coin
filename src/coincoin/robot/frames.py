"""Bounded media.frame client and camera UYVY to upright JPEG conversion."""

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import numpy as np
from PIL import Image

from coincoin.robot.jsonrpc import API_VERSION, JsonRpcError, decode_message, encode_request
from coincoin.robot.jsonrpc import response_result as rpc_result
from coincoin.robot.link import Frame, LinkDown

LOGGER = logging.getLogger(__name__)


class FrameError(Exception):
    """Camera data is malformed, incomplete or unavailable."""


@dataclass(frozen=True)
class MediaFrameHeader:
    width: int
    height: int
    format: str
    bytes: int
    captured_at_unix_us: int
    rotate: int

    @classmethod
    def parse(cls, result: Any) -> "MediaFrameHeader":
        fields = ("width", "height", "bytes", "captured_at_unix_us", "rotate")
        if not isinstance(result, dict) or any(
            type(result.get(field)) is not int for field in fields
        ):
            raise FrameError("Invalid media.frame header")
        header = cls(
            result["width"],
            result["height"],
            result.get("format", ""),
            result["bytes"],
            result["captured_at_unix_us"],
            result["rotate"],
        )
        if not (
            0 < header.width <= 2**32 - 1
            and 0 < header.height <= 2**32 - 1
            and header.width % 2 == 0
            and header.format == "UYVY"
            and header.rotate in (0, 90, 180, 270)
            and header.bytes <= 16 * 1024 * 1024
            and header.width * header.height * 2 == header.bytes
            and 0 <= header.captured_at_unix_us <= 2**128 - 1
        ):
            raise FrameError("Invalid UYVY geometry or size")
        return header


def decode_uyvy(header: MediaFrameHeader, raw: bytes) -> Frame:
    if len(raw) != header.bytes:
        raise FrameError("Incomplete UYVY frame")
    pairs = np.frombuffer(raw, dtype=np.uint8).reshape(header.height, header.width // 2, 4)
    y = pairs[:, :, (1, 3)].reshape(header.height, header.width).astype(np.float32) - 16
    u = np.repeat(pairs[:, :, 0], 2, axis=1).astype(np.float32) - 128
    v = np.repeat(pairs[:, :, 2], 2, axis=1).astype(np.float32) - 128
    rgb = np.stack(
        (1.164 * y + 1.596 * v, 1.164 * y - 0.392 * u - 0.813 * v, 1.164 * y + 2.017 * u),
        axis=-1,
    )
    pixels = np.rint(np.clip(rgb, 0, 255)).astype(np.uint8)
    pixels = np.rot90(pixels, k=-(header.rotate // 90))
    image = Image.fromarray(pixels)
    output = BytesIO()
    image.save(output, format="JPEG", quality=80)
    return Frame(
        output.getvalue(), image.width, image.height, header.captured_at_unix_us / 1_000_000
    )


async def _response(reader: asyncio.StreamReader, id_: int) -> Any:
    try:
        line = await reader.readline()
    except ValueError as error:
        raise FrameError("Snapshot header exceeds 4096 bytes") from error
    if len(line) > 4096 or not line.endswith(b"\n"):
        raise FrameError("Invalid snapshot header length")
    message = decode_message(line)
    if type(message.get("id")) is not int or message["id"] != id_:
        raise FrameError("Snapshot response does not match request")
    return rpc_result(message)


async def fetch_frame(
    host: str,
    port: int,
    *,
    token: str = "",
    timeout: float = 3.0,  # noqa: ASYNC109 - bounded camera network operation
) -> Frame:
    if "\n" in token or "\r" in token:
        raise ValueError("Invalid authentication token")
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port, limit=4096)
            if token:
                writer.write(token.encode() + b"\n")
            writer.write(encode_request(1, "hello", {"api_version": API_VERSION}))
            await writer.drain()
            try:
                await _response(reader, 1)
            except JsonRpcError:
                LOGGER.info("hello rejected; trying the snapshot request")
            writer.write(encode_request(2, "media.frame", None))
            await writer.drain()
            header = MediaFrameHeader.parse(await _response(reader, 2))
            raw = await reader.readexactly(header.bytes)
        return await asyncio.to_thread(decode_uyvy, header, raw)
    except (OSError, TimeoutError, asyncio.IncompleteReadError) as error:
        raise FrameError(f"Camera unavailable ({type(error).__name__})") from error
    except LinkDown as error:
        raise FrameError(str(error)) from error
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
