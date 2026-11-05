# WebRTC LAN : S9

**Intégration pré-réception autorisée sous réserves, 2026-11-05 :**
user demande « finissons d'intégrer s9 et tout le reste, tout ce qu'on
peut avant d'avoir le robot ». Le coordonnateur prépare l'intégration ;
aucune fusion n'est revendiquée ici. TCP reste le transport par défaut.
Cette décision ne vaut ni validation du critère sim ni `agent-done` strict.
Résultats et nettoyage ont été contre-vérifiés ; les deux échecs de marche
restent ouverts sans cause imputée au transport.

Après revue de
`2fc23f9` sur `main=8de5e3e`, l'unique suite réelle autorisée avec SIMWALK
fait **6 réussites, 2 échecs, zéro saut** (8 tests, code retour 1).
Les marches S1/S9 échouent à **0,13731089587383769 m** et
**0,14548907224457483 m**, commande de 2 s et seuil strict > 0,15 m inchangés.
Tours, regards, caméra et métriques passent. Aucun nouvel essai ni correctif
de transport ; ressources S9 nettoyées. Le critère sim reste ouvert.
Preuves complètes : [suite réelle unique](evidence/s9-final-sim-20261105/README.md).
Toutes les campagnes antérieures restent ci-dessous. Les preuves à 3 s
ne valent pas acceptation ; durées et seuils ne sont pas assouplis.

## Revalidation unique après SIMWALK — 2026-11-05

Commande exécutée une seule fois à 20:49:07 UTC, sur `2fc23f9` :

```sh
ROBOT_LINK=webrtc://192.0.2.1:8443 COINCOIN_TEST_SIM=1 \
  uv run pytest --no-cov -m sim -s -rs
```

**6 passed, 2 failed, 551 deselected in 58.74s**, aucun saut, code retour
réel **1**. Les fixtures S1 et S9 utilisent toutes deux WebRTC ; il ne s'agit
pas d'une comparaison TCP/WebRTC. Le helper SIMWALK partagé lit l'odométrie
fraîche/stabilisée avant et après `move(0.2,0,0,2)` ; les deux déplacements
restent sous le seuil. Aucune cause racine n'est démontrée par cette suite.

| Observation | Scénario S1 partagé | Scénario S9 |
|---|---|---|
| Marche | 0,13731089587383769 m, échec | 0,14548907224457483 m, échec |
| Tour | 92,977° | 94,815° |
| Regard gauche après 0,6 s, maintien 1 s | 0,337 rad | 0,298 rad |

Caméra : **640x360**, JPEG préparé **13 220 octets**, vérification du fichier
puis suppression réussies ; assertions S7 préservées.
RTT de 25 appels `robot.health` : médiane **0,56 ms**, p95 **0,81 ms**.
Cadence nominale d'émission 20 Hz ; cadence réellement observée **19,44 Hz**,
**50 updates sur 2,520 s**, intervalle médian **42,59 ms**, gigue absolue
par rapport à 50 ms p95 **28,99 ms**, `deadman_during_move=False`.
Ces transitions sont vues dans les états publiés par robotd, pas des
horodatages d'arrivée internes à robotd. La durée physique individuelle des
marches n'est pas instrumentée ici ; aucun nominal n'est présenté comme
une mesure. Les positions complètes de la marche S1 sont dans la sortie anonymisée.

Le surveillant indépendant a seulement lu `{"op":"read"}` sur le corps S9 :
237 échantillons, hauteur du tronc **0,113279–0,121467 m**, gravité z
au plus **-0,997557**, aucun déclenchement de danger (seuils d'arrêt :
hauteur <= 0,085 m ou gravité z >= -0,8). Fenêtre réelle **59,189407 s**,
simulée **59,220000 s** ; ce n'est pas une mesure synchronisée des marches.

Verrou acquis atomiquement avec propriétaire S9/date ; ports recontrôlés
libres, scène libre `backlash`, caméra `a`, viewer 0, état `ds-s9`,
ports corps/images 7911/8001. Les variables GStreamer n'ont été définies que
dans l'environnement de ce lancement. Aucun relais TCP, Compose/Docker,
OpenAI/OpenClaw ou serveur web Coin-Coin ; seuls les ports mediad 8443/8080
nécessaires à la caméra ont été ouverts.
Après inspection des PID/argv individuels, arrêt sans TTY et sans privilèges :

```sh
DUCK_SIM_STATE=/home/user/.cache/ds-s9 \
DUCK_SIM_PORT=7911 DUCK_SIM_FRAME_PORT=8001 \
  setsid --wait timeout 90 setpriv --no-new-privs \
  /home/user/projects/microduck/microduck/scripts/duck-sim down </dev/null
```

