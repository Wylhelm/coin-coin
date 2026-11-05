"""Silero ONNX voice activity detection with 300 ms pre-roll and 700 ms hangover."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import numpy as np
from numpy.typing import NDArray

SAMPLE_RATE = 16_000
WINDOW_SAMPLES = 512
WINDOW_BYTES = WINDOW_SAMPLES * 2


@dataclass(frozen=True)
class VadEvent:
    """A voice-state transition or PCM frame ready for the follow-up gate."""

    kind: Literal["speech_start", "speech_frame", "speech_end"]
    timestamp_s: float
    pcm16: bytes = b""


class VoiceActivityDetector:
    """Stream 512-sample frames through Silero VAD and preserve speech pre-roll."""

    def __init__(
        self,
        model_path: str | Path = "assets/models/silero_vad.onnx",
        *,
        threshold: float = 0.5,
        pre_roll_ms: int = 300,
        hangover_ms: int = 700,
        probability_fn: Callable[[NDArray[np.int16]], float] | None = None,
    ) -> None:
        if not 0 < threshold < 1:
            raise ValueError("VAD threshold must be between 0 and 1")
        if pre_roll_ms < 0 or hangover_ms < 0:
            raise ValueError("VAD durations must be nonnegative")
        self.threshold = threshold
        self._pre_roll_limit = max(1, round(pre_roll_ms * SAMPLE_RATE / 1000 / WINDOW_SAMPLES))
        self._hangover_frames = round(hangover_ms * SAMPLE_RATE / 1000 / WINDOW_SAMPLES)
        self._pending = bytearray()
        self._pre_roll: deque[bytes] = deque(maxlen=self._pre_roll_limit)
        self._samples = 0
        self._speaking = False
        self._quiet_frames = 0
        if probability_fn is None:
            self._probability_fn, self._reset_model_state = self._load_model(Path(model_path))
        else:
            self._probability_fn = probability_fn
            self._reset_model_state = lambda: None

    @staticmethod
    def _load_model(
        model_path: Path,
    ) -> tuple[Callable[[NDArray[np.int16]], float], Callable[[], None]]:
        if not model_path.is_file():
            raise FileNotFoundError(
                f"Silero VAD model missing: {model_path}. Run scripts/download_wake_models.sh."
            )
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime is required; install the project dependencies."
            ) from exc
        session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        inputs = {item.name for item in session.get_inputs()}
        state = np.zeros((2, 1, 128), dtype=np.float32)

        def probability(frame: NDArray[np.int16]) -> float:
            nonlocal state
            input_array = np.empty((1, len(frame)), dtype=np.float32)
            input_array[0, :] = frame / 32768.0
            feed: dict[str, np.ndarray] = {
                "input": input_array,
            }
            if "state" in inputs:
                feed["state"] = state
            if "sr" in inputs:
                feed["sr"] = np.asarray(SAMPLE_RATE, dtype=np.int64)
            outputs = session.run(None, feed)
            state = cast(NDArray[np.float32], outputs[-1])
            scores = cast(NDArray[np.float32], outputs[0])
            return float(scores[0, 0])

        def reset_state() -> None:
            nonlocal state
            state = np.zeros((2, 1, 128), dtype=np.float32)

        return probability, reset_state

    @property
    def speech_active(self) -> bool:
        return self._speaking

    def reset(self) -> None:
        """Discard buffered samples and reset segmentation state."""
        self._pending.clear()
        self._pre_roll.clear()
        self._samples = 0
        self._speaking = False
        self._quiet_frames = 0
        self._reset_model_state()

    def feed(self, pcm16_16k: bytes) -> list[VadEvent]:
        """Consume arbitrary PCM16 chunks and emit transitions at frame timestamps."""
        if len(pcm16_16k) % 2:
            raise ValueError("VAD input must contain complete PCM16 samples")
        self._pending.extend(pcm16_16k)
        events: list[VadEvent] = []
        while len(self._pending) >= WINDOW_BYTES:
            frame = bytes(self._pending[:WINDOW_BYTES])
            del self._pending[:WINDOW_BYTES]
            samples = np.frombuffer(frame, dtype="<i2").astype(np.int16)
            probability = self._probability_fn(samples)
            self._samples += WINDOW_SAMPLES
            timestamp = self._samples / SAMPLE_RATE
            voiced = probability >= self.threshold
            if not self._speaking:
                self._pre_roll.append(frame)
                if voiced:
                    self._speaking = True
                    self._quiet_frames = 0
                    events.append(
                        VadEvent(
                            "speech_start",
                            timestamp,
                            b"".join(self._pre_roll),
                        )
                    )
            elif voiced:
                self._quiet_frames = 0
                events.append(VadEvent("speech_frame", timestamp, frame))
            else:
                self._quiet_frames += 1
                events.append(VadEvent("speech_frame", timestamp, frame))
                if self._quiet_frames >= self._hangover_frames:
                    self._speaking = False
                    self._quiet_frames = 0
                    self._pre_roll.clear()
                    self._reset_model_state()
                    events.append(VadEvent("speech_end", timestamp))
        return events
