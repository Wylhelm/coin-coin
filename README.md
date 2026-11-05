# Coin-Coin — audio navigateur, réveil local et conversation Realtime (S1–S4)

Service vocal du MicroDuck Coin-Coin, développé séparément du code Pollen.
S0 fournit le socle asynchrone ; S2 ajoute le micro et le haut-parleur du
navigateur, deux WebSockets PCM16, le mode écho et le test de latence par tonalité.
S3 ajoute le réveil local, le VAD, la porte audio et la machine d'états.
**S1 livre le relais filtrant, `TcpLink`, le gouverneur et `coincoin-cli`**,
en plus du socle S0. S4 ajoute la conversation vocale bilingue par Realtime
WebSocket côté serveur. Les sessions ne s'ouvrent qu'après le réveil local.
Le serveur vocal n'établit aucune connexion robot ; les outils robot restent
réservés aux futures features.

## Piloter la simulation S1 depuis le conteneur

Ressources S1 exclusivement : état `~/.cache/ds-s1`, corps `7821`, images
`7921`, relais robot `7830`, relais images `7831`, HTTP `8771`, projet
Compose `coincoin-s1`. Ne pas utiliser les ressources live de user.

Après fusion, depuis WSL dans le dépôt principal (pendant le développement
S1, utiliser à la place `~/projects/microduck/coin-coin-wt/S1`) :

```sh
cd ~/projects/microduck/coin-coin
test -e .env || cp .env.example .env
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
export UV_CACHE_DIR="$PWD/.cache/uv"
export PYRIGHT_PYTHON_CACHE_DIR="$PWD/.cache/pyright"
export DUCK_SIM_STATE="$HOME/.cache/ds-s1"
export DUCK_SIM_PORT=7821 DUCK_SIM_FRAME_PORT=7921 DUCK_SIM_VIEWER=0
export DUCK_SIM_RL="$HOME/projects/microduck/microduck_rl"
mkdir -p .cache/logs
uv sync --locked
"$HOME/projects/microduck/microduck/scripts/duck-sim"
ip -4 -o addr show dev eth0
```

Le `.env` du worktree n'est pas transporté par Git. Dans le dépôt principal,
configurer les ports ci-dessous et un jeton partagé non vide. Si aucun jeton
n'y est configuré, le générer sans l'afficher :

```sh
uv run python - <<'PY'
from pathlib import Path
import secrets

path = Path(".env")
lines = path.read_text().splitlines()
if not any(line.startswith("RELAY_TOKEN=") and line.partition("=")[2].strip()
           for line in lines):
    lines = [line for line in lines if not line.startswith("RELAY_TOKEN=")]
    lines.append("RELAY_TOKEN=" + secrets.token_urlsafe(32))
    path.write_text("\n".join(lines) + "\n")
path.chmod(0o600)
PY
```

Redécouvrir l'IP `eth0` après chaque redémarrage WSL et adapter **les deux**
liens dans `.env`. Exemple non secret de la configuration mesurée :

```dotenv
ROBOT_LINK=tcp://192.0.2.1:7830
FRAME_LINK=tcp://192.0.2.1:7831
COINCOIN_PORT=8771
COMPOSE_PROJECT_NAME=coincoin-s1
```

`RELAY_TOKEN` doit être non vide et rester uniquement dans `.env`. Ne pas
l'afficher, le passer dans la ligne de commande ou le copier dans les logs.
Lancer le relais dans un deuxième terminal WSL, depuis le même dépôt :

```sh
set -a
source .env
set +a
python3 relay/duck_relay.py --host 192.0.2.1 \
  --robot-port 7830 --frame-port 7831 --state-dir "$HOME/.cache/ds-s1"
```

Dans un troisième terminal, depuis ce même dépôt :

```sh
docker compose up --build --detach --wait --wait-timeout 60
docker compose exec -T coin-coin coincoin-cli status
docker compose exec -T coin-coin coincoin-cli move 0.2 0 0 3
docker compose exec -T coin-coin coincoin-cli turn 90
docker compose exec -T coin-coin coincoin-cli look gauche
docker compose exec -T coin-coin coincoin-cli skills
docker compose exec -T coin-coin coincoin-cli stop
docker compose exec -T coin-coin coincoin-cli raw robot.relax
```

