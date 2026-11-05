import numpy as np
import pytest

from coincoin.audio.resample import (
    Pcm16Resampler,
    float32_to_pcm16,
    pcm16_to_float32,
    resample_pcm16,
)
from coincoin.audio.tone import find_tone_offset, make_tone


def _sine(samples: int, rate: int, frequency: float = 1_000.0) -> bytes:
    time = np.arange(samples, dtype=np.float32) / rate
    return float32_to_pcm16(0.25 * np.sin(2 * np.pi * frequency * time))


def test_pcm16_conversion_is_little_endian_and_clipped() -> None:
    pcm = float32_to_pcm16(np.array([-1.0, -0.5, 0.0, 0.5, 1.0], dtype=np.float32))
    assert pcm == b"\x00\x80\x00\xc0\x00\x00\x00\x40\xff\x7f"
    np.testing.assert_allclose(
        pcm16_to_float32(pcm), [-1.0, -0.5, 0.0, 0.5, 32767 / 32768], atol=1e-6
    )


def test_resample_24khz_sine_to_16khz_preserves_frequency_energy_and_length() -> None:
    source = _sine(24_000, 24_000)
    output = resample_pcm16(source, 24_000, 16_000)
    samples = pcm16_to_float32(output)
    spectrum = np.abs(np.fft.rfft(samples))
    frequency = int(np.argmax(spectrum) * 16_000 / len(samples))

    assert len(samples) == 16_000
    assert frequency == 1_000
    assert np.sqrt(np.mean(samples**2)) == pytest.approx(
        np.sqrt(np.mean(pcm16_to_float32(source) ** 2)), rel=0.01
    )


def test_stateful_resampler_keeps_exact_length_across_20ms_frames() -> None:
    source = _sine(24_000, 24_000)
    frames = [source[offset : offset + 960] for offset in range(0, len(source), 960)]
    converter = Pcm16Resampler(24_000, 16_000)
    converted = b"".join(
        converter.process(frame, final=index == len(frames) - 1)
        for index, frame in enumerate(frames)
    )

    assert len(pcm16_to_float32(converted)) == 16_000
    assert np.sqrt(np.mean(pcm16_to_float32(converted) ** 2)) == pytest.approx(0.1768, rel=0.01)
    with pytest.raises(RuntimeError, match="finished"):
        converter.process(b"\x00\x00")


def test_resampler_rejects_invalid_input() -> None:
    with pytest.raises(ValueError, match="whole samples"):
        pcm16_to_float32(b"\x01")
    with pytest.raises(ValueError, match="positive"):
        resample_pcm16(b"\x00\x00", 0, 16_000)
    with pytest.raises(ValueError, match="finite"):
        float32_to_pcm16(np.array([np.nan], dtype=np.float32))


def test_tone_generation_and_correlation_find_the_round_trip_offset() -> None:
    tone = make_tone()
    silence = np.zeros(2_400, dtype=np.float32)
    samples = np.concatenate((silence, pcm16_to_float32(tone), silence))

    assert len(pcm16_to_float32(tone)) == 4_800
    assert find_tone_offset(samples, tone) == 2_400
    assert find_tone_offset(np.zeros(7_200, dtype=np.float32), tone) is None
    with pytest.raises(ValueError, match="must be positive"):
        make_tone(frequency=0)


def test_resample_converts_24khz_to_16khz_pcm16() -> None:
    samples = np.full(480, 1000, dtype="<i2")
    output = resample_pcm16(samples.tobytes(), 24_000, 16_000)
    assert len(output) == 320 * 2
    converted = np.frombuffer(output, dtype="<i2")
    assert np.mean(converted) == pytest.approx(1000, abs=1)


@pytest.mark.parametrize(
    "pcm,input_rate,output_rate",
    [(b"\x00", 24_000, 16_000), (b"\x00\x00", 0, 16_000)],
)
def test_resample_rejects_invalid_inputs(pcm: bytes, input_rate: int, output_rate: int) -> None:
    with pytest.raises(ValueError):
        resample_pcm16(pcm, input_rate, output_rate)
