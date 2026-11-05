"""Opt-in real Silero checks; model and source WAV remain local and Git-ignored."""

import os
import wave
from pathlib import Path

import numpy as np
import onnxruntime as ort
import pytest

from coincoin.wake.vad import WINDOW_SAMPLES, VoiceActivityDetector

MODEL = Path("assets/models/silero_vad.onnx")
SPEECH = Path("assets/wake/vad/positives/fr/fr_near_01.wav")


@pytest.fixture
def real_samples() -> np.ndarray:
    missing = [str(path) for path in (MODEL, SPEECH) if not path.is_file()]
    if missing:
        message = f"Real VAD assets missing: {', '.join(missing)}; see assets/wake/README.md"
        if os.environ.get("COINCOIN_REQUIRE_VAD_ASSETS") == "1":
            pytest.fail(message)
        pytest.skip(message)
    with wave.open(str(SPEECH), "rb") as source:
        assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (
            16_000,
            1,
            2,
        )
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")


def test_real_onnx_matches_official_context_contract_and_reset(real_samples: np.ndarray) -> None:
    options = ort.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    reference = ort.InferenceSession(
        str(MODEL), sess_options=options, providers=["CPUExecutionProvider"]
    )
    probability, reset = VoiceActivityDetector._load_model(MODEL)
    reference_state = np.zeros((2, 1, 128), dtype=np.float32)
    previous = np.zeros((1, 64), dtype=np.float32)
    actual: list[float] = []
    expected: list[float] = []
    frames = [
        real_samples[offset : offset + WINDOW_SAMPLES]
        for offset in range(0, len(real_samples) - WINDOW_SAMPLES + 1, WINDOW_SAMPLES)
    ]
    for frame in frames:
        current = frame.astype(np.float32).reshape(1, WINDOW_SAMPLES) / 32768.0
        scores, reference_state = reference.run(
            None,
            {
                "input": np.concatenate((previous, current), axis=1),
                "state": reference_state,
                "sr": np.array(16_000, dtype=np.int64),
            },
        )
        assert isinstance(scores, np.ndarray)
        assert isinstance(reference_state, np.ndarray)
        previous = current[:, -64:].copy()
        expected.append(float(scores[0, 0]))
        actual.append(probability(frame))
    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-7)
    assert np.array_equal(np.array(actual) >= 0.5, np.array(expected) >= 0.5)
    reset()
    np.testing.assert_allclose([probability(frame) for frame in frames], actual, rtol=0, atol=1e-7)
    print(
        f"Silero real WAV: {len(frames)} windows, max probability delta "
        f"{np.max(np.abs(np.array(actual) - expected)):.9f}; reset reproduces all scores"
    )


def test_real_speech_silence_wav_timestamps_and_detector_reset(
    real_samples: np.ndarray, tmp_path: Path
) -> None:
    # This TTS fixture's audible speech occupies 0.200-0.808 s before padding.
    annotated_start = 1.200
    annotated_end = 1.808
    padded = np.concatenate(
        (np.zeros(16_000, dtype="<i2"), real_samples, np.zeros(32_000, dtype="<i2"))
    )
    path = tmp_path / "speech_silence.wav"
    with wave.open(str(path), "wb") as target:
        target.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        target.writeframes(padded.astype("<i2").tobytes())
    with wave.open(str(path), "rb") as source:
        pcm = source.readframes(source.getnframes())
    detector = VoiceActivityDetector(MODEL)

    def transitions() -> list[tuple[str, float]]:
        events = []
        for offset in range(0, len(pcm), 640):
            events.extend(detector.feed(pcm[offset : offset + 640]))
        return [(event.kind, event.timestamp_s) for event in events if event.kind != "speech_frame"]

    measured = transitions()
    assert [kind for kind, _ in measured] == ["speech_start", "speech_end"]
    assert measured[0][1] == pytest.approx(annotated_start, abs=0.100)
    assert measured[1][1] == pytest.approx(annotated_end + 0.700, abs=0.100)
    assert not detector.speech_active
    detector.reset()
    assert transitions() == measured
    print(
        f"Silero real speech/silence: {measured}; expected onset {annotated_start:.3f}s "
        f"and end + hangover {annotated_end + 0.700:.3f}s, tolerance 100 ms"
    )
