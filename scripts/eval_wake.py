"""Measure local wake-word recall, false activations per hour, and latency."""

import argparse
import hashlib
import wave
from dataclasses import dataclass
from pathlib import Path

from coincoin.wake.engine import WakeEngine
from coincoin.wake.vosk_engine import VoskWakeEngine, load_pcm16_16k

FRAME_BYTES = 16_000 * 2 * 80 // 1000
RECORDED_ATTESTATION = (
    "I attest that every positive WAV in this directory is a human recording, "
    "not synthesized speech; speaker identity, phrase and distance are not automatically verified."
)


def validate_positive_source(directory: Path, source: str, attestation: str | None) -> str:
    if source == "synthetic":
        if attestation:
            raise ValueError("A recorded attestation cannot label a synthetic corpus")
        return "User-declared synthetic corpus; no automatic provenance verification."
    if attestation != RECORDED_ATTESTATION:
        raise ValueError(
            f"--positive-source recorded requires --attest-recorded '{RECORDED_ATTESTATION}'"
        )
    synthetic_roots = [Path("assets/wake/positives"), Path("assets/wake/vad")]
    resolved = directory.resolve()
    if any(
        resolved == root.resolve() or root.resolve() in resolved.parents for root in synthetic_roots
    ):
        raise ValueError("Known synthetic directory cannot be evaluated as recorded")
    known_hashes = {
        hashlib.sha256(path.read_bytes()).digest()
        for root in synthetic_roots
        for path in root.rglob("*.wav")
    }
    if any(
        hashlib.sha256(path.read_bytes()).digest() in known_hashes
        for path in directory.rglob("*.wav")
    ):
        raise ValueError("Positive WAV matches a known local synthetic WAV; recorded label refused")
    return (
        f"User attestation (not independently verified): {attestation} "
        "Hash comparison only excludes exact matches to available local synthetic WAVs; "
        "it cannot detect other TTS or identify a speaker."
    )


@dataclass(frozen=True)
class Clip:
    path: Path
    category: str
    source: str
    lang: str | None
    duration_s: float
    scores: tuple[float, ...]
    latency_ms: int | None


def _language(path: Path) -> str | None:
    name = path.name.casefold()
    parts = {part.casefold() for part in path.parts}
    for lang in ("fr", "en"):
        if lang in parts or name.startswith(f"{lang}_"):
            return lang
    return None


def _duration(path: Path) -> float:
    with wave.open(str(path), "rb") as source:
        if source.getframerate() != 16_000 or source.getnchannels() != 1:
            raise ValueError(f"{path} must be mono 16 kHz WAV")
        if source.getsampwidth() != 2 or source.getcomptype() != "NONE":
            raise ValueError(f"{path} must be uncompressed PCM16 WAV")
        return source.getnframes() / source.getframerate()


def _detect(engine: WakeEngine, pcm: bytes) -> tuple[tuple[float, ...], int | None]:
    hit_scores: list[float] = []
    latency: int | None = None
    for offset in range(0, len(pcm), FRAME_BYTES):
        hit = engine.feed(pcm[offset : offset + FRAME_BYTES])
        if hit is not None:
            hit_scores.append(hit.score)
            latency = hit.end_offset_ms if latency is None else min(latency, hit.end_offset_ms)
    return tuple(hit_scores), latency


def _negative_source(path: Path) -> str:
    if path.parent.name == "quacks":
        return "duck sound bank"
    if path.stem.startswith("context_"):
        return "context TTS"
    if path.stem.startswith("fleurs_"):
        return "FLEURS"
    if path.stem.startswith("ls_"):
        return "LibriSpeech"
    return path.parent.name


def _clips(
    directory: Path,
    category: str,
    engine: WakeEngine,
    positive_source: str = "synthetic TTS",
) -> list[Clip]:
    clips: list[Clip] = []
    for path in sorted(directory.rglob("*.wav")):
        pcm = load_pcm16_16k(path)
        scores, latency = _detect(engine, pcm)
        source = positive_source if category == "positive" else _negative_source(path)
        clips.append(
            Clip(path, category, source, _language(path), _duration(path), scores, latency)
        )
        engine.reset()
    return clips


