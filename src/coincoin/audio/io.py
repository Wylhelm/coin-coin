"""PCM transport contract shared by browser and robot audio."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

SAMPLE_RATE = 24_000
FRAME_MS = 20


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
        """Empty playback in less than 50 ms and reset its progress."""
        ...

    @property
    def playing(self) -> bool:
        """Whether output samples are currently being played."""
        ...

    @property
    def played_ms(self) -> int:
        """Milliseconds actually played since the last flush or response."""
        ...
