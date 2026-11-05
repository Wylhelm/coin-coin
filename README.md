# Coin-Coin — voix Realtime et outils robot (S1–S9)

Service vocal du MicroDuck Coin-Coin, développé séparément du code Pollen.
S0 fournit le socle asynchrone ; S2 ajoute le micro et le haut-parleur du
navigateur, deux WebSockets PCM16, le mode écho et le test de latence par tonalité.
S3 ajoute le réveil local, le VAD, la porte audio et la machine d'états.
**S1 livre le relais filtrant, `TcpLink`, le gouverneur et `coincoin-cli`**,
en plus du socle S0. S4 ajoute la conversation vocale bilingue par Realtime
WebSocket côté serveur. Les sessions ne s'ouvrent qu'après le réveil local.
S5 installe `RobotFeature` : le serveur vocal maintient désormais une liaison
robot et expose les actions via le gouverneur, sans contourner ses sécurités.
S6 ajoute l'animation du bec et les cancanements : en simulation, `QUACK_SINK=stream`
mixe les WAV dans l'audio navigateur ; `QUACK_SINK=robot` utilise `robot.sound`.
Les modes `--echo` et `--tone` restent sans liaison robot.

## S6 — bec et cancanements

`VoiceFeature` s'installe après `RobotFeature` et crée un runtime indépendant par
application. Le bec suit les fenêtres PCM réellement envoyées au haut-parleur,
avec l'enveloppe et le décalage d'horloge de la spécification. `greet` est lancé
au réveil, `coo` au coucher et `inquire` après une transcription finissant par
`?`, avec un intervalle minimal de 30 secondes. Le volume `assets/sounds` est
monté en lecture seule dans Compose. Si la banque est absente ou vide, l'erreur
est journalisée une fois et seuls les cancanements sont désactivés.

Après autorisation explicite du créneau de simulation par la coordination,
procéder depuis WSL et le worktree S6. Ces commandes sont une procédure, pas la
preuve d'un essai déjà effectué. Le service Docker n'est pas nécessaire aux
tests unitaires ; ne modifier ni la configuration Docker/WSL ni les ressources
d'une autre session.

Terminal de simulation :

```sh
cd ~/projects/microduck/coin-coin-wt/S6
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
export DUCK_SIM_STATE="$HOME/.cache/ds-s6"
export DUCK_SIM_PORT=7861 DUCK_SIM_FRAME_PORT=7961
export DUCK_SIM_VIEWER=0 DUCK_SIM_CAMERAS=
export DUCK_SIM_RL="$HOME/projects/microduck/microduck_rl"
setpriv --no-new-privs ~/projects/microduck/microduck/scripts/duck-sim
```

Après le démarrage autorisé, copier la banque attribuée à S6 :

```sh
cd ~/projects/microduck/coin-coin-wt/S6
DUCK_SIM_STATE="$HOME/.cache/ds-s6" ./scripts/copy_sound_bank.sh
```

Dans un second terminal WSL, charger le `.env` S6 sans afficher son contenu,
redécouvrir l'IP de `eth0`, puis démarrer le relais :

```sh
cd ~/projects/microduck/coin-coin-wt/S6
set -a
source .env
set +a
HOST_IP="$(ip -4 -o addr show dev eth0 | awk '{split($4, a, "/"); print a[1]}')"
python3 relay/duck_relay.py --host "$HOST_IP" \
  --robot-port 7870 --frame-port 7871 --state-dir "$HOME/.cache/ds-s6"
```

Seulement si Docker Desktop est disponible, un troisième terminal démarre
Compose sur le port S6 :

```sh
cd ~/projects/microduck/coin-coin-wt/S6
COINCOIN_PORT=8776 COMPOSE_PROJECT_NAME=coincoin-s6 \
  docker compose up --build --detach --wait --wait-timeout 60
curl --fail http://localhost:8776/healthz
```

Dans le navigateur, ouvrir `http://localhost:8776/`, démarrer l'écoute et
vérifier le réveil, le mouvement du bec pendant une réponse, sa fermeture après
la réponse et sur interruption/STOP, puis l'audibilité de `greet`, `coo` et
`inquire`. Vérifier que les sons ne provoquent ni faux réveil ni faux barge-in.
Consigner séparément les résultats visuels et auditifs ; aucune réussite humaine
n'est revendiquée avant cet essai. Après la mesure, arrêter uniquement les
ressources S6 : Ctrl+C dans le terminal du relais, puis :

