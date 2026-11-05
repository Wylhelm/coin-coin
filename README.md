# Coin-Coin — audio navigateur et réveil local (S2/S3)

Service vocal du MicroDuck Coin-Coin, développé séparément du code Pollen.
S0 fournit le socle asynchrone ; S2 ajoute le micro et le haut-parleur du
navigateur, deux WebSockets PCM16, le mode écho et le test de latence par tonalité.
Le service n'ouvre toujours aucun accès au robot, à OpenAI ou à OpenClaw.
S3 ajoute le réveil local, le VAD, la porte audio et la machine d'états.
Les sessions Realtime et l'accès au robot restent livrés par d'autres sessions.

## Prérequis

- WSL2 Ubuntu, Python 3.12 et `uv`.
- Docker Desktop avec son intégration WSL2 et `docker compose` disponible depuis WSL.
- Chrome ou Edge sous Windows pour la future interface vocale.
- Le service écrit ses logs dans `.cache/logs/`, monté dans le conteneur et
  ignoré par Git. Aucun secret n'est requis pour les essais audio S2.

## Lancer l'audio depuis le dépôt principal

Cette procédure est utilisable après la fusion de S2, depuis le clone principal
`~/projects/microduck/coin-coin` (le worktree S2 est alors supprimé). Dans WSL :

```sh
cd ~/projects/microduck/coin-coin
test -e .env || cp .env.example .env
mkdir -p .cache/logs
COINCOIN_PORT=8772 COMPOSE_PROJECT_NAME=coincoin-s2 \
  docker compose run --build --rm --service-ports coin-coin coincoin --echo
```

Ouvrir `http://localhost:8772/` sous Windows, cliquer sur **Démarrer** et
autoriser le micro. `--echo` (ou `ECHO=1`) renvoie le PCM reçu sans traitement.
Remplacer `--echo` par `--tone` pour le bip de 1 kHz et la mesure de latence
aller-retour.
Le conteneur écoute en interne sur `0.0.0.0:8765` ; Compose publie uniquement
sur le loopback du PC. Arrêter le processus avec Ctrl+C, puis nettoyer uniquement
le projet S2 :

```sh
COINCOIN_PORT=8772 COMPOSE_PROJECT_NAME=coincoin-s2 docker compose down
```

**Mesures S0 du 2026-11-04** : `/healthz` retourne HTTP 200 et le JSON attendu
depuis WSL **et** Windows ; le conteneur est `healthy` et non root (UID/GID 1000).
Le chemin réseau retenu est **B : IP `eth0` WSL + jeton**, car le plan A
échoue avec `Network is unreachable`. Voir [le relevé réseau](docs/network.md)
et [ADR-001](docs/decisions.md).

Si un téléchargement depuis WSL échoue avec
`docker-credential-desktop.exe: exec format error`, précharger seulement les
deux images publiques depuis PowerShell Windows, puis reprendre les commandes
Compose **depuis WSL** :

```powershell
docker pull python:3.12-slim
docker pull ghcr.io/astral-sh/uv:0.13.7
```

Cela a suffi en S0 avec le même moteur Docker Desktop, sans changer la
configuration Docker/WSL ni utiliser un `DOCKER_CONFIG` alternatif.

## Vérifier l'audio

Le test ci-dessous demande environ cinq minutes et doit être effectué dans
Chrome ou Edge sous Windows avec les haut-parleurs du PC. Lancer d'abord le
mode écho selon la section précédente, retirer le casque et vérifier que
`http://localhost:8772/` est bien ouvert :

1. Parler en mode écho : on doit s'entendre avec un retard ressenti faible et
   sans larsen croissant.
2. Arrêter avec Ctrl+C, relancer la même commande en remplaçant `--echo` par
   `--tone`, puis noter la latence affichée et parler par-dessus le bip. Le
   vumètre doit réagir à la voix et très peu au bip si l'AEC est active.
3. Si le bip reste nettement visible au vumètre ou qu'un larsen apparaît,
   consigner l'échec dans ADR-002 ; l'alternative aiortc y est décrite mais
   n'est pas implémentée en S2.
4. Après le test, arrêter le conteneur avec Ctrl+C puis exécuter depuis le
   dépôt principal `~/projects/microduck/coin-coin` :

   ```sh
   cd ~/projects/microduck/coin-coin
   COINCOIN_PORT=8772 COMPOSE_PROJECT_NAME=coincoin-s2 docker compose down
   ```

Noter le résultat (latence et efficacité AEC) dans ADR-002 après le test live.

## Développement et qualité