Retour 0, aucun PID démarré survivant, aucun pidfile, ports
7911/8001/7930/7931/8443/8080/8779 libres et seul le verrou S9 retiré.
SHA-256 du script Pollen identique avant/après ; aucune source Pollen modifiée.
Q10 conserve ses preuves historiques ; aucun nouveau contrôle Docker autorisé.

## Signalisation et transport

`WebRtcLink` traduit `webrtc://host:8443` en WebSocket `ws://host:8443` et
reprend la séquence de la console LAN de `mediad`: `welcome`, `list`,
`startSession` pour le producteur vidéo, puis réception de son SDP `offer` et
envoi de l'`answer`. Les messages `peer` transportent SDP et ICE candidates.
Les candidates reçues avant le SDP distant sont conservées jusqu'à l'installation
de l'offre.

Le client attend à la fois l'état ICE `connected` et l'ouverture de la
datachannel fiable et ordonnée `control`. Chaque message JSON-RPC reçu sur
cette channel devient une ligne de flux pour le `JsonRpcConnection` de S1 ;
les appels, notifications, identifiants, erreurs et `robot.state` gardent donc
les contrats existants. `robot.stop` demeure une requête avec identifiant.
Un `robot.move` n'est admis que si `bufferedAmount == 0`. Dès qu'un message
est tamponné, les mouvements suivants remplacent un unique mouvement différé,
envoyé au retour à zéro. Un seuil de 16 KiB admettait auparavant des centaines
de messages obsolètes dans aiortc : ce seuil est supprimé.

STOP invalide synchroniquement le différé avant son envoi. Un mouvement
explicitement demandé après STOP reste autorisé et conserve la politique du
plus récent. Les tests reproduisent les deux ordres reprise/vidage du tampon,
y compris via `Governor` et `JsonRpcConnection`.

**Limite du canal fiable ordonné :** un message déjà admis dans aiortc/SCTP
ne peut être ni retiré ni dépassé. La nouvelle politique n'admet plus de
mouvement tant que le tampon visible par `bufferedAmount` n'est pas vide ;
elle ne borne pas les messages déjà transmis à SCTP ou en vol et ne garantit
pas la livraison de STOP sur un canal bloqué. Le gouverneur exige son
acquittement en 100 ms et renvoie
un échec explicite (`robot request timed out`) sinon ; ce n'est pas une
garantie d'arrêt physique. Le deadman robot reste le filet de sécurité en cas
de perte du transport. Les régressions vérifient l'absence de reprise différée
après STOP et le résultat d'échec si le tampon ne se vide pas.

La piste vidéo conserve la dernière trame décodée et son heure de réception ;
`frame()` l'encode en JPEG qualité 80 à la demande. Les pertes de peer/datachannel
déclenchent un nettoyage puis une reconnexion exponentielle avec gigue, plafonnée
à 30 secondes.

La signalisation LAN de `mediad` n'est pas authentifiée. N'exposer ni son port
8443 ni la caméra hors du LAN.

### Filtrage S1 avant transmission

Depuis la correction du 2026-11-05, la datachannel applique la liste
`ROBOT_METHODS` de S1 avant `send`, pour les appels et les notifications.
La liste a une source unique stdlib dans `coincoin.robot.methods`, importée
aussi par le relais autonome ; ses méthodes permises n'ont pas changé.
`coincoin-cli raw robot.init` échoue avec `-32601` et
`refusee par WebRtcLink (S1 method policy)` sans transmettre la requête.
Le journal ne contient que le nom de méthode refusé, jamais ses paramètres.

Les tests sur le pair local couvrent les familles interdites, les appels et
notifications, la CLI utilisant réellement la fabrique WebRTC, toutes les
méthodes permises et la réutilisation du lien après refus. Une régression
CLI/TcpLink/relais vérifie le refus S1 historique et un appel permis. Le
relais reste exécutable avec Python système sans dépendances (`-I -S`,
`--help`). Aucun appel interdit n'a été envoyé à une sim ou un robot.
Le filtre ne valide pas les paramètres des méthodes permises et ne remplace
pas le gouverneur ; `raw` reste réservé au diagnostic manuel.

## Procédure de simulation S9

Les ressources exclusives de cette session sont : état
`~/.cache/ds-s9`, corps 7911, images 8001, mediad 8443/8080, HTTP 8779 et projet
Compose `coincoin-s9`. Aucun relais TCP n'est requis pour `WebRtcLink`.

Avant le démarrage caméra :

1. Redécouvrir l'IPv4 de `eth0`; mettre à jour `ROBOT_LINK` et `FRAME_LINK`
   dans le `.env` de la session uniquement si elle a changé. Ne pas afficher
   le contenu complet de `.env`.
