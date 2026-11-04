"""PCM16 conversion and sample-rate conversion helpers."""

import numpy as np
import soxr
from numpy.typing import NDArray


def pcm16_to_float32(pcm: bytes) -> NDArray[np.float32]:
    """Decode little-endian PCM16 mono samples into normalized float32."""
    if len(pcm) % 2:
        raise ValueError("PCM16 data must contain whole samples")
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def float32_to_pcm16(samples: NDArray[np.floating]) -> bytes:
    """Encode normalized float samples as clipped little-endian PCM16."""
    values = np.asarray(samples, dtype=np.float32)
    if not np.isfinite(values).all():
        raise ValueError("Audio samples must be finite")
    scaled = np.rint(np.clip(values, -1.0, 1.0) * 32768.0)
    return np.clip(scaled, -32768, 32767).astype("<i2").tobytes()


def resample_float32(
    samples: NDArray[np.floating], source_rate: int, target_rate: int
) -> NDArray[np.float32]:
    """Resample mono audio with SoXR and return exactly the rounded target length."""
    _validate_rates(source_rate, target_rate)
    values = np.asarray(samples, dtype=np.float32)
    if not np.isfinite(values).all():
        raise ValueError("Audio samples must be finite")
    expected = round(len(values) * target_rate / source_rate)
    if not len(values):
        return np.empty(0, dtype=np.float32)
    output = np.asarray(soxr.resample(values, source_rate, target_rate), dtype=np.float32)
    if len(output) > expected:
        return output[:expected]
    if len(output) < expected:
        return np.pad(output, (0, expected - len(output)))
    return output


def resample_pcm16(pcm: bytes, source_rate: int, target_rate: int) -> bytes:
    """Resample little-endian PCM16 mono audio."""
    return float32_to_pcm16(resample_float32(pcm16_to_float32(pcm), source_rate, target_rate))


class Pcm16Resampler:
    """Stateful SoXR resampler for PCM16 chunks in a continuous mono stream."""

    def __init__(self, source_rate: int, target_rate: int) -> None:
        _validate_rates(source_rate, target_rate)
        self.source_rate = source_rate
        self.target_rate = target_rate
        self._stream = soxr.ResampleStream(
            source_rate, target_rate, num_channels=1, dtype="float32"
        )
        self._input_samples = 0
        self._output_samples = 0
        self._finished = False

    def process(self, pcm: bytes, *, final: bool = False) -> bytes:
        """Convert one chunk; set ``final`` on the final chunk to flush the filter."""
        if self._finished:
            raise RuntimeError("Cannot write to a finished resampler")
        samples = pcm16_to_float32(pcm)
        self._input_samples += len(samples)
        result = self._stream.resample_chunk(samples, last=final)
        output = np.asarray(result, dtype=np.float32)
        self._output_samples += len(output)
        if final:
            self._finished = True
            expected = round(self._input_samples * self.target_rate / self.source_rate)
            if self._output_samples > expected:
                output = output[: max(0, len(output) - (self._output_samples - expected))]
            elif self._output_samples < expected:
                output = np.pad(output, (0, expected - self._output_samples))
        return float32_to_pcm16(output)


def _validate_rates(source_rate: int, target_rate: int) -> None:
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("Sample rates must be positive")
