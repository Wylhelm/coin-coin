import asyncio

import numpy as np
import pytest

from coincoin.audio.resample import float32_to_pcm16
from coincoin.clock import FakeClock
from coincoin.robot.mouth import MouthAnimator


def tone(db: float, ms: int = 400) -> bytes:
    t = np.arange(ms * 24, dtype=np.float32) / 24_000
    return float32_to_pcm16(np.sin(2 * np.pi * 600 * t) * 10 ** (db / 20))


async def tick(clock: FakeClock, seconds: float = 0.01) -> None:
    clock.advance(seconds)
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize("pcm,maximum", [(bytes(19200), 0), (tone(-10), 0.9)])
async def test_envelope_threshold_and_continuity(pcm: bytes, maximum: float) -> None:
    clock = FakeClock()
    sent: list[float] = []

    async def mouth(opening: float) -> None:
        sent.append(opening)

    animator = MouthAnimator(mouth, lambda: round(clock.now() * 1000), clock)
    animator.feed(pcm, 0)
    for _ in range(45):
        await tick(clock)
    assert sent
    assert max(sent) <= maximum
    assert max(abs(b - a) for a, b in zip([0.0, *sent], sent, strict=False)) <= 0.3 + 1e-9
    if maximum:
        assert max(sent) >= 0.85
    await animator.shutdown()


async def test_variable_speech_and_split_windows() -> None:
    clock = FakeClock()
    sent: list[float] = []

    async def mouth(opening: float) -> None:
        sent.append(opening)

    animator = MouthAnimator(mouth, lambda: round(clock.now() * 1000), clock)
    pcm = tone(-10, 160) + bytes(7680) + tone(-25, 160)
    for offset in range(0, len(pcm), 480):
        animator.feed(pcm[offset : offset + 480], offset / 48)
    for _ in range(60):
        await tick(clock)
    assert len(sent) == 12
    assert max(sent) - min(sent) > 0.4
    assert all(0 <= value <= 0.9 for value in sent)
    await animator.shutdown()


async def test_playback_latency_and_immediate_flush_close() -> None:
    clock = FakeClock()
    sent: list[tuple[float, float]] = []
    played = 0

    async def mouth(opening: float) -> None:
        sent.append((clock.now(), opening))

    animator = MouthAnimator(mouth, lambda: played, clock)
    animator.feed(tone(-10), 0)
    for _ in range(12):
        await tick(clock)
    assert not sent
    played = 10
    await tick(clock)
    assert sent[0][0] == pytest.approx(0.12, abs=0.02)
    await animator.close()
    assert sent[-1][1] == 0
    before = len(sent)
    await tick(clock, 0.2)
    assert len(sent) == before + 1
    assert sent[-1][1] == 0
    played = 500
    await tick(clock, 0.4)
    assert sent[-1][1] == 0
    await animator.shutdown()


async def test_new_audio_cancels_delayed_close() -> None:
    clock = FakeClock()
    sent: list[float] = []

    async def mouth(opening: float) -> None:
        sent.append(opening)

    animator = MouthAnimator(mouth, lambda: round(clock.now() * 1000), clock)
    await animator.close()
    animator.feed(tone(-10, 400), 0)
    for _ in range(40):
        await tick(clock)
    assert all(value > 0 for value in sent[1:])
    await animator.shutdown()


async def test_playback_reset_discards_old_mouth_positions() -> None:
    clock = FakeClock()
    sent: list[float] = []
    played = 0

    async def mouth(opening: float) -> None:
        sent.append(opening)

    animator = MouthAnimator(mouth, lambda: played, clock)
    animator.feed(tone(-10), 0)
    played = 50
    for _ in range(5):
        await tick(clock)
    assert any(value > 0 for value in sent)

    reset_output_index = len(sent)
    played = 0
    animator.feed(bytes(19200), 0)
    for _ in range(45):
        played = round(clock.now() * 1000)
        await tick(clock)
    assert all(value == 0 for value in sent[reset_output_index:])
    await animator.shutdown()
