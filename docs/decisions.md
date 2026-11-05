# Décisions

## ADR-000 — aiohttp pour le socle web asynchrone

Date : 2026-11-04. Statut : accepté.

**Contexte.** S0 doit fournir HTTP et des contrats asyncio ; S2 ajoutera les
WebSockets audio/UI. Il faut éviter un deuxième serveur ou une abstraction
de transport inutile.

**Décision.** Utiliser `aiohttp`, l'une des options de S0, pour HTTP et les
futurs WebSockets. `create_app()` ne construit aucun client externe et se teste
avec `aiohttp.test_utils`. L'entrée `coincoin` charge configuration et journal,
puis sert le port interne fixe 8765. Le port publié et le nom de projet sont
propres à chaque session ; S0 réserve 8770 et `coincoin-s0`.

**Conséquences.** Une seule dépendance web runtime. Le conteneur s'exécute
en UID/GID 1000 et son `/healthz` vérifie la vie du service, pas des API externes.
L'absence de secrets n'empêche pas de lancer le socle.

Les signatures prescrites restent intactes. L'unique exemption ruff
`ASYNC109` est locale au `timeout` imposé par `RobotLink.call`.
À S0, le seuil de réveil n'était pas choisi ; S3 publie désormais un seuil
Vosk provisoire mesuré en ADR-003. Le modèle et la voix Realtime restent
explicitement non sélectionnés (chaînes vides) jusqu'à S4.

## ADR-001 — plan B : IP eth0 WSL et jeton sur la première ligne

Date : 2026-11-04. Statut : accepté, mesures réelles.

**Contexte.** Docker Engine 29.8.2 et Compose v5.6.0 répondent depuis WSL.
Le premier téléchargement de `python:3.12-slim` échouait avant toute mesure :

```text
error getting credentials - err: fork/exec
/usr/bin/docker-credential-desktop.exe: exec format error
```

Les deux images publiques ont été préchargées via Docker Windows sur ce même
moteur : `python:3.12-slim` et `ghcr.io/astral-sh/uv:0.13.7`. Les commandes
Compose et le spike ont ensuite été exécutés normalement depuis WSL, sans
modifier le helper, la configuration globale ni `DOCKER_CONFIG`.

**Mesures.** Le conteneur S0 sert `/healthz` depuis WSL et Windows
(HTTP 200, `{"status": "ok"}`), avec HEALTHCHECK `healthy` et UID/GID 1000.
Le spike réel donne :

```text
Testing A: host.docker.internal -> WSL 127.0.0.1:7899
OSError: [Errno 101] Network is unreachable
A failed; testing B on eth0 with a temporary shared token
PASS: fallback rejects unauthenticated connections
PASS: exact TCP echo from the Docker container
RESULT=B: use tcp://192.0.2.1:<relay-port>, bind eth0,
          require RELAY_TOKEN on the first line
```

Code de sortie : 0. Après sortie, aucun listener 7899 ni fichier temporaire
du spike ne reste. Le jeton éphémère n'est ni affiché ni conservé.

**Décision.** Retenir **B** pour les relais TCP des sessions suivantes :
écoute sur l'IP IPv4 `eth0` WSL, liens TCP utilisant cette IP, authentification
par `RELAY_TOKEN` obligatoire sur la première ligne de chaque connexion.
Les ports restent ceux attribués par COORDINATION.md. L'IP mesurée
`192.0.2.1` doit être redécouverte après un redémarrage WSL.

**Conséquences.** Le relais S1 doit implémenter le plan B et refuser les
connexions sans jeton avant tout transfert. Un jeton persistant appartient
uniquement au `.env` de la session concernée, jamais au repo ou aux logs.
S0 n'en crée aucun : son jeton sert seulement au test d'écho.
`Settings` et `.env.example` conservent les défauts de la spec maître ;
la configuration effective des sessions robot remplace ces liens selon B.

## ADR-002 — audio navigateur sur WebSocket et AudioWorklet

Date : 2026-11-04. Statut : retenu pour S2 ; validation humaine AEC en attente.

