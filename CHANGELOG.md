# Changelog

## S2 — 2026-11-04

- Ajout de `BrowserAudio` : micro et lecture PCM16 sur `/ws/audio`, contrôles
  et acquittements sur `/ws/ui`, tampon de lecture limité à 300 ms, flush et
  remplacement de page.
- Ajout de la page navigateur, des AudioWorklets (trames 20 ms, précharge 60 ms,
  compte rendu de lecture et rééchantillonnage selon le taux réel du contexte),
  ainsi que des modes `coincoin --echo` et `coincoin --tone`.
- Ajout du rééchantillonnage SoXR, du mixeur sans délai et du mesureur de latence
  par corrélation ; documentation ADR-002 et procédure AEC manuelle.
- Validation : 55 tests réussis, couverture 93,82 %, ruff et pyright verts.
  Le test humain d'écho/AEC reste à faire par user.

## 0.2.0 — 2026-11-04

- S3 : machine d'états pure, porte audio par état, VAD Silero ONNX avec pré-roll
  et maintien, rééchantillonnage PCM16 et moteurs de réveil Vosk FR/EN.
- Banc hors ligne `scripts/eval_wake.py`, enregistrement via PulseAudio/ffmpeg,
  obtention des modèles et corpus publics documentés ; données audio et modèles
  ignorés par Git.
- Valeurs de mesure, choix du seuil et limites documentés dans
  `docs/wake-eval.md` et l'ADR-003 ; modèles montés en lecture seule dans Compose.
- Correction Silero v5.1.2 : contexte de 64 échantillons, état récurrent et
  contexte réinitialisés ensemble, preuve ONNX réelle parole/silence (±100 ms).
- Lectures voix/cancanement suivies séparément pour conserver le barge-in et
  le délai de réveil quand les deux cycles se chevauchent.
- Rebase S3 sur S2 : API et tests audio conservés, SoXR partagé, montage des
  logs et dépendances des deux sessions préservés ; reprise depuis le repo
  principal et conservation des corpus ignorés documentées.

## 0.1.0 — 2026-11-04

- S0 : repo Git indépendant sur `session/S0`, Python 3.12, `uv.lock`,
  ruff, pyright basique et pytest avec tests asynchrones.
- Interfaces figées `Clock`, `AudioIO`, `RobotLink`, `QuackSink`, `Brain` ;
  `FakeClock`, exceptions robot, `Frame` immuable et `NullBrain` explicite.
- Configuration complète de la spec maître, secrets en `SecretStr`,
  journal JSONL UTC avec masquage récursif, rotation quotidienne et purge.
- Page « Coin-Coin — hello », `/healthz`, image non root avec HEALTHCHECK
  et Compose avec port paramétrable et volume de logs.
- Spike réseau rejouable sur le seul port 7899, plan B authentifié,
  nettoyage automatique et tests unitaires avec commandes Docker/IP factices.
- `/healthz` validé depuis WSL et Windows (HTTP 200), conteneur `healthy`
  en UID/GID 1000 ; tous les services S0 sont arrêtés à la livraison.
- Chemin réseau réel B validé sur `eth0` WSL avec jeton obligatoire :
  plan A injoignable, rejet sans jeton puis écho authentifié exact.
- Défaut initial du helper Docker Windows résolu pour les essais par
  préchargement des seules images publiques depuis Windows, sans modifier
  la configuration Docker/WSL ni utiliser de configuration Docker alternative.
- Commande qualité complète verte : 39 tests, couverture 100 %, aucune erreur
  ruff/pyright ; audit Git sans valeur de clé API ni fichier de secrets suivi.