2. Prendre le verrou par `mkdir ~/.cache/coin-coin-locks/sim-camera`; inscrire
   le propriétaire `S9` et la date dans son fichier `owner`. Si le verrou existe,
   ne pas toucher à sa caméra : attendre au plus deux heures, en sondant toutes
   les 60 secondes.
3. Vérifier que les ports TCP 8443 et 8080 sont libres. Si l'un est occupé,
   ne pas arrêter son propriétaire.
4. Lancer la simulation caméra avec les valeurs affectées, puis libérer
   uniquement le verrou S9 après l'arrêt et la vérification des ports.

Exemple depuis le worktree S9 :

```sh
cd ~/projects/microduck/coin-coin-wt/S9
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export UV_CACHE_DIR="$PWD/.cache/uv"
export PYRIGHT_PYTHON_CACHE_DIR="$PWD/.cache/pyright"
export DUCK_SIM_STATE="$HOME/.cache/ds-s9"
export DUCK_SIM_PORT=7911 DUCK_SIM_FRAME_PORT=8001 DUCK_SIM_VIEWER=0
export DUCK_SIM_RL="$HOME/projects/microduck/microduck_rl"
export DUCK_SIM_CAMERAS=a
export GST_PLUGIN_PATH=/opt/gst-plugins-rs/lib/gstreamer-1.0
export GST_PLUGIN_FEATURE_RANK=nvh264enc:NONE
```

La configuration `.env` de cette session choisit `ROBOT_LINK=webrtc://<eth0>:8443`,
`FRAME_LINK=tcp://<eth0>:7931`, `COINCOIN_PORT=8779` et
`COMPOSE_PROJECT_NAME=coincoin-s9`. Le `RELAY_TOKEN` existant reste secret et
n'est ni requis ni utilisé par le transport WebRTC.

## Mesures et Q10

### Intégration hors sim sur `main=8de5e3e`

Le rebase autorisé conserve les six commits S9 et toutes les preuves
historiques ci-dessous, ainsi que les ajouts S4-S8 et SIMWALK aux documents.
Les conflits mécaniques concernaient README/CHANGELOG/ADR, le lock, le test
de marche et le fixture global. La politique de mesure SIMWALK de
`cc30372` est conservée intégralement : même helper
`tests.sim.test_sim_motion._wait_for_settled_odom`, désormais typé
`RobotLink`, importé par la marche S9 au lieu d'une copie. Il attend au
plus 1 s, un état reçu depuis <= 200 ms, `abs(applied) < 0,01` par axe et
deux écarts XY consécutifs <= 5 mm entre mises à jour distinctes, avant et
après `move(0.2,0,0,2)`. Le seuil strict > 0,15 m ne change pas. Il s'agit
du déplacement odométrique stabilisé incluant la décélération post-STOP,
pas d'une pose interpolée à t=2 s. Tour et regard restent inchangés.

Le test caméra S7 utilise le même chargement `.env` puis override explicite
`ROBOT_LINK`, et la fabrique. Avec WebRTC il se connecte avant de demander
`frame()` ; TCP conserve la capture indépendante par `FRAME_LINK`.
Les assertions S7 (fraîcheur, dimensions, poids JPEG, préparation et
suppression du fichier temporaire) sont conservées. En opt-in, une
indisponibilité provoque une erreur explicite plutôt qu'un saut.
Le fixture global conserve simultanément l'isolation
`app_module.FEATURES=[]` de main et la préservation ciblée de `ROBOT_LINK`
pour les seuls tests sim opt-in. Les autres paramètres et secrets restent
isolés.

`RobotFeature` utilise désormais `create_robot_link` pour ses connexions
non injectées. La fabrique transmet `Clock`, `on_up` et `on_down` aux deux
transports, ainsi que `frame_url`/token à TCP ; un lien fourni explicitement
reste prioritaire, même s'il est faux en contexte booléen. La gestion S5
des tâches, la reconnexion, les outils S7 et le runtime vocal S6 restent
inchangés. Le lock combine `httpx` de main et les dépendances WebRTC ;
`aioice` reste à **0.12.1**, comme avant rebase, sans mise à jour transport.

Les nouveaux tests hors sim utilisent exclusivement des données synthétiques
et des pairs locaux : vrais `RobotFeature`/TcpLink/relais authentifié ou
`RobotFeature`/WebRtcLink/pair mediad, callbacks initiaux puis perte/reprise,
annonce unique de perte, cleanup des tâches et de la liaison, et priorité
d'un lien injecté. Les tests de mesure vérifient les mises à jour distinctes,
le rejet des états périmés/en décélération, la remise à zéro après un saut
XY, le timeout, le partage du helper et l'appel exact de 2 s. Les tests de
caméra couvrent cible divergente `.env`/override, choix TCP/WebRTC, connexion
conditionnelle, erreurs de connexion/capture et fermeture, sans caméra réelle.