La dernière commande doit sortir avec le code **1** et l'erreur JSON-RPC
**−32601** : le canard reste debout. `raw` est un diagnostic manuel filtré
par le relais, **pas** un outil à exposer au modèle : il contourne le
gouverneur. Les autres commandes passent par le gouverneur. `--link URL`
remplace `ROBOT_LINK` sans modifier le jeton ni le lien images.

La CLI attend la fin de `move`/`turn` et du maintien de `look`, émet du JSON
et termine par un arrêt/bec fermé. `move` et `turn` ajoutent
`state_before`/`state_after`. L'EMA de `robotd` amortit encore la vitesse
après l'arrêt ; la CLI attend au plus 1 s que `applied` soit inférieur à
0,01 sur chaque axe, sans prolonger les commandes de mouvement.

`.cache/logs/` doit appartenir à UID/GID 1000 : le créer avant Compose évite que
Docker crée un dossier root. Si cela s'est déjà produit, réparer seulement
ce montage S1 :

```sh
docker compose run --rm --no-deps --user root coin-coin chown 1000:1000 /app/logs
```

Arrêt : `docker compose down`, Ctrl-C dans le terminal du relais, puis :

```sh
DUCK_SIM_STATE="$HOME/.cache/ds-s1" DUCK_SIM_PORT=7821 \
  "$HOME/projects/microduck/microduck/scripts/duck-sim" down
```

Le script Pollen vérifie aussi des unités systemd via un `sudo` interactif,
même pour la sim simple. Pour un arrêt non interactif sous le pseudo-TTY
de l'agent, sans demander de privilèges ni modifier Pollen :

```sh
setsid --wait timeout 45s env DUCK_SIM_STATE="$HOME/.cache/ds-s1" \
  DUCK_SIM_PORT=7821 DUCK_SIM_FRAME_PORT=7921 \
  "$HOME/projects/microduck/microduck/scripts/duck-sim" down </dev/null
```

Cela prive `sudo` de terminal de saisie ; ses vérifications privilégiées
échouent, puis les PID utilisateur de S1 sont arrêtés normalement. Ne jamais
tuer un processus par son nom. Tous les services S1 sont arrêtés à la livraison.

## Contrats robot pour S5 / S7 / S9

- `TcpLink(url, frame_url=..., token=..., clock=..., on_up=..., on_down=...)`
  implémente `RobotLink`. `connect()` fait `hello` (API 47), s'abonne à 10 Hz,
  ferme le bec et initialise la reconnexion avec gigue (0,5 à 30 s).
  `call()` conserve les erreurs `JsonRpcError` et `RobotRefused.reason` ;
  `notify()` échoue en `LinkDown` si l'envoi dépasse 50 ms, verrou inclus.
- `JsonRpcConnection(reader, writer, clock=...)` est indépendant de TCP :
  `readline()` côté lecteur, `write(bytes)`, `drain()`, `close()` et
  `wait_closed()` côté écrivain. Il corrèle les réponses et cache le flux
  `robot.state`. S9 peut réutiliser cette couche sans recopier la logique.
- Construire `Governor(link, settings, clock, journal)`, puis appeler
  `await governor.start()` après la connexion pour surveiller même au repos.
  Les actions le démarrent aussi automatiquement. `move()` et `turn()`
  rendent immédiatement un `ToolResult` ; `wait_motion()` sert à la CLI et
  aux tests. Les arrêts annulent aussi le maintien du regard.
- `stop(reason)` est prioritaire sur les préparations en cours. Les bornes
  walk/roller, l'état frais, la sécurité, l'activation par `on:true` et
  l'attente de `homed` sont vérifiés avant une action. Aucun `init`/`toggle`.
  Un changement de mode observé arrête le mouvement actif.
  STOP ne prend pas le verrou des actions : il invalide et annule leurs RPC
  en attente, puis envoie `robot.stop` avant d'attendre leur nettoyage ou de
  fermer le bec. Une réponse tardive ne peut pas réarmer une action antérieure.
