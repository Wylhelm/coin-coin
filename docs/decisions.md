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

L'appel `flush()` applique une échéance monotone unique de 100 ms couvrant
l'attente de son verrou, l'envoi de la commande UI et l'acquittement. Il ne prend
pas le verrou d'envoi audio : les trames déjà en transit sont invalidées par
génération et numéro de séquence. Un timeout, une erreur d'envoi ou une
annulation est signalé à l'appelant ; la page est alors considérée
désynchronisée et les nouveaux `play()` échouent jusqu'à un flush acquitté.
Cela ne garantit pas l'arrêt physique de l'audio si le navigateur ou sa voie UI
ne répond pas.

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

Les doubles WebSocket déterministes vérifient également qu'un `send_bytes`
bloqué ne retient pas le flush UI, qu'un `send_json` bloqué échoue dans son
échéance, et que deux flushs simultanés respectent chacun leur propre borne.
Les régressions couvrent l'annulation, l'acquittement de génération périmée, le
remplacement de page, le rejet des nouveaux `play()` après échec et le filtrage
d'une ancienne trame reçue après un flush acquitté.

**Validation de la correction rouverte.** La commande complète
`uv run ruff check . && uv run ruff format --check . && uv run pyright &&
uv run pytest` est verte sur la base `main=0597380` : 332 tests réussis,
5 ignorés, couverture totale de 93,16 %, aucun diagnostic pyright. Les tests
ciblés WebSocket réussissent (14 tests), et le harness AudioWorklet séparé
réussit ses 5 tests sous Node.js. Le README donne la procédure d'essai et de
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

**Seconde correction : H-POSITIFS et provenance.** Les enregistrements humains
vont désormais dans `assets/wake/recorded/session-01`, séparés des anciens TTS
sous `positives/` et `vad/`, qui restent intacts. Le script refuse un répertoire
existant sans `--resume`, enregistre dans un temporaire unique, valide le WAV
et publie sans écrasement par lien dur. Une interruption conserve les prises
complètes et le temporaire ; la reprise explicite avec le même plan enregistre
uniquement les emplacements manquants. Les WAV invalides ou inattendus sont
signalés sans suppression. Le plan est une intention d'enregistrement et non
une preuve d'identité ou de contenu.

Le seul choix `--positive-source recorded` n'est plus suffisant : le banc exige
l'attestation exacte documentée dans `assets/wake/README.md` pour tous les WAV
du dossier. Le rapport la cite comme déclaration utilisateur non vérifiée,
indique les comptes par langue et la couverture des noms près/loin sans
certifier la complétude H-POSITIFS. Il ne prétend pas identifier user,
vérifier la phrase ou la distance. Les imports humains restent possibles.
Les dossiers TTS connus et leurs copies strictement identiques par SHA-256
sont refusés comme humains ; ce contrôle local n'est pas un détecteur général
de parole synthétique. Les prises doivent être écoutées et attestées par la
personne réalisant la validation. Le rapport historique Vosk synthétique et
ses mesures ne sont pas recalculés. `recorded/` est ignoré par Git et tout
`assets/wake/` exclu de Docker pour ne pas envoyer de prises personnelles
dans le contexte de construction.

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

**Correction de priorité STOP.** La séparation des tâches ne suffisait pas :
`look`, les skills et la revérification de posture attendaient des RPC sous
le verrou partagé avec STOP. Une réponse lente empêchait donc l'envoi de
l'arrêt. STOP ne prend désormais jamais ce verrou. Il incrémente la génération,
annule immédiatement les RPC d'action et les tâches mouvement/regard, puis
envoie la requête d'arrêt avant d'attendre leur nettoyage et de fermer le bec.
Le délai de réponse de l'arrêt reste 100 ms ; la fermeture du bec est tentée
même si l'arrêt est refusé.

Les appels d'action vérifient sécurité et génération juste avant le transport
et après la réponse ; une annulation par STOP devient un refus explicite.
La vérification après attente du verrou empêche aussi une ancienne action en
file de reprendre. Les réponses déjà en vol ne peuvent plus créer de maintien,
enchaîner un skill ou activer le robot. Une commande déjà traitée côté robot
ne peut pas être retirée ; STOP la suit et l'EMA reste observable (ADR-004).
Le verrou sérialise toujours les actions ordinaires, notamment les toggles
assis/debout et les remplacements, mais n'est plus une barrière à la sécurité.

