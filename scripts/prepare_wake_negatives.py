"""Convert public FLEURS French and LibriSpeech English audio to WAVs."""

import argparse
import subprocess
import wave
from pathlib import Path

TARGET_SECONDS_PER_LANGUAGE = 1800
SAMPLE_RATE = 16_000


def _duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _convert(source: Path, destination: Path) -> float:
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        check=True,
        capture_output=True,
    )
    with wave.open(str(destination), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def _fleurs(parquet_path: Path, output: Path, limit_s: float) -> float:
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError("Run with `uv run --with pyarrow` to read FLEURS parquet.") from exc
    total = 0.0
    audio_rows = parquet.read_table(parquet_path, columns=["audio"])["audio"].to_pylist()
    for index, row in enumerate(audio_rows):
        if not isinstance(row, dict) or not isinstance(row.get("bytes"), bytes):
            continue
        path = row.get("path")
        suffix = Path(path).suffix if isinstance(path, str) else ".wav"
        temporary = output / f".fleurs_{index}{suffix}"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(row["bytes"])
        destination = output / f"fleurs_{index:05}.wav"
        try:
            duration = _convert(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        total += duration
        if total >= limit_s:
            break
    if total < limit_s:
        raise RuntimeError(f"FLEURS French subset was only {total / 60:.1f} minutes")
    return total


def _librispeech(source_dir: Path, output: Path, limit_s: float) -> float:
    total = 0.0
    for source in sorted(source_dir.rglob("*.flac")):
        duration = _convert(source, output / f"ls_{source.stem}.wav")
        total += duration
        if total >= limit_s:
            break
    if total < limit_s:
        raise RuntimeError(f"LibriSpeech English subset was only {total / 60:.1f} minutes")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", type=Path, default=Path("assets/wake/downloads"))
    parser.add_argument("--output", type=Path, default=Path("assets/wake/negatives"))
    args = parser.parse_args()
    fleurs = args.downloads / "fleurs-fr-test.parquet"
    if not fleurs.is_file():
        parser.error(f"FLEURS French parquet missing: {fleurs}")
    librispeech = next(
        (path for path in args.downloads.rglob("test-clean") if path.is_dir()),
        None,
    )
    if librispeech is None:
        parser.error(f"No LibriSpeech test-clean directory found under {args.downloads}")
    fr_seconds = _fleurs(fleurs, args.output / "fr", TARGET_SECONDS_PER_LANGUAGE)
    en_seconds = _librispeech(librispeech, args.output / "en", TARGET_SECONDS_PER_LANGUAGE)
    print(
        f"Prepared licensed negatives: fr={fr_seconds / 60:.1f} min, en={en_seconds / 60:.1f} min"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
