"""Tone generation and correlation for browser audio round-trip measurements."""

import numpy as np
from numpy.typing import NDArray

from coincoin.audio.resample import float32_to_pcm16, pcm16_to_float32


def make_tone(
    frequency: float = 1_000.0, duration_s: float = 0.2, sample_rate: int = 24_000
) -> bytes:
    """Create a Hann-windowed PCM16 tone to avoid clicks at its boundaries."""
    if frequency <= 0 or duration_s <= 0 or sample_rate <= 0:
        raise ValueError("Tone frequency, duration, and sample rate must be positive")
    count = round(duration_s * sample_rate)
    phase = np.arange(count, dtype=np.float32) / sample_rate
    envelope = np.hanning(count).astype(np.float32)
    return float32_to_pcm16(0.25 * envelope * np.sin(2 * np.pi * frequency * phase))


def find_tone_offset(
    samples: NDArray[np.floating], reference_pcm: bytes, *, threshold: float = 0.25
) -> int | None:
    """Find a tone in PCM16 audio using normalized FFT cross-correlation."""
    reference = pcm16_to_float32(reference_pcm)
    values = np.asarray(samples, dtype=np.float32)
    if len(reference) == 0 or len(values) < len(reference):
        return None
    convolution_size = len(values) + len(reference) - 1
    fft_size = 1 << (convolution_size - 1).bit_length()
    correlation = np.fft.irfft(
        np.fft.rfft(values, fft_size) * np.fft.rfft(reference[::-1], fft_size),
        fft_size,
    )[len(reference) - 1 : len(values)]
    squared = np.square(values, dtype=np.float32)
    prefix_energy = np.concatenate(([0.0], np.cumsum(squared, dtype=np.float64)))
    window_energy = prefix_energy[len(reference) :] - prefix_energy[: -len(reference)]
    denominator = np.sqrt(np.maximum(window_energy, 1e-12) * np.sum(reference**2))
    scores = np.abs(correlation) / np.maximum(denominator, 1e-12)
    index = int(np.argmax(scores))
    return index if scores[index] >= threshold else None
