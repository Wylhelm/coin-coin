"""Bilingual Vosk recognizer using the measured wake phrase grammar."""

import json
import re
import wave
from pathlib import Path
from typing import Any

from coincoin.wake.engine import WakeHit

SAMPLE_RATE = 16_000
MAX_FRAME_BYTES = int(SAMPLE_RATE * 2 * 0.08)
MODEL_NAMES = {
    "fr": "vosk-model-small-fr",
    "en": "vosk-model-small-en-us",
}
GRAMMAR = json.dumps(["coin coin", "[unk]"])
_WORDS = re.compile(r"[^a-z]+")


def _phrase_score(result: dict[str, Any]) -> float | None:
    """Return the weakest word confidence only for a contiguous coin coin phrase."""
    words = result.get("result")
    if not isinstance(words, list):
        return None
    normalized: list[tuple[str, float]] = []
    for word in words:
        if not isinstance(word, dict) or not isinstance(word.get("word"), str):
            continue
        token = _WORDS.sub("", word["word"].casefold())
        confidence = word.get("conf")
        if token and isinstance(confidence, (int, float)):
            normalized.append((token, float(confidence)))
    scores = [
        min(normalized[index][1], normalized[index + 1][1])
        for index in range(len(normalized) - 1)
        if normalized[index][0] == normalized[index + 1][0] == "coin"
    ]
    return max(scores) if scores else None


class VoskWakeEngine:
    """Run French and English small Vosk models with a strict two-word grammar."""

    def __init__(
        self,
        threshold: float,
        model_root: str | Path = "assets/models",
        *,
        vosk_module: Any | None = None,
    ) -> None:
        if not 0 <= threshold <= 1:
            raise ValueError("Wake threshold must be between 0 and 1")
        root = Path(model_root)
        missing = [root / name for name in MODEL_NAMES.values() if not (root / name).is_dir()]
        if missing:
            paths = ", ".join(str(path) for path in missing)
            raise FileNotFoundError(
                f"Vosk models missing: {paths}. Run scripts/download_wake_models.sh."
            )
        if vosk_module is None:
            try:
                import vosk as vosk_module
            except ImportError as exc:
                raise RuntimeError("Vosk is required; install the project dependencies.") from exc

        vosk_module.SetLogLevel(-1)
        self._threshold = threshold
        self._recognizers: list[tuple[str, Any]] = []
        for lang, model_name in MODEL_NAMES.items():
            model = vosk_module.Model(str(root / model_name))
            recognizer = vosk_module.KaldiRecognizer(model, SAMPLE_RATE, GRAMMAR)
            recognizer.SetWords(True)
            self._recognizers.append((lang, recognizer))
        self._samples = 0

    def feed(self, pcm16_16k: bytes) -> WakeHit | None:
        if not pcm16_16k or len(pcm16_16k) > MAX_FRAME_BYTES or len(pcm16_16k) % 2:
            raise ValueError("Wake frames must contain 1–80 ms of complete PCM16 samples")
        self._samples += len(pcm16_16k) // 2
        hits: list[WakeHit] = []
        for lang, recognizer in self._recognizers:
            if recognizer.AcceptWaveform(pcm16_16k):
                result = json.loads(recognizer.Result())
            else:
                result = json.loads(recognizer.PartialResult())
            if not isinstance(result, dict):
                continue
            score = _phrase_score(result)
            if score is not None and score >= self._threshold:
                hits.append(
                    WakeHit(
                        score=score,
                        end_offset_ms=round(self._samples * 1000 / SAMPLE_RATE),
                        lang=lang,
                    )
                )
        return max(hits, key=lambda hit: hit.score, default=None)

    def reset(self) -> None:
        self._samples = 0
        for _, recognizer in self._recognizers:
            recognizer.Reset()


def load_pcm16_16k(path: Path) -> bytes:
    """Read an uncompressed mono PCM16 WAV, rejecting implicit conversions."""
    with wave.open(str(path), "rb") as source:
        if source.getframerate() != SAMPLE_RATE or source.getnchannels() != 1:
            raise ValueError(f"{path} must be mono 16 kHz PCM16 WAV")
        if source.getsampwidth() != 2 or source.getcomptype() != "NONE":
            raise ValueError(f"{path} must be uncompressed PCM16 WAV")
        return source.readframes(source.getnframes())
