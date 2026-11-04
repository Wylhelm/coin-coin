# Coin-Coin — socle S0

Service vocal du MicroDuck Coin-Coin, développé séparément du code Pollen.
**S0 livre uniquement le socle** : configuration, journal JSONL, horloges,
interfaces communes et une page HTTP « hello ». Aucun accès au robot, au micro,
à OpenAI ou à OpenClaw n'est ouvert.

## Prérequis

- WSL2 Ubuntu, Python 3.12 et `uv`.
- Docker Desktop avec son intégration WSL2 et `docker compose` disponible depuis WSL.
- Chrome ou Edge sous Windows pour la future interface vocale.
- Pour les logs montés : un dossier `logs/` accessible à l'utilisateur du conteneur
  (UID/GID 1000). S0 n'utilise aucun secret.

## Lancer S0 depuis WSL

Depuis `~/projects/microduck/coin-coin/` :

```sh
test -e .env || cp .env.example .env
mkdir -p logs
export COINCOIN_PORT=8770 COMPOSE_PROJECT_NAME=coincoin-s0
docker compose up --build --detach --wait --wait-timeout 60
curl --fail http://localhost:8770/healthz
```

Ouvrir `http://localhost:8770/` sous Windows. Le JSON attendu sur `/healthz` est
`{"status": "ok"}`. Le conteneur écoute en interne sur `0.0.0.0:8765`, mais
Compose publie seulement sur le loopback du PC. Ne pas utiliser le port live
8765 de user pour les essais d'agent.

Depuis PowerShell Windows :

```powershell
(Invoke-WebRequest http://localhost:8770/healthz -UseBasicParsing).Content
```

Arrêter uniquement le projet S0 :

```sh
COINCOIN_PORT=8770 COMPOSE_PROJECT_NAME=coincoin-s0 docker compose down
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

## Développement et qualité

```sh
export UV_CACHE_DIR="$PWD/.cache/uv"
export PYRIGHT_PYTHON_CACHE_DIR="$PWD/.cache/pyright"
uv sync --locked
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

Résultat S0 : commande complète verte, 39 tests réussis, couverture des
implémentations livrées de 100 %, aucun diagnostic pyright.

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
  robot/link.py      RobotLink, Frame, RobotRefused, LinkDown
  robot/quack.py     QuackSink
  brain/base.py      Brain, NullBrain sans mémoire ni réseau
  web/server.py      / et /healthz
  web/static/        page hello
tests/unit/          configuration, horloges, journal, contrats, HTTP, spike avec faux
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