- `frame()` utilise déjà `frames.fetch_frame()` : authentification sur la
  connexion images, en-tête ≤ 4096 octets, UYVY ≤ 16 Mio, rotation horaire et
  JPEG qualité 80. `FrameError` explicite si la caméra manque ; S7 ne doit
  pas modifier `tcp_link.py`. Aucune caméra n'est nécessaire aux tests S1.

**Important : `robot.stop` est une requête avec `id`, pas une notification.**
`robotd` ignore cette notification en API 47. Voir ADR-004 et les mesures
dans [docs/network.md](docs/network.md).

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

## Conversation Realtime S4

Cette procédure est destinée au worktree S4 et utilise uniquement ses
ressources : HTTP `8774`, projet Compose `coincoin-s4`. Elle ne démarre ni
simulation ni liaison robot. Le `.env` local doit contenir `OPENAI_API_KEY` ;
ne jamais l'afficher, la placer sur la ligne de commande ou la copier dans le
dépôt. Le modèle tracé est `gpt-realtime-2.1` et la voix initiale `marin` ;
le choix de voix reste à confirmer par écoute.

Depuis WSL, entrer explicitement dans le worktree S4 et lancer le service :

```sh
cd ~/projects/microduck/coin-coin-wt/S4
mkdir -p .cache/logs
COINCOIN_PORT=8774 COMPOSE_PROJECT_NAME=coincoin-s4 \
  docker compose up --build --wait --wait-timeout 60
```

Ouvrir `http://localhost:8774/` sous Windows, cliquer sur **Démarrer** et
autoriser le micro. Vérifier la réponse FR à « Coin-Coin, bonjour, qui es-tu ? »
et la réponse EN à « Hey Coin-Coin, who are you? ». Pendant une réponse longue,
parler pour vérifier le barge-in et l'arrêt de lecture ; après 7 minutes sans
parole, vérifier le retour en sommeil, l'état affiché et le journal. Les
transcriptions et événements d'usage sont conservés dans `.cache/logs/` ;
traiter ces fichiers locaux comme des données personnelles.

Arrêter uniquement les ressources attribuées à S4 :

```sh
cd ~/projects/microduck/coin-coin-wt/S4
COINCOIN_PORT=8774 COMPOSE_PROJECT_NAME=coincoin-s4 docker compose down
```

Les régressions S4 vérifient également STOP en sommeil et avec hooks/UI/annulation
lents, l'invalidation d'une ouverture périmée, l'ordre du tampon micro, la
troncature barge-in par item et par échantillon, le traitement prioritaire des
évènements durant un outil, la reconnexion après refus TCP local, le contexte
mémoire avant l'ouverture Realtime, l'état de parole UI et les compteurs d'audio
transmis sans conserver le PCM. La revalidation complète après correction
compte 366 tests réussis, 3 tests de simulation ignorés et 90,40 % de couverture ;
ruff, format et Pyright passent, ainsi que les 5 tests AudioWorklet Node. Les
tests automatisés ne remplacent pas les validations vocales humaines ci-dessus.

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

`BrowserAudio.flush()` applique une échéance totale de 100 ms, incluant
l'attente des verrous, l'envoi du contrôle UI et l'acquittement. Un timeout,
un échec d'envoi ou une annulation est signalé par exception. Si le flush n'est
pas acquitté, la lecture est considérée désynchronisée et les nouveaux appels
`play()` échouent jusqu'à un flush acquitté. Cette garantie côté serveur ne
peut pas assurer le silence physique d'un navigateur injoignable.