Aucun code de `webrtc_link.py`, `tcp_link.py`, `jsonrpc.py` ou Pollen n'a
été modifié par cette intégration. Aucune sim, snapshot réel, commande de
robot, service, relais attribué, verrou, Docker, appel OpenAI/OpenClaw ou
configuration GStreamer n'a été lancé/changé. Les faux pairs des tests
restent locaux, sans accès externe. Les preuves Docker/Q10 ci-dessous sont
historiques, **pas** une revalidation Docker sur la nouvelle base.

Qualité complète exécutée **en série**, hors opt-in :

```sh
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
"$PYRIGHT_PYTHON_CACHE_DIR/pyright-python/nodeenv/bin/node" --test tests/js/play_worklet.test.mjs
```

Résultat : Ruff vert, **114 fichiers** au format attendu, Pyright sans
diagnostic, **551 passed, 8 skipped**, couverture **91,20 %** ; Node
**6/6**, aucun saut. Les huit sauts sont exclusivement les tests sim hors
opt-in ; les deux tests Silero réels et les régressions S5/S6/S7,
STOP/filtrage/cible/secrets passent. Les **21 cas WebRTC locaux** incluent
les trois nouvelles régressions de `RobotFeature` (TCP, WebRTC, injection).
Le log complet est archivé dans les artefacts de session sous
`s9-integrated-offline-quality.log`. La comparaison des locks ne montre
aucun changement de version des paquets existant sur main ou l'ancien S9.
Une comparaison AST confirme le helper SIMWALK identique à main, à la seule
annotation `RobotLink` près.

La suite réelle attend désormais **8 tests sim** : trois scénarios partagés
S1, une capture S7 et quatre tests S9. Elle n'a pas été exécutée à cette
étape ; le critère reste ouvert jusqu'à autorisation puis observation d'une
unique commande complète sans saut.

### Reprise autorisée du 2026-11-05

Correctif de filtrage livré séparément dans `f61ac1a`, sur
`session/S9`, base inchangée `main=0597380`. Qualité complète :
`uv run ruff check . && uv run ruff format --check . && uv run pyright &&
uv run pytest -q` : **355 passed, 7 skipped hors opt-in**, couverture
**91,52 %**, Ruff/format/Pyright verts. Le ciblage WebRTC/relais/CLI
donne **108 passed** ; 18 cas WebRTC locaux passent dans la suite complète.
TCP et JSON-RPC S1 sont inchangés.

Une nouvelle campagne bornée à quatre marches, ordre fixé TCP / WebRTC /
WebRTC / TCP, appelle la fonction S1 existante sans modification, avec
`move(0.2,0,0,2)` et mesure après 0,3 s. Même sim caméra S9, scène
`backlash`, aucun Compose pendant les marches, préparation STOP + 2 s
puis STOP + 0,3 s, enabled/homed/walk/applied<0,01/sécurité saine.
Le diagnostic ajoute uniquement des lectures `{"op":"read"}` du corps sur
7911 ; aucune écriture de cible, gain ou torque par l'observateur. Il conserve
le `sim_time`, le vrai `trunk` MuJoCo, l'état robot et les envois RPC.

| Ordre | Transport | Odométrie au contrôle (m) | > 0,15 m | Déplacement physique projeté (m) | Moves | Premier move → STOP (s) | Cadence envoyée (Hz) | Temps simulé / temps réel |
|---|---|---:|---|---:|---:|---:|---:|---:|
| 1 | TCP | 0,161 (arrondi) | réussite | indisponible | indisponible | indisponible | indisponible | indisponible |
| 2 | WebRTC | 0,120182 | échec | 0,145790 | 40 | 2,000919 | 19,9918 | 1,0036 |
| 3 | WebRTC | 0,134663 | échec | 0,173432 | 40 | 2,000901 | 19,9982 | 0,9909 |
| 4 | TCP | 0,144034 | échec | 0,164727 | 40 | 2,000626 | 19,9877 | 0,9956 |

L'essai 1 a passé l'assertion, puis le post-traitement diagnostique a
échoué sur `KeyError: limited_by` : ce champ est omis quand il est vide
par le contrat de sérialisation Pollen. Seuls la sortie de test arrondie
et cet incident sont conservés ; la télémétrie de cet essai a été perdue.
Le diagnostic a été corrigé puis a exécuté **uniquement les trois essais
restants**, sans rejouer le premier ni ajouter d'essai favorable.

