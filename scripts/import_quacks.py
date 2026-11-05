"""Convert the existing duck-sim quack bank into local wake negatives."""

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=Path.home() / ".cache/duck-sim/sounds/duck-a"
    )
    parser.add_argument("--output", type=Path, default=Path("assets/wake/negatives/quacks"))
    args = parser.parse_args()
    if not args.source.is_dir():
        parser.error(f"Sound bank does not exist: {args.source}")
    sources = sorted(args.source.rglob("*.wav"))
    if not sources:
        parser.error(f"No WAV files found under {args.source}")
    args.output.mkdir(parents=True, exist_ok=True)
    for source in sources:
        tag = source.parent.name
        destination = args.output / f"{tag}_{source.stem}.wav"
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
                "16000",
                "-c:a",
                "pcm_s16le",
                str(destination),
            ],
            check=True,
        )
    print(f"Converted {len(sources)} WAVs into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
