# Réseau Coin-Coin — état actuel et relevés historiques

> Les captures, commandes et chemins publiés ont été anonymisés ; les mesures
> et résultats des essais restent inchangés. Les adresses `192.0.2.x` sont des
> substituts de documentation, pas les adresses utilisées lors des essais.
> Les SHA-256 des captures publiées ont été recalculés.

## État assemblé au 2026-11-05

Base intégrée `main=e7cb24f` : S0–S8 intégrées côté agent, validations humaines
ouvertes ; S9 intégrée sous réserves, TCP conservé par défaut ; S10 non commencée.
La [procédure actuelle depuis main](delivery.md) remplace les anciens lancements
par worktree ci-dessous : IP `eth0` redécouverte, relais TCP authentifié,
modèles et sons locaux hors Git, lancement et arrêt réservés à l'utilisateur.
La dernière suite sim S9 conserve **6 réussites, 2 échecs de marche, zéro saut**,
sans nouvel essai. Docker reste arrêté pour cette préparation documentaire.

Les sections suivantes sont des **relevés historiques** : dates, commandes,
sorties, incidents et preuves sont conservés sans les présenter comme une
revalidation de la livraison assemblée.

## S5 — correction de la course d'annonce

`46bdee4` a été refusé après reproduction d'une double continuation.
Le correctif ne touche que l'ordonnancement App et ses régressions ;
aucun lancement sim, relais ou Compose, aucun appel réel OpenAI.
Les tests sim et leurs seuils restent inchangés, sans nouvelle campagne.
À cette remise, S5 attendait la contre-vérification. Le correctif `cacac70`
est maintenant intégré dans la base `e7cb24f`, avec S6/S7 ; les essais
humains restent ouverts.

Depuis `~/projects/microduck/coin-coin-wt/S5`, environnement commun :

```sh
export PATH="$HOME/.local/bin:$PATH"
export UV_CACHE_DIR="$PWD/.cache/uv"
export PYRIGHT_PYTHON_CACHE_DIR="$PWD/.cache/pyright"
set -o pipefail
```

Commandes et sorties anonymisées :

```sh
# Avant correction : les deux ordonnancements échouent (double continuation).
uv run pytest tests/integration/test_tools_e2e_fakes.py -k racing_blocked_mouth --no-cov -q \
  2>&1 | tee docs/evidence/s5/announcement-race-before.txt
# Après correction : fin/échec, outputs ordonnés, interruption et STOP.
timeout 90s uv run pytest tests/integration/test_tools_e2e_fakes.py \
  tests/integration/test_s4_regressions.py tests/integration/test_s4_followup.py \
  --no-cov -q 2>&1 | tee docs/evidence/s5/announcement-verified-targeted.txt
(uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest) \
  2>&1 | tee docs/evidence/s5/announcement-verified-quality.txt
"$PYRIGHT_PYTHON_CACHE_DIR/pyright-python/nodeenv/bin/node" --test tests/js/play_worklet.test.mjs \
  2>&1 | tee docs/evidence/s5/announcement-verified-node.txt
```

Résultats : **2 failed, 30 deselected** avant correction ; **80 passed**
ciblés après ; qualité **459 passed, 3 skipped**, couverture **91,78 %**,
96 fichiers déjà formatés, 0 diagnostic Pyright ; Node **6/6**.
Les trois sauts sim hors opt-in ne sont pas une preuve sim supplémentaire.
La preuve sim antérieure ci-dessous reste inchangée ; la contre-vérification
indépendante rapportée par le coordonnateur donne aussi 3/3 (avance 0,162 m,
tour 89,033°, regard 0,795 rad), avec son nettoyage vérifié.