```sh
cd ~/projects/microduck/coin-coin-wt/S6
COINCOIN_PORT=8776 COMPOSE_PROJECT_NAME=coincoin-s6 docker compose down
DUCK_SIM_STATE="$HOME/.cache/ds-s6" DUCK_SIM_PORT=7861 DUCK_SIM_FRAME_PORT=7961 \
  setsid --wait timeout 45s setpriv --no-new-privs \
  "$HOME/projects/microduck/microduck/scripts/duck-sim" down
```

**État de validation (2026-11-05).** La qualité complète passe : ruff, format
et Pyright verts ; 479 tests réussis, 5 ignorés, couverture 91,78 %. Le test sim
autorisé donne 3 réussites (motion 0,177 m, tour 87,710°, regard 0,796 rad) ;
ces résultats ne valident pas le bec ou l'audio. Docker Desktop est arrêté :
Compose n'a pas été lancé, et les critères visuels/auditifs restent ouverts.

## S7 — Vision

`RobotFeature` expose `see(question?)` sur son `RobotLink` existant :
capture bornée à 2 s, une nouvelle demande si l'image a plus de 2 s, conversion
JPEG de largeur au plus 640 px et taille au plus 150 000 octets.
Sans caméra, l'outil répond « je n'ai pas d'yeux en ce moment ».
L'image et la question éventuelle forment un message utilisateur Realtime ;
le résultat d'outil précède l'unique continuation du lot S5. L'annulation,
le sommeil et le remplacement de session ou de réponse empêchent l'envoi d'une
capture orpheline. Le tour est revérifié après préparation et sous le verrou
d'envoi, juste avant de transmettre le message au WebSocket.
Le journal garde l'appel, la taille et la latence, jamais le JPEG ni sa base64.
Le stockage d'images est désactivé (politique `LOG_IMAGES=false`, sans opt-in).

### Caméra sur les ressources S7

Caméra réelle validée avant rebase le 2026-11-05 : snapshot explicite sur le
socket S7 sous délai de 2 s, puis test via le relais S7 : JPEG 360x640,
12 218 octets, vérifié dans un dossier temporaire puis supprimé.
L'erreur de découverte codec `webrtcsink` concernait le streaming, pas le
snapshot. Le wrapper `duck-sim ctl frame` visait à tort le socket `/run` par
défaut ; aucune correction Pollen/GStreamer n'a été effectuée.
Suite sim complète pré-rebase : **3 réussis, 1 échec** (marche 0,137 m,
seuil partagé > 0,15 m inchangé ; tour 91,007°, regard 0,796 rad).
Après rebase sur `2298ffb` : ruff/format/Pyright verts, **508 réussis,
6 ignorés**, couverture **91,88 %**, Node **6/6**. Contre-vérification du
coordonnateur sur `f333ee6` : **4 tests sim réussis, 510 désélectionnés**,
JPEG 360x640 de 12 244 octets vérifié puis dossier temporaire supprimé,
marche 0,156 m (> 0,15 m), tour 87,055°, regard 0,796 rad.
Le précédent échec de marche à 0,137 m reste consigné ; seuil et test inchangés.
Compose et essai vocal humain restent non validés.

Depuis WSL, réserver d'abord le verrou caméra partagé (ne jamais effacer celui
d'une autre session ; attendre 60 s entre tentatives, maximum 2 h) :

```sh
cd ~/projects/microduck/coin-coin-wt/S7
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
export UV_CACHE_DIR="$PWD/.cache/uv" PYRIGHT_PYTHON_CACHE_DIR="$PWD/.cache/pyright"
mkdir -p "$HOME/.cache/coin-coin-locks"
mkdir "$HOME/.cache/coin-coin-locks/sim-camera"
printf 'S7 %s\n' "$(date -Is)" > "$HOME/.cache/coin-coin-locks/sim-camera/owner"
export DUCK_SIM_STATE="$HOME/.cache/ds-s7"
export DUCK_SIM_PORT=7881 DUCK_SIM_FRAME_PORT=7981 DUCK_SIM_VIEWER=0
export DUCK_SIM_CAMERAS=a DUCK_SIM_RL="$HOME/projects/microduck/microduck_rl"
export GST_PLUGIN_PATH="/opt/gst-plugins-rs/lib/gstreamer-1.0${GST_PLUGIN_PATH:+:$GST_PLUGIN_PATH}"
setpriv --no-new-privs "$HOME/projects/microduck/microduck/scripts/duck-sim"
```

