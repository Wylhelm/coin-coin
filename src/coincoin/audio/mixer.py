"""Low-latency PCM16 mixer for voice and streamed quacks."""

import numpy as np

from coincoin.audio.resample import float32_to_pcm16, pcm16_to_float32


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
