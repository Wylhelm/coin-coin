# Wake-word evaluation

- Engine: `vosk`
- Positives: FR=40, EN=40 synthetic TTS WAVs.
- Synthetic voices use Windows System.Speech and are only a smoke-test corpus; they are not user's recordings.
- Public negatives: FLEURS French (CC BY 4.0) and LibriSpeech test-clean English (CC BY 4.0), plus the read-only duck sound bank and contextual TTS.

| Threshold | FR recall | EN recall | FA / h (all) | FA / h (public + quacks) | Median detection offset |
|---:|---:|---:|---:|---:|---:|
| 0.30 | 15/40 (37.5%) | 21/40 (52.5%) | 33.77 | 11.78 | 1600 ms |
| 0.35 | 15/40 (37.5%) | 21/40 (52.5%) | 33.77 | 11.78 | 1600 ms |
| 0.40 | 15/40 (37.5%) | 21/40 (52.5%) | 33.77 | 11.78 | 1600 ms |
| 0.45 | 15/40 (37.5%) | 21/40 (52.5%) | 33.77 | 11.78 | 1600 ms |
| 0.50 | 15/40 (37.5%) | 21/40 (52.5%) | 32.81 | 11.78 | 1600 ms |
| 0.55 | 15/40 (37.5%) | 21/40 (52.5%) | 29.91 | 9.82 | 1600 ms |
| 0.60 | 15/40 (37.5%) | 21/40 (52.5%) | 28.95 | 8.84 | 1600 ms |
| 0.65 | 15/40 (37.5%) | 21/40 (52.5%) | 27.98 | 7.86 | 1600 ms |
| 0.70 | 15/40 (37.5%) | 21/40 (52.5%) | 27.98 | 7.86 | 1600 ms |
| 0.75 | 15/40 (37.5%) | 21/40 (52.5%) | 26.05 | 6.87 | 1600 ms |
| 0.80 | 15/40 (37.5%) | 21/40 (52.5%) | 24.12 | 4.91 | 1600 ms |
| 0.85 | 15/40 (37.5%) | 21/40 (52.5%) | 23.16 | 3.93 | 1600 ms |
| 0.90 | 15/40 (37.5%) | 21/40 (52.5%) | 19.30 | 0.00 | 1600 ms |

Selected Vosk threshold: `0.90` (best measured recall/false-activation trade-off; the 1 false activation/hour target was not reached.)

Detection offset is measured from the beginning of each WAV. Recordings must start immediately before the wake phrase to make this a meaningful approximation of wake latency.

The all-negative rate includes contextual TTS containing the exact wake phrase; the adjacent public+quack column separates it from ordinary speech and duck sounds. A threshold passing only public audio does not erase contextual false activations.

## Corpus

| Split | Source | Files | Duration (h) | Wake hits at selected threshold |
|---|---|---:|---:|---:|
| negative | `FLEURS` (fr) | 169 | 0.501 | 0 |
| negative | `LibriSpeech` (en) | 199 | 0.501 | 0 |
| negative | `context TTS` (en) | 10 | 0.008 | 10 |
| negative | `context TTS` (fr) | 10 | 0.010 | 10 |
| negative | `duck sound bank` (n/a) | 82 | 0.016 | 0 |
| positive | `synthetic TTS` (en) | 40 | 0.016 | 21 |
| positive | `synthetic TTS` (fr) | 40 | 0.017 | 15 |