Un premier lancement ciblé a été interrompu : le nouveau helper de test
bouclait sur des tâches déjà terminées avant leurs callbacks de retrait.
Le helper attend désormais uniquement les tâches encore pendantes ; ce
diagnostic n'a nécessité aucune autre modification de production.
Les captures de diagnostic et les premiers passages verts sont conservés
en plus des captures finales. Le manifeste `evidence/s5/announcement-captures.json`
référence les commandes exactes, codes et SHA-256. Le contrôle final est dans
`evidence/s5/announcement-cleanup.txt` : aucune ressource S5 restante,
Pollen et toutes les anciennes preuves inchangés ; confirmation après la
dernière vérification dans `evidence/s5/announcement-verified-cleanup.txt`.
Les captures `announcement-final-*` précèdent les deux régressions
additionnelles d'attente UI ; les résultats définitifs sont `announcement-verified-*`.

## S5 — revalidation après SIMWALK

Rebase autorisé de `session/S5` sur `main=cc30372` : S8 et le helper stabilisé
SIMWALK sont conservés. Aucun changement supplémentaire du test de marche ou
du gouverneur ; aucune autre mesure par délai fixe dans les tests S5.
La consigne reste 0,2 m/s pendant 2 s et le seuil reste strictement > 0,15 m.

| Vérification du 2026-11-05 après rebase | Résultat |
|---|---|
| Qualité complète `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` | Verts, 96 fichiers formatés, 0 diagnostic ; **447 passed, 3 skipped**, couverture **91,72 %** |
| Sauts hors opt-in | Les trois sim uniquement ; deux tests Silero réels exécutés |
| Vrai AudioWorklet | **6 passed** |
| Unique passage `COINCOIN_TEST_SIM=1 uv run pytest --no-cov -m sim -s -rs` | **3 passed, 447 deselected**, aucun saut, 8,17 s |
| Marche stabilisée | **0,162 m**, `applied=[0.0003868562621978774, 0.0, 0.0]` |
| Odométrie de marche | `[0.05339529642001246, 0.012213127569438806, 0.11751788367113818]` → `[0.21340207200867353, -0.012326239385973575, 0.11747163834432499]` |
| Tour et regard | **89,222°**, **0,795 rad** |
| Ressources S5 | IP eth0 redécouverte `192.0.2.1`, sim `ds-s5` sans caméra, 7841/7941, relais authentifié 7850/7851 ; aucun Compose lancé pour ce passage |
| Nettoyage | Compose down, relais **PID 71133** vérifié cwd/argv puis SIGTERM (code 0), sim down sans TTY (code 0) |
| Vérification finale | Aucun listener 7841/7941/7850/7851/8775, conteneur/réseau Compose S5 ou pidfile sim ; blob Pollen inchangé `b95192ba23fa78c91ef3c0db8cc250c4a00fd774` |

Captures anonymisées suivies par Git :
[qualité](evidence/s5/rebased-quality.txt),
[démarrage](evidence/s5/rebased-sim-start.txt),
[passage sim unique](evidence/s5/rebased-sim-single-run.txt),
[nettoyage](evidence/s5/rebased-cleanup.txt).
[Manifeste](evidence/s5/rebased-captures.json) : commandes anonymisées, UTC,
identifiants d'appels, méthode de capture, codes et SHA-256. Les deux sorties
sim/nettoyage ont été capturées par `tee` puis anonymisées pour publication.
Les nouveaux logs simulateur sont conservés dans `.cache/logs/s5-simulator-rebased/`,
séparément de la copie initiale `.cache/logs/s5-simulator/`.

Le blocage initial ci-dessous est levé par ce passage du protocole SIMWALK,
sur décision du responsable du projet. Aucune sélection/reprise d'essai,
aucun nouvel appel OpenAI et aucun travail S9. La mesure inclut la décélération post-STOP ;
elle reste odométrique, pas une preuve indépendante de déplacement physique.
Les critères humains demeurent ouverts ; l'intégration reste au coordonnateur.

## S5 — outils robot et blocage de marche

Section historique de la livraison initiale, avant revalidation SIMWALK.

Branche `session/S5`, base `051eb33`, ressources exclusivement S5, sans caméra :
`~/.cache/ds-s5`, corps 7841, frame réservée 7941, relais 7850/7851,
HTTP 8775, projet `coincoin-s5`. IP eth0 redécouverte : `192.0.2.1`.

