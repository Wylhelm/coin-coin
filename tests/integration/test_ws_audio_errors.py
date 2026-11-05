import asyncio
from pathlib import Path

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from coincoin.audio.browser import BrowserAudio
from coincoin.journal import Journal
from coincoin.web.server import create_app


async def test_mic_queue_drops_oldest_frames_and_pads_short_chunks() -> None:
    audio_service = BrowserAudio()
    async with TestClient(TestServer(create_app(audio_service))) as client:
        websocket = await client.ws_connect("/ws/audio")
        for index in range(105):
            await websocket.send_bytes(index.to_bytes(2, "little"))
        await asyncio.sleep(0.01)

        assert audio_service.dropped_mic_frames == 5
        frames = audio_service.mic_frames()
        assert await anext(frames) == (5).to_bytes(2, "little") + bytes(958)
        await frames.aclose()

        with pytest.raises(ValueError, match="little-endian PCM16"):
            await audio_service.play(b"\x01")
        with pytest.raises(ConnectionError, match="UI WebSocket is disconnected"):
            await audio_service.flush()
        await websocket.close()


async def test_ui_requires_audio_page_and_static_files_are_allowlisted() -> None:
    async with TestClient(TestServer(create_app())) as client:
        response = await client.get("/static/not-a-client-file")
        assert response.status == 404

        ui = await client.ws_connect("/ws/ui")
        assert await ui.receive_json() == {
            "type": "error",
            "message": "Audio WebSocket not connected",
        }
        assert (await ui.receive()).type in {WSMsgType.CLOSE, WSMsgType.CLOSED}


async def test_malformed_audio_and_ui_messages_are_rejected() -> None:
    async with TestClient(TestServer(create_app())) as client:
        audio = await client.ws_connect("/ws/audio")
        ui = await client.ws_connect("/ws/ui")
        await audio.send_bytes(b"\x01")
        assert (await audio.receive()).type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

        await ui.send_str("not json")
        assert (await ui.receive()).type in {WSMsgType.CLOSE, WSMsgType.CLOSED}


async def test_ui_ignores_invalid_progress_and_reports_unknown_events() -> None:
    async with TestClient(TestServer(create_app())) as client:
        audio = await client.ws_connect("/ws/audio")
        ui = await client.ws_connect("/ws/ui")
        await ui.send_json({"type": "played", "ms": "not a number"})
        await ui.send_json({"type": "unknown"})
        assert await ui.receive_json() == {"type": "error", "message": "Unknown UI event"}
        await audio.close()
        await ui.close()


async def test_tone_mode_measures_and_journals_loopback_latency(tmp_path: Path) -> None:
    journal = Journal(log_dir=tmp_path)
    async with TestClient(TestServer(create_app(tone=True, journal=journal))) as client:
        audio = await client.ws_connect("/ws/audio")
        ui = await client.ws_connect("/ws/ui")
        output = []
        for _ in range(10):
            message = await asyncio.wait_for(audio.receive(), timeout=1.5)
            assert message.type == WSMsgType.BINARY
            output.append(message.data)

        await asyncio.sleep(0.25)
        for frame in output:
            await audio.send_bytes(frame)
        event = await asyncio.wait_for(ui.receive_json(), timeout=1)

        assert event["type"] == "latency"
        assert event["ms"] > 0
        log_file = await asyncio.to_thread(lambda: next(tmp_path.glob("*.jsonl")))
        log_contents = await asyncio.to_thread(log_file.read_text)
        assert '"type": "latency"' in log_contents
        await audio.close()
        await ui.close()