Vérifier le snapshot sur le socket de l'instance, sans utiliser le défaut `/run` :

```sh
timeout 2s "$HOME/projects/microduck/microduck/target/debug/robotctl" \
  --media-socket "$DUCK_SIM_STATE/duck-a-frame.sock" frame \
  --output "$DUCK_SIM_STATE/frame-check.uyvy"
rm "$DUCK_SIM_STATE/frame-check.uyvy"
```

En cas d'échec du snapshot ou d'arrêt de `mediad`,
arrêter et signaler le blocage, sans changer Pollen.
Dans un autre terminal S7, charger `.env` sans l'afficher (`set -a; source .env;
set +a`), redécouvrir `ip -4 -o addr show dev eth0`, puis lancer :

```sh
python3 relay/duck_relay.py --host <IPv4-eth0> --robot-port 7890 \
  --frame-port 7891 --state-dir "$HOME/.cache/ds-s7"
```

Le `.env` S7 doit viser ces deux ports et contenir le jeton S7 déjà attribué.
Tests sans OpenAI : `COINCOIN_TEST_SIM=1 uv run pytest --no-cov -m sim -s -rs`.
Le test caméra vérifie le JPEG dans un répertoire temporaire puis le supprime.
Pour l'essai vocal de user seulement, choisir `DUCK_SIM_SCENE=apartment`
avant de lancer la sim (le canard n'y marche pas), puis `docker compose up`
avec `COINCOIN_PORT=8777 COMPOSE_PROJECT_NAME=coincoin-s7`. Ouvrir
`http://localhost:8777` et demander « Coin-Coin, qu'est-ce que tu vois ? ».

Après usage : arrêter le relais avec son PID exact, le Compose S7 si lancé,
puis :

```sh
DUCK_SIM_STATE="$HOME/.cache/ds-s7" DUCK_SIM_PORT=7881 DUCK_SIM_FRAME_PORT=7981 \
  setsid --wait timeout 45s setpriv --no-new-privs \
  "$HOME/projects/microduck/microduck/scripts/duck-sim" down
```

Libérer ensuite uniquement le verrou S7 (`rm -r ~/.cache/coin-coin-locks/sim-camera`).

## S5 — correctif de reprise unique, en attente de contre-vérification

La livraison `46bdee4` a été refusée pour une course entre fin de réponse
(`mouth_close` en attente réseau) et annonce de perte robot : deux tâches
pouvaient émettre une annonce puis une réponse normale concurrente.
Une reprise sans annonce exige désormais un lot d'outils valide ; les tâches
d'annonce obsolètes ne font rien et une réponse active conserve la priorité.
L'annonce reste en attente jusqu'à son envoi réussi : une interruption avant
envoi ne la perd pas, une nouvelle perte pendant l'envoi reste distincte,
et STOP l'invalide et annule l'envoi comme auparavant.