**Sorties anonymisées, sans rejouer les essais.** Les captures ont été
extraites des résultats d'outils puis anonymisées, sans modifier leurs mesures :
[premier passage](evidence/s5/sim-pass.txt),
[qualité puis dernier passage en échec](evidence/s5/quality-and-sim-failure.txt),
[tentative initiale non probante](evidence/s5/initial-unavailable.txt) et
[nettoyage](evidence/s5/cleanup.txt). Le
[manifeste](evidence/s5/captures.json) conserve les commandes PowerShell/WSL
anonymisées, l'identifiant d'appel, l'heure UTC, le code de sortie et le SHA-256
de chaque capture. Le contenu inclut les en-têtes et tableaux pytest complets,
pas seulement une synthèse. Les logs originaux du simulateur sont conservés
dans `~/.cache/ds-s5/*.log` et copiés sans écrasement dans
`.cache/logs/s5-simulator/` ; les journaux applicatifs locaux restent dans
`.cache/logs/2026-11-05.jsonl`. Ils ne sont pas suivis par Git.

| Vérification du 2026-11-05 | Résultat exact |
|---|---|
| Qualité finale `ruff check`, `ruff format --check`, `pyright`, `pytest` | Verts, 0 diagnostic ; **419 passed, 3 skipped**, couverture **91,33 %** |
| Nouveaux cas S5 | **30 passed**, WebSocket GA réel vers fake Realtime et TCP JSON-RPC vers fake robotd ; refus, lots/annonces, STOP et fermeture |
| Silero avec assets locaux | **2 passed**, aucun saut VAD |
| Vrai AudioWorklet | **6 passed** |
| Première commande opt-in correctement configurée | **3 passed, 413 deselected**, aucun saut ; avance **0,156 m**, tour **89,491°**, regard **0,796 rad** |
| Dernière commande `COINCOIN_TEST_SIM=1 uv run pytest -m sim --cov-append -s -rs` | **1 failed, 2 passed, 419 deselected**, aucun saut |
| Avance de ce dernier passage | **0,12035963701374283 m**, seuil inchangé **> 0,15 m** ; le RPC de mouvement termine avec succès, pas la distance exigée |
| Odométrie de ce dernier passage | `[0.23583695881905897, -0.007036742004301863, 0.11728879250210117]` → `[0.3072591412978083, 0.08984883077854772, 0.11936911012051625]` ; projection sur le cap initial |
| Tour et regard du dernier passage | **89,390°**, **0,797 rad**, tous deux conformes |
| Smoke Docker intermédiaire | `docker compose up --build --detach --wait --wait-timeout 60` puis `/healthz` : **healthy**, HTTP 200 `{"status":"ok"}` ; CLI `status` authentifiée : debout, walk, tout va bien ; UID/GID **1000** |
| Nettoyage final | Compose S5 down ; relais **PID 51272** certifié par cwd/argv puis SIGTERM, code **0** ; script sim down sans TTY, code **0** |
| Contrôle après nettoyage | Aucun listener **7841/7941/7850/7851/8775**, aucun conteneur S5 ni pidfile dans `ds-s5` |
| Code Pollen préexistant | Blob `scripts/duck-sim` toujours **b95192ba23fa78c91ef3c0db8cc250c4a00fd774**, aucune modification par S5 |

**Blocage d'acceptation.** Le premier passage a été réalisé avant lancement
du service vocal Compose ; le dernier avec ce service actif mais sans page
micro ni session OpenAI. Cela ne démontre aucune cause : aucune mesure CPU,
gait ou propre au transport n'établit l'origine de l'écart. On ne relance pas
en boucle pour sélectionner un résultat favorable. Aucun seuil, scénario,
code Pollen, verrou caméra ni ressource S9 n'a été modifié.

