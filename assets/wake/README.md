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

## Preserve local assets before retiring S3

The durable checkout is `~/projects/microduck/coin-coin`, not the temporary S3
worktree. Before removing that worktree, preserve `assets/models/` and
`assets/wake/{positives,negatives,downloads,vad}/` in the durable checkout.
Git does not transfer these ignored directories. From WSL, copy without
overwriting any recordings or models already present at the destination:

```sh
cd ~/projects/microduck/coin-coin
mkdir -p assets/models assets/wake
cp -a --update=none ../coin-coin-wt/S3/assets/models/. assets/models/
for name in positives negatives downloads vad; do
    mkdir -p "assets/wake/$name"
    cp -a --update=none "../coin-coin-wt/S3/assets/wake/$name/." "assets/wake/$name/"
done
```

Verify the copied WAVs and models before retiring the worktree. Do not remove
the source assets as part of this procedure. Continue all recording/evaluation
commands from `~/projects/microduck/coin-coin` after integration. The real
recording command also needs `export PULSE_SERVER=unix:/mnt/wslg/PulseServer`
on this WSLg setup.

## Real Silero regression

`tests/integration/test_vad_real.py` runs the actual ignored Silero v5.1.2 ONNX
model, not an injected probability function. Its dedicated synthetic fixture
is `assets/wake/vad/positives/fr/fr_near_01.wav`, kept apart from user's
recordings. It is the first FR TTS positive used in the original measurement.
The preserved fixture has SHA-256
`6b2339ed5495e9a06f286da85c8099bb57cceae754292a1fb7eecd04f2744b2f`.
If it has not been preserved, regenerate the dedicated corpus from Windows
PowerShell in the durable checkout (same installed FR voice required):

```powershell
.\scripts\synthesize_wake_samples.ps1 -OutDir "$PWD\assets\wake\vad\positives" -Count 1
```

The model SHA-256 is
`2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f`.
From WSL in `~/projects/microduck/coin-coin`:

```sh
export PATH="$HOME/.local/bin:$PATH" UV_CACHE_DIR="$PWD/.cache/uv" \
    PYRIGHT_PYTHON_CACHE_DIR="$PWD/.cache/pyright"
COINCOIN_REQUIRE_VAD_ASSETS=1 uv run pytest tests/integration/test_vad_real.py -s --no-cov
```

The flag makes missing assets an error; ordinary CI skips these two tests if
the ignored files are absent. One test compares 51 windows with the official
64-context + 512-new-sample ONNX contract and checks exact replay after reset.
The other writes/reads a temporary PCM16 WAV containing one second of silence,
the fixture, and two seconds of silence. Its speech envelope is approximately
1.200–1.808 s after padding, independently of model output. At threshold 0.5,
the real model emits `speech_start` at 1.248 s and `speech_end` at 2.496 s
(expected end plus 700 ms hold: 2.508 s), both within 100 ms. Detector reset
reproduces these timestamps. These are real-model checks on synthetic speech,
not validation of user's real wake recordings.