**Contexte.** L'audio de simulation doit rester dans la page navigateur pour que
l'annulation d'écho du navigateur puisse utiliser la sortie comme référence.
Le transport doit aussi éviter ICE et les candidats injoignables depuis Docker
Desktop sur localhost.

**Décision.** Utiliser un WebSocket binaire `/ws/audio` pour les trames PCM16
mono little-endian 24 kHz de 20 ms, et `/ws/ui` pour les contrôles. Le micro
utilise les contraintes navigateur echo cancellation, noise suppression et auto
gain ; les AudioWorklets rééchantillonnent l'entrée si nécessaire, tamponnent
la lecture avec 60 ms de précharge, rapportent la progression toutes les
100 ms et vident la lecture sur commande. Le mode écho vérifie le trajet audio ;
`--tone` émet un bip de 1 kHz et mesure sa présence par corrélation.

Chaque paquet audio descendant préfixe son PCM d'un numéro de séquence uint64
little-endian. Le contrôle de flush annonce le dernier numéro antérieur ; le
client élimine ces séquences même si elles arrivent après le message UI de flush.
Le serveur suspend aussi les nouveaux appels `play()` jusqu'à l'acquittement du
worklet. Les paquets transportent leur longueur PCM exacte : les fragments
incomplets ne sont ni complétés par du silence ni retenus, et le worklet les
concatène avant lecture.

