import numpy as np
import pytest

from coincoin.wake.vad import WINDOW_BYTES, VoiceActivityDetector


def pcm_frame(level: int) -> bytes:
    return np.full(512, level, dtype="<i2").tobytes()


def test_vad_emits_preroll_start_frames_and_700ms_hangover() -> None:
    detector = VoiceActivityDetector(
        probability_fn=lambda samples: 0.9 if np.max(np.abs(samples)) > 1000 else 0.1
    )
    silence = pcm_frame(0)
    speech = pcm_frame(4000)
    assert detector.feed(silence * 10) == []
    start = detector.feed(speech)
    assert len(start) == 1
    assert start[0].kind == "speech_start"
    assert start[0].timestamp_s == pytest.approx(11 * 512 / 16_000)
    assert len(start[0].pcm16) == 9 * WINDOW_BYTES
    assert detector.speech_active
    active = detector.feed(speech)
    assert [event.kind for event in active] == ["speech_frame"]
    tail = detector.feed(silence * 22)
    assert tail[-1].kind == "speech_end"
    assert tail[-1].timestamp_s == pytest.approx(34 * 512 / 16_000)
    assert len([event for event in tail if event.kind == "speech_frame"]) == 22
    assert not detector.speech_active


def test_vad_buffers_arbitrary_chunking_and_ignores_incomplete_window() -> None:
    detector = VoiceActivityDetector(probability_fn=lambda _: 0.1)
    raw = pcm_frame(0)
    assert detector.feed(raw[:100]) == []
    assert detector.feed(raw[100:]) == []
    assert detector.speech_active is False
    with pytest.raises(ValueError, match="complete PCM16"):
        detector.feed(b"\x00")


def test_vad_reset_discards_partial_input_and_state() -> None:
    detector = VoiceActivityDetector(
        probability_fn=lambda samples: 0.9 if np.max(np.abs(samples)) > 1000 else 0.1
    )
    detector.feed(pcm_frame(0) * 2 + pcm_frame(5000))
    assert detector.speech_active
    detector.feed(b"\x00\x00")
    detector.reset()
    assert not detector.speech_active
    assert detector.feed(pcm_frame(0) * 2) == []


def test_vad_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="threshold"):
        VoiceActivityDetector(threshold=1)
    with pytest.raises(ValueError, match="durations"):
        VoiceActivityDetector(pre_roll_ms=-1)
