# Coin-Coin — audio navigateur (S2)

Service vocal du MicroDuck Coin-Coin, développé séparément du code Pollen.
S0 fournit le socle asynchrone ; S2 ajoute le micro et le haut-parleur du
navigateur, deux WebSockets PCM16, le mode écho et le test de latence par tonalité.
Le service n'ouvre toujours aucun accès au robot, à OpenAI ou à OpenClaw.

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

Pyright installe son runtime Node localement si Node manque dans WSL.
Les tests ne lisent pas les vrais secrets de `.env` et ne contactent aucun
service externe. Les essais unitaires de `net_spike.sh` remplacent Docker et
la découverte `eth0` par des faux ; ils ne prouvent pas le chemin Docker réel.
Ils utilisent uniquement le port réservé 7899 et vérifient le nettoyage.

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
Modèle et voix Realtime restent vides jusqu'à S4 ; le seuil de réveil reste
`None` jusqu'aux mesures S3. Les trois identifiants sensibles utilisent
`SecretStr`. Le journal masque récursivement les clés sensibles, les motifs
de clé API et les en-têtes Authorization ; il refuse les données non JSON
et laisse remonter les erreurs d'écriture.