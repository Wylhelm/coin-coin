"""Low-latency PCM16 mixer for voice and streamed quacks."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import numpy as np

from coincoin.audio.io import AudioIO, PlaybackProgress, TrackedPlayback
from coincoin.audio.resample import float32_to_pcm16, pcm16_to_float32
from coincoin.clock import Clock


def mix_pcm16(
    voice: bytes,
    quacks: bytes = b"",
    *,
    voice_gain: float = 1.0,
    quack_gain: float = 1.0,
) -> bytes:
    """Mix two PCM16 streams from sample zero and apply a smooth saturation knee."""
    if voice_gain < 0 or quack_gain < 0:
        raise ValueError("Audio gains must not be negative")
    voice_samples = pcm16_to_float32(voice) * voice_gain
    quack_samples = pcm16_to_float32(quacks) * quack_gain
    length = max(len(voice_samples), len(quack_samples))
    mixed = np.zeros(length, dtype=np.float32)
    mixed[: len(voice_samples)] += voice_samples
    mixed[: len(quack_samples)] += quack_samples

    magnitude = np.abs(mixed)
    knee = 0.9
    above_knee = magnitude > knee
    magnitude[above_knee] = knee + (1.0 - knee) * np.tanh(
        (magnitude[above_knee] - knee) / (1.0 - knee)
    )
    return float32_to_pcm16(np.copysign(magnitude, mixed))


@dataclass
class _VoiceSpan:
    physical: int
    samples: int


class OutputMixer:
    """One physical output; Realtime sees only voice samples in its playback clock."""

    def __init__(
        self,
        audio: AudioIO,
        clock: Clock,
        on_pcm: Callable[[bytes, float], None],
    ) -> None:
        self.audio = audio
        self.clock = clock
        self.on_pcm = on_pcm
        self._lock = asyncio.Lock()
        self._spans: deque[_VoiceSpan] = deque()
        self._voice_played = 0
        self._sent = 0
        self._physical_sent = 0
        self._epoch = self.physical_progress.epoch
        self._quack = b""
        self._quack_end = 0
        self._quack_generation = 0

    @property
    def physical_progress(self) -> PlaybackProgress:
        if isinstance(self.audio, TrackedPlayback):
            return self.audio.playback_progress
        return PlaybackProgress(0, self._physical_sent, self.audio.played_ms * 24)

    def _sync_epoch(self) -> None:
        epoch = self.physical_progress.epoch
        if epoch != self._epoch:
            self._epoch = epoch
            self._spans.clear()
            self._voice_played = 0
            self._sent = self._physical_sent = 0
            self._quack = b""
            self._quack_generation += 1

    @property
    def playback_progress(self) -> PlaybackProgress:
        self._sync_epoch()
        played = self.physical_progress.played_samples
        while self._spans and played >= self._spans[0].physical + self._spans[0].samples:
            self._voice_played += self._spans.popleft().samples
        voice_played = self._voice_played + sum(
            min(span.samples, max(0, played - span.physical)) for span in self._spans
        )
        return PlaybackProgress(self._epoch, self._sent, voice_played)

    @property
    def played_ms(self) -> int:
        return self.playback_progress.played_samples // 24

    @property
    def playing(self) -> bool:
        return self.audio.playing

    def mic_frames(self) -> AsyncIterator[bytes]:
        return self.audio.mic_frames()

    async def _write(self, voice: bytes = b"") -> None:
        self._sync_epoch()
        length = len(voice) if voice else min(960, len(self._quack))
        sound, self._quack = self._quack[:length], self._quack[length:]
        pcm = mix_pcm16(voice, sound)
        start = self.physical_progress.sent_samples
        epoch = self._epoch
        self.on_pcm(pcm, start / 24)
        await self.audio.play(pcm)
        if self.physical_progress.epoch != epoch:
            self._sync_epoch()
            return
        self._physical_sent += len(pcm) // 2
        if voice:
            self._spans.append(_VoiceSpan(start, len(voice) // 2))
            self._sent += len(voice) // 2
        if sound:
            self._quack_end = start + len(sound) // 2

    async def play(self, pcm: bytes) -> None:
        if len(pcm) % 2:
            raise ValueError("Playback expects PCM16")
        for offset in range(0, len(pcm), 960):
            async with self._lock:
                await self._write(pcm[offset : offset + 960])

    async def quack(self, pcm: bytes) -> None:
        self._sync_epoch()
        self._quack_generation += 1
        generation = self._quack_generation
        self._quack = pcm
        self._quack_end = self.physical_progress.sent_samples
        epoch = self._epoch
        deadline = self.clock.now() + len(pcm) / 48000 + 5
        while generation == self._quack_generation:
            if self.physical_progress.epoch != epoch:
                self._sync_epoch()
                raise ConnectionError("Quack playback page or generation was replaced")
            if not self._quack and self.physical_progress.played_samples >= self._quack_end:
                return
            if self.clock.now() >= deadline:
                raise TimeoutError("Quack playback progress timed out")
            async with self._lock:
                progress = self.physical_progress
                if self._quack and progress.sent_samples - progress.played_samples < 2880:
                    await self._write()
            await self.clock.sleep(0.02)

    async def cancel_quack(self) -> None:
        active = bool(self._quack) or (self.physical_progress.played_samples < self._quack_end)
        self._quack = b""
        self._quack_generation += 1
        voice = self.playback_progress
        voice_pending = voice.sent_samples > voice.played_samples
        quack_pending = self.physical_progress.played_samples < self._quack_end
        if active and quack_pending and not voice_pending:
            await self.flush()

    async def flush(self) -> None:
        self._quack = b""
        self._quack_generation += 1
        await self.audio.flush()
        self._spans.clear()
        self._voice_played = 0
        self._sent = self._physical_sent = self._quack_end = 0
        self._epoch = self.physical_progress.epoch