Sur les trois essais instrumentés, la consigne active observée dure
2,002 / 1,939 / 2,021 s à la résolution de l'abonnement 10 Hz ; l'intégrale
trapézoïdale de `applied.vx` sur les états vaut 0,3979 / 0,3988 / 0,4000 m.
Cette intégrale est une **consigne filtrée**, pas une vitesse physique.
Les intervalles d'émission maximaux sont 50,96 / 50,93 / 50,82 ms,
aucun deadman actif ni danger observé. Boucle finale 49,998–50,049 Hz,
compteur missed stable à 1. Hauteur physique initiale 0,11548–0,11563 m,
gravité z -0,99995 à -0,99980 ; yaw initial 0,050 / -0,210 / -0,366 rad.
Les positions, phases et graines ne sont pas réinitialisées.

**Limites et conclusion.** Le temps simulé avance approximativement au
temps réel (2,28–2,30 s physiques pour 2,29–2,30 s réelles entre lectures).
Les échecs ne montrent donc pas de ralentissement massif de la physique
ou de raccourcissement des 2 s de commande. Les écarts physique/odométrie
observés valent environ 2–4 cm ; l'essai 2 reste aussi sous le seuil physique.
Ce n'est pas une preuve d'erreur d'odométrie isolée : les snapshots ne sont
pas synchrones. L'âge de réception des états aux contrôles vaut
53 / 68 / 96 ms, celui des lectures physiques 12 / 3 / 12 ms ; les RTT de
lecture sont < 0,70 ms. Un rapprochement temporel approximatif hors ligne,
par l'offset monotone minimal état/réception puis le capteur le plus proche,
donne 0,144810 / 0,173107 / 0,161372 m physiques. L'alignement initial
n'est pas exact, et les latences ne sont pas des horodatages internes.
L'odométrie Pollen est un estimateur de contact (pas le `trunk` MuJoCo) ;
aucune cause racine de locomotion/estimation ni correction S9 légitime
n'est démontrée. Aucun changement de Pollen, de paramètres de politique,
de fixture, de durée, de seuil ou d'attente de mesure.

La campagne est suivie d'une **unique** commande complète, sans sélection :

```sh
ROBOT_LINK=webrtc://192.0.2.1:8443 COINCOIN_TEST_SIM=1 \
  uv run pytest --no-cov -m sim -s -rs
```