**Préparation locale corrigée, sans fuite de secrets.** Le `.env` préparé
ne contenait pas `ROBOT_LINK` ; la première tentative de tests opt-in a donc
ignoré les trois essais faute de connexion. Ces sauts ne sont **pas** une preuve.
Ajout de la seule clé `ROBOT_LINK=tcp://192.0.2.1:7850` ; `FRAME_LINK`
pointait déjà vers 7851, le jeton propre à S5 est resté inchangé, mode 600.
Le premier démarrage Compose avait aussi échoué faute de modèles Vosk locaux ;
les modèles existants du clone principal ont été recopiés sans écrasement,
ainsi que le WAV synthétique Silero requis par le test réel. Les données,
modèles et journaux restent ignorés. Aucun secret n'a été affiché.

## Correction finale de priorité STOP sur `main` `143e015`

Le verrou d'action pouvait retarder STOP pendant un RPC lent. L'arrêt ne
prend plus ce verrou, annule les RPC d'action et invalide leur génération.
Voir ADR-005 pour les courses et la limite des commandes déjà traitées.

| Vérification finale | Résultat |
|---|---|
| Commande qualité complète | Ruff/format verts, 0 diagnostic pyright ; **314 passed, 5 skipped** |
| Couverture | **93,04 %** totale ; gouverneur **96 %**, relais **84 %** |
| Sauts | 3 sim sans opt-in ; 2 Silero réels sans assets locaux S1 |
| Tests gouverneur ciblés | **68 passed**, dont 20 nouveaux cas de réponses lentes/tardives et courses STOP |
| Harness AudioWorklet sur la base S2 corrigée | **5 passed** |
| `COINCOIN_TEST_SIM=1 uv run pytest -m sim --cov-append -s` | **3 passed, 316 deselected**, aucun saut ; avance **0,161 m**, tour **89,198°**, regard gauche **0,795 rad** |
| Reproduction sur la vraie sim S1 | Marche vx=0,2, attente `applied.vx > 0,05`, réponse `robot.look` retenue par événement ; appel `link.call` STOP débuté en **0,116 ms**, RPC acquitté en **< 200 ms**, consigne `[0,0,0]`, ancienne action refusée `action cancelled by stop` |
| Nettoyage final | Relais PID 12938 certifié par argv/cwd puis SIGTERM code 0 ; sim `ds-s1` down sans TTY code 0 ; aucun listener 7821/7921/7830/7831/8771 ni conteneur S1 |

Simulation sans caméra (`DUCK_SIM_CAMERAS` vide), ressources S1 uniquement ;
aucun processus, port, verrou ou donnée ignorée de S9 modifié. Les essais
Docker/deadman précédents restent les preuves historiques ci-dessous ;
ils ne sont pas présentés comme de nouvelles mesures de cette correction.

## Revalidation S1 après rebase sur S2/S3

Base `main` : `c1ee52d`. Dépendances ONNX/Vosk/SoXR conservées, Pillow
ajouté et `uv.lock` régénéré. Aucun module S2/S3 modifié ; leurs procédures
audio/réveil et réserves humaines restent présentes. Les décisions robot
portent désormais les numéros ADR-004/005, sans renuméroter les ADR S2/S3.
La procédure robot décrit aussi la configuration locale depuis le dépôt
principal : `.env` non transporté par Git, jeton généré sans affichage,
liens eth0 et montage `.cache/logs/`.

| Vérification après rebase | Résultat |
|---|---|
| Commande qualité complète ci-dessous | Ruff/format verts, 0 diagnostic pyright ; **294 passed, 5 skipped** |
| Couverture combinée | **92,95 %** totale ; gouverneur **95 %**, relais **84 %** |
| Sauts hors opt-in | 3 sim ; 2 tests Silero réels sans leurs assets locaux (pas de régression revendiquée sur ces assets) |
| `.cache/pyright/pyright-python/nodeenv/bin/node --test tests/js/play_worklet.test.mjs` | **3 passed**, vrai AudioWorklet S2 |
| `COINCOIN_TEST_SIM=1 uv run pytest -m sim --cov-append -s` | **3 passed, 296 deselected**, aucun saut ; avance **0,154 m**, tour **88,647°**, regard gauche **0,795 rad** |
| `docker compose up --build --detach --wait --wait-timeout 60` | Lock combiné installé ; projet S1 healthy avec `.cache/logs/` |
| HTTP `8771/healthz` puis CLI Docker `status` | HTTP 200 ; robot debout, walk, enabled, healthy, état frais |
| Nettoyage | Compose down, relais PID 71600 certifié puis SIGTERM (code 0), duck-sim down sans TTY (code 0) |
| Contrôle final | Aucun listener 7821/7921/7830/7831/8771 ni conteneur `coincoin-s1` |

