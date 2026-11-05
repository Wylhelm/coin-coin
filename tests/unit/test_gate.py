import random

import pytest

from coincoin.audio.gate import AudioGate
from coincoin.statemachine import Mode

FRAME = bytes(960)


def test_sleeping_sends_zero_bytes_for_ten_minutes_of_noise() -> None:
    gate = AudioGate()
    source = random.Random(7)
    for _ in range(10 * 60 * 50):
        frame = source.randbytes(960)
        assert gate.process(frame, Mode.SLEEPING) == []
    assert gate.bytes_sent == {}


def test_post_wake_buffer_is_replayed_in_order_when_ready() -> None:
    gate = AudioGate()
    first, second, current = b"\x01\x00", b"\x02\x00", b"\x03\x00"
    assert gate.process(first, Mode.AWAKE) == []
    assert gate.process(second, Mode.AWAKE) == []
    assert gate.process(current, Mode.AWAKE, session_ready=True) == [first, second, current]
    assert gate.buffered_bytes == 0
    assert gate.bytes_sent == {"AWAKE": 6}


def test_wake_buffer_is_bounded_to_five_seconds_and_drops_oldest() -> None:
    gate = AudioGate()
    size = int(5 * 24_000 * 2)
    gate.process(b"\x01\x00" * (size // 2), Mode.AWAKE)
    newer = b"\x02\x00" * 480
    gate.process(newer, Mode.AWAKE)
    assert gate.buffered_bytes == size
    frames = gate.process(b"", Mode.AWAKE, session_ready=True)
    assert len(frames) == 2
    assert frames[0] == b"\x01\x00" * (size // 2 - 480)
    assert frames[1] == newer


def test_followup_sends_only_vad_speech_frames() -> None:
    gate = AudioGate()
    assert gate.process(FRAME, Mode.FOLLOWUP) == []
    assert gate.process(FRAME, Mode.FOLLOWUP, speech=True) == [FRAME]
    assert gate.bytes_sent == {"FOLLOWUP": len(FRAME)}


def test_awake_sends_all_frames_after_session_is_ready() -> None:
    gate = AudioGate()
    assert gate.process(FRAME, Mode.AWAKE, session_ready=True) == [FRAME]
    assert gate.bytes_sent == {"AWAKE": len(FRAME)}


def test_sleeping_discards_any_unflushed_wake_audio() -> None:
    gate = AudioGate()
    gate.process(FRAME, Mode.AWAKE)
    assert gate.process(FRAME, Mode.SLEEPING) == []
    assert gate.buffered_bytes == 0
    assert gate.process(FRAME, Mode.AWAKE, session_ready=True) == [FRAME]


def test_reset_preserves_cumulative_counters() -> None:
    gate = AudioGate()
    gate.process(FRAME, Mode.AWAKE, session_ready=True)
    gate.reset()
    assert gate.bytes_sent == {"AWAKE": len(FRAME)}


@pytest.mark.parametrize("duration,rate", [(0, 24_000), (5, 0)])
def test_invalid_gate_limits_are_rejected(duration: float, rate: int) -> None:
    with pytest.raises(ValueError):
        AudioGate(max_buffer_s=duration, sample_rate=rate)


def test_odd_pcm16_payload_is_rejected() -> None:
    with pytest.raises(ValueError, match="PCM16"):
        AudioGate().process(b"\x00", Mode.AWAKE)
