import asyncio
import time

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from coincoin.web.server import create_app


async def test_echo_websocket_limits_playback_and_flushes_in_under_100ms() -> None:
    async with TestClient(TestServer(create_app(echo=True))) as client:
        audio = await client.ws_connect("/ws/audio")
        ui = await client.ws_connect("/ws/ui")
        frames = [index.to_bytes(2, "little") * 480 for index in range(50)]
        for frame in frames:
            await audio.send_bytes(frame)

        received = []
        for index in range(50):
            message = await audio.receive()
            assert message.type == WSMsgType.BINARY
            received.append(message.data)
            if index == 14:
                try:
                    await asyncio.wait_for(audio.receive(), timeout=0.02)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("Playback exceeded the 300 ms flow-control window")
            if (index + 1) % 15 == 0:
                await ui.send_json({"type": "played", "ms": (index + 1) * 20})
        assert received == frames

        started = time.monotonic()
        await ui.send_json({"type": "stop"})
        flush = await ui.receive_json()
        assert flush["type"] == "flush"
        await ui.send_json({"type": "flushed", "generation": flush["generation"]})
        complete = await ui.receive_json()
        assert complete == {
            "type": "flush_complete",
            "generation": flush["generation"],
        }
        assert (time.monotonic() - started) < 0.1
        await audio.close()
        await ui.close()


async def test_new_audio_page_replaces_the_previous_page() -> None:
    async with TestClient(TestServer(create_app())) as client:
        old_audio = await client.ws_connect("/ws/audio")
        old_ui = await client.ws_connect("/ws/ui")
        new_audio = await client.ws_connect("/ws/audio")

        assert await old_ui.receive_json() == {"type": "replaced"}
        assert (await old_audio.receive()).type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

        await new_audio.close()
        await old_ui.close()
