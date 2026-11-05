"""OpenWakeWord is not retained until it beats Vosk on the evaluation corpus."""

from coincoin.wake.engine import WakeHit


class OpenWakeWordEngine:
    """Explicit placeholder for the unselected OpenWakeWord candidate."""

    def __init__(self) -> None:
        raise NotImplementedError(
            "OpenWakeWord is not selected; run scripts/eval_wake.py before adding a model."
        )

    def feed(self, pcm16_16k: bytes) -> WakeHit | None:
        del pcm16_16k
        raise NotImplementedError("OpenWakeWord is not selected.")

    def reset(self) -> None:
        raise NotImplementedError("OpenWakeWord is not selected.")
