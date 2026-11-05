import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest

from coincoin.audio.io import PlaybackProgress
from coincoin.audio.mixer import OutputMixer
from coincoin.audio.resample import float32_to_pcm16
from coincoin.clock import FakeClock
from coincoin.robot.quack import QuackUnavailable, RobotQuack, StreamQuack
from tests.fakes.fake_robotd import FakeRobotLink
from tests.unit.test_mouth import tick


class Playback:
    def __init__(self) -> None:
        self.sent = 0
        self.played = 0
        self.epoch = 0
        self.output: list[bytes] = []
        self.flushes = 0

    @property
    def playback_progress(self) -> PlaybackProgress:
        return PlaybackProgress(self.epoch, self.sent, self.played)

    @property
    def played_ms(self) -> int:
        return self.played // 24

    @property
    def playing(self) -> bool:
        return self.sent > self.played

    async def play(self, pcm: bytes) -> None:
        self.output.append(pcm)
        self.sent += len(pcm) // 2

    async def flush(self) -> None:
        self.flushes += 1
        self.sent = self.played = 0
        self.epoch += 1

    async def mic_frames(self) -> AsyncIterator[bytes]:
        yield bytes(960)


def bank(path: Path, tag: str = "greet", rate: int = 16000, channels: int = 1) -> Path:
    directory = path / tag
    directory.mkdir(parents=True, exist_ok=True)
    with wave.open(str(directory / "sound.wav"), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(float32_to_pcm16(np.full(rate // 5 * channels, 0.2, np.float32)))
    return path


def rig(path: Path):
    clock = FakeClock()
    audio = Playback()
    events: list[bool] = []
    errors: list[str] = []
    blocks: list[tuple[bytes, float]] = []

    async def speaking(active: bool) -> None:
        events.append(active)

    mixer = OutputMixer(audio, clock, lambda pcm, start: blocks.append((pcm, start)))
    sink = StreamQuack(mixer, path, speaking, errors.append)
    return clock, audio, events, errors, blocks, mixer, sink


@pytest.mark.parametrize("rate,channels", [(16000, 1), (24000, 2), (48000, 1)])
async def test_stream_resamples_sample_count_and_holds_speaking_until_played(
    tmp_path: Path, rate: int, channels: int
) -> None:
    clock, audio, events, errors, blocks, mixer, sink = rig(
        bank(tmp_path, rate=rate, channels=channels)
    )
    task = asyncio.create_task(sink.quack("greet"))
    for _ in range(12):
        audio.played = audio.sent
        await tick(clock, 0.02)
    await task
    assert events == [True, False]
    assert not errors
    assert sum(len(pcm) for pcm, _ in blocks) == 9600
    assert mixer.playback_progress.sent_samples == 0
    assert mixer.played_ms == 0
    assert audio.flushes == 0


async def test_stream_cancel_flushes_pending_output(tmp_path: Path) -> None:
    clock, audio, events, _, _, _, sink = rig(bank(tmp_path))
    task = asyncio.create_task(sink.quack("greet"))
    await tick(clock, 0.02)
    assert events == [True]
    assert audio.sent > 0
    await sink.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert events == [True, False]
    assert audio.flushes == 1
    before = len(audio.output)
    await tick(clock, 1)
    assert len(audio.output) == before


async def test_voice_mix_uses_virtual_clock_for_truncation(tmp_path: Path) -> None:
    clock, audio, _, _, blocks, mixer, sink = rig(bank(tmp_path))
    task = asyncio.create_task(sink.quack("greet"))
    await tick(clock, 0.02)
    prefix = audio.sent
    assert prefix > 0
    voice = float32_to_pcm16(np.full(960, 0.3, np.float32))
    await mixer.play(voice)
    audio.played = prefix + 480
    assert mixer.playback_progress == PlaybackProgress(0, 960, 480)
    mixed = np.frombuffer(blocks[-1][0], dtype="<i2") / 32768
    assert np.mean(mixed) == pytest.approx(0.5, abs=0.001)
    await sink.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert mixer.playback_progress == PlaybackProgress(0, 960, 480)
    assert audio.flushes == 0


async def test_cancel_quack_preserves_queued_voice() -> None:
    clock = FakeClock()
    audio = Playback()
    mixer = OutputMixer(audio, clock, lambda _pcm, _start: None)
    voice = float32_to_pcm16(np.full(480, 0.3, np.float32))
    await mixer.play(voice)
    mixer._quack = bytes(960)
    await mixer._write()

    await mixer.cancel_quack()

    assert audio.flushes == 0
    assert mixer.playback_progress.sent_samples == 480


async def test_missing_bank_logs_once(tmp_path: Path) -> None:
    _, _, events, errors, _, _, sink = rig(tmp_path)
    for tag in ("greet", "coo"):
        with pytest.raises(QuackUnavailable):
            await sink.quack(tag)
    assert len(errors) == 1
    assert events == []


async def test_wheee_loops_to_requested_length(tmp_path: Path) -> None:
    clock, audio, _, _, blocks, _, sink = rig(bank(tmp_path, "wheee"))
    task = asyncio.create_task(sink.quack("wheee", 0.4))
    for _ in range(25):
        audio.played = audio.sent
        await tick(clock, 0.02)
    await task
    assert sum(len(pcm) for pcm, _ in blocks) == 19200


async def test_robot_wheee_hold_release_and_cancel() -> None:
    clock = FakeClock()
    link = FakeRobotLink(clock=clock)
    events: list[bool] = []

    async def speaking(active: bool) -> None:
        events.append(active)

    sink = RobotQuack(link, clock, speaking)
    task = asyncio.create_task(sink.quack("wheee", 0.3))
    for _ in range(4):
        await tick(clock, 0.1)
    await task
    held = [record for record in link.records if record.params and record.params.get("hold")]
    assert len(held) == 4
    assert link.records[-1].params == {"tag": "wheee", "hold": False}
    task = asyncio.create_task(sink.quack("wheee", 3))
    await tick(clock)
    await sink.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert link.records[-1].params == {"tag": "wheee", "hold": False}
    assert events == [True, False, True, False]


@pytest.mark.parametrize("tag,hold", [("invalid", 0), ("greet", 1), ("wheee", 4)])
async def test_invalid_quacks_are_refused(tmp_path: Path, tag: str, hold: float) -> None:
    *_, sink = rig(bank(tmp_path))
    with pytest.raises(ValueError):
        await sink.quack(tag, hold)
