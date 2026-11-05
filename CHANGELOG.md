# Changelog

## Correctif S3 H-POSITIFS — 2026-11-04

- Sessions humaines isolées sous `assets/wake/recorded/`, capture temporaire et
  publication sans écrasement ; reprise explicite conservant les WAV complets
  et les temporaires interrompus.
- Attestation exacte exigée pour les imports humains ; provenance déclarée,
  identité et complétude non vérifiées. Refus des copies exactes des TTS locaux.
- Corpus `assets/wake/` exclus du contexte Docker et sessions humaines ignorées
  par Git. Données existantes conservées, aucune capture micro lancée.
- Validation après rebase sur `19bd4c3` : 329 tests réussis, 3 ignorés, dont 13 nouvelles
  régressions déterministes ; qualité complète verte.

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

## S1 — 2026-11-04

- Correction de priorité STOP sur `main` `143e015` : aucun verrou d'action
  ni réponse RPC lente ne retarde son envoi ; RPC d'action invalidés/annulés,
  générations vérifiées avant envoi et après réponse, aucune reprise tardive.
  20 nouveaux cas déterministes ; 314 tests Python réussis, 5 sauts documentés,
  couverture totale 93,04 % (gouverneur 96 %, relais 84 %), qualité verte.
  3 tests sim et 5 AudioWorklet passent ; STOP pendant un regard retardé et
  une marche réelle acquitté en moins de 200 ms (appel transport débuté en
  0,116 ms), consigne zéro, action retardée refusée.
- Rebase sur `main` `c1ee52d` : dépendances et procédures S2/S3 préservées,
  lock combiné régénéré, ADR robot renumérotées 004/005, procédure S1 adaptée
  au dépôt principal et au montage `.cache/logs/`.
- Revalidation combinée : 294 tests réussis, 5 sauts documentés, couverture
  92,95 %, qualité verte ; 3 tests AudioWorklet et 3 tests sim réels réussis
  (avance 0,154 m, tour 88,647°, regard 0,795 rad). Compose reconstruit healthy,
  CLI authentifiée vérifiée, toutes les ressources S1 arrêtées.
- Relais TCP/Unix stdlib autonome, refus par défaut par port, lignes bornées,
  authentification avant connexion Unix et arrêt propre SIGINT/SIGTERM.
- Couche `JsonRpcConnection` réutilisable en S9 : corrélation par identifiant,
  flux d'état sur la même connexion, délais et notifications bornées à 50 ms.
  `TcpLink` authentifié, reconnexion exponentielle avec gigue et callbacks.
- Client images déjà branché dans `TcpLink.frame()` : validation du header,
  lecture binaire exacte, conversion UYVY, rotation et JPEG qualité 80.
- Gouverneur : limites walk/roller et pose, commande à 20 Hz, mouvement
  remplaçable, surveillance 10 Hz, arrêt prioritaire, activation explicite et
  homing, tour en boucle fermée, regard maintenu, posture et skills dynamiques.
- `coincoin-cli` utilisable depuis le conteneur, avec avant/après odométriques,
  erreurs explicites et diagnostic `raw` filtré par le relais.
- Correction vérifiée contre `robotd` : `robot.stop` doit porter un `id` ;
  une notification ne déclenche pas l'arrêt. L'EMA de vitesse de `robotd`
  est prise en compte dans les observations, sans modifier Pollen.
- Qualité : 225 tests hors sim, couverture totale 93,99 %, gouverneur 95 %,
  relais 84 %, ruff/format/pyright verts ; faux robotd TCP/Unix et tests
  de reconnexion, backpressure, erreurs, binaire et signaux.
- Tests sim réels : avance 0,162 m en 2 s, tour 88,813°, regard 0,797 rad.
  Depuis Docker : avance 0,261 m pour une commande de 3 s ; `robot.relax`
  refusé −32601 sans chute ; deadman observé 0,496 s après arrêt du relais.
  Uniquement état/ports/projet Compose S1 ; tous les services arrêtés.

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