Les tests par événements retiennent les réponses `look`, `skills`, `stop`
avant skill, `do`, `policies` (y compris sous verrou) et `enable`, avec ou
sans annulation effective de la réponse. STOP doit terminer sans libérer la
réponse retenue (< 100 ms), aucune commande d'action ne suit ; watchdog et
action en attente du verrou sont aussi couverts. La reproduction réelle avec
marche et réponse regard retenue mesure l'entrée dans `link.call` STOP en
0,116 ms et vérifie son acquittement en moins de 200 ms, consigne zéro et
refus de l'ancienne action. Le premier délai n'est pas une mesure de
réception réseau ou d'immobilisation physique.

**Conséquences.** Les interfaces S0 sont inchangées ; S9 peut adapter les
flux WebRTC sans recopier corrélation/état. S5 utilise le gouverneur, jamais
la CLI `raw`. Les tests contre la simulation sont en opt-in explicite
(`COINCOIN_TEST_SIM=1`) et lisent alors uniquement le `.env` du dépôt exécuté.
La suite hors sim reste indépendante de tous les vrais secrets.

## ADR-006 — Conversation vocale via Realtime WebSocket

Date : 2026-11-05. Statut : implémenté ; validation vocale humaine en attente.

**Contexte.** S4 doit fournir une conversation vocale bilingue et interruptible
sans exposer de clé dans le navigateur, et enregistrer une trace réelle avant
d'implémenter son client. L'identifiant ADR-004 étant déjà attribué à l'arrêt
robot, cette décision prend le prochain numéro libre plutôt que de dupliquer
ou renommer une décision existante.

**Décision.** Utiliser l'API GA `wss://api.openai.com/v1/realtime` avec la clé
uniquement côté serveur. La trace réelle, capturée avant l'implémentation du
client, utilise le modèle `gpt-realtime-2.1`, la voix provisoire `marin`,
l'audio PCM16 mono 24 kHz, la transcription d'entrée et un outil `ping`
factice. Elle a confirmé ces événements serveur : `session.created`,
`session.updated`, `input_audio_buffer.committed`,
`conversation.item.added`, `conversation.item.done`,
`conversation.item.input_audio_transcription.completed`,
`response.created`, `response.function_call_arguments.delta`,
`response.function_call_arguments.done`, `response.output_audio.delta`,
`response.output_audio.done`, `response.output_audio_transcript.delta`,
`response.output_audio_transcript.done`, `response.output_item.added`,
`response.output_item.done`, `response.done` et
`conversation.item.truncated`, ainsi que les événements de partie de contenu
et de limite de débit. Le client reconnaît aussi les évènements VAD GA
`input_audio_buffer.speech_started` et `input_audio_buffer.speech_stopped`,
conformément à la documentation Realtime ; le WAV de trace utilisait un
commit explicite et ne les a donc pas émis. Après un résultat d'outil, le
client demande explicitement la réponse suivante. En interruption, il capture
l'item et sa progression avant le flush, envoie `response.cancel`, puis tronque
à la durée réellement jouée.

La fixture `tests/fakes/fixtures/realtime_trace.jsonl` conserve les 77
événements serveur de la trace ; la sortie audio est limitée à 80 ms et
aucune trame micro, clé ou donnée personnelle n'y est enregistrée. L'interruption
de la trace API a utilisé `audio_end_ms=0`, car aucun son n'était lu par le
script ; cela confirme le protocole, pas le silence physique d'un haut-parleur.

**Conséquences.** La session est ouverte uniquement après le réveil local ;
aucun audio n'est envoyé en sommeil et la conversation n'appelle aucun outil
robot. Les features ajoutent leurs outils, hooks et contextes d'instructions
via `App` et la liste `FEATURES`. Une perte de connexion déclenche une
reconnexion bornée ; une session est renouvelée une minute avant expiration
avec un résumé de conversation court et non fiable injecté comme contexte.
La voix `marin` est un choix de travail, pas une sélection humaine validée.
FR/EN, le barge-in sous 500 ms et le retour en sommeil après 7 minutes restent
à écouter/vérifier par user.

