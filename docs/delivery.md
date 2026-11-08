# Livraison assemblée avant réception

> Les chemins, identités et adresses réseau des relevés historiques ont été
> anonymisés pour la publication. Les adresses `192.0.2.x` sont des exemples
> réservés à la documentation et doivent être remplacées.

## État de référence

Procédure depuis `~/projects/microduck/coin-coin`, branche `main`, base intégrée
`e7cb24f` du 2026-11-05. Les worktrees S1–S9 servent à conserver l'historique,
pas à lancer cette livraison. S0–S8 sont intégrées côté agent ; leurs critères
humains restent ouverts. S9 est intégrée **sous réserves**, TCP reste le défaut.
S10 n'est pas commencée : cette procédure ne vise **jamais le robot réel**.

La dernière suite sim S9 conservée donne **6 réussites, 2 échecs, zéro saut**,
code 1. Les marches S1/S9 mesurent 0,137311/0,145489 m, sous le seuil strict
> 0,15 m pour `move(0.2, 0, 0, 2)`. Aucun seuil n'est assoupli et aucune
cause n'est attribuée au transport. Voir les
[preuves anonymisées](evidence/s9-final-sim-20261105/README.md) et
[les limites WebRTC/Q10](webrtc.md). L'intégration n'est ni un passage du
critère sim ni un statut strict `agent-done` S9.

**État de cette préparation : Docker reste arrêté.** Aucun service, simulation,
snapshot, appel OpenAI/OpenClaw ou commande robot n'est exécuté pour cette
livraison documentaire. Les commandes de lancement ci-dessous sont réservées
à un **futur créneau explicitement autorisé par user**, pas une preuve
d'exécution. Ne pas reprendre les anciennes IP ou ressources des worktrees.

## Actifs locaux, hors Git

Git ne transporte ni `.env`, ni modèles, ni banque sonore, ni corpus personnels.
Depuis WSL, commencer dans le clone durable :

```sh
cd ~/projects/microduck/coin-coin
test -f .env
test -s assets/models/silero_vad.onnx
for lang in fr en-us; do
    test -s "assets/models/vosk-model-small-$lang/am/final.mdl" || exit 1
    test -s "assets/models/vosk-model-small-$lang/graph/HCLr.fst" || exit 1
    test -s "assets/models/vosk-model-small-$lang/graph/Gr.fst" || exit 1
done
for tag in alarm chirp coo greet inquire peck wheee; do
    find "assets/sounds/$tag" -maxdepth 1 -name '*.wav' -type f
done
sha256sum assets/models/silero_vad.onnx
```

Le SHA-256 Silero v5.1.2 attendu est
`2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f`.
Vosk attend les dossiers **sans suffixe de version**
`vosk-model-small-fr` et `vosk-model-small-en-us`.
[Préparation et corpus S3](../assets/wake/README.md) : les corpus ne sont pas
nécessaires au démarrage vocal ; ils le sont pour les évaluations correspondantes.
Le seuil Vosk 0,9 reste provisoire, pas validé sur la voix réelle de user.

La banque S6 a été restaurée localement le 2026-11-05 dans `coin-coin/assets/sounds`
et `coin-coin-wt/PRE-DELIVERY/assets/sounds`, depuis
`coin-coin-wt/S6/assets/sounds`. Les deux destinations étaient absentes.
Le script existant `scripts/copy_sound_bank.sh` a été utilisé pour le worktree
vide ; le clone durable a reçu une copie `cp -a --update=none`.
**Aucun fichier préexistant écrasé, aucun téléchargement.**

Contrôle effectué sur les trois arbres : mêmes 83 fichiers, 5 642 106 octets,
chaque chemin et chaque SHA-256 identiques. Les 82 WAV sont non vides,
PCM16 et complets : alarm 10, chirp 12, coo 10, greet 12, inquire 10, peck 10,
wheee 18 ; le dernier fichier est `.seed`.
Le SHA-256 du manifeste trié est
`fd38040497d26909fdd2999d71866d3b318d0a0c77f78329a4dc5552ad7f8af8`.
Pour le reproduire, depuis chaque racine `assets/sounds` :

```sh
find . -type f -printf '%P\0' | LC_ALL=C sort -z | \
  xargs -0 sha256sum | sha256sum
```

Les modèles du clone durable sont présents ; le fichier Silero correspond à
l'empreinte attendue. Ces contrôles de fichiers ne prétendent pas charger ou
évaluer les modèles. PRE-DELIVERY n'a pas reçu de copie des modèles ni du `.env`.
Pour préserver plus tard une banque déjà partiellement présente, utiliser
`cp -a --update=none SOURCE/. assets/sounds/`, puis comparer les empreintes :
**le script `copy_sound_bank.sh` utilise `cp -R` et écrase en cas de collision**.
Un conflit doit être signalé, pas remplacé automatiquement.

