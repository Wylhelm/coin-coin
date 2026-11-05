# WebRTC LAN : S9

**Blocage d'acceptation après correction de revue :** la marche stricte
`move(0.2, 0, 0, 2)` n'atteint pas fiablement > 0,15 m. La commande complète
fait 6 réussites et 1 échec sans saut ; dix répétitions contrôlées montrent
2 réussites et 8 échecs. Les preuves antérieures à 3 s sont retirées comme
preuves d'acceptation. Durées et seuils ne sont pas assouplis.

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
