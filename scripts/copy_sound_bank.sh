#!/usr/bin/env bash
set -euo pipefail
source_dir="${1:-${DUCK_SIM_STATE:-$HOME/.cache/duck-sim}/sounds/duck-a}"
target_dir="$(cd "$(dirname "$0")/.." && pwd)/assets/sounds"
if [[ ! -d "$source_dir" ]]; then
    printf 'Sound bank not found: %s\n' "$source_dir" >&2
    exit 1
fi
mkdir -p "$target_dir"
cp -R "$source_dir/." "$target_dir/"
printf 'Copied sound bank to %s\n' "$target_dir"