Les douze nouvelles régressions sont entièrement en mémoire, avec le vrai
`App` : fin normale/échec avec bec bloqué, tâche obsolète, outputs ordonnés,
barge-in, nouvelle perte pendant envoi réussi/échoué et STOP prioritaire.
L'identité de session/réponse est revérifiée après l'attente de notification
UI, pour ne pas envoyer une annonce devenue concurrente ou orpheline.
Résultats : **80 tests ciblés réussis**, qualité complète **459 réussis,
3 sim ignorés**, couverture **91,78 %**, ruff/format/Pyright verts, Node **6/6**.
Les 42 cas S5 incluent toujours les vrais transports vers les faux.
Les [preuves et commandes](docs/network.md#s5--correction-de-la-course-dannonce)
conservent aussi la reproduction rouge avant correction.
Aucun service S5 ni appel réel OpenAI n'a été lancé pour ce correctif.
S5 reste non intégrée ; les essais humains restent ouverts.

### Historique : validation sim après SIMWALK

Après rebase sur `main=cc30372` (S8 et correctif test-only SIMWALK), la suite
sim complète exécutée **une seule fois** passe : **3 réussis, 447 désélectionnés,
aucun saut**, avance stabilisée **0,162 m**, tour **89,222°**, regard **0,795 rad**.
Le test commun attend une odométrie fraîche et stabilisée après STOP ;
consigne **0,2 m/s pendant 2 s** et seuil strict **> 0,15 m** inchangés.
Aucun test sim supplémentaire S5 ne reproduisait l'ancien délai fixe.
Le blocage de mesure sim a été levé ; la course d'annonce ci-dessus a ensuite
rouvert la livraison. La sim n'est pas rejouée pour cette correction App/tests.
Les anciens résultats (3/3 puis marche à 0,120359637 m) sont conservés dans
[les preuves S5](docs/network.md#s5--revalidation-apres-simwalk) et expliqués
par le protocole de mesure dans [SIMWALK](docs/simwalk-motion.md), sans
prétendre mesurer une vérité terrain physique indépendante.

La qualité hors sim de cette livraison précédente passait : **447 réussis, 3 sim ignorés**, couverture
**91,72 %**, ruff/format/Pyright verts et vrai AudioWorklet **6/6**.
Les deux tests Silero utilisent les assets locaux, sans saut. Les **30 cas S5**
de `tests/integration/test_tools_e2e_fakes.py` couvrent les vrais transports
WebSocket/TCP vers les faux, les refus intacts, les schémas/arguments, la
reconnexion, les annonces sans réponse concurrente, STOP et le nettoyage.
Aucun nouvel appel OpenAI n'a été exécuté.

### Démarrer exclusivement les ressources S5

Depuis WSL, le worktree existe déjà ; ne pas créer un autre worktree :

```sh
cd ~/projects/microduck/coin-coin-wt/S5
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
export UV_CACHE_DIR="$PWD/.cache/uv"
export PYRIGHT_PYTHON_CACHE_DIR="$PWD/.cache/pyright"
export DUCK_SIM_STATE="$HOME/.cache/ds-s5"
export DUCK_SIM_PORT=7841 DUCK_SIM_FRAME_PORT=7941 DUCK_SIM_VIEWER=0
export DUCK_SIM_CAMERAS=
export DUCK_SIM_RL="$HOME/projects/microduck/microduck_rl"
mkdir -p .cache/logs
ip -4 -o addr show dev eth0
```

Le `.env` ignoré, mode 600, doit contenir `ROBOT_LINK` **et** `FRAME_LINK` :
`tcp://<IPv4 eth0>:7850` et `tcp://<IPv4 eth0>:7851`, ainsi que
`COINCOIN_PORT=8775`, `COMPOSE_PROJECT_NAME=coincoin-s5` et le `RELAY_TOKEN`
propre à S5 déjà créé. Ne jamais afficher ni régénérer ce jeton pour un simple
redémarrage. Vérifier les deux liens après chaque changement d'IP.

Le montage `assets/models` doit contenir les modèles Vosk/Silero décrits dans
`assets/wake/README.md`. Ils restent ignorés par Git. S'ils sont déjà présents
dans le clone principal, les recopier **sans écrasement** :

```sh
mkdir -p assets/models
cp -a --update=none ~/projects/microduck/coin-coin/assets/models/. assets/models/
"$HOME/projects/microduck/microduck/scripts/duck-sim"
```

Dans un deuxième terminal WSL, avec l'IP redécouverte ci-dessus (mesure :
192.0.2.1), lancer le relais depuis le même worktree :

```sh
cd ~/projects/microduck/coin-coin-wt/S5
set -a
source .env
set +a
echo $$ > .cache/logs/s5-relay.pid
exec python3 relay/duck_relay.py --host 192.0.2.1 \
  --robot-port 7850 --frame-port 7851 --state-dir "$HOME/.cache/ds-s5"
```

Dans un troisième terminal WSL, depuis S5 :

```sh
docker compose up --build --detach --wait --wait-timeout 60
curl --fail http://localhost:8775/healthz
docker compose exec -T coin-coin coincoin-cli status
```

Le démarrage du service ne déclenche pas OpenAI : la session ne s'ouvre
qu'après réveil. Les appels réels ci-dessous sont réservés aux essais
explicitement lancés par user. Le développement utilise uniquement
`fake_realtime` et la trace S4 inchangée.

### Essais humains encore ouverts

Ouvrir `http://localhost:8775/` dans Chrome/Edge sous Windows, cliquer sur
**Démarrer**, autoriser le micro, puis prononcer les quatre séries suivantes.
Ne cocher les critères humains de S5 qu'après observation et écoute réelles.

1. « Coin-Coin, avance un peu puis tourne à gauche » : observer une avance,
   puis un tour gauche et une annonce brève honnête. `move`/`turn` rendent
   immédiatement la main ; une nouvelle action remplace la précédente.
   `status` indique un mouvement encore actif, et le modèle doit attendre sa
   fin avant de prétendre avoir exécuté une séquence.
2. « Assieds-toi », deux fois, puis « lève-toi », « fais une roulade » et
   « regarde en haut » : vérifier l'idempotence assis/debout et les actions,
   ou un refus expliqué sans inventer une réussite. La liste de figures est
   relue à chaque ouverture de session ; `sit_toggle` n'est jamais exposé.
3. Pendant « avance pendant cinq secondes », cliquer sur **STOP** : observer
   l'arrêt de consigne et le silence. Refaire une marche, puis interrompre
   le relais par Ctrl-C dans son terminal : observer l'arrêt autonome
   (deadman) et une annonce unique de perte des pattes. Relancer le relais
   pour vérifier le retour des outils, sans redémarrer l'application.
4. Consulter `.cache/logs/*.jsonl` : chaque appel/résultat Realtime possède
   `call_id`, `name` et `latency_ms`. Les actions/refus sont aussi sur la page
   (20 dernières entrées). `move_to_first_command_ms` mesure seulement l'envoi
   de la première consigne, **pas** le début du déplacement physique.
   Pour le critère humain `< 1 s`, mesurer séparément l'intervalle entre l'appel
   et le mouvement observé ; ne pas substituer cette métrique de transport
   à cette observation. Les journaux sont des données personnelles locales.

`quack` est un **stub explicitement journalisé**, sans son jusqu'à S6 ;
aucun `robot.sound` n'est utilisé en simulation. Vision/S7 et mémoire/S8 ne
sont pas implémentées par S5. Le réveil reste provisoire selon ADR-003.

### Qualité et arrêt S5

```sh
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
"$PYRIGHT_PYTHON_CACHE_DIR/pyright-python/nodeenv/bin/node" --test tests/js/play_worklet.test.mjs
COINCOIN_TEST_SIM=1 uv run pytest --no-cov -m sim -s -rs
```

Exiger **3 essais exécutés**, pas trois sauts : sans relais attribué, les tests
sim sont ignorés et ne prouvent rien. La revalidation SIMWALK passe 3/3.
Arrêter uniquement le projet S5 et le relais S5 (Ctrl-C dans son terminal) :

```sh
docker compose down
setsid --wait timeout 45s env DUCK_SIM_STATE="$HOME/.cache/ds-s5" \
  DUCK_SIM_PORT=7841 DUCK_SIM_FRAME_PORT=7941 \
  "$HOME/projects/microduck/microduck/scripts/duck-sim" down </dev/null
ss -ltn '( sport = :7841 or sport = :7941 or sport = :7850 or sport = :7851 or sport = :8775 )'
docker ps -a --filter label=com.docker.compose.project=coincoin-s5
```

À la livraison, aucun listener sur ces cinq ports, aucun pidfile de la sim
S5, ni conteneur/réseau Compose S5 ne subsiste. Les modèles et journaux locaux
sont conservés ; les ressources live de user et celles de S9 sont intactes.

## Transport WebRTC S9

`ROBOT_LINK=webrtc://host:8443` sélectionne `WebRtcLink`, qui suit la
signalisation LAN de `mediad`, réutilise `JsonRpcConnection` sur la datachannel
`control` et reçoit les images de la piste vidéo. Le lien TCP authentifié reste
le défaut des sessions S1. Le service vocal (`RobotFeature`), comme la CLI,
utilise la fabrique de transport ; un lien injecté explicitement reste
prioritaire. Horloge, paramètres TCP et callbacks de connexion/déconnexion
sont conservés. Procédure, mesures et décision Q10 :
[docs/webrtc.md](docs/webrtc.md).

Pour les tests contre la caméra de la simulation S9, garder le verrou partagé
`~/.cache/coin-coin-locks/sim-camera`, vérifier que les ports mediad 8443/8080
sont libres et utiliser exclusivement les ressources indiquées dans le
document. Avec le `.env` de la session configuré et la simulation active :

```sh
export GST_PLUGIN_FEATURE_RANK=nvh264enc:NONE
COINCOIN_TEST_SIM=1 uv run pytest --no-cov -m sim -s -rs
```

Sur cet hôte WSL, `nvh264enc` est détecté mais son preset matériel échoue ;
le rang `NONE` laisse `x264enc` logiciel traiter la vidéo.
Les fixtures sim lisent le `.env` du worktree et donnent priorité au
`ROBOT_LINK` explicite de la commande. Seule cette variable non secrète est
préservée pour les tests marqués `sim` avec opt-in ; les autres tests restent
isolés de l'environnement et des features installées par défaut.
Le test caméra S7 suit aussi cette cible : WebRTC se connecte avant de lire
la vidéo, TCP garde sa capture indépendante sur `FRAME_LINK`. Une caméra
indisponible en opt-in fait échouer le test, sans saut silencieux.

Après rebase sur `main=8de5e3e`, les marches S1 et S9 partagent le helper
SIMWALK typé `RobotLink` : état frais (200 ms), vitesse appliquée < 0,01,
deux écarts XY consécutifs <= 5 mm, attente bornée à 1 s avant et après
la commande inchangée de 2 s. La mesure inclut la décélération post-STOP ;
elle n'est pas interpolée à l'instant exact t=2 s. Les seuils de distance,
tour et regard sont conservés.

Le test choisit le transport depuis `ROBOT_LINK`; avec le schéma `webrtc://`,
les scénarios `move`, `turn`, `look` et les mesures de RTT/gigue passent par
WebRTC. La signalisation LAN n'a pas d'authentification : ne pas exposer
`mediad` au-delà du LAN.

Le transport WebRTC applique avant tout envoi la même liste de méthodes S1
que `duck-relay`, partagée dans `coincoin.robot.methods`. Cela couvre aussi
`coincoin-cli raw` : une méthode interdite échoue localement avec le code
JSON-RPC `-32601`, sans transmettre l'appel ou ses paramètres. Les méthodes
permises gardent leurs contrats ; ce filtre ne remplace ni le gouverneur
ni l'isolation réseau, et une CLI `raw` n'est pas une API sûre pour un modèle.

**Acceptation S9 encore bloquée :** après revue de `2fc23f9` sur `8de5e3e`,
l'unique suite réelle autorisée avec SIMWALK donne **6 réussites, 2 échecs,
zéro saut** (8 tests, code retour 1). Marches S1/S9 : **0,137311/0,145489 m**,
en dessous du seuil strict > 0,15 m, commande de 2 s inchangée. Tours, regards,
caméra et métriques passent. Aucun rerun ni correctif spéculatif ; ressources
S9 arrêtées, ports libres et verrou retiré. Les preuves antérieures sont
conservées, sans sélection de réussite ni acceptation des anciennes marches
à 3 s. [Sortie complète et nettoyage](docs/evidence/s9-final-sim-20261105/README.md),
mesures et limites dans `docs/webrtc.md`. Attente de contre-vérification.

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

## Test de marche simulée (SIMWALK)

Le test `tests/sim/test_sim_motion.py::test_move_advances_more_than_fifteen_cm`
garde sa consigne `move(0.2, 0, 0, 2)` et son seuil strict `> 0,15 m`.
Il mesure maintenant l'odométrie après un état frais, `applied < 0,01` et
deux écarts consécutifs entre mises à jour XY d'au plus 5 mm. Le retour de
`wait_motion()` acquitte l'arrêt, mais ne signifie pas que l'EMA a déjà
ramené la vitesse appliquée à zéro. Cette attente ne prolonge ni la consigne
ni sa durée ; elle attend seulement la fin de la décélération et la
stabilisation de la mesure. Aucun nouvel essai automatique n'est fait.

Après avoir démarré la simulation et son relais comme ci-dessus, sans lancer
Compose, lancer une exécution isolée avec :

```sh
COINCOIN_TEST_SIM=1 uv run pytest \
  tests/sim/test_sim_motion.py::test_move_advances_more_than_fifteen_cm \
  -s --no-cov
```

Le diagnostic, les dix exécutions consécutives consignées et leurs limites
sont dans [docs/simwalk-motion.md](docs/simwalk-motion.md) et
[docs/evidence/simwalk/final-motion-runs.txt](docs/evidence/simwalk/final-motion-runs.txt).

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
transmis sans conserver le PCM. La première correction (`5cc8a42`) comptait
366 tests réussis, 3 tests de simulation ignorés et 90,40 % de couverture,
mais une seconde revue a révélé d'autres régressions.

La seconde correction ajoute 23 cas dans `tests/integration/test_s4_followup.py` :
réarmement silencieux 500 ms après STOP (même déjà en sommeil), arrêt de service
laissant au hook robot jusqu'à 1 s pour finir ou signaler son échec, et vrai
outil `sleep` sans auto-annulation. Les appels périmés sont filtrés par réponse ;
les outils parallèles rendent leurs résultats dans l'ordre des appels puis
demandent une seule reprise après `response.done` du parent et tous les résultats.
L'interruption annule ce lot et interdit la reprise. Un échec d'envoi est
journalisé et termine l'état de parole, sans succès factice.

Le worklet rapporte aussi les échantillons exacts (25 échantillons restent
distincts des 24 correspondant à 1 ms arrondie), avec un suivi par génération
de page/flush. Une page remplacée ne reçoit pas l'ancien audio en file ; une
réponse inachevée perdue est signalée comme un échec, et la suivante repart
sur les compteurs de la nouvelle page. Les hooks `audio_out` sont initiés lors
de l'appel réel à `AudioIO.play`, hors du consommateur Realtime ; un hook lent
ne retarde pas le flush. L'upgrade WebSocket est borné et testé contre une
socket TCP locale bloquée ; STOP hors ligne, écho et tonalité conserve le flush.

La commande qualité complète passe désormais **389 tests, 3 tests de simulation
ignorés, couverture 90,91 %** ; ruff, format et Pyright sont verts, et le vrai
AudioWorklet passe **6/6** sous Node. Aucune nouvelle session OpenAI, simulation
ou liaison robot n'a été utilisée ; la trace réelle unique est inchangée.
Ces tests ne remplacent pas les validations vocales humaines ci-dessus.

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
  brain/openclaw.py  OpenClawBrain, client HTTPX de la gateway OpenClaw
  brain/feature.py   BrainFeature : réveil, coucher, {memory_context}
  brain/summary.py   résumé local borné depuis le journal
  realtime/tools/brain.py  outils ask_openclaw et remember
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

## Mémoire OpenClaw (S8)

`BrainFeature` (inscrit dans `FEATURES`) relie l'agent OpenClaw `coin-coin` à
la conversation : au réveil, `session_start` (3 s maximum) alimente
`{memory_context}` avant l'ouverture Realtime ; le modèle dispose des outils
`remember` et `ask_openclaw` ; au coucher, un résumé local (transcriptions
finales et résultats d'outils de la conversation, ≤ 1 500 caractères) est envoyé
par `session_end` en tâche de fond, sans retarder le sommeil. Sans
`OPENCLAW_TOKEN`, `NullBrain` reste actif et Coin-Coin dit qu'il n'a pas de
mémoire. Gateway lente, refusée (401/403), archivée (409) ou en erreur : phrase
explicite, journal sans jeton, conversation poursuivie sans mémoire.

Livré et vérifié uniquement contre le faux HTTP loopback et des doubles
Realtime : **aucune API OpenClaw réelle n'a été appelée ni observée**, aucune
requête OpenAI nouvelle. Configuration de la gateway, risques, procédure
`/v1/models` et validations humaines (port `8778`, projet `coincoin-s8`) :
[`docs/openclaw.md`](docs/openclaw.md).

```sh
cd ~/projects/microduck/coin-coin-wt/S8
export PATH="$HOME/.local/bin:$PATH"
export UV_CACHE_DIR="$PWD/.cache/uv"
export PYRIGHT_PYTHON_CACHE_DIR="$PWD/.cache/pyright"
uv run pytest tests/unit/test_openclaw_brain.py tests/unit/test_brain_tools.py \
  tests/integration/test_memory_flow.py --no-cov
```