def _metrics(clips: list[Clip], threshold: float) -> tuple[dict[str, tuple[int, int]], int, float]:
    positives = [clip for clip in clips if clip.category == "positive"]
    negatives = [clip for clip in clips if clip.category == "negative"]
    by_lang: dict[str, tuple[int, int]] = {}
    for lang in ("fr", "en"):
        matching = [clip for clip in positives if clip.lang == lang]
        by_lang[lang] = (
            sum(any(score >= threshold for score in clip.scores) for clip in matching),
            len(matching),
        )
    false_activations = sum(score >= threshold for clip in negatives for score in clip.scores)
    negative_hours = sum(clip.duration_s for clip in negatives) / 3600
    return by_lang, false_activations, negative_hours


def _source(clip: Clip) -> str:
    return clip.source


def _write_report(
    path: Path,
    clips: list[Clip],
    thresholds: list[float],
    engine_name: str,
    positive_source: str,
    provenance: str,
) -> float | None:
    positive_counts = {
        lang: sum(clip.category == "positive" and clip.lang == lang for clip in clips)
        for lang in ("fr", "en")
    }
    positive_label = (
        "user-declared synthetic" if positive_source == "synthetic" else "user-attested human"
    )
    lines = [
        "# Wake-word evaluation",
        "",
        f"- Engine: `{engine_name}`",
        f"- Positives: FR={positive_counts['fr']}, EN={positive_counts['en']} "
        f"{positive_label} WAVs.",
        f"- Positive provenance: {provenance}",
        "- File counts do not verify speaker identity, phrase, distance or H-POSITIFS completion.",
        "- Negative source names are inferred from filenames, not verified automatically; "
        "see the documented acquisition procedure for FLEURS/LibriSpeech and duck/TTS sources.",
        "",
        "| Threshold | FR recall | EN recall | FA / h (all) | FA / h (public + quacks) | "
        "Median detection offset |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    if positive_source == "recorded":
        counts = {
            f"{lang}/{distance}": sum(
                clip.category == "positive"
                and clip.lang == lang
                and clip.path.name.startswith(f"{lang}_{distance}_")
                for clip in clips
            )
            for lang in ("fr", "en")
            for distance in ("near", "far")
        }
        coverage = ", ".join(f"{key}={value}/20" for key, value in counts.items())
        lines.insert(
            7,
            f"- H-POSITIFS filename coverage only: {coverage}. "
            "This is not verified human acceptance; inspect/listen to every WAV.",
        )
    measured: list[tuple[float, float, int, float]] = []
    compromise: list[tuple[float, float, int, float]] = []
    has_both_languages = all(
        any(clip.category == "positive" and clip.lang == lang for clip in clips)
        for lang in ("fr", "en")
    )
    for threshold in thresholds:
        recalls, false_activations, hours = _metrics(clips, threshold)
        false_per_hour = false_activations / hours if hours else float("inf")
        public_negatives = [
            clip for clip in clips if clip.category == "negative" and _source(clip) != "context TTS"
        ]
        public_hours = sum(clip.duration_s for clip in public_negatives) / 3600
        public_hits = sum(score >= threshold for clip in public_negatives for score in clip.scores)
        public_false_per_hour = public_hits / public_hours if public_hours else float("inf")
        total_positive = sum(denominator for _, denominator in recalls.values())
        correct = sum(numerator for numerator, _ in recalls.values())
        recall = correct / total_positive if total_positive else 0
        eligible = (
            false_per_hour <= 1
            and hours >= 1
            and all(denominator for _, denominator in recalls.values())
        )
        fr_recall = f"{recalls['fr'][0] / recalls['fr'][1]:.1%}" if recalls["fr"][1] else "n/a"
        en_recall = f"{recalls['en'][0] / recalls['en'][1]:.1%}" if recalls["en"][1] else "n/a"
        median_values = sorted(
            clip.latency_ms
            for clip in clips
            if clip.category == "positive"
            and any(score >= threshold for score in clip.scores)
            and clip.latency_ms is not None
        )
        median = median_values[len(median_values) // 2] if median_values else None
        lines.append(
            f"| {threshold:.2f} | {recalls['fr'][0]}/{recalls['fr'][1]} ({fr_recall}) | "
            f"{recalls['en'][0]}/{recalls['en'][1]} ({en_recall}) | "
            f"{false_per_hour:.2f} | {public_false_per_hour:.2f} | "
            f"{median if median is not None else 'n/a'} ms |"
        )
        if eligible:
            measured.append((recall, -false_per_hour, threshold, median or 0))
        penalty = min(false_per_hour, 10) if hours else 10
        compromise.append((recall - penalty / 10, -false_per_hour, threshold, median or 0))
    selected = max(measured)[2] if measured else None
    selection_note = "highest combined recall among thresholds at or below 1 false activation/hour."
    if (
        selected is None
        and has_both_languages
        and sum(clip.duration_s for clip in clips if clip.category == "negative") >= 3600
    ):
        selected = max(compromise)[2]
        selection_note = (
            "best measured recall/false-activation trade-off; the 1 false activation/hour "
            "target was not reached."
        )
    lines.extend(
        [
            "",
            (
                f"Selected Vosk threshold: `{selected:.2f}` ({selection_note})"
                if selected is not None
                else "No WAKE_THRESHOLD is selected: the corpus must contain at least one "
                "hour of negatives, positives in both languages, and a passing threshold."
            ),
            "",
            "Detection offset is measured from the beginning of each WAV. Recordings must start "
            "immediately before the wake phrase to make this a meaningful approximation of "
            "wake latency.",
            "",
            "The all-negative rate includes contextual TTS containing the exact wake phrase; "
            "the adjacent public+quack column separates it from ordinary speech and duck sounds. "
            "A threshold passing only public audio does not erase contextual false activations.",
            "",
            "## Corpus",
            "",
            "| Split | Source | Files | Duration (h) | Wake hits at selected threshold |",
            "|---|---|---:|---:|---:|",
        ]
    )
    groups: dict[tuple[str, str, str], list[Clip]] = {}
    for clip in clips:
        groups.setdefault((clip.category, _source(clip), clip.lang or "n/a"), []).append(clip)
    for (category, source, language), grouped in sorted(groups.items()):
        duration_hours = sum(clip.duration_s for clip in grouped) / 3600
        if selected is None:
            hit_count = "n/a"
        elif category == "positive":
            hit_count = str(
                sum(any(score >= selected for score in clip.scores) for clip in grouped)
            )
        else:
            hit_count = str(sum(score >= selected for clip in grouped for score in clip.scores))
        lines.append(
            f"| {category} | `{source}` ({language}) | {len(grouped)} | "
            f"{duration_hours:.3f} | {hit_count} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positives", type=Path, default=Path("assets/wake/positives"))
    parser.add_argument("--negatives", type=Path, default=Path("assets/wake/negatives"))
    parser.add_argument("--engine", choices=("vosk",), default="vosk")
    parser.add_argument("--threshold", type=float, help="Evaluate only this threshold")
    parser.add_argument(
        "--positive-source",
        choices=("synthetic", "recorded"),
        default="synthetic",
        help="Label the positive WAVs in the report",
    )
    parser.add_argument(
        "--attest-recorded", help=f"Exact required attestation: {RECORDED_ATTESTATION}"
    )
    parser.add_argument("--models", type=Path, default=Path("assets/models"))
    parser.add_argument("--report", type=Path, default=Path("docs/wake-eval.md"))
    args = parser.parse_args()
    positive_files = list(args.positives.rglob("*.wav")) if args.positives.is_dir() else []
    negative_files = list(args.negatives.rglob("*.wav")) if args.negatives.is_dir() else []
    if not positive_files:
        parser.error(f"No positive WAVs found under {args.positives}")
    try:
        provenance = validate_positive_source(
            args.positives, args.positive_source, args.attest_recorded
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not negative_files:
        parser.error(f"No negative WAVs found under {args.negatives}; see assets/wake/README.md")
    thresholds = (
        [args.threshold]
        if args.threshold is not None
        else [round(0.30 + index * 0.05, 2) for index in range(13)]
    )
    if any(not 0 <= threshold <= 1 for threshold in thresholds):
        parser.error("Threshold must be between 0 and 1")
    engine = VoskWakeEngine(threshold=0, model_root=args.models)
    positive_source = (
        "user-declared synthetic" if args.positive_source == "synthetic" else "user-attested human"
    )
    clips = _clips(args.positives, "positive", engine, positive_source=positive_source) + _clips(
        args.negatives, "negative", engine
    )
    selected = _write_report(
        args.report, clips, thresholds, args.engine, args.positive_source, provenance
    )
    if selected is None:
        print(f"Wrote {args.report}; no WAKE_THRESHOLD selected (negative corpus < 1 hour)")
    else:
        print(f"Wrote {args.report}; selected WAKE_THRESHOLD={selected:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