**Corrections après revue S4.** Le hook `stop` est initié avant le flush,
l'interface et l'annulation Realtime ; le flush et l'annulation ne dépendent pas
de leur achèvement. Les ouvertures et travaux audio/VAD anciens sont invalidés
au coucher, les trames micro gardent leur ordre, et la troncature utilise
l'identité d'item et le nombre d'échantillons réellement joués capturés avant
flush. Les outils s'exécutent hors du consommateur d'évènements ; leurs refus
valides sont conservés. Une perte TCP locale a été testée avec un port refusé,
sans appel externe. Les régressions App/UI/transport et la suite complète
valident les compteurs audio uniquement après acceptation par le transport,
l'état de parole synchronisé à la connexion, les hooks de sommeil et les
erreurs UI bornées. Les vérifications humaines ci-dessus restent ouvertes.

**Seconde correction après revue de `5cc8a42`.** STOP ne joue aucun son et
réarme le réveil après 500 ms, même s'il était déjà en sommeil. À l'arrêt du
service, le hook `stop` a une échéance de 1 s, indépendante de l'UI et de Realtime ;
son timeout/échec est journalisé avant l'annulation générale des tâches.
L'invalidation n'annule pas sa propre tâche : l'outil `sleep` termine les hooks,
le coucher et le journal de résultat sans reprendre une session fermée.

`response.output_item.done.response_id`, présent dans la trace initiale, est
conservé pour corréler chaque outil au parent. Une interruption invalide
immédiatement les appels en cours et les appels tardifs. Un lot d'outils ne
reprend la génération qu'après `response.done completed` du parent et tous
les résultats, transmis dans l'ordre initial des appels, avec un unique
`response.create`. `send_tool_result()` envoie seulement l'output ;
`create_response()` est désormais explicite. La
[documentation Realtime actuelle](https://developers.openai.com/api/docs/guides/realtime-conversations#function-calling)
confirme le séquencement `function_call_output` puis `response.create` ;
aucune nouvelle trace API n'a été créée pour tester les ordonnancements.
Les faux couvrent les deux ordres de fin lent/rapide, leur interruption et
l'échec d'envoi d'un résultat. Les transcriptions finales sont journalisées
dès leur réception, y compris pour un parent avec outils : ce sont des textes
générés, pas une preuve de ce qui a été entendu.

L'extension optionnelle `TrackedPlayback` de `AudioIO` expose
`PlaybackProgress(epoch, sent_samples, played_samples)`. BrowserAudio change
d'epoch au flush et au remplacement/débranchement de page ; le message worklet
`played` transporte aussi `samples`, relayé par la vraie route `/ws/ui`.
Les anciens rapports millisecondes restent acceptés, mais les queues
sous-milliseconde exigent le champ exact de la page/worklet mis à jour.
La fin de réponse n'arrondit plus sa cible au-dessus du compteur disponible
et n'annonce pas un succès sur timeout ou perte de page. Les intervalles
d'items sont semi-ouverts : à la frontière exacte, le suivant est à zéro.
Un bloc déjà en file ou un delta tardif d'une ancienne page n'est pas rejoué
sur la nouvelle ; une nouvelle réponse utilise ses nouveaux compteurs.

Le hook `audio_out(pcm, play_at_ms)` est initié à l'appel effectif `AudioIO.play`,
avec la position prévue dans la file de lecture, dans une tâche indépendante.
Un hook lent n'arrête ni le consommateur Realtime ni le flush ; les hooks
anciens sont annulés à l'interruption. L'upgrade WebSocket est lui aussi
soumis à `_OPEN_TIMEOUT_S`, avec fermeture et erreur réseau normalisée avant
le backoff. STOP hors ligne conserve `BrowserAudio.flush()` comme écho/tone.

**Preuves de seconde correction.** Commande qualité complète verte :
389 tests réussis, 3 simulations ignorées, couverture 90,91 %, aucun diagnostic
ruff/Pyright. Les 23 cas de `tests/integration/test_s4_followup.py` utilisent
App, BrowserAudio, les routes UI/audio, le faux serveur Realtime et une socket
TCP locale bloquant l'upgrade. Le harness du vrai worklet passe 6/6 et vérifie
explicitement 25 échantillons joués pour seulement 1 ms entière rapportée.
La trace réelle initiale reste inchangée ; aucune sim, connexion robot ou
nouvelle requête OpenAI. Cela ne lève pas les validations humaines ni la
limite du silence physique d'un navigateur injoignable.
