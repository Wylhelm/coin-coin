#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/assets/models"
mkdir -p "$root"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

download_model() {
    local archive="$1"
    local directory="$2"
    local url="$3"
    if [[ -d "$root/$directory" ]]; then
        printf 'Already present: %s\n' "$root/$directory"
        return
    fi
    curl --fail --location --retry 3 "$url" --output "$tmp/$archive"
    python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
        "$tmp/$archive" "$tmp"
    mv "$tmp/$directory"* "$root/$directory"
}

download_model vosk-fr.zip vosk-model-small-fr \
    https://alphacephei.com/vosk/models/vosk-model-small-fr-0.22.zip
download_model vosk-en.zip vosk-model-small-en-us \
    https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
curl --fail --location --retry 3 \
    https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx \
    --output "$root/silero_vad.onnx"
printf 'Models installed under %s (ignored by Git).\n' "$root"
