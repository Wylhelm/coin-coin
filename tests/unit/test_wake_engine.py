import json
from pathlib import Path

import pytest

from coincoin.wake.engine import WakeHit
from coincoin.wake.vosk_engine import (
    GRAMMAR,
    MAX_FRAME_BYTES,
    VoskWakeEngine,
    _phrase_score,
    load_pcm16_16k,
)


class FakeRecognizer:
    def __init__(self, words: list[dict[str, object]]) -> None:
        self.words = words
        self.reset_calls = 0

    def SetWords(self, enabled: bool) -> None:
        assert enabled

    def AcceptWaveform(self, pcm: bytes) -> bool:
        assert pcm
        return True

    def Result(self) -> str:
        return json.dumps({"result": self.words})

    def PartialResult(self) -> str:
        return "{}"

    def Reset(self) -> None:
        self.reset_calls += 1


class FakeVosk:
    def __init__(self) -> None:
        self.recognizers: list[FakeRecognizer] = []

    def SetLogLevel(self, _: int) -> None:
        pass

    def Model(self, _: str) -> object:
        return object()

    def KaldiRecognizer(self, _: object, rate: int, grammar: str) -> FakeRecognizer:
        assert rate == 16_000
        assert json.loads(grammar) == ["coin coin", "[unk]"]
        words = [
            {"word": "coin", "conf": 0.9},
            {"word": "coin", "conf": 0.8},
        ]
        recognizer = FakeRecognizer(words)
        self.recognizers.append(recognizer)
        return recognizer


def test_phrase_confidence_requires_adjacent_wake_words() -> None:
    assert _phrase_score({"result": [{"word": "coin", "conf": 0.8}]}) is None
    assert (
        _phrase_score(
            {
                "result": [
                    {"word": "coin", "conf": 0.8},
                    {"word": "noise", "conf": 1.0},
                    {"word": "coin", "conf": 0.9},
                ]
            }
        )
        is None
    )
    assert (
        _phrase_score({"result": [{"word": "Coin,", "conf": 0.9}, {"word": "coin", "conf": 0.7}]})
        == 0.7
    )


def test_vosk_engine_loads_both_local_models_and_enforces_threshold(tmp_path: Path) -> None:
    for directory in ("vosk-model-small-fr", "vosk-model-small-en-us"):
        (tmp_path / directory).mkdir()
    vosk = FakeVosk()
    engine = VoskWakeEngine(0.75, tmp_path, vosk_module=vosk)
    hit = engine.feed(bytes(MAX_FRAME_BYTES))
    assert hit == WakeHit(score=0.8, end_offset_ms=80, lang="fr")
    engine.reset()
    assert all(recognizer.reset_calls == 1 for recognizer in vosk.recognizers)


def test_vosk_engine_rejects_invalid_frames_and_threshold(tmp_path: Path) -> None:
    for directory in ("vosk-model-small-fr", "vosk-model-small-en-us"):
        (tmp_path / directory).mkdir()
    with pytest.raises(ValueError, match="threshold"):
        VoskWakeEngine(1.1, tmp_path, vosk_module=FakeVosk())
    engine = VoskWakeEngine(0.5, tmp_path, vosk_module=FakeVosk())
    for pcm in (b"", b"\x00", bytes(MAX_FRAME_BYTES + 2)):
        with pytest.raises(ValueError, match="frames"):
            engine.feed(pcm)


def test_vosk_engine_reports_actionable_missing_models(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="download_wake_models.sh"):
        VoskWakeEngine(0.5, tmp_path, vosk_module=FakeVosk())


def test_wav_reader_requires_exact_format(tmp_path: Path) -> None:
    import wave

    path = tmp_path / "test.wav"
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\x00\x00" * 512)
    assert load_pcm16_16k(path) == b"\x00\x00" * 512
    assert GRAMMAR == '["coin coin", "[unk]"]'
