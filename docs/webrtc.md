# WebRTC LAN : S9

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
`robot.move` n'est pas mis en file si le tampon de la channel dépasse 16 KiB.

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

Commande exécutée avec `ROBOT_LINK=webrtc://192.0.2.1:8443` dans le `.env`
S9, caméra active et `COINCOIN_TEST_SIM=1` :

```sh
uv run pytest --no-cov -m sim -s -rs
```

Résultat : **7 passed, 309 deselected, aucun saut**. Les trois tests S1 et les
quatre tests S9 ont tous utilisé WebRTC, y compris les anciens scénarios de
mouvement du fichier `test_sim_motion.py`. Le fixture global efface les
variables de configuration et désactive la source `.env` de `Settings` ; les
fixtures sim reconstruisent toutefois explicitement `Settings` à partir du
`.env` du worktree, puis passent `robot_link` à la fabrique. Le réglage
`webrtc://` a donc été exercé par les anciens tests S1, sans modification de
leur marqueur ni saut de connexion.

| Mesure réelle | Résultat |
|---|---:|
| `robot.health` RTT | p50 **0,57 ms**, p95 **0,76 ms** |
| `robot.move` observé par `robotd` | **20 Hz** ; intervalle p50 **42,36 ms** |
| Gigue absolue des intervalles `robot.move` | p95 **33,03 ms** |
| `limited_by` pendant les mouvements | aucun `deadman` |
| S1/WebRTC, avance 3 s | **0,216 m** |
| S1/WebRTC, tour | **90,735°** |
| S1/WebRTC, regard gauche | yaw **0,294 rad** |
| S9/WebRTC, avance 3 s | **0,217 m** |
| S9/WebRTC, tour | **92,854°** |
| S9/WebRTC, regard gauche | yaw **0,246 rad** |

Une commande `coincoin-cli status` depuis WSL a aussi renvoyé un état debout,
mode `walk`, activé et healthy.

### Q10 : WSL et Docker Desktop

Depuis WSL, `coincoin-cli status` a établi la session WebRTC et reçu
`robot.health`. Docker Engine **29.8.2**, Compose **5.6.0**. Le bridge Docker
par défaut a joint `192.0.2.1:8443`, établi WebRTC, puis exécuté avec
succès `coincoin-cli status` et `coincoin-cli move 0.1 0 0 1`. `/healthz`
répond HTTP **200** sur le port affecté **8779**. Une commande Docker de
3 secondes `move 0.2 0 0 3` a avancé de **0,211 m**.

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
