import numpy as np
import pytest

from coincoin.audio.mixer import mix_pcm16
from coincoin.audio.resample import float32_to_pcm16, pcm16_to_float32


def test_mixer_sums_voice_and_quack_from_sample_zero_without_delay() -> None:
    voice = float32_to_pcm16(np.array([0.1, 0.2, 0.3], dtype=np.float32))
    quacks = float32_to_pcm16(np.array([0.2, -0.1], dtype=np.float32))

    actual = pcm16_to_float32(mix_pcm16(voice, quacks))

    assert len(actual) == 3
    np.testing.assert_allclose(actual[:2], [0.3, 0.1], atol=1 / 32768)
    assert actual[2] == pytest.approx(0.3, abs=1 / 32768)


def test_mixer_smoothly_limits_overload_and_honors_gain() -> None:
    loud = float32_to_pcm16(np.array([0.8, 0.8], dtype=np.float32))
    mixed = pcm16_to_float32(mix_pcm16(loud, loud))

    assert np.all(np.abs(mixed) <= 1.0)
    assert np.all(mixed > 0.9)
    assert np.all(mixed < 1.0)
    np.testing.assert_allclose(
        pcm16_to_float32(mix_pcm16(loud, voice_gain=0.5)), [0.4, 0.4], atol=1 / 32768
    )


def test_mixer_rejects_invalid_pcm_and_gain() -> None:
    with pytest.raises(ValueError, match="negative"):
        mix_pcm16(b"", voice_gain=-1)
    with pytest.raises(ValueError, match="whole samples"):
        mix_pcm16(b"\x01")