Les relevés ci-dessous conservent les essais initiaux Docker, refus et deadman.

## S1 : conteneur → relais authentifié → vraie simulation

IP `eth0` redécouverte : **192.0.2.1**. `.env` ignoré configuré avec
`ROBOT_LINK=tcp://192.0.2.1:7830`,
`FRAME_LINK=tcp://192.0.2.1:7831`, `COINCOIN_PORT=8771` et
`COMPOSE_PROJECT_NAME=coincoin-s1`. Le jeton préexistant est resté uniquement
dans `.env` ; il n'est pas reproduit dans ce relevé.

La simulation a été lancée depuis WSL via le script Pollen inchangé, avec
`PATH` comprenant `~/.cargo/bin`, `DUCK_SIM_STATE=~/.cache/ds-s1`,
`DUCK_SIM_PORT=7821`, `DUCK_SIM_FRAME_PORT=7921`, `DUCK_SIM_VIEWER=0`,
`DUCK_SIM_RL=~/projects/microduck/microduck_rl`, scène `backlash`, sans caméra.
Le relais est lié exclusivement à cette IP sur `7830`/`7831`. Le port
`7921` est réservé mais sans listener tant que la caméra est désactivée.

| Essai réel S1 | Résultat |
|---|---|
| Démarrage `duck-sim` | `robot healthy`, boucle 50 Hz, debout, gravité z −1 |
| `docker compose up --build --detach --wait --wait-timeout 60` | Projet `coincoin-s1`, conteneur healthy |
| `curl --fail http://localhost:8771/healthz` | `{"status":"ok"}` |
| `docker compose exec -T coin-coin coincoin-cli move 0.2 0 0 3` | Code 0 ; déplacement projeté avant **0,261 m** ; durée totale CLI **3,955 s**, démarrage et amortissement inclus |
| Odométrie de cette commande | `[0.470568, -0.006700, 0.117554]` → `[0.252392, 0.136667, 0.117339]` m ; déplacement dans le repère du cap initial |
| Vitesse appliquée après amortissement | vx **0,004504**, vy 0, vyaw ≈ 0 ; aucune nouvelle commande de marche |
| `docker compose exec -T coin-coin coincoin-cli raw robot.relax` | Code 1, JSON-RPC **−32601** ; `fallen=false`, `enabled=true`, `homed=true`, `sitting=false` |
| Arrêt du relais pendant `coincoin-cli move 0.2 0 0 5` dans Docker | **`limited_by=["deadman"]` après 0,496 s** ; CLI finit en refus explicite `motion interrupted` |
| Dernier `pytest -m sim --cov-append -s` avec opt-in | **3 passed** : avance **0,162 m**, tour **88,813°**, regard gauche **0,797 rad** |

Pour le deadman, un abonnement **direct** au socket Unix S1, à 50 Hz,
est resté ouvert indépendamment du relais. Le PID du relais (36061 lors
de la mesure) a été certifié par son argv **et son cwd S1**, puis seul
ce PID a reçu SIGTERM après observation d'un `applied.vx > 0,05`.
Au premier état portant `deadman`, la consigne demandée vaut encore
`[0.2,0,0]` et l'EMA appliquée vaut environ `[0.127516,0,0]` : le critère
est bien le **flag deadman en < 1 s**, pas une fausse affirmation de vitesse
physique instantanément nulle. Voir ADR-004.

### Résultats qualité S1

