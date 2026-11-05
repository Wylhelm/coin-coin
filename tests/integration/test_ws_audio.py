import asyncio
import struct
import time

import numpy as np
import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from coincoin.audio.browser import BrowserAudio, _Page
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


class _ControlledWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.sent_bytes: list[bytes] = []
        self.sent_json: list[dict[str, object]] = []
        self.audio_started = asyncio.Event()
        self.control_started = asyncio.Event()
        self.release_audio = asyncio.Event()
        self.release_control = asyncio.Event()
        self.hold_audio = False
        self.hold_control = False
        self.on_json = None

    async def send_bytes(self, data: bytes) -> None:
        self.audio_started.set()
        if self.hold_audio:
            await self.release_audio.wait()
        self.sent_bytes.append(data)

    async def send_json(self, data: dict[str, object]) -> None:
        self.sent_json.append(data)
        if data.get("type") == "flush":
            self.control_started.set()
            if self.on_json is not None:
                self.on_json(data)
            if self.hold_control:
                await self.release_control.wait()

    async def close(self, **_kwargs: object) -> None:
        self.closed = True


def _controlled_audio() -> tuple[BrowserAudio, _Page, _ControlledWebSocket, _ControlledWebSocket]:
    audio = BrowserAudio()
    audio_ws = _ControlledWebSocket()
    ui_ws = _ControlledWebSocket()
    page = _Page(audio_ws)  # type: ignore[arg-type]
    page.ui_ws = ui_ws  # type: ignore[assignment]
    audio._page = page
    return audio, page, audio_ws, ui_ws


def _generation(message: dict[str, object]) -> int:
    generation = message["generation"]
    assert isinstance(generation, int)
    return generation


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


async def test_flush_bypasses_blocked_audio_send_and_stale_audio_stays_discarded() -> None:
    audio_io, page, audio_ws, ui_ws = _controlled_audio()
    audio_ws.hold_audio = True
    old_pcm = struct.pack("<h", 1200) * 480
    old_play = asyncio.create_task(audio_io.play(old_pcm))
    await audio_ws.audio_started.wait()

    flush_task = asyncio.create_task(audio_io.flush())
    await asyncio.wait_for(ui_ws.control_started.wait(), timeout=0.05)
    flush_event = ui_ws.sent_json[-1]
    assert flush_event["generation"] == 1
    assert flush_event["through_sequence"] == "1"
    audio_io.acknowledge_flush(page, 1)
    await flush_task

    audio_ws.release_audio.set()
    await old_play
    harness = _PlaybackHarness()
    harness.receive_flush(flush_event)
    harness.receive_audio(audio_ws.sent_bytes[0])
    assert harness.queued_samples == []

    audio_ws.hold_audio = False
    fresh_pcm = struct.pack("<h", -2300) * 480
    await audio_io.play(fresh_pcm)
    fresh_packet = audio_ws.sent_bytes[-1]
    harness.receive_audio(fresh_packet)
    assert _SEQUENCE.unpack_from(fresh_packet)[0] == 2
    assert harness.queued_samples == [-2300] * 480


async def test_flush_deadline_includes_blocked_control_send_and_fails_closed() -> None:
    audio_io, page, _audio_ws, ui_ws = _controlled_audio()
    ui_ws.hold_control = True
    started = asyncio.get_running_loop().time()
    flush_task = asyncio.create_task(audio_io.flush())
    await ui_ws.control_started.wait()

    with pytest.raises(TimeoutError, match="exceeded 100 ms"):
        await asyncio.wait_for(flush_task, timeout=0.15)
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.15
    assert page.flush_error is not None
    assert page.flush_complete.is_set()
    with pytest.raises(ConnectionError, match="playback is unavailable"):
        await audio_io.play(struct.pack("<h", 1) * 480)


async def test_concurrent_flushes_each_use_one_absolute_deadline() -> None:
    audio_io, _page, _audio_ws, ui_ws = _controlled_audio()
    first_started = asyncio.get_running_loop().time()
    first = asyncio.create_task(audio_io.flush())
    await ui_ws.control_started.wait()
    await asyncio.sleep(0.02)
    second_started = asyncio.get_running_loop().time()
    second = asyncio.create_task(audio_io.flush())

    results = await asyncio.gather(first, second, return_exceptions=True)
    first_elapsed = asyncio.get_running_loop().time() - first_started
    second_elapsed = asyncio.get_running_loop().time() - second_started
    assert all(isinstance(result, TimeoutError) for result in results)
    assert first_elapsed < 0.15
    assert second_elapsed < 0.15


async def test_cancelled_flush_fails_closed_until_current_generation_is_acknowledged() -> None:
    audio_io, page, audio_ws, ui_ws = _controlled_audio()
    cancelled = asyncio.create_task(audio_io.flush())
    await ui_ws.control_started.wait()
    old_generation = _generation(ui_ws.sent_json[-1])
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    assert page.flush_error == "flush was cancelled"
    with pytest.raises(ConnectionError, match="playback is unavailable"):
        await audio_io.play(struct.pack("<h", 2) * 480)

    ui_ws.control_started.clear()
    recovered = asyncio.create_task(audio_io.flush())
    await ui_ws.control_started.wait()
    current_generation = _generation(ui_ws.sent_json[-1])
    assert current_generation > old_generation
    audio_io.acknowledge_flush(page, old_generation)
    assert not page.flushed.is_set()
    audio_io.acknowledge_flush(page, current_generation)
    await recovered
    assert page.flush_error is None

    await audio_io.play(struct.pack("<h", 3) * 480)
    assert _SEQUENCE.unpack_from(audio_ws.sent_bytes[-1])[0] == 1


async def test_page_replacement_aborts_old_flush_without_touching_new_generation() -> None:
    audio_io, old_page, _old_audio, old_ui = _controlled_audio()
    old_ui.hold_control = True
    old_flush = asyncio.create_task(audio_io.flush())
    await old_ui.control_started.wait()
    old_generation = _generation(old_ui.sent_json[-1])

    new_audio = _ControlledWebSocket()
    new_page = await audio_io.connect_audio(new_audio)  # type: ignore[arg-type]
    new_ui = _ControlledWebSocket()
    assert await audio_io.connect_ui(new_ui) is new_page  # type: ignore[arg-type]
    old_ui.release_control.set()
    with pytest.raises(ConnectionError, match="replaced"):
        await asyncio.wait_for(old_flush, timeout=0.05)

    recovery = asyncio.create_task(audio_io.flush())
    await new_ui.control_started.wait()
    new_generation = _generation(new_ui.sent_json[-1])
    assert new_generation == 1
    audio_io.acknowledge_flush(old_page, old_generation)
    assert not new_page.flushed.is_set()
    audio_io.acknowledge_flush(new_page, new_generation)
    await recovery


async def test_new_audio_page_replaces_the_previous_page() -> None:
    async with TestClient(TestServer(create_app())) as client:
        old_audio = await client.ws_connect("/ws/audio")
        old_ui = await client.ws_connect("/ws/ui")
        new_audio = await client.ws_connect("/ws/audio")

        assert await old_ui.receive_json() == {"type": "replaced"}
        assert (await old_audio.receive()).type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

        await new_audio.close()
        await old_ui.close()
