# Changelog

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