Le harness du vrai AudioWorklet est séparé des tests Python. Après
`uv run pyright`, utiliser le runtime Node local que Pyright place dans le
cache indiqué ci-dessus (aucun Node global n'est requis) :

```sh
"$PYRIGHT_PYTHON_CACHE_DIR/pyright-python/nodeenv/bin/node" --test tests/js/play_worklet.test.mjs
```

Node.js n'est pas requis pour exécuter le service.

Après la seconde correction S3 rebasée sur `main=19bd4c3` :
329 tests réussis, 3 tests de simulation ignorés, dont deux tests Silero ONNX
réels avec les assets locaux et 13 tests déterministes de corpus/provenance ;
couverture totale 93,04 %, machine d'états
98 %, porte audio 96 %, aucun diagnostic ruff ou pyright. Les tests ONNX réels
sont ignorés en CI sans assets ; leur commande obligatoire figure dans
`assets/wake/README.md`.

Résultat S1 initial : commande complète verte, 225 tests réussis, 3 tests sim
sautés volontairement hors opt-in ; couverture totale 93,99 %, gouverneur
95 %, relais 84 % ; aucun diagnostic pyright.

Après rebase S1 sur `main` `c1ee52d` (S2/S3 corrigées), la même commande
complète est verte : **294 tests réussis, 5 ignorés**, couverture totale
**92,95 %**, gouverneur 95 %, relais 84 %, aucun diagnostic ruff/pyright.
Les cinq sauts correspondent aux trois tests sim sans opt-in et aux deux
tests Silero réels dont les assets ne sont pas présents dans le worktree S1.
Les trois tests AudioWorklet S2 passent aussi avec le runtime Node local.

Après correction de la priorité STOP sur `main` `143e015` : **314 tests
réussis, 5 ignorés**, couverture totale **93,04 %**, gouverneur **96 %** et
relais **84 %** ; ruff/format/pyright verts. Les 20 nouveaux cas couvrent
les RPC regard/skills/politiques/activation lents, leurs réponses tardives,
les actions en attente du verrou et le watchdog. Les 5 tests AudioWorklet
courants passent aussi. Les 3 tests sim passent sans saut (avance 0,161 m,
tour 89,198°, regard 0,795 rad). Avec une réponse `robot.look` volontairement
retenue pendant une marche réelle, l'appel transport STOP débute en
**0,116 ms**, le RPC est acquitté en **moins de 200 ms**, la consigne revient
à zéro et l'action retardée est refusée. Le premier délai mesure l'entrée
dans `link.call`, pas l'arrivée réseau ni l'immobilisation physique (EMA).

Pour les trois tests contre la sim **attribuée explicitement**, après la
commande qualité et avec la sim/relais actifs :

```sh
COINCOIN_TEST_SIM=1 uv run pytest -m sim --cov-append -s
```

`--cov-append` conserve la mesure de couverture unitaire, qui ne se mesure
pas sur trois essais physiques seuls. Pour un essai ciblé sans couverture,
utiliser `--no-cov`. Sans opt-in ou si le relais attribué est indisponible,
les tests sim sont sautés ; ils ne se rabattent jamais sur une sim live.
Mesure après rebase : **3 passed, 296 deselected**, aucun saut ; avance
**0,154 m** en 2 s (> 0,15), tour **88,647°** (erreur < 15°), regard gauche
**0,795 rad** (> 0,2). La reconstruction Compose avec le lock combiné,
le montage `.cache/logs`, `/healthz` et la CLI `status` authentifiée ont
aussi été vérifiés. Sim, relais et projet Compose S1 arrêtés ensuite.

Pyright installe son runtime Node localement si Node manque dans WSL.
Les tests hors sim ne lisent pas les vrais secrets de `.env` et ne contactent aucun
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
uv run python scripts/record_positives.py --out assets/wake/recorded/session-01 --count 20 --seconds 2.5
```

Les sessions humaines sont séparées des TTS et ne remplacent aucun WAV.
Après interruption, relancer la même commande avec `--resume` ; les prises
complètes sont conservées et seuls les emplacements manquants sont enregistrés.
Les fichiers temporaires interrompus sont préservés. Un WAV existant invalide
arrête la reprise sans modification. Utiliser un nouveau dossier pour refaire
des prises. Écouter/vérifier les 80 WAV (20 par langue/distance) avant de
valider H-POSITIFS, puis lancer :

```sh
uv run python scripts/eval_wake.py \
  --positives assets/wake/recorded/session-01 --positive-source recorded \
  --attest-recorded 'I attest that every positive WAV in this directory is a human recording, not synthesized speech; speaker identity, phrase and distance are not automatically verified.' \
  --report assets/wake/recorded/session-01/wake-eval.md
```

Cette attestation doit être exacte et vraie pour chaque WAV ; le banc ne
reconnaît pas le locuteur et ne vérifie pas le contenu ou la distance.
Les imports humains sont possibles avec la même attestation et un autre
`--positives DIR`, sans les attribuer automatiquement à user. Les copies
exactes des TTS locaux connus sont refusées, mais aucune détection générale
de TTS n'est revendiquée. Le rapport affiche une provenance attestée et des
comptes de fichiers, jamais une complétude humaine vérifiée.

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
`assets/wake/negatives/`, `assets/wake/downloads/`, `assets/wake/vad/` et
`assets/wake/recorded/` :
ils ne sont pas transportés par une fusion Git. La procédure de copie sans
écrasement est donnée dans ce même document ; ne pas supprimer ces données.
Tout `assets/wake/` est exclu du contexte Docker ; les sessions humaines et
leurs rapports sous `recorded/` sont également ignorés par Git.

S0 ne lançait aucune simulation. S1 utilise uniquement son instance réservée.
`.env`, logs, caches, modèles et sons sont ignorés par Git et par le contexte
Docker.

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
  app.py, config.py, features.py, journal.py, clock.py
  realtime/          session WebSocket GA, évènements, instructions, outils
  audio/io.py        AudioIO : PCM16 mono 24 kHz, trames 20 ms
  audio/browser.py   BrowserAudio, WebSockets audio et contrôle
  audio/resample.py  conversions PCM16/float32 et SoXR 24→16 kHz
  audio/mixer.py     mixage voix/cancanements et écrêtage doux
  audio/tone.py      génération et corrélation du bip de test
  robot/link.py      RobotLink, Frame, RobotRefused, LinkDown
  robot/jsonrpc.py   corrélation des réponses et cache robot.state
  robot/tcp_link.py  transport authentifié, reconnexion, callbacks
  robot/frames.py    client media.frame et UYVY → JPEG
  robot/governor.py  sécurité et API d'actions pour S5
  robot/quack.py     QuackSink
  brain/base.py      Brain, NullBrain sans mémoire ni réseau
  web/server.py      /, /healthz, /ws/audio et /ws/ui
  web/static/        page navigateur et AudioWorklets micro/lecture
  cli.py            coincoin-cli
relay/duck_relay.py  relais stdlib autonome ; voir relay/README.md
tests/unit/          socle, audio, réveil, gouverneur, relais, images, mapping CLI
tests/integration/   WebSockets audio, TCP/Unix, erreurs, binaire et signaux
tests/sim/           mouvement, tour, regard (opt-in)
scripts/net_spike.sh
docs/                décisions et preuves réseau
```

`Clock` utilise des secondes monotones ; `FakeClock.advance(s)` libère les
attentes échues par deadline, puis par ordre d'inscription.
`Frame.captured_at` représente au contraire une date Unix en secondes.
`RobotLink.state()` retourne l'état poussé en cache, jamais une méthode RPC ;
`state_age()` vaut l'infini avant le premier état.

La configuration reprend toutes les variables de la spec maître §4.2 j.
Le modèle Realtime `gpt-realtime-2.1` et la voix provisoire `marin` sont ceux
de la trace réelle S4 ; l'écoute humaine reste requise. Le seuil Vosk
provisoire sélectionné en S3 vaut `0.9`. Les identifiants secrets sont des
`SecretStr`. Le journal masque récursivement les clés sensibles, les motifs
de clé API et les en-têtes Authorization ; il refuse les données non JSON
et laisse remonter les erreurs d'écriture.