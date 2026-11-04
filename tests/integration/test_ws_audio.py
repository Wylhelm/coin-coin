import asyncio
import struct
import time

import numpy as np
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from coincoin.audio.browser import BrowserAudio
from coincoin.web.server import create_app

_SEQUENCE = struct.Struct("<Q")


class _PlaybackHarness:
    """Model the browser's sequence barrier and worklet queue for reordered WS events."""

    def __init__(self) -> None:
        self.discard_through = 0
        self.queued_samples: list[int] = []

    def receive_audio(self, packet: bytes) -> None:
        sequence = _SEQUENCE.unpack_from(packet)[0]
        if sequence <= self.discard_through:
            return
        samples = np.frombuffer(packet, dtype="<i2", offset=_SEQUENCE.size)
        self.queued_samples.extend(int(sample) for sample in samples)

    def receive_flush(self, event: dict[str, object]) -> None:
        self.discard_through = int(str(event["through_sequence"]))
        self.queued_samples.clear()


async def _flush(audio: BrowserAudio, ui) -> dict[str, object]:
    flush_task = asyncio.create_task(audio.flush())
    event = await ui.receive_json()
    assert event["type"] == "flush"
    await ui.send_json({"type": "flushed", "generation": event["generation"]})
    assert await ui.receive_json() == {
        "type": "flush_complete",
        "generation": event["generation"],
    }
    await flush_task
    return event


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
            received.append(_SEQUENCE.unpack_from(message.data)[0])
            if index == 14:
                try:
                    await asyncio.wait_for(audio.receive(), timeout=0.02)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError("Playback exceeded the 300 ms flow-control window")
            if (index + 1) % 15 == 0:
                await ui.send_json({"type": "played", "ms": (index + 1) * 20})
        assert received == list(range(1, len(frames) + 1))

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


async def test_reordered_old_audio_is_discarded_across_repeated_flushes() -> None:
    audio_io = BrowserAudio()
    async with TestClient(TestServer(create_app(audio_io))) as client:
        audio_ws = await client.ws_connect("/ws/audio")
        ui = await client.ws_connect("/ws/ui")
        harness = _PlaybackHarness()

        old_pcm = b"".join(struct.pack("<h", sample) * 480 for sample in (1200, 2400, 3600))
        await audio_io.play(old_pcm)
        stale_packets = [(await audio_ws.receive()).data for _ in range(3)]
        flush_task = asyncio.create_task(audio_io.flush())
        first_flush = await ui.receive_json()
        assert first_flush["type"] == "flush"
        harness.receive_flush(first_flush)
        first_fresh = struct.pack("<h", 5000) * 480
        fresh_task = asyncio.create_task(audio_io.play(first_fresh))
        try:
            await asyncio.wait_for(asyncio.shield(fresh_task), timeout=0.01)
        except TimeoutError:
            pass
        else:
            raise AssertionError("New playback must wait until the flush acknowledgement")
        await ui.send_json({"type": "flushed", "generation": first_flush["generation"]})
        assert await ui.receive_json() == {
            "type": "flush_complete",
            "generation": first_flush["generation"],
        }
        await flush_task
        await fresh_task

        assert first_flush["through_sequence"] == "3"
        for packet in stale_packets:
            harness.receive_audio(packet)
        assert harness.queued_samples == []

        fresh_packet = (await audio_ws.receive()).data
        harness.receive_audio(fresh_packet)
        assert _SEQUENCE.unpack_from(fresh_packet)[0] == 4
        assert harness.queued_samples == [5000] * 480

        second_stale = struct.pack("<h", -1200) * 480
        await audio_io.play(second_stale)
        second_stale_packet = (await audio_ws.receive()).data
        second_flush = await _flush(audio_io, ui)
        harness.receive_flush(second_flush)
        harness.receive_audio(second_stale_packet)
        assert second_flush["through_sequence"] == "5"
        assert harness.queued_samples == []

        second_fresh = struct.pack("<h", -4000) * 480
        await audio_io.play(second_fresh)
        final_packet = (await audio_ws.receive()).data
        harness.receive_audio(final_packet)
        assert _SEQUENCE.unpack_from(final_packet)[0] == 6
        assert harness.queued_samples == [-4000] * 480

        await audio_ws.close()
        await ui.close()


async def test_play_preserves_arbitrary_fragment_boundaries_and_short_tail() -> None:
    audio_io = BrowserAudio()
    async with TestClient(TestServer(create_app(audio_io))) as client:
        audio_ws = await client.ws_connect("/ws/audio")
        ui = await client.ws_connect("/ws/ui")
        harness = _PlaybackHarness()

        fragment_a = np.arange(400, dtype="<i2").tobytes()
        fragment_b = (np.arange(400, dtype="<i2") + 1000).tobytes()
        await audio_io.play(fragment_a)
        await audio_io.play(fragment_b)
        first = (await audio_ws.receive()).data
        second = (await audio_ws.receive()).data
        assert len(first) == _SEQUENCE.size + len(fragment_a)
        assert len(second) == _SEQUENCE.size + len(fragment_b)
        harness.receive_audio(first)
        harness.receive_audio(second)
        expected = np.concatenate(
            (np.frombuffer(fragment_a, dtype="<i2"), np.frombuffer(fragment_b, dtype="<i2"))
        )
        np.testing.assert_array_equal(harness.queued_samples, expected)

        short_tail = np.array([31, -32, 33], dtype="<i2").tobytes()
        started = time.monotonic()
        await asyncio.wait_for(audio_io.play(short_tail), timeout=0.1)
        tail_packet = (await audio_ws.receive()).data
        assert time.monotonic() - started < 0.1
        assert tail_packet[_SEQUENCE.size :] == short_tail
        harness.receive_audio(tail_packet)
        np.testing.assert_array_equal(harness.queued_samples[-3:], [31, -32, 33])

        await audio_ws.close()
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
