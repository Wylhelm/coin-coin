"""Record near/far French and English wake phrases as local, untracked WAV files."""

import argparse
import json
import math
import os
import subprocess
import tempfile
import wave
from pathlib import Path

TARGET_RATE = 16_000
PHRASES = {"fr": "« Coin-Coin »", "en": "“Coin-Coin”"}


def validate_recording(path: Path, seconds: float) -> None:
    with wave.open(str(path), "rb") as recorded:
        if (
            recorded.getnchannels() != 1
            or recorded.getframerate() != TARGET_RATE
            or recorded.getsampwidth() != 2
            or recorded.getcomptype() != "NONE"
            or abs(len(recorded.readframes(recorded.getnframes())) / 2 / TARGET_RATE - seconds)
            > 0.100
        ):
            raise ValueError(f"Incomplete or unexpected recording: {path}")


def record_session(out: Path, count: int, seconds: float, device: str, *, resume: bool) -> None:
    for synthetic in (Path("assets/wake/positives"), Path("assets/wake/vad")):
        if out.resolve().is_relative_to(synthetic.resolve()):
            raise ValueError("Recording output must be separate from synthetic corpus directories")
    plan = {"version": 1, "count_per_language_distance": count, "seconds": seconds}
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "recording-plan.json"
    slots = [
        (language, distance, index, out / language / f"{language}_{distance}_{index:02}.wav")
        for language in PHRASES
        for distance in ("near", "far")
        for index in range(1, count + 1)
    ]
    if resume:
        if not manifest.is_file() or json.loads(manifest.read_text()) != plan:
            raise ValueError("Resume requires an existing matching recording-plan.json")
    else:
        if manifest.exists() or any(out.rglob("*.wav")):
            raise FileExistsError(
                "Output already contains a session or WAVs; use --resume or new DIR"
            )
        with manifest.open("x", encoding="utf-8") as target:
            json.dump(plan, target)
    expected = {path for _, _, _, path in slots}
    if any(path not in expected for path in out.rglob("*.wav")):
        raise ValueError(
            "Unexpected WAVs in session; preserve them and use a separate output directory"
        )
    for _, _, _, path in slots:
        if path.exists():
            validate_recording(path, seconds)
    partials = list(out.glob("*.partial"))
    if partials:
        print(f"Preserving {len(partials)} interrupted temporary recordings in {out}")
    print(f"Recording from PulseAudio source {device!r} at {TARGET_RATE} Hz")
    for language, distance, index, output in slots:
        if output.exists():
            print(f"Resume: preserving existing WAV {output}; content is not verified")
            continue
        input(
            f"{language.upper()} {distance} {index}/{count}: say {PHRASES[language]}; "
            "position yourself, then press Enter to record"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="capture-", suffix=".partial", dir=out)
        os.close(fd)
        partial = Path(temporary)
        # ffmpeg writes only our unique temporary file; publication never replaces a WAV.
        with partial.open("wb") as target:
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
                    str(seconds),
                    "-ac",
                    "1",
                    "-ar",
                    str(TARGET_RATE),
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "wav",
                    "pipe:1",
                ],
                stdout=target,
                check=True,
            )
        validate_recording(partial, seconds)
        # A WAV on stdout has an unseekable header; persist its actual frame count.
        with wave.open(str(partial), "rb") as source:
            pcm = source.readframes(source.getnframes())
        with wave.open(str(partial), "wb") as target:
            target.setparams((1, 2, TARGET_RATE, 0, "NONE", "not compressed"))
            target.writeframes(pcm)
        os.link(partial, output)
        partial.unlink()
        print(f"Saved {output}")
    print(f"All {len(slots)} planned WAV slots present; speaker, phrase and distance not verified.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("assets/wake/recorded/session-01"))
    parser.add_argument("--resume", action="store_true", help="Keep valid existing session WAVs")
    parser.add_argument(
        "--count", type=int, default=20, help="Recordings per language and distance"
    )
    parser.add_argument("--seconds", type=float, default=2.5)
    parser.add_argument("--device", default="default", help="PulseAudio source name")
    args = parser.parse_args()
    if args.count < 1 or not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--count and --seconds must be positive")
    record_session(args.out, args.count, args.seconds, args.device, resume=args.resume)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
