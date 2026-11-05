# Réseau Coin-Coin — relevés S0 / S1 du 2026-11-04

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