```sh
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
COINCOIN_TEST_SIM=1 uv run pytest -m sim --cov-append -s
```

Ruff et format verts, aucun diagnostic pyright ; **225 passed, 3 skipped**
hors opt-in ; couverture totale **93,99 %**, `governor.py` **95 %** et
`relay/duck_relay.py` **84 %**. Puis **3 passed** contre la simulation
réelle, sans tests sautés. L'option `--cov-append` conserve la mesure
unitaire au lieu de demander à trois tests physiques de couvrir le socle.

Le montage historique `logs/` créé initialement par Docker appartenait à root :
la première tentative web échouait donc avec `PermissionError`.
Réparation limitée à **ce montage S1** via
`docker compose run --rm --no-deps --user root coin-coin chown 1000:1000 /app/logs`,
puis démarrage healthy. Après rebase sur S2/S3, le montage hôte est
`.cache/logs/` ; la cible `/app/logs` est inchangée. Aucune permission ni
configuration globale modifiée.

Les services sont arrêtés à la livraison : Compose S1 supprimé, relais S1
terminé, `DUCK_SIM_STATE=~/.cache/ds-s1 DUCK_SIM_PORT=7821 scripts/duck-sim down`,
aucun listener sur 7821/7921/7830/7831/8771. Les autres sessions et la sim live
n'ont pas été arrêtées.

Le premier `down` attendait dans `sudo systemctl is-active --quiet duck-a`
(fonction Pollen `down_containers`, exécutée même en sim simple). Seuls
ce processus et son script parent, certifiés par PPID/argv/cwd S1, ont été
terminés. Le même script inchangé a ensuite été relancé sans terminal de
saisie : `setsid --wait timeout 45s env DUCK_SIM_STATE=... DUCK_SIM_PORT=7821
DUCK_SIM_FRAME_PORT=7921 .../scripts/duck-sim down </dev/null`. Résultat :
`== down`, code 0 ; `ss` affiche uniquement son en-tête et
`docker ps -a --filter label=com.docker.compose.project=coincoin-s1` est vide.
Aucun mot de passe, privilège ou changement de configuration n'a été requis.

## S0 : plan B validé, sans blocage restant

Le moteur Docker Desktop répond depuis WSL : **29.8.2**, Compose **v5.6.0**.
Le premier téléchargement depuis WSL échouait avec :

```text
fork/exec /usr/bin/docker-credential-desktop.exe: exec format error
```

Le défaut a été résolu pour les mesures en préchargeant les deux images
publiques depuis Docker Windows sur le même moteur. Compose et le spike ont
ensuite tourné **depuis WSL** sans autre changement de configuration.
Le plan A est réellement injoignable ; **B est validé sur `192.0.2.1`**,
avec rejet sans jeton puis écho exact. L'accès HTTP au conteneur fonctionne
depuis WSL et Windows. Aucune simulation n'a été lancée.

| Vérification réelle | Résultat |
|---|---|
| `docker version --format '{{.Server.Version}}'` depuis WSL | `29.8.2` |
| `docker compose version` depuis WSL | `v5.6.0` |
| Disponibilité initiale des ports réservés 8770 / 7899 | Aucun listener |
| Premier pull depuis WSL | Erreur du helper d'identifiants, code 1 |
| `docker pull python:3.12-slim` depuis Windows | Image publique chargée, code 0 |
| `docker pull ghcr.io/astral-sh/uv:0.13.7` depuis Windows | Image publique chargée, code 0 |
| `docker compose up --build --detach --wait --wait-timeout 60` depuis WSL | Service `running healthy`, code 0 |
| `curl http://localhost:8770/healthz` depuis WSL | HTTP 200, `{"status": "ok"}` |
| `Invoke-WebRequest http://localhost:8770/healthz` depuis Windows | HTTP 200, `{"status": "ok"}` |
| Page `/` depuis Windows | HTTP 200, titre « Coin-Coin — hello » |
| Identité du conteneur | `uid=1000(coincoin) gid=1000(coincoin)` |
| A : `host.docker.internal` → WSL loopback 7899 | `OSError: [Errno 101] Network is unreachable` |
| B : conteneur → IP eth0 `192.0.2.1:7899` | Rejet sans jeton, écho authentifié exact, `RESULT=B`, code 0 |
| `ss -ltn 'sport = :7899'` après le spike réel | Aucun listener |
| `docker compose down` avec `COMPOSE_PROJECT_NAME=coincoin-s0` | Conteneur et réseau S0 supprimés |
| Contrôle final des ports 7899 / 8770 et du label Compose S0 | Aucun listener ni conteneur restant |