Résultat : **4 passed, 3 failed, 354 deselected, zéro saut, 53,22 s**
(avant l'ajout de la dernière régression CLI/TCP hors sim).
S1 : marche **0,128577 m** (échec), tour **89,584°**, regard **0,310 rad**.
S9 : marche **0,125208 m** (échec), tour **94,625°**, regard
**0,145847 rad** (échec au seuil inchangé > 0,2 après 0,6 s).
Ce nouvel échec de regard est conservé, sans nouvelle tentative ciblée
ni conclusion causale. RTT health p50/p95 **0,90/1,72 ms** ; cadence
observée **19,75 Hz**, **50 updates / 2,481 s**, nominal 20 Hz,
intervalle médian **57,02 ms**, gigue absolue p95 **31,63 ms**, aucun
deadman pendant les mouvements mesurés. Critère sim toujours ouvert.

Q10 revalidée avec le correctif : `coincoin-cli raw robot.health` permis
depuis WSL et le bridge Docker reconstruit renvoie `ok:true`, robot healthy.
HTTP `/healthz` répond 200 sur 8779, seul mapping `127.0.0.1:8779->8765/tcp`,
sans host networking ni nouveau mouvement Docker. Aucun appel interdit
n'a été envoyé à la simulation ; les refus restent testés sur faux pair.

Nettoyage : relais fermé dans `finally`, Compose `coincoin-s9` supprimé,
PID S9 74367/74458/74465/74472/74477/74621 inspectés individuellement
avant `setsid --wait timeout 45s setpriv --no-new-privs ... duck-sim down
</dev/null`. Tous disparus ; ports 7911/8001/7930/7931/8443/8080/8779 libres,
aucun conteneur S9 restant. Verrou créé atomiquement, propriétaire S9 daté,
libéré après vérification ; aucune ressource étrangère touchée. Scripts,
JSON anonymisés des essais 2–4, incident de l'essai 1 et journaux pytest sont
préservés dans les artefacts de session `s9-groundtruth-*` et
`s9-resume-{sim,quality}.log`.

### Simulation WSL

`gst-inspect-1.0 webrtcsink` confirma le plugin requis. Sans rangage, la
découverte du codec choisissait `nvh264enc` puis échouait (`Selected preset not
supported`, aucun caps de sortie). `GST_PLUGIN_FEATURE_RANK=nvh264enc:NONE`
sélectionne `x264enc`; `mediad` a alors annoncé la branche H.264, reçu la caméra
à 30 fps et inscrit le producteur. Aucun fichier du dépôt `microduck` ou
`microduck_rl` n'a été changé.

Le premier échange réel atteignait ICE mais échouait au DTLS : l'offreur
`webrtcsink` envoyait un SDP `actpass`; la réponse `active` par défaut d'aiortc
produisait un échec SSL. Les réponses `passive` ont établi le DTLS et le canal
`control`. Le test local vérifie désormais ce rôle.

Commande exécutée après rebase sur `main` `0597380`, avec
`ROBOT_LINK=webrtc://192.0.2.1:8443` dans le `.env` S9, caméra active et
`COINCOIN_TEST_SIM=1` :

```sh
ROBOT_LINK=webrtc://192.0.2.1:8443 COINCOIN_TEST_SIM=1 \
  uv run pytest --no-cov -m sim -s -rs
```

Résultat après correction : **6 passed, 1 failed, 350 deselected, aucun
saut** (53,41 s). L'échec est le test de marche S9 : **0,143831 m**, en
dessous de > 0,15 m. Les trois scénarios S1 et leurs équivalents S9 gardent
strictement les mêmes paramètres : marche de 2 s ; tour 90° ±15° ; regard
maintenu 1 s avec yaw > 0,2 rad contrôlé après 0,6 s.

Le fixture global ne préserve `ROBOT_LINK` que pour un test marqué `sim`
avec `COINCOIN_TEST_SIM=1`. Les fixtures partagées chargent le `.env`, puis
appliquent cet override non secret. Trois régressions sans réseau vérifient
une cible `webrtc://override.invalid:8443` différente du `.env`, l'absence
d'opt-in et les tests hors marqueur ; les secrets restent effacés.

| Mesure réelle | Résultat |
|---|---:|
| `robot.health` RTT | p50 **0,51 ms**, p95 **0,65 ms** |
| Cadence d'émission nominale `robot.move` | **20 Hz** |
| Changements de consigne observés dans `robot.state` | **50 updates** en **2,479 s** |
| Cadence observée `(updates - 1) / (dernier - premier)` | **19,77 Hz** |
| Intervalle observé médian | **42,17 ms** |
| Gigue absolue des intervalles `robot.move` | p95 **29,30 ms** |
| `limited_by` pendant les mouvements | aucun `deadman` |
| S1/WebRTC, avance 2 s | **0,160 m**, réussite |
| S1/WebRTC, tour | **89,645°** |
| S1/WebRTC, regard gauche | yaw **0,352 rad** |
| S9/WebRTC, avance 2 s | **0,143831 m**, échec |
| S9/WebRTC, tour | **89,536°** |
| S9/WebRTC, regard gauche | yaw **0,610 rad** |

La mesure de cadence utilise des consignes variant à chaque émission et
observe leurs transitions dans l'état poussé par `robotd` à 50 Hz, sondé
toutes les 5 ms. C'est une cadence observée via les états reçus, pas un
horodatage interne de réception des RPC. La durée va de la première à la
dernière transition et couvre donc 49 intervalles pour 50 updates. La gigue
est l'écart absolu à la période nominale de 50 ms.

### Investigation de la marche : toutes les répétitions

Dix essais supplémentaires alternent l'appel des deux fonctions de test
existantes, sans changer leurs paramètres/assertions. Même simulation caméra
sur le sol libre `backlash`, Compose arrêté, transport WebRTC dans les deux
cas, connexion/gouverneur recréés à chaque essai. Avant chaque préparation
de fixture (STOP puis 0,3 s), un STOP et 2 s de stabilisation vérifient
`applied < 0,01`, `enabled`, `homed`, mode `walk`, et l'absence de chute ou
prise en main. Le robot reste debout ; la hauteur initiale est de
0,1174–0,1176 m, gravité z de -0,99990 à -0,99965. Position/yaw ne sont pas
réinitialisés : le yaw initial varie de 2,654 à 3,035 rad, la distance est
projetée sur l'orientation initiale de chaque essai. Ce sont des conditions
comparables au repos, pas des états physiques identiques.

| Essai | Distance du scénario à +0,3 s (m) | Résultat > 0,15 m | Envois move | Premier move → STOP (s) | Cadence envoyée (Hz) |
|---|---:|---|---:|---:|---:|
| S1-1 | 0,151 | réussite | 40 | 2,000516 | 19,996 |
| S9-1 | 0,141 | échec | 40 | 2,000809 | 19,996 |
| S1-2 | 0,145 | échec | 40 | 2,000265 | 19,997 |
| S9-2 | 0,145 | échec | 40 | 2,000838 | 19,992 |
| S1-3 | 0,140 | échec | 40 | 2,000548 | 19,993 |
| S9-3 | 0,133 | échec | 40 | 2,000358 | 19,995 |
| S1-4 | 0,135 | échec | 40 | 2,000642 | 19,992 |
| S9-4 | 0,158 | réussite | 40 | 2,000581 | 19,998 |
| S1-5 | 0,121 | échec | 40 | 2,000897 | 19,991 |
| S9-5 | 0,141 | échec | 40 | 2,000070 | 19,996 |

Instrumentation au véritable `RTCDataChannel.send` : 40 messages par essai,
premier→dernier envoi 1,9502–1,9509 s, intervalle maximal 51,143 ms,
`bufferedAmount` nul avant chaque envoi. Le STOP est envoyé après 2,000–2,001 s.
La boucle robot reste à 49,959–50,029 Hz, son compteur `missed` reste à 1.
Les quatre derniers essais observent aussi les transitions de consigne dans
`robot.state` (abonnement de fixture inchangé à 10 Hz) : durée active
**1,982 / 2,020 / 2,040 / 1,980 s**, résolution liée à l'abonnement ; aucun
deadman. Les consignes finales sont nulles, aucun état dangereux observé.

**Conclusion bornée :** pas de troncature de durée, perte d'émission,
backlog client ou arrêt watchdog mis en évidence dans ces essais. Le même
scénario échoue aussi sous le nom S1 et peut réussir sous le nom S9.
Les observations sont compatibles avec une variabilité de locomotion/phase
de la simulation ; elles ne prouvent pas son mécanisme ni n'excluent toute
cause de transport. Aucune cause logicielle corrigeable n'est démontrée ;
le seuil reste non fiable. Aucun changement de firmware, politique, durée,
seuil ou attente de mesure pour obtenir une passe favorable.

### Seconde investigation : transports TCP et WebRTC réels

Comparaison supplémentaire autorisée, sans changement logiciel, sur le
commit `d0a6643` et la base `0597380`. Une nouvelle simulation caméra S9
utilise les mêmes scène `backlash`, ports 7911/8001, `mediad` 8443/8080,
rang logiciel et verrou exclusif. Aucun Compose n'est lancé. Un relais
`DuckRelay` de S1 est lancé dans le processus du diagnostic sur
`192.0.2.1:7930/7931`, pointant uniquement sur les sockets de `ds-s9`.
Le jeton vient du `.env`, sans affichage ni argument de processus.
`TcpLink` joint ce relais ; `WebRtcLink` joint réellement `mediad` sur 8443.
Les dix essais précédents comparaient les fonctions S1/S9 **toutes deux
sur WebRTC** ; seuls les huit essais ci-dessous comparent les transports.

Ordre préétabli équilibré : TCP, WebRTC, WebRTC, TCP, TCP, WebRTC, WebRTC,
TCP. Chaque essai recrée connexion et gouverneur, exécute STOP + 2 s de
repos, vérifie `enabled`, `homed`, mode `walk`, `applied < 0,01`, sécurité
saine, puis la préparation de fixture STOP + 0,3 s. La fonction de test S1
existante est appelée **sans modification**, avec le tuple
`(governor, link, clock)` : `move(0.2,0,0,2)`, attente de fin, attente 0,3 s,
distance projetée > 0,15 m et consigne nulle. Le clock diagnostique capture
l'état exactement au retour de cette attente de 0,3 s : contrairement aux
premiers diagnostics, les distances brutes ci-dessous ne sont pas relues
après l'attente supplémentaire d'amortissement.

| Ordre | Transport | Distance au contrôle (m) | > 0,15 m | Moves | Premier move → STOP (s) | Cadence envoyée (Hz) | Consigne active observée (s) |
|---|---|---:|---|---:|---:|---:|---:|
| 1 | TCP | 0,150465 | réussite | 40 | 2,000930 | 19,990 | 2,003 |
| 2 | WebRTC | 0,150838 | réussite | 40 | 2,000309 | 19,988 | 1,959 |
| 3 | WebRTC | 0,143790 | échec | 40 | 2,001096 | 19,993 | 2,001 |
| 4 | TCP | 0,151537 | réussite | 40 | 2,000201 | 19,991 | 1,997 |
| 5 | TCP | 0,170451 | réussite | 40 | 2,000389 | 19,989 | 1,997 |
| 6 | WebRTC | 0,159113 | réussite | 40 | 2,000627 | 19,997 | 2,077 |
| 7 | WebRTC | 0,161683 | réussite | 40 | 2,000307 | 19,992 | 2,022 |
| 8 | TCP | 0,143836 | échec | 40 | 2,000369 | 19,991 | 2,078 |

Instrumentation identique au niveau de `JsonRpcConnection` : horodatage de
`writer.write` pour les notifications `move` et la requête STOP. C'est une
cadence d'émission au transport, pas une mesure d'arrivée réseau. L'intervalle
maximal d'émission est de 50,863–50,968 ms en TCP, de 50,896–70,400 ms en
WebRTC (le maximum de 70,400 ms concerne l'essai 7, qui passe). Les états
`robot.state` sont conservés sans changement de fréquence de fixture
(10 Hz), avec leur horodatage robot, la consigne et l'EMA appliquée. Les
durées actives observées ont cette résolution d'abonnement ; elles ne
mesurent pas la durée interne d'application avec précision milliseconde.
Le pic de `applied.vx` atteint environ 0,2 pour chaque essai, aucun
`deadman` pendant la consigne active, consigne finale nulle. Aucun état de
chute/prise en main ou risque de collision observé.

Conditions initiales comparables au repos, mais **non identiques** : hauteur
0,11707–0,11783 m, gravité z -0,99989 à -0,99967, yaw de 0,299 à -0,893 rad ;
pas de reset de position, phase de politique ou graine physique entre essais.
Le coût de connexion WebRTC allonge aussi le temps entre deux essais.
La boucle robot en fin d'essai varie de 48,985 à 50,051 Hz, compteur
`missed` à zéro sur les huit essais. Pas de changement de code Pollen ou TCP.

**Résultat :** TCP **3/4 réussites**, plage 0,143836–0,170451 m ; WebRTC
**3/4 réussites**, plage 0,143790–0,161683 m. Le critère strict échoue dans
les deux transports malgré les durées/cadences correctes ; cette petite
série ne démontre ni une régression propre à WebRTC ni l'équivalence
statistique des transports. Aucune cause logicielle démontrée à corriger
dans S9. Le critère d'acceptation reste ouvert, sans nouvelle tentative
sélectionnée ou dérogation. Les huit mesures, états et script diagnostique
sont préservés dans les artefacts de session.

Une commande `coincoin-cli status` depuis WSL a aussi renvoyé un état debout,
mode `walk`, activé et healthy.

### Q10 : WSL et Docker Desktop

Après les corrections de revue, `coincoin-cli status` depuis WSL a établi
la session WebRTC et reçu `robot.health`. Docker Engine **29.8.2**,
Compose **5.6.0**. Le bridge Docker
par défaut a joint `192.0.2.1:8443`, établi WebRTC, puis exécuté avec
succès `coincoin-cli status` et `coincoin-cli move 0.2 0 0 2`. `/healthz`
répond HTTP **200** sur le port affecté **8779**. Le mouvement de 2 secondes
avance de **0,128 m** : réussite du contrôle réseau, mais pas preuve du
critère de distance > 0,15 m.

Le premier démarrage Compose échouait parce que le montage local `.cache/logs`
était `root:root`; le répertoire de ce worktree S9 seulement a été corrigé en
UID/GID 1000 puis le service est devenu healthy. Le mapping vérifié est
`127.0.0.1:8779->8765/tcp`; aucun port hôte 8765 n'est ouvert. Le bridge par
défaut passe : **`network_mode: host` n'est pas nécessaire et n'a pas été
essayé**.

**Décision Q10 :** conserver le bridge Docker par défaut; les liens WebRTC
fonctionnent depuis WSL et le conteneur sur l'IP `eth0`. L'IP doit être
redécouverte après un redémarrage WSL. Aucun jeton n'est requis pour le lien
WebRTC LAN; la signalisation reste non authentifiée et ne doit pas être
exposée hors du LAN.

### Nettoyage

Compose `coincoin-s9` a été arrêté; la simulation est arrêtée avec le script
Pollen en mode sans TTY et `setpriv --no-new-privs`. Les PID de chaque daemon
ont été vérifiés dans leur état S9 et les ports 7911, 8001, 8443, 8080 et 8779
libérés. Seul le verrou caméra possédé par S9 a été supprimé.
Après la comparaison TCP/WebRTC, les six PID de la nouvelle simulation
(52648, 52727, 52734, 52743, 52757, 52977) ont été inspectés puis arrêtés
par le même `down` protégé. Le relais du diagnostic a été fermé dans son
`finally` ; absence de listeners 7930/7931 et de tous les ports S9 vérifiée.
Le verrou portant le propriétaire S9 a ensuite été libéré.