## Configuration locale sans publier de secret

Vérifier uniquement la présence du `.env`, ne pas le joindre aux diagnostics.
S'il manque, `test -e .env || cp .env.example .env`, puis `chmod 600 .env`.
Le compléter dans un éditeur local de confiance, sans `cat`, `printenv`,
`set -x` ou copie de ses valeurs dans un message. Préserver les jetons existants.
Le gabarit n'est pas une configuration réseau déjà validée.

Pour le futur essai TCP depuis le clone durable, utiliser les ressources
**live de user uniquement avec son accord**, sans autre client pilote :

| Paramètre | Valeur / règle |
|---|---|
| `ROBOT_LINK` | `tcp://<IPv4 eth0 WSL actuelle>:7810` |
| `FRAME_LINK` | `tcp://<même IPv4>:7811` |
| `RELAY_TOKEN` | jeton local non vide, identique pour relais et service ; jamais en argument |
| `COINCOIN_PORT`, `COMPOSE_PROJECT_NAME` | `8765`, `coincoin` |
| `ECHO`, `QUACK_SINK` | `0`, `stream` |
| `WAKE_ENGINE`, `WAKE_THRESHOLD`, `FOLLOWUP_S`, `LANGS` | `vosk`, `0.9`, `420`, `fr,en` |
| `OPENAI_REALTIME_MODEL`, `OPENAI_REALTIME_VOICE` | `gpt-realtime-2.1`, `verse` pour audition ; style demandé : jeune homme drôle et joueur, rendu perçu (dont âge/genre) à confirmer à l'écoute |
| `OPENAI_API_KEY` | requise pour la conversation réelle, pas pour écho/tonalité |
| `OPENCLAW_URL`, `OPENCLAW_TOKEN`, `OPENCLAW_AGENT` | facultatifs ; sans jeton, mémoire explicitement indisponible |

La langue n'est pas imposée dans le code : `RESPONSE_LANGUAGE=auto` suit celle
de l'utilisateur. Une préférence locale telle que `RESPONSE_LANGUAGE=French`
ou `English` s'applique dès l'ouverture/reconnexion ; une demande explicite de
changement de langue reste prioritaire pendant la conversation. Les annonces
avant/après outil suivent la langue de conversation.

