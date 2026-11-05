"""Record near/far French and English wake phrases as local, untracked WAV files."""

import argparse
import subprocess
import wave
from pathlib import Path

TARGET_RATE = 16_000
PHRASES = {"fr": "« Coin-Coin »", "en": "“Coin-Coin”"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("assets/wake/positives"))
    parser.add_argument(
        "--count", type=int, default=20, help="Recordings per language and distance"
    )
    parser.add_argument("--seconds", type=float, default=2.5)
    parser.add_argument("--device", default="default", help="PulseAudio source name")
    args = parser.parse_args()
    if args.count < 1 or args.seconds <= 0:
        parser.error("--count and --seconds must be positive")
    device = args.device
    print(f"Recording from PulseAudio source {device!r} at {TARGET_RATE} Hz")
    for language, phrase in PHRASES.items():
        for distance in ("near", "far"):
            directory = args.out / language
            directory.mkdir(parents=True, exist_ok=True)
            for index in range(1, args.count + 1):
                input(
                    f"{language.upper()} {distance} {index}/{args.count}: "
                    f"say {phrase}; position yourself at the requested distance, "
                    "then press Enter to record"
                )
                output = directory / f"{language}_{distance}_{index:02}.wav"
                output.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostdin",
                        "-f",
                        "pulse",
                        "-i",
                        device,
                        "-t",
                        str(args.seconds),
                        "-ac",
                        "1",
                        "-ar",
                        str(TARGET_RATE),
                        "-c:a",
                        "pcm_s16le",
                        "-y",
                        str(output),
                    ],
                    check=True,
                )
                with wave.open(str(output), "rb") as recorded:
                    if recorded.getnchannels() != 1 or recorded.getframerate() != TARGET_RATE:
                        raise RuntimeError(f"Unexpected recording format: {output}")
                print(f"Saved {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
