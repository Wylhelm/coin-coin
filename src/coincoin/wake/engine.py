"""Wake-word engine contract and configuration-based factory."""

from dataclasses import dataclass
from typing import Protocol

from coincoin.config import Settings


@dataclass(frozen=True)
class WakeHit:
    """A detected wake phrase, scored by the selected recognizer."""

    score: float
    end_offset_ms: int
    lang: str | None


class WakeEngine(Protocol):
    """Incrementally consume mono PCM16 16 kHz audio."""

    def feed(self, pcm16_16k: bytes) -> WakeHit | None:
        """Consume at most 80 ms and return a hit when the wake phrase is heard."""
        ...

    def reset(self) -> None:
        """Clear recognizer audio and endpointing state."""
        ...


def create_wake_engine(settings: Settings) -> WakeEngine:
    """Build the configured local engine without importing model runtimes eagerly."""
    if settings.wake_engine == "vosk":
        from coincoin.wake.vosk_engine import VoskWakeEngine

        return VoskWakeEngine(
            threshold=settings.wake_threshold,
            model_root="assets/models",
        )
    if settings.wake_engine == "oww":
        from coincoin.wake.oww_engine import OpenWakeWordEngine

        return OpenWakeWordEngine()
    raise ValueError(f"Unsupported wake engine: {settings.wake_engine}")