Le lanceur reproductible `scripts/live_stack.py` démarre la simulation native,
la caméra, le relais et Coin-Coin en une commande, sans OpenClaw ni Docker.
Voir le [README anglais](../README.md#one-command-simulator-stack) pour les
prérequis, `--check`, les chemins et l'arrêt. `--max-move-s 30` autorise les
mouvements de dix à trente secondes dans ce créneau de simulation ; le défaut
public `MAX_MOVE_S=5` reste inchangé. STOP, échéance et contrôles de sécurité
du gouverneur restent actifs.

L'animation visuelle du bec dans le simulateur suit les commandes de bouche.
C'est une présentation seulement : elle ne modifie pas la physique et ne valide
ni le servo ni le mécanisme du bec.

Conserver les bornes du gouverneur de `.env.example`, `LOG_AUDIO=false`.
Les secrets ne vont ni dans Git ni dans l'historique shell.
Pour OpenClaw, [politique minimale, configuration et essais](openclaw.md) :
l'API réelle n'a pas été observée, la mémoire est testée contre un faux.
Un jeton configuré autorise des appels au réveil/coucher ; ne pas le renseigner
pour un essai qui doit rester sans gateway. Un changement du `.env` exige
un redémarrage du service.

## Contrôles hors réseau et sans service

Avec l'environnement Python déjà installé, ces commandes ne démarrent rien :

```sh
cd ~/projects/microduck/coin-coin
git status --short
git check-ignore .env assets/models/silero_vad.onnx assets/sounds/.seed
test -x .venv/bin/python
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m coincoin.app --help
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m coincoin.cli --help
python3 relay/duck_relay.py --help
bash -n scripts/copy_sound_bank.sh
git diff --check
```

Sans environnement installé, s'arrêter : une restauration de dépendances est
une opération distincte, pas un « contrôle hors réseau » implicite.
Ne pas exécuter `coincoin-cli status` comme contrôle hors réseau : même cette
commande se connecte et ferme le bec à la connexion/fermeture.
`/healthz` vérifie uniquement la vie du serveur, pas la liaison, les modèles
ou les API. Les suites historiques ne sont pas rejouées par cette préparation.

## Futur lancement assemblé depuis main

**À exécuter seulement après autorisation**, Docker Desktop disponible et
ressources live libres. Les commandes supposent Python 3.12, `uv` et les
dépendances installées ; aucun nouveau téléchargement n'est prévu ici.
Les chemins suivants concernent le simulateur, jamais le matériel reçu.

Terminal 1 WSL : réserver la caméra pour la vision, puis lancer la sim.
Si le verrou ou un port est occupé, s'arrêter et faire attribuer le créneau ;
ne jamais effacer le verrou d'autrui.

```sh
cd ~/projects/microduck/coin-coin
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
mkdir -p "$HOME/.cache/coin-coin-locks"
mkdir "$HOME/.cache/coin-coin-locks/sim-camera" || exit 1
printf 'user-main\n' > "$HOME/.cache/coin-coin-locks/sim-camera/owner"
ss -ltn '( sport = :7801 or sport = :7901 or sport = :7810 or sport = :7811 or sport = :8765 or sport = :8443 or sport = :8080 )'
```

Prérequis caméra sur cet hôte : le plugin local `webrtcsink` doit déjà être
disponible dans `/opt/gst-plugins-rs/lib/gstreamer-1.0`, comme lors des
validations S7/S9. Cette procédure ne l'installe pas ; s'il manque, signaler
le blocage plutôt que lancer la sim caméra.
Après vérification de ce prérequis et qu'aucun listener n'occupe ces ports :

```sh
export DUCK_SIM_STATE="$HOME/.cache/duck-sim"
export DUCK_SIM_PORT=7801 DUCK_SIM_FRAME_PORT=7901 DUCK_SIM_VIEWER=0
export DUCK_SIM_CAMERAS=a DUCK_SIM_SCENE=backlash
export DUCK_SIM_RL="$HOME/projects/microduck/microduck_rl"
export GST_PLUGIN_PATH="/opt/gst-plugins-rs/lib/gstreamer-1.0${GST_PLUGIN_PATH:+:$GST_PLUGIN_PATH}"
export GST_PLUGIN_FEATURE_RANK=nvh264enc:NONE
setpriv --no-new-privs "$HOME/projects/microduck/microduck/scripts/duck-sim"
```

`backlash` permet la marche ; `apartment` est une scène de vision, **pas** un
scénario de marche. Aucun snapshot préalable n'est nécessaire à cette procédure.

Terminal 2 WSL : redécouvrir `eth0`, reporter cette IP dans **les deux** liens
du `.env` via l'éditeur, puis lancer le relais depuis le même clone :

```sh
cd ~/projects/microduck/coin-coin
ip -4 -o addr show dev eth0
```

Après mise à jour des liens :

```sh
set -a
source .env
set +a
HOST_IP="$(ip -4 -o addr show dev eth0 | awk '{split($4, a, "/"); print a[1]}')"
test -n "$HOST_IP" && test -n "$RELAY_TOKEN" || exit 1
python3 relay/duck_relay.py --host "$HOST_IP" --robot-port 7810 \
  --frame-port 7811 --state-dir "$HOME/.cache/duck-sim"
```

Terminal 3 WSL : `.cache/logs` doit être accessible à UID/GID 1000 avant
Compose ; ne pas appliquer de changement de permissions global :

```sh
cd ~/projects/microduck/coin-coin
mkdir -p .cache/logs
COINCOIN_PORT=8765 COMPOSE_PROJECT_NAME=coincoin \
  docker compose up --build --detach --wait --wait-timeout 60
curl --fail http://localhost:8765/healthz
COINCOIN_PORT=8765 COMPOSE_PROJECT_NAME=coincoin \
  docker compose exec -T coin-coin coincoin-cli status
```

Ouvrir `http://localhost:8765/` dans Chrome/Edge sous Windows, cliquer
**Démarrer**, autoriser le micro. Realtime s'ouvre après réveil local ;
la liaison robot est établie dès le démarrage du service.
Les outils robot passent par le gouverneur ; `move`/`turn` rendent la main
avant la fin physique de l'action. `quack` est réel depuis S6 :
`QUACK_SINK=stream` mixe la banque dans la sortie navigateur/AEC ;
`robot.sound` n'est pas utilisé pour les cancanements de ce mode.
`see` utilise la caméra S7, `remember`/`ask_openclaw` la feature S8.

Pour un essai **audio seul**, ne lancer ni sim ni relais : remplacer le
lancement Compose ci-dessus par
`COINCOIN_PORT=8765 COMPOSE_PROJECT_NAME=coincoin docker compose run --build --rm --service-ports coin-coin coincoin --echo`
(ou `--tone`, pas les deux), puis Ctrl+C et `docker compose down` avec
les mêmes variables. Ces modes ne créent pas de liaison robot ou d'appel
Realtime/OpenClaw. Hors conteneur, `coincoin` écoute toujours `0.0.0.0:8765` :
il n'existe pas d'option `--port`, et `COINCOIN_PORT` ne règle que Compose.

### Arrêt exact du futur créneau

Appuyer sur **STOP** avant de quitter la page, puis arrêter le projet du
terminal 3 :

```sh
cd ~/projects/microduck/coin-coin
COINCOIN_PORT=8765 COMPOSE_PROJECT_NAME=coincoin docker compose down
```

Ctrl+C dans le terminal 2 du relais. Ne pas tuer par nom. Arrêter ensuite
uniquement la sim live **démarrée dans ce créneau par user** :

```sh
setsid --wait timeout 45s setpriv --no-new-privs \
  env DUCK_SIM_STATE="$HOME/.cache/duck-sim" \
  DUCK_SIM_PORT=7801 DUCK_SIM_FRAME_PORT=7901 \
  "$HOME/projects/microduck/microduck/scripts/duck-sim" down </dev/null
ss -ltn '( sport = :7801 or sport = :7901 or sport = :7810 or sport = :7811 or sport = :8765 or sport = :8443 or sport = :8080 )'
COINCOIN_PORT=8765 COMPOSE_PROJECT_NAME=coincoin docker compose ps -a
```

Exiger ports libres, aucun conteneur de ce projet et aucun pidfile de la sim
encore active ; si un arrêt échoue, signaler le résidu sans lancer un autre
essai. Après confirmation du nettoyage, libérer seulement le verrou pris ici :

```sh
test "$(cat "$HOME/.cache/coin-coin-locks/sim-camera/owner")" = user-main && \
  rm "$HOME/.cache/coin-coin-locks/sim-camera/owner" && \
  rmdir "$HOME/.cache/coin-coin-locks/sim-camera"
```

Ne supprimer ni modèles, ni sons, ni journaux personnels au nettoyage.

## Jour de livraison : connexion en une commande

Chemin unique, identique à la sim : signalisation `mediad` 8443 et datachannel
WebRTC, sans relais. Détails : [WebRTC, chemin du robot livré](webrtc.md#chemin-du-robot-livré--profil-doctor-mode-par-une-personne).

1. Canard allumé et sur le Wi-Fi (provisionnement BLE seulement ; code
   d'appairage par défaut : 6 derniers chiffres du numéro de série).
2. Trouver son adresse : `duckctl ip` (ou `duckctl open`). Le nom d'hôte par
   défaut n'est pas unique ; préférer l'IP.
3. Vérifier, depuis le poste Coin-Coin sur le même LAN :

   ```sh
   ROBOT_TARGET=robot ROBOT_HOST=<ip> uv run coincoin-cli doctor
   ```

   Chaque étape en échec affiche son correctif (Wi-Fi, service `mediad`,
   UDP/ICE bloqué, version d'API, batterie, caméra). Code retour 0 = prêt.
4. Écrire `ROBOT_TARGET=robot` et `ROBOT_HOST=<ip>` dans `.env`, puis lancer
   `uv run coincoin --host 127.0.0.1`.

À savoir avant de parler au robot :

- Changer de mode (patins/marche) demande une personne : poser ou retirer
  l'équipement, puis choisir le mode sur le robot (`sudo robotctl configure`,
  `[policy] mode`, redémarrage de `robotd`). Le canard le dit, attend jusqu'à
  `PERSON_MODE_TIMEOUT_S` (120 s) et ne bouge pas pendant ce temps.
- `go_to` et le relevé assisté sont désactivés : le robot ne déclare pas
  `simulated: true`.
- Pas de micro robot par WebRTC (aucune piste audio `mediad`).
- Une mise à jour OTA est bloquée tant que Coin-Coin est connecté.
- La signalisation LAN n'est pas authentifiée : ne pas exposer 8443 hors du LAN.
- Coin-Coin parle l'API v47 ; un robot plus récent donne un avertissement du
  doctor, à vérifier avant tout mouvement.

## Ce qui reste humain ou matériel

Restent ouverts : positifs réels FR/EN et qualité du réveil ; AEC/écho ;
choix de voix, conversation bilingue, barge-in, fenêtre de suivi et absence
d'envoi en sommeil ; actions/refus/STOP/deadman observés ; bec et sons
audibles sans faux réveil ; vision en conversation ; mémoire entre sessions
et dégradation gateway. Les relevés du journal ne remplacent pas les
observations humaines, notamment le début du mouvement ou de l'audio audible.
Aucun de ces critères n'est coché par cette livraison.

S9 reste optionnelle : [procédure WebRTC](webrtc.md), signalisation LAN sans
authentification, réserves sim ci-dessus et preuves Q10 historiques uniquement.
Ne pas remplacer TCP par WebRTC par défaut.
S10 attend le robot et un accord explicite, ainsi qu'une solution audio
robot/AEC : la piste audio `mediad` manque encore. Aucun `RobotAudio`, bruit
moteur, son sur haut-parleur matériel ou comportement physique n'est validé ici.
