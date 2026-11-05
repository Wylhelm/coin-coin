"""PCM transport contract shared by browser and robot audio."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

SAMPLE_RATE = 24_000
FRAME_MS = 20


@dataclass(frozen=True)
class PlaybackProgress:
    """Sample-accurate progress within one page/flush playback epoch."""

    epoch: int
    sent_samples: int
    played_samples: int


@runtime_checkable
class AudioIO(Protocol):
    """Exchange PCM16 mono 24 kHz audio without exposing the transport."""

    def mic_frames(self) -> AsyncIterator[bytes]:
        """Yield incoming 20 ms frames (480 samples, 960 bytes each)."""
        ...

    async def play(self, pcm: bytes) -> None:
        """Queue PCM16 mono 24 kHz samples for output."""
        ...

    async def flush(self) -> None:
        """Empty playback within 100 ms or raise, and reset its progress."""
        ...

    @property
    def playing(self) -> bool:
        """Whether output samples are currently being played."""
        ...

    @property
    def played_ms(self) -> int:
        """Milliseconds actually played since the last flush or response."""
        ...


@runtime_checkable
class TrackedPlayback(AudioIO, Protocol):
    @property
    def playback_progress(self) -> PlaybackProgress: ...