```sh
export UV_CACHE_DIR="$PWD/.cache/uv"
export PYRIGHT_PYTHON_CACHE_DIR="$PWD/.cache/pyright"
uv sync --locked
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

Résultat S0 : commande complète verte, 39 tests réussis, couverture des
implémentations livrées de 100 %, aucun diagnostic pyright. Les résultats
courants de S2 et les preuves du harness AudioWorklet sont consignés dans
l'ADR-002. La mesure live de l'AEC reste en attente dans l'ADR-002.

Le harness du vrai AudioWorklet est un test séparé, à lancer dans un
environnement où Node.js est disponible :

```sh
node --test tests/js/play_worklet.test.mjs
```

Node.js n'est pas requis pour exécuter le service.

Après intégration et correction S3 : 108 tests réussis, dont deux tests Silero
ONNX réels avec les assets locaux, couverture totale 92,58 %, machine d'états
98 %, porte audio 96 %, aucun diagnostic ruff ou pyright. Les tests ONNX réels
sont ignorés en CI sans assets ; leur commande obligatoire figure dans
`assets/wake/README.md`.

Pyright installe son runtime Node localement si Node manque dans WSL.
Les tests ne lisent pas les vrais secrets de `.env` et ne contactent aucun
service externe. Les essais unitaires de `net_spike.sh` remplacent Docker et
la découverte `eth0` par des faux ; ils ne prouvent pas le chemin Docker réel.
Ils utilisent uniquement le port réservé 7899 et vérifient le nettoyage.

## Mot de réveil et machine d'états (S3)

Le réveil et le VAD sont locaux : `assets/wake/README.md` décrit le téléchargement
des modèles (Vosk FR/EN et Silero ONNX), la génération d'échantillons synthétiques,
l'enregistrement de positifs réels et la constitution d'un corpus négatif licencié.
Les modèles et corpus audio sont ignorés par Git. Après leur obtention :

```sh
cd ~/projects/microduck/coin-coin
scripts/download_wake_models.sh
scripts/download_wake_negatives.sh
uv run python scripts/import_quacks.py
uv run python scripts/record_positives.py --count 20 --seconds 2.5
uv run python scripts/eval_wake.py --positive-source recorded
```

Le rapport `docs/wake-eval.md` contient les mesures ; `WAKE_THRESHOLD=0.9` est
un meilleur compromis provisoire, pas un seuil validé (le rappel synthétique et
les faux réveils en contexte n'atteignent pas la cible). Les positifs synthétiques
ne remplacent pas les enregistrements de user.
`VoiceActivityDetector` consomme du PCM16 mono à 16 kHz par fenêtres de 512
nouveaux échantillons, avec les 64 échantillons de contexte requis par Silero
v5.1.2 (entrée ONNX de 576), et réinitialise contexte et état récurrent ensemble.
Il produit le pré-roll/la fin de parole avec maintien. La fonction
pure `transition()` pilote les états sans effectuer d'I/O ; `AudioGate` garantit
qu'aucun octet n'est transmis en ENDORMI et tient le compteur par état.
Les drapeaux `response_active` et `quack_active` conservent les deux lectures
concurrentes ; `speaking` reste actif jusqu'à leur fin ou au barge-in, et le
réveil reste coupé jusqu'à 500 ms après la dernière fin.

Le test ONNX réel et les horodatages WAV sont rejouables selon
`assets/wake/README.md`. Avant tout retrait du worktree S3, préserver dans le
repo principal les dossiers ignorés `assets/models/`, `assets/wake/positives/`,
`assets/wake/negatives/`, `assets/wake/downloads/` et `assets/wake/vad/` :
ils ne sont pas transportés par une fusion Git. La procédure de copie sans
écrasement est donnée dans ce même document ; ne pas supprimer ces données.

Les futurs tests `@pytest.mark.sim` sont enregistrés mais S0 ne lance **aucune**
simulation. `.env`, logs, caches, modèles et sons sont ignorés par Git et par
le contexte Docker.

## Spike réseau

```sh
scripts/net_spike.sh
```

Le script compare l'écho octet par octet depuis un conteneur Python :
plan A via `host.docker.internal` vers `127.0.0.1:7899` WSL ; en cas d'échec,
plan B via l'IP `eth0`, avec jeton éphémère et rejet des connexions sans jeton.
Il n'écrit ni ne réutilise de secret réel. Le serveur d'écho et ses fichiers
temporaires sont supprimés à chaque sortie ; un processus préexistant n'est
jamais arrêté. Une image indisponible est signalée comme blocage, pas comme
échec réseau du plan A.

**Résultat réel S0 : B**, IP WSL mesurée `192.0.2.1`. Pour les sessions
robot suivantes, redécouvrir cette IP, utiliser leurs ports de relais réservés
et exiger un `RELAY_TOKEN` stocké uniquement dans leur `.env`. Les défauts
`host.docker.internal` de `Settings` et `.env.example` restent ceux de la spec
maître ; ils doivent être remplacés dans la configuration de chaque session
qui utilise effectivement le robot. S0 n'ouvre aucun lien robot.

## Contrats et structure

```text
src/coincoin/
  app.py, config.py, journal.py, clock.py
  audio/io.py        AudioIO : PCM16 mono 24 kHz, trames 20 ms
  audio/browser.py   BrowserAudio, WebSockets audio et contrôle
  audio/resample.py  conversions PCM16/float32 et SoXR 24→16 kHz
  audio/mixer.py     mixage voix/cancanements et écrêtage doux
  audio/tone.py      génération et corrélation du bip de test
  robot/link.py      RobotLink, Frame, RobotRefused, LinkDown
  robot/quack.py     QuackSink
  brain/base.py      Brain, NullBrain sans mémoire ni réseau
  web/server.py      /, /healthz, /ws/audio et /ws/ui
  web/static/        page navigateur et AudioWorklets micro/lecture
tests/unit/          configuration, audio PCM, mixage, horloges, journal, contrats
tests/integration/   WebSockets audio, écho, flux, flush et remplacement de page
scripts/net_spike.sh
docs/                décisions et preuves réseau
```

`Clock` utilise des secondes monotones ; `FakeClock.advance(s)` libère les
attentes échues par deadline, puis par ordre d'inscription.
`Frame.captured_at` représente au contraire une date Unix en secondes.
`RobotLink.state()` retourne l'état poussé en cache, jamais une méthode RPC ;
`state_age()` vaut l'infini avant le premier état.

La configuration reprend toutes les variables de la spec maître §4.2 j.
Modèle et voix Realtime restent vides jusqu'à S4 ; le seuil Vosk provisoire
sélectionné en S3 vaut `0.9`. Les trois identifiants sensibles utilisent
`SecretStr`. Le journal masque récursivement les clés sensibles, les motifs
de clé API et les en-têtes Authorization ; il refuse les données non JSON
et laisse remonter les erreurs d'écriture.