## Rejouer les mesures

Si les images publiques ne sont pas encore locales et le helper WSL échoue,
les précharger depuis PowerShell Windows :

```powershell
docker pull python:3.12-slim
docker pull ghcr.io/astral-sh/uv:0.13.7
```

Ne copier aucun identifiant et ne modifier ni `~/.docker/config.json`, ni la
configuration WSL. En S0, aucune configuration Docker alternative n'a été utilisée.

Depuis WSL, dans `~/projects/microduck/coin-coin/` :

```sh
test -e .env || cp .env.example .env
mkdir -p .cache/logs
export COINCOIN_PORT=8770 COMPOSE_PROJECT_NAME=coincoin-s0
docker compose up --build --detach --wait --wait-timeout 60
curl --fail --silent --show-error http://localhost:8770/healthz
docker compose exec -T coin-coin id
docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q coin-coin)"
scripts/net_spike.sh
```

Depuis PowerShell Windows :

```powershell
(Invoke-WebRequest http://localhost:8770/healthz -UseBasicParsing).Content
```

Attendus : `/healthz` retourne `{"status": "ok"}` depuis WSL **et** Windows,
`id` montre UID/GID 1000, état Docker `healthy`, puis le spike confirme l'écho
exact. Sur l'environnement mesuré, il retourne `RESULT=B` ; une machine
différente peut retenir A. Consigner toute nouvelle mesure sans écrire de jeton.

Nettoyer uniquement les ressources S0 :

```sh
COINCOIN_PORT=8770 COMPOSE_PROJECT_NAME=coincoin-s0 docker compose down
ss -ltn 'sport = :7899 or sport = :8770'
```

## Chemins mesurés et configuration retenue

**A — échec réel.** Écho dans WSL sur `127.0.0.1:7899`, client dans
`python:3.12-slim` avec
`--add-host=host.docker.internal:host-gateway` : connexion impossible,
`Network is unreachable`. Ce chemin n'est pas retenu sur cette machine.

**B — retenu après échec réseau réel de A.** Le script découvre l'adresse
IPv4 `eth0` WSL, arrête son écho A et relance sur cette seule adresse,
toujours sur 7899. Un jeton généré en mémoire est exigé sur la première ligne.
Le client vérifie d'abord qu'une connexion sans jeton est rejetée, puis l'écho
authentifié. L'adresse peut changer après redémarrage WSL ; un futur jeton
persistant devra rester exclusivement dans `.env`. Les sessions robot doivent
lier leurs relais à cette IP, configurer `ROBOT_LINK` et `FRAME_LINK` avec
leurs ports réservés, et imposer le jeton avant toute requête.
Les défauts de `Settings`/`.env.example` ne sont pas une validation du plan A.

Le script vérifie la disponibilité du moteur et de l'image **avant** de
démarrer l'écho. Il piège les sorties normales, erreurs, SIGINT et SIGTERM,
arrête uniquement le PID qu'il a créé et retire ses fichiers temporaires.
Un port déjà occupé provoque une erreur explicite, sans arrêter son propriétaire.

## Ce que prouvent les tests unitaires

Les commandes Docker et `ip` sont remplacées par des faux locaux dans
`tests/unit/test_net_spike.py`. Ces tests vérifient le flux du script,
l'écho exact, l'authentification B et le nettoyage sur succès/port occupé.
Ils **ne remplacent pas** les mesures Docker Desktop/WSL ni le test Windows.
