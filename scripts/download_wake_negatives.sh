#!/usr/bin/env bash
set -euo pipefail

downloads="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/assets/wake/downloads"
mkdir -p "$downloads"
librispeech="test-clean.tar.gz"
fleurs="fleurs-fr-test.parquet"

if [[ ! -f "$downloads/$fleurs" ]]; then
    curl --fail --location --retry 3 \
        https://huggingface.co/datasets/google/fleurs/resolve/main/parquet-data/fr_fr/test-00000-of-00001.parquet \
        --output "$downloads/$fleurs"
fi

if [[ ! -f "$downloads/$librispeech" ]]; then
    curl --fail --location --retry 3 \
        https://www.openslr.org/resources/12/test-clean.tar.gz \
        --output "$downloads/$librispeech"
fi
if [[ ! -d "$downloads/LibriSpeech/test-clean" ]]; then
    tar -xzf "$downloads/$librispeech" -C "$downloads"
fi

printf 'Downloaded source corpora under %s (ignored by Git).\n' "$downloads"
printf 'Preparing one half-hour subset per language; this may take several minutes.\n'
uv run --with pyarrow python scripts/prepare_wake_negatives.py --downloads "$downloads"
