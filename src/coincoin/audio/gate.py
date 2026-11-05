"""State-aware audio gate: never forward sleeping audio and buffer post-wake audio."""

from collections import deque
from dataclasses import dataclass, field

from coincoin.statemachine import Mode


@dataclass
class AudioGate:
    """Select PCM frames for Realtime and count transmitted bytes by conversation mode."""

    max_buffer_s: float = 5.0
    sample_rate: int = 24_000
    _buffer: deque[bytes] = field(default_factory=deque, init=False)
    _buffer_bytes: int = field(default=0, init=False)
    _counters: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.max_buffer_s <= 0 or self.sample_rate <= 0:
            raise ValueError("Audio gate buffer duration and sample rate must be positive")

    @property
    def bytes_sent(self) -> dict[str, int]:
        """Return a copy suitable for journal telemetry."""
        return dict(self._counters)

    @property
    def buffered_bytes(self) -> int:
        return self._buffer_bytes

    def reset(self) -> None:
        """Drop buffered audio without changing cumulative transmission counters."""
        self._buffer.clear()
        self._buffer_bytes = 0

    def record_sent(self, mode: Mode, byte_count: int) -> None:
        """Record PCM only after the transport has accepted it."""
        if byte_count < 0 or byte_count % 2:
            raise ValueError("Sent audio byte count must be nonnegative and sample-aligned")
        if byte_count:
            self._counters[mode.value] = self._counters.get(mode.value, 0) + byte_count

    def process(
        self,
        pcm16_24k: bytes,
        mode: Mode,
        *,
        session_ready: bool = False,
        speech: bool = False,
    ) -> list[bytes]:
        """Return zero or more ordered frames permitted by the current state."""
        if len(pcm16_24k) % 2:
            raise ValueError("Audio gate accepts complete PCM16 samples only")
        if mode == Mode.SLEEPING:
            self.reset()
            return []
        if mode == Mode.FOLLOWUP and not speech:
            return []
        if mode == Mode.AWAKE and not session_ready:
            if pcm16_24k:
                self._buffer.append(pcm16_24k)
                self._buffer_bytes += len(pcm16_24k)
                max_bytes = int(self.max_buffer_s * self.sample_rate * 2)
                excess = self._buffer_bytes - max_bytes
                while self._buffer and excess > 0:
                    oldest = self._buffer.popleft()
                    if len(oldest) > excess:
                        self._buffer.appendleft(oldest[excess:])
                        self._buffer_bytes -= excess
                        break
                    self._buffer_bytes -= len(oldest)
                    excess -= len(oldest)
            return []

        frames = list(self._buffer)
        if pcm16_24k:
            frames.append(pcm16_24k)
        self.reset()
        return frames
