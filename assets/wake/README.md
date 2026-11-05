# Wake-word assets

Model files and recordings in this directory are local-only and Git-ignored.
Run `scripts/download_wake_models.sh` from the repository root to obtain the
small French and US-English Vosk models and Silero's ONNX VAD model. The script
downloads the published Vosk model archives and the pinned Silero VAD v5.1.2
ONNX file; it does not install or start any simulator.

Generate a **synthetic-only smoke corpus** with the Windows voices available on
user's PC:

```powershell
.\scripts\synthesize_wake_samples.ps1
```

These are not real positives and do not satisfy the [user] acceptance
criterion. Once available, record 20 examples in each language at both near and
far distances from WSL:

```sh
uv run python scripts/record_positives.py --count 20 --seconds 2.5
```

The script records from the WSL PulseAudio `default` source with ffmpeg and
writes 16 kHz mono PCM16 files under `assets/wake/positives/{fr,en}`. Pass
`--device SOURCE_NAME` to use another PulseAudio source.
Use 20 natural repetitions per language at approximately 0.5 m and 2–3 m; include
both « Coin-Coin » and « Hey Coin-Coin » in the recordings.
`--count 20` means 20 examples at each language/distance combination (80 WAVs).
The recorded filenames replace the synthetic files with matching names. After
recording, label the positives correctly in the report:

```sh
uv run python scripts/eval_wake.py --positive-source recorded
```

Without real recordings, the default `--positive-source synthetic` labels the
Windows TTS smoke corpus.

For negatives, place at least one hour of licensed French and English speech or
radio clips as mono 16 kHz PCM16 WAV under `assets/wake/negatives/fr` and
`assets/wake/negatives/en`. Obtain FLEURS French (CC BY 4.0) and LibriSpeech
English (CC BY 4.0) with:

```sh
scripts/download_wake_negatives.sh
```

The script selects 30 minutes per language and converts it to the required WAV
format. It downloads source data under the ignored `assets/wake/downloads/`.
FLEURS citation: Conneau et al., *FLEURS: Few-shot Learning Evaluation of
Multilingual Speech*, 2022.
Convert copies of the duck's existing quack WAVs (read-only source) with
`uv run python scripts/import_quacks.py`. The synthetic Windows TTS helper also
generates non-wake sentences where « coin-coin » occurs in context. Keep
source/license notes locally; neither source audio nor conversions belong in
Git. For other media use
`ffmpeg -i INPUT -ac 1 -ar 16000 -c:a pcm_s16le OUTPUT.wav`.

Run `uv run python scripts/eval_wake.py`. The threshold sweep report includes
recall by language, false activations per negative-hour, and detection offset
from each WAV's start. The evaluation rejects missing corpora rather than
reporting a false pass.