**Preuve automatisée.** `uv run pytest tests/integration/test_ws_audio.py`
envoie 1 s de PCM au faux client et vérifie l'écho octet par octet, la limite
de 300 ms d'avance, le remplacement d'une page et le flush acquitté en moins
de 100 ms. Les tests unitaires vérifient la conversion/rééchantillonnage SoXR
24→16 kHz, la longueur exacte sur trames, la conservation de la fréquence et
de l'énergie (±1 %), le mixage sans délai et la corrélation du bip. `node
--test tests/js/play_worklet.test.mjs` exécute le vrai worklet et la vraie
barrière de séquence : trames livrées en retard après deux flushs, trames
fraîches après flush, fragments de 400+400+3 échantillons sans trou, et premier
rapport de lecture autour de 100 ms à 16, 24 et 48 kHz.

L'interpolation linéaire conserve sa phase fractionnaire relativement au premier
échantillon restant après chaque quantum : elle interpole ce point avec le
suivant, sans réutiliser l'échantillon consommé du quantum précédent. Le
harness compare la rampe complète du vrai worklet à la référence calculée à
16, 24, 44,1 et 48 kHz, y compris au-delà de plusieurs blocs, entre deux
fragments et après une sous-alimentation suivie d'une reprise. Un contre-exemple
à 48 kHz vérifie aussi qu'avec les échantillons −30000, 0, 30000 aux indices
63–65, le rendu n°129 interpole bien à +15000/32768.

**Validation de la correction rouverte.** La commande complète
`uv run ruff check . && uv run ruff format --check . && uv run pyright &&
uv run pytest` est verte sur le `main` courant : 294 tests réussis, 5 ignorés,
couverture totale de 92,95 %, aucun diagnostic pyright. Le harness AudioWorklet
séparé réussit ses 5 tests sous Node.js, dont les nouvelles vérifications
d'interpolation à 16/24/44,1/48 kHz. Le README donne la procédure d'essai et de
nettoyage Compose depuis le clone principal `~/projects/microduck/coin-coin`,
après la suppression du worktree S2.

**Mesure humaine AEC (à faire par user).** Pas encore exécutée : aucun
résultat de latence ou d'atténuation n'est revendiqué. Suivre la procédure
« Vérifier l'audio » du README : haut-parleurs sans casque, écouter le mode
`--echo` (retard ressenti faible, pas de larsen), puis lancer `--tone`, parler
par-dessus et noter le retard affiché ainsi que la présence du bip au vumètre.
Si le bip est nettement visible ou le système larsène, noter Q9 en échec.

**Alternative si l'AEC échoue.** Ouvrir une décision séparée sur aiortc et
`RTCPeerConnection` côté page, en configurant ICE vers `127.0.0.1` et un port
UDP publié. Elle n'est pas implémentée dans S2.

**Conséquences.** Les sons de Coin-Coin doivent être mixés et joués par
`BrowserAudio`, jamais via `robot.sound` en simulation, afin de rester dans
la référence AEC. Le choix reste à confirmer par le test live avant S4.

## ADR-003 — Vosk provisoire, seuil 0.90

Date : 2026-11-04. Statut : compromis mesuré, validation humaine en attente.

**Contexte.** S3 devait mesurer un moteur de réveil bilingue avec au moins 90 %
de rappel par langue, au plus un faux réveil par heure et aucun déclenchement
sur les cancanements. Un modèle openWakeWord personnalisé n'était envisagé
qu'après la mesure Vosk. Les positifs de user n'étaient pas enregistrés.

**Mesures.** Le banc `scripts/eval_wake.py` a évalué 40 positifs TTS synthétiques
par langue (près/loin), 30 minutes de FLEURS français, 30 minutes de
LibriSpeech test-clean anglais, 82 cancanements copiés en lecture seule et
20 phrases TTS contenant « Coin-Coin » dans un contexte. Positifs et audio
téléchargés/synthétisés restent ignorés par Git. À `WAKE_THRESHOLD=0.90` :

- rappel synthétique : FR 15/40 (37,5 %), EN 21/40 (52,5 %) ;
- 0 faux réveil/h sur FLEURS + LibriSpeech + banque de sons, 0 sur la banque ;
- 20 déclenchements sur 20 phrases TTS en contexte ; 19,30 faux réveils/h en
  comptant tous les négatifs, donc la cible globale n'est pas atteinte ;
- la grille 0,30–0,90 ne change pas le rappel ; 0,90 réduit le plus les faux
  positifs en discours public parmi les seuils mesurés ;
- les deux recognizers ont évalué le corpus en 2 min 15,5 s dans l'image Docker
  S3 isolée du réseau (environ 1,07 h d'audio au total, environ 28× le temps réel).

**Décision.** Garder Vosk small FR 0.22 et EN-US 0.15 comme référence exécutable,
grammaire stricte `["coin coin", "[unk]"]`, et exposer `WAKE_THRESHOLD=0.90`
comme **meilleur compromis provisoire**, pas comme seuil validé. Le
`OpenWakeWordEngine` reste un stub explicite : aucun modèle candidat n'a été
mesuré comme meilleur, et les phrases où le mot de réveil est prononcé
intentionnellement ne peuvent pas être séparées par un simple seuil Vosk.

**Conséquences.** S4 peut charger un moteur et un seuil configurables, mais
l'activation réelle reste conditionnée à la vérification des vrais positifs
par user. Le rapport final est `docs/wake-eval.md`; procédure de reprise
dans `assets/wake/README.md`. Les critères FR/EN et faux réveils totaux restent
ouverts tant que les positifs réels ne sont pas enregistrés et le compromis
réévalué.

**Correctifs et intégration après revue.** S3 est rebasée sur S2 corrigée
(`81a4e0d`, flush et continuité audio) et réutilise
son module `audio/resample.py` sans changement de son API ; ses tests audio et
WebSocket, le montage `.cache/logs` et ses dépendances sont conservés.
Le wrapper Silero v5.1.2 suit désormais
[`OnnxWrapper.__call__`](https://github.com/snakers4/silero-vad/blob/v5.1.2/src/silero_vad/utils_vad.py) :
64 échantillons de contexte + 512 nouveaux (entrée 576), contexte et état
récurrent remis à zéro ensemble. Les tests unitaires vérifient les entrées
ONNX, le contexte de la fenêtre précédente et les resets explicite/fin de
parole. Le test réel `tests/integration/test_vad_real.py` constate un écart
maximal de probabilité de 0 sur 51 fenêtres et un replay identique après reset.
Le WAV parole/silence produit 1,248 s pour le début et 2,496 s pour la fin
avec maintien (attendus 1,200 s et 2,508 s, tolérance ±100 ms). Modèle et WAV
restent ignorés ; commande, empreintes et conservation dans `assets/wake/README.md`.
Le banc Vosk et son compromis ne sont pas modifiés.

Les cycles `response_active`/`quack_active` sont indépendants : terminer un
cancanement ne termine pas une réponse, et inversement. `speaking` reste actif,
la bouche n'est pas fermée par la fin du seul premier cycle, le barge-in annule
les deux lectures et le réveil ne reprend que 500 ms après la dernière fin.
La fin du cancanement ne réarme pas le délai de suivi démarré par la réponse.

## ADR-004 — requête `robot.stop`, pas notification ; observer l'EMA

Date : 2026-11-04. Statut : accepté, vérifié contre la simulation réelle.

**Contexte.** Les commandes continues `move`, `head`, `pose`, `mouth`
acceptent des notifications. Cela n'est pas vrai de tous les intents :
`robotd/src/main.rs` route une notification uniquement vers `apply_intent`.
`RobotStop` n'y figure pas ; seul `dispatch` appelle `intents.stop()` et
renvoie `IntentResult`. Un faux serveur trop permissif masquerait ce défaut.

**Décision.** Tous les arrêts du gouverneur sont des requêtes avec `id`,
bornées à 100 ms pour l'accusé de réception. Un refus est remonté tel quel ;
un timeout est explicite. Le bec est fermé sur arrêt et à la connexion.
Une annulation de remplacement ne produit pas d'arrêt tardif de l'ancienne
tâche. STOP invalide aussi les actions encore en attente du homing.
Le faux reproduit l'absence d'effet d'une notification `robot.stop` et les
tests vérifient que l'arrêt effectif est une requête.

**Observation importante.** `robot.stop` met immédiatement la **consigne**
à zéro, mais `state.move.applied` contient l'EMA interne de `robotd`, pas
la consigne brute. De même, le deadman porte la cible à zéro après 500 ms ;
`limited_by=["deadman"]` apparaît avant disparition complète de l'EMA.
La CLI attend au plus 1 s que chaque axe appliqué soit < 0,01 après la fin
de la tâche, sans réémettre `move`. Le test de marche conserve son seuil
> 0,15 m sur la commande de 2 s et vérifie séparément la consigne nulle puis
l'amortissement. La preuve deadman emploie le critère prévu par la spec :
présence de `deadman` en moins d'une seconde (mesuré : 0,496 s).

**Conséquences.** Ni firmware ni code Pollen ne sont modifiés. On ne prétend
pas que l'EMA ou le corps physique passe instantanément à zéro. Un nouvel
essai doit conserver les vrais seuils de distance/angle, pas les remplacer
par la seule réussite d'un RPC.

## ADR-005 — transport partageable et tâches de sécurité indépendantes

Date : 2026-11-04. Statut : accepté.

**Décision.** `JsonRpcConnection` dépend seulement d'un lecteur de lignes et
d'un écrivain asynchrone ; `TcpLink` possède connexion, authentification,
handshake, callbacks et reconnexion. La caméra utilise une connexion
distincte avec la même authentification, car son en-tête est suivi de
binaire. Sa validation reprend `MediaFrameHeader::valid_uyvy` (16 Mio,
géométrie, rotation) avant allocation/décodage ; numpy et Pillow produisent
le JPEG demandé par S7.

Le gouverneur possède un surveillant à 10 Hz, une tâche de mouvement et une
tâche de maintien du regard. La lecture du mode, potentiellement lente,
est une tâche séparée et ne bloque pas la surveillance. Le mode est relu
toutes les 30 s et après reconnexion ; un changement observé interrompt le
mouvement pour ne jamais continuer avec les anciennes bornes.

**Conséquences.** Les interfaces S0 sont inchangées ; S9 peut adapter les
flux WebRTC sans recopier corrélation/état. S5 utilise le gouverneur, jamais
la CLI `raw`. Les tests contre la simulation sont en opt-in explicite
(`COINCOIN_TEST_SIM=1`) et lisent alors uniquement le `.env` du dépôt exécuté.
La suite hors sim reste indépendante de tous les vrais secrets